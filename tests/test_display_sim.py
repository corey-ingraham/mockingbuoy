"""Tests for the display-only instrument sim (``web/display_sim.py``).

The function is pure and deterministic: every value drifts from the snapshot's tz-aware
``utc`` with no randomness, so these tests pin determinism, value ranges, the ``None`` /
``str`` special cases, the physical monotonicity the display relies on, and the exact key
set the frontend JS contract reads off ``s.sim``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nmea_sim.state import VesselState
from web.display_sim import simulate_display_instruments

# The exact key set the conning-tab JS reads off ``s.sim`` (the frontend contract).
_SIM_KEYS = {
    "rpm_port",
    "rpm_stbd",
    "load_port_pct",
    "load_stbd_pct",
    "fuel_rate_lph",
    "fuel_per_nm_l",
    "fuel_total_l",
    "water_temp_c",
    "air_temp_c",
    "humidity_pct",
    "pressure_hpa",
    "ap_mode",
    "ap_off_course_deg",
    "ap_track_course_deg",
    "ap_xtd_m",
    "ap_distance_nm",
    "ap_time_to_go_s",
    "ap_track_lat",
    "ap_track_lon",
}


def _state(
    *,
    sog_kn: float = 6.0,
    rot_dpm: float = 0.0,
    cog_deg: float = 90.0,
    when: datetime | None = None,
) -> VesselState:
    """Build a geographically-neutral, tz-aware vessel snapshot for the sim.

    All positional/heading values are synthetic 0-region placeholders (R39): no real
    coordinates, places, or names anywhere.
    """
    return VesselState(
        lat=0.0,
        lon=0.0,
        sog_kn=sog_kn,
        cog_deg=cog_deg,
        heading_true_deg=90.0,
        heading_mag_deg=90.0,
        mag_variation_deg=0.0,
        altitude_m=0.0,
        fix_quality=1,
        satellites=9,
        hdop=0.8,
        utc=when or datetime(2024, 1, 1, 8, 30, 0, tzinfo=UTC),
        rot_dpm=rot_dpm,
    )


def test_deterministic_repeat() -> None:
    """The same snapshot yields an equal dict every call (no randomness, no hidden state)."""
    state = _state()
    assert simulate_display_instruments(state) == simulate_display_instruments(state)


def test_key_set_matches_frontend_contract() -> None:
    sim = simulate_display_instruments(_state())
    assert set(sim) == _SIM_KEYS


def test_value_ranges() -> None:
    """Ranges the panels assume: rpm in [650, 3400]; loads/humidity in [0, 100]; pressure in
    [1000, 1030]; fuel_total in (0, 4000]."""
    # Sweep several times of day + speeds so the oscillators explore their range.
    for hour in range(0, 24, 3):
        for sog in (0.0, 3.0, 6.0, 12.0):
            sim = simulate_display_instruments(
                _state(sog_kn=sog, when=datetime(2024, 6, 21, hour, 17, 5, tzinfo=UTC))
            )
            for key in ("rpm_port", "rpm_stbd"):
                assert 650.0 <= float(sim[key]) <= 3400.0  # type: ignore[arg-type]
            for key in ("load_port_pct", "load_stbd_pct", "humidity_pct"):
                assert 0.0 <= float(sim[key]) <= 100.0  # type: ignore[arg-type]
            assert 1000.0 <= float(sim["pressure_hpa"]) <= 1030.0  # type: ignore[arg-type]
            fuel_total = float(sim["fuel_total_l"])  # type: ignore[arg-type]
            assert 0.0 < fuel_total <= 4000.0


def test_fuel_per_nm_is_none_when_stopped() -> None:
    """At sog=0 there is no distance to burn per nm -> ``None`` (frontend renders ``---``)."""
    sim = simulate_display_instruments(_state(sog_kn=0.0))
    assert sim["fuel_per_nm_l"] is None


def test_fuel_per_nm_is_float_when_underway() -> None:
    sim = simulate_display_instruments(_state(sog_kn=6.0))
    assert isinstance(sim["fuel_per_nm_l"], float)


def test_ap_mode_is_str() -> None:
    assert simulate_display_instruments(_state())["ap_mode"] == "NAV"


def test_rpm_rises_with_sog() -> None:
    """Higher speed over ground -> higher engine speed on both shafts (same clock)."""
    when = datetime(2024, 3, 10, 14, 0, 0, tzinfo=UTC)
    slow = simulate_display_instruments(_state(sog_kn=1.0, when=when))
    fast = simulate_display_instruments(_state(sog_kn=8.0, when=when))
    assert float(fast["rpm_port"]) > float(slow["rpm_port"])  # type: ignore[arg-type]
    assert float(fast["rpm_stbd"]) > float(slow["rpm_stbd"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "when",
    [
        datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2024, 5, 15, 9, 42, 17, tzinfo=UTC),
        datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC),
    ],
)
def test_port_below_stbd_on_starboard_turn(when: datetime) -> None:
    """With a modest starboard rate of turn the port shaft reads below the starboard shaft,
    for any clock phase (the +18/-18 offset dominates the small turn coupling here)."""
    sim = simulate_display_instruments(_state(sog_kn=6.0, rot_dpm=5.0, when=when))
    assert float(sim["rpm_port"]) < float(sim["rpm_stbd"])  # type: ignore[arg-type]
