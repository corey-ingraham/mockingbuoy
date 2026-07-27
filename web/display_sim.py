"""Display-only instrument simulation for the conning tab.

The conning display wants panels — the main-engine propulsion readout, fuel, cabin/sea
environment, autopilot — that the NMEA wire never carries. Rather than invent new sentences (a
hard rule: nothing new on the wire / config / role-emit / TCP tap), those panels are driven by
this pure, deterministic function. Its output rides ONLY on the SSE ``state`` frame, under a
``sim`` key, and is rendered in amber ("display-only") so an operator never mistakes it for
NMEA-backed truth.

The vessel is modelled as a large merchant ship: a single low-speed 2-stroke main engine,
direct-coupled to a single fixed-pitch propeller (engine rpm = shaft rpm = propeller rpm). RPM and
load are driven by the ENGINE ORDER telegraph (the ``engine_order_pct`` display-override, negative =
astern via engine reversal), following the propeller cube law, with governor hunt and heavy-weather
added resistance.

:func:`simulate_display_instruments` is a pure function of the vessel snapshot plus the operator
overrides: every value drifts smoothly from the snapshot's tz-aware ``utc`` timestamp — there is no
randomness, no hidden state, and no I/O — so the same ``(snapshot, overrides)`` always yields the
same dict (a property the tests pin). It never mutates the passed ``overrides`` mapping. Each
quantity is computed in a typed ``float`` local; the ``float | str | None`` union dict is assembled
as a single literal at the end so the arithmetic stays mypy-clean.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import cos, pi, radians, sin

from nmea_sim.state import VesselState

# --- tuning constants (display-only; no config surface) ----------------------------

#: Single low-speed 2-stroke main engine, direct-coupled to a fixed-pitch propeller. MCR ~100 rpm /
#: ~20 MW is a large-bore (Panamax+) main engine; engine rpm == shaft rpm == propeller rpm.
_MCR_RPM = 100.0
_MCR_MW = 20.0
_DEFAULT_ORDER_PCT = 90.0  # telegraph at "Navigation Full" / service speed by default
_ASTERN_RPM_FRAC = 0.70  # direct-reversing engines are limited astern (~70 % MCR rpm)
_HUNT_AMP = 0.01  # governor hunt, ±1 % of setpoint — MULTIPLICATIVE so STOP (order 0) stays 0
_HUNT_PERIOD_S = 8.0  # seconds-scale so the hunt reads as governor jitter, not a slow drift
_WEATHER_PER_SS = 0.03  # added-resistance load rise per sea-state step above calm baseline (SS1)
_SAG_PER_SS = 0.005  # heavy-running rpm sag per sea-state step above the calm baseline

#: Fuel model in MERCHANT units (TONNES). Rate = brake power × SFOC. The sim keys keep their
#: historical ``_l``/``_lph`` suffixes (renaming them would churn the override/persist surface) but
#: now carry tonnes / tonnes-per-hour / tonnes-per-nm; the UI labels them ``t``.
_SFOC_G_PER_KWH = 170.0  # specific fuel-oil consumption of a modern slow-speed 2-stroke
_BUNKERS_T = 1500.0  # nominal bunker capacity (tonnes)
_NOMINAL_BURN_TH = 2.7  # tonnes/hour; drives the slow cosmetic refill sawtooth
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


def simulate_display_instruments(
    state: VesselState, overrides: Mapping[str, float] | None = None
) -> dict[str, float | str | None]:
    """Derive display-only instrument values from a vessel snapshot, deterministically.

    Pure and side-effect free: all drift comes from ``state.utc`` (tz-aware) and the operator
    ``overrides`` (the engine-order telegraph), so repeated calls on the same inputs return equal
    dicts and the passed ``overrides`` mapping is never mutated. Exactly one value is ``None``
    (``fuel_per_nm_l`` when the vessel is effectively stopped, rendered ``---``) and exactly one is
    ``str`` (``ap_mode``); every other value is a ``float``.
    """
    ov = overrides or {}

    # --- time drift (all smooth, deterministic oscillators) ------------------------
    t: float = state.utc.timestamp()
    sod: float = t % 86400.0  # seconds-of-day, for diurnal (weather-like) shaping
    d1: float = sin(2.0 * pi * t / 900.0)
    d2: float = sin(2.0 * pi * t / 3600.0 + 1.3)
    d3: float = sin(2.0 * pi * t / 240.0)

    sog: float = state.sog_kn

    # --- propulsion: single slow-speed main engine, telegraph-driven ---------------
    # ENGINE ORDER (%) is the telegraph: +ahead / -astern, default "Navigation Full". RPM follows
    # the order along the propeller law; a direct-reversing engine is limited astern. Governor hunt
    # is multiplicative (STOP -> rpm 0). Heavy weather sags rpm and raises load (torque-rich).
    order: float = _clamp(float(ov.get("engine_order_pct", _DEFAULT_ORDER_PCT)), -100.0, 100.0)
    mag: float = abs(order) / 100.0
    demand: float = min(mag, _ASTERN_RPM_FRAC) if order < 0.0 else mag
    ss: float = float(max(state.sea_state - 1, 0))  # sea_state defaults to 1 == calm baseline
    sag: float = 1.0 - _SAG_PER_SS * ss
    hunt: float = 1.0 + _HUNT_AMP * sin(2.0 * pi * t / _HUNT_PERIOD_S)
    rpm: float = _MCR_RPM * demand * sag * hunt  # UNSIGNED; astern is carried by the order sign
    weather: float = 1.0 + _WEATHER_PER_SS * ss
    load_pct: float = _clamp((rpm / _MCR_RPM) ** 3 * 100.0 * weather, 0.0, 110.0)
    shaft_power_mw: float = load_pct / 100.0 * _MCR_MW
    # Telegraph-ORDERED shaft rpm (signed: +ahead / -astern): the demand the bridge is calling for,
    # WITHOUT the governor hunt or heavy-weather sag that ride the ACTUAL rpm above. The conning tach
    # draws this as a separate "order" marker so order-vs-actual divergence is visible (weather/manoeuvre).
    rpm_ordered: float = _MCR_RPM * demand * (-1.0 if order < 0.0 else 1.0)

    # --- fuel (tonnes; rate = brake power x SFOC; slow cosmetic 72 h refill sawtooth) ----
    fuel_rate_th: float = shaft_power_mw * _SFOC_G_PER_KWH / 1000.0
    fuel_per_nm_t: float | None = fuel_rate_th / sog if sog > 0.1 else None
    fuel_total_t: float = _BUNKERS_T - (_NOMINAL_BURN_TH / 3600.0) * (t % _REFILL_WINDOW_S)
    # Derived bunker figures for the Ship-panel gauge (all computed here so the JS stays a dumb
    # formatter): % of capacity remaining, endurance (time to empty at current burn; None at STOP),
    # and range (distance at current economy; None below steerage speed where t/NM is undefined).
    fuel_pct: float = _clamp(fuel_total_t / _BUNKERS_T * 100.0, 0.0, 100.0)
    fuel_endurance_days: float | None = fuel_total_t / fuel_rate_th / 24.0 if fuel_rate_th > 1e-6 else None
    fuel_range_nm: float | None = fuel_total_t / fuel_per_nm_t if fuel_per_nm_t else None

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
        "rpm": rpm,
        "rpm_ordered": rpm_ordered,
        "load_pct": load_pct,
        "shaft_power_mw": shaft_power_mw,
        "engine_order_pct": order,
        "fuel_rate_lph": fuel_rate_th,
        "fuel_per_nm_l": fuel_per_nm_t,
        "fuel_total_l": fuel_total_t,
        "fuel_pct": fuel_pct,
        "fuel_endurance_days": fuel_endurance_days,
        "fuel_range_nm": fuel_range_nm,
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
