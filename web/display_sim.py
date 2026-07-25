"""Display-only instrument simulation for the conning tab.

The conning display wants panels — twin-engine propulsion, fuel, cabin/sea environment,
autopilot — that the NMEA wire never carries. Rather than invent new sentences (a hard
rule: nothing new on the wire / config / role-emit / TCP tap), those panels are driven by
this pure, deterministic function. Its output rides ONLY on the SSE ``state`` frame, under a
``sim`` key, and is rendered in amber ("display-only") so an operator never mistakes it for
NMEA-backed truth.

:func:`simulate_display_instruments` is a pure function of the vessel snapshot: every value
drifts smoothly from the snapshot's tz-aware ``utc`` timestamp — there is no randomness, no
hidden state, and no I/O — so the same snapshot always yields the same dict (a property the
tests pin). Each quantity is computed in a typed ``float`` local; the ``float | str | None``
union dict is assembled as a single literal at the end so the arithmetic stays mypy-clean.
"""

from __future__ import annotations

from math import cos, pi, radians, sin

from nmea_sim.state import VesselState

# --- tuning constants (display-only; no config surface) ----------------------------

#: Idle / redline engine speed (rpm) and the hull speed (kn) mapped to redline. The
#: propeller law then derives load from rpm, so cruise (~6 kn) sits near a third of MAX.
_IDLE_RPM = 650.0
_MAX_RPM = 3400.0
_HULL_MAX_SOG_KN = 12.0

#: Fuel model. ``TANK_L`` is nominal capacity; burn rates in litres/hour; the cosmetic
#: ``REFILL_WINDOW_S`` (72 h) makes ``fuel_total_l`` a slow bounded sawtooth (4000 -> 1264 L)
#: that visibly refills, so a long-running display never flatlines at empty.
_TANK_L = 4000.0
_MAX_BURN_LPH = 90.0
_IDLE_BURN_LPH = 4.0
_NOMINAL_BURN_LPH = 38.0
_REFILL_WINDOW_S = 259200.0  # 72 h

#: Autopilot leg length (nm); the along-leg distance counts down as a sawtooth.
_LEG_NM = 24.0


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into the inclusive ``[lo, hi]`` range."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def simulate_display_instruments(state: VesselState) -> dict[str, float | str | None]:
    """Derive display-only instrument values from a vessel snapshot, deterministically.

    Pure and side-effect free: all drift comes from ``state.utc`` (tz-aware), so repeated
    calls on the same snapshot return equal dicts. Exactly one value is ``None``
    (``fuel_per_nm_l`` when the vessel is effectively stopped, rendered ``---``) and exactly
    one is ``str`` (``ap_mode``); every other value is a ``float``.
    """
    # --- time drift (all smooth, deterministic oscillators) ------------------------
    t: float = state.utc.timestamp()
    sod: float = t % 86400.0  # seconds-of-day, for diurnal (weather-like) shaping
    d1: float = sin(2.0 * pi * t / 900.0)
    d2: float = sin(2.0 * pi * t / 3600.0 + 1.3)
    d3: float = sin(2.0 * pi * t / 240.0)

    sog: float = state.sog_kn
    rot: float = state.rot_dpm

    # --- propulsion (propeller-law load, rot-coupled port/stbd) --------------------
    frac: float = _clamp(sog / _HULL_MAX_SOG_KN, 0.0, 1.0)
    base_rpm: float = _IDLE_RPM + (_MAX_RPM - _IDLE_RPM) * frac
    # Turning eases the inside engine and loads the outside one (opposite rot signs).
    rpm_port: float = _clamp(base_rpm - 18.0 + 12.0 * d1 + 0.8 * rot, _IDLE_RPM, _MAX_RPM)
    rpm_stbd: float = _clamp(base_rpm + 18.0 + 12.0 * d2 - 0.8 * rot, _IDLE_RPM, _MAX_RPM)
    load_port_pct: float = _clamp(160.0 * (rpm_port / _MAX_RPM) ** 3, 3.0, 100.0)
    load_stbd_pct: float = _clamp(160.0 * (rpm_stbd / _MAX_RPM) ** 3, 3.0, 100.0)

    # --- fuel (burn from mean load; bounded cosmetic 72 h refill sawtooth) ---------
    mean_load: float = (load_port_pct + load_stbd_pct) / 2.0
    fuel_rate_lph: float = _IDLE_BURN_LPH + _MAX_BURN_LPH * mean_load / 100.0
    fuel_per_nm_l: float | None = fuel_rate_lph / sog if sog > 0.1 else None
    fuel_total_l: float = _TANK_L - (_NOMINAL_BURN_LPH / 3600.0) * (t % _REFILL_WINDOW_S)

    # --- environment (diurnal shaping -> reads like real weather) ------------------
    water_temp_c: float = 12.0 + 1.5 * sin(2.0 * pi * (sod - 46800.0) / 86400.0) + 0.2 * d2
    air_temp_c: float = 13.0 + 6.0 * sin(2.0 * pi * (sod - 54000.0) / 86400.0) + 0.4 * d1
    humidity_pct: float = _clamp(78.0 - 3.0 * (air_temp_c - water_temp_c) + 4.0 * d1, 30.0, 100.0)
    pressure_hpa: float = 1015.0 + 6.0 * sin(2.0 * pi * t / 190080.0) + 1.5 * d2

    # --- autopilot (display-only; track point numeric/synthetic -> R39-safe) -------
    ap_off_course_deg: float = 1.8 * d3
    ap_track_course_deg: float = (state.cog_deg - ap_off_course_deg) % 360.0
    ap_xtd_m: float = 12.0 * sin(2.0 * pi * t / 300.0 + 0.7)
    leg_period_s: float = _LEG_NM / max(sog, 0.5) * 3600.0
    ap_distance_nm: float = _LEG_NM * (1.0 - (t % leg_period_s) / leg_period_s)
    ap_time_to_go_s: float = ap_distance_nm / max(sog, 0.5) * 3600.0
    # Project the track point ahead along the track course (synthetic lat/lon, never a name).
    theta: float = radians(ap_track_course_deg)
    ap_track_lat: float = state.lat + (ap_distance_nm / 60.0) * cos(theta)
    ap_track_lon: float = state.lon + (ap_distance_nm / 60.0) * sin(theta) / max(
        cos(radians(state.lat)), 0.1
    )

    return {
        "rpm_port": rpm_port,
        "rpm_stbd": rpm_stbd,
        "load_port_pct": load_port_pct,
        "load_stbd_pct": load_stbd_pct,
        "fuel_rate_lph": fuel_rate_lph,
        "fuel_per_nm_l": fuel_per_nm_l,
        "fuel_total_l": fuel_total_l,
        "water_temp_c": water_temp_c,
        "air_temp_c": air_temp_c,
        "humidity_pct": humidity_pct,
        "pressure_hpa": pressure_hpa,
        "ap_mode": "NAV",
        "ap_off_course_deg": ap_off_course_deg,
        "ap_track_course_deg": ap_track_course_deg,
        "ap_xtd_m": ap_xtd_m,
        "ap_distance_nm": ap_distance_nm,
        "ap_time_to_go_s": ap_time_to_go_s,
        "ap_track_lat": ap_track_lat,
        "ap_track_lon": ap_track_lon,
    }
