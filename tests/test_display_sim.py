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
    "rpm",
    "rpm_ordered",
    "load_pct",
    "shaft_power_mw",
    "engine_order_pct",
    "fuel_rate_lph",
    "fuel_per_nm_l",
    "fuel_pct",
    "fuel_endurance_days",
    "fuel_range_nm",
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
    sea_state: int = 1,
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
        sea_state=sea_state,
    )


def test_deterministic_repeat() -> None:
    """The same snapshot yields an equal dict every call (no randomness, no hidden state)."""
    state = _state()
    assert simulate_display_instruments(state) == simulate_display_instruments(state)


def test_key_set_matches_frontend_contract() -> None:
    sim = simulate_display_instruments(_state())
    assert set(sim) == _SIM_KEYS


def test_value_ranges() -> None:
    """Ranges the panels assume: rpm in [0, 120]; load_pct in [0, 110]; humidity in [0, 100];
    pressure in [1000, 1030]; fuel_total in (0, 4000]."""
    # Sweep several times of day + speeds so the oscillators explore their range.
    for hour in range(0, 24, 3):
        for sog in (0.0, 3.0, 6.0, 12.0):
            sim = simulate_display_instruments(
                _state(sog_kn=sog, when=datetime(2024, 6, 21, hour, 17, 5, tzinfo=UTC))
            )
            assert 0.0 <= float(sim["rpm"]) <= 120.0  # type: ignore[arg-type]
            assert 0.0 <= float(sim["load_pct"]) <= 110.0  # type: ignore[arg-type]
            assert 0.0 <= float(sim["humidity_pct"]) <= 100.0  # type: ignore[arg-type]
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


def test_default_engine_order_is_navigation_full() -> None:
    """With no overrides the telegraph sits at +90 % ("Navigation Full"); ``engine_order_pct`` is a
    display-only float (keeps the one-None / one-str purity of the sim dict intact)."""
    sim = simulate_display_instruments(_state())
    assert sim["engine_order_pct"] == pytest.approx(90.0)
    assert isinstance(sim["engine_order_pct"], float)


def test_rpm_and_load_rise_with_engine_order() -> None:
    """A higher ahead telegraph order -> higher engine rpm and load (same clock)."""
    when = datetime(2024, 3, 10, 14, 0, 0, tzinfo=UTC)
    slow = simulate_display_instruments(_state(when=when), overrides={"engine_order_pct": 30.0})
    fast = simulate_display_instruments(_state(when=when), overrides={"engine_order_pct": 90.0})
    assert float(fast["rpm"]) > float(slow["rpm"])  # type: ignore[arg-type]
    assert float(fast["load_pct"]) > float(slow["load_pct"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "when",
    [
        datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2024, 5, 15, 9, 42, 17, tzinfo=UTC),
        datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC),
    ],
)
def test_stop_order_yields_zero_rpm_and_load(when: datetime) -> None:
    """A STOP telegraph (order 0) drives rpm and load to exactly 0.0 at any clock phase (the
    multiplicative governor hunt can never lift a zero setpoint off the floor)."""
    sim = simulate_display_instruments(_state(when=when), overrides={"engine_order_pct": 0.0})
    assert float(sim["rpm"]) == 0.0  # type: ignore[arg-type]
    assert float(sim["load_pct"]) == 0.0  # type: ignore[arg-type]


def test_full_astern_rpm_is_limited() -> None:
    """A direct-reversing engine is capped astern at ~70 % MCR rpm, so order -100 lands near 70
    (±governor hunt)."""
    sim = simulate_display_instruments(_state(), overrides={"engine_order_pct": -100.0})
    assert float(sim["rpm"]) == pytest.approx(70.0, abs=1.0)  # type: ignore[arg-type]


def test_rpm_ordered_is_signed_by_telegraph_direction() -> None:
    """``rpm_ordered`` is the telegraph demand carried with a sign: positive ahead, negative astern,
    and (unlike actual ``rpm``) free of governor hunt / weather sag. Astern is capped at ~70 rpm.

    Uses a clock where hunt != 0 (t % 8 != 0) AND rough weather (sea_state > 1) so the assertions
    actually pin the hunt/sag independence: rpm_ordered stays the clean demand while ACTUAL rpm is
    pulled off it by hunt+sag -- the divergence the tach's order-vs-actual marker depends on."""
    rough = _state(sea_state=6, when=datetime(2024, 1, 1, 8, 30, 3, tzinfo=UTC))  # t % 8 == 3 -> hunt != 0
    ahead = simulate_display_instruments(rough, overrides={"engine_order_pct": 90.0})
    astern = simulate_display_instruments(rough, overrides={"engine_order_pct": -100.0})
    stop = simulate_display_instruments(rough, overrides={"engine_order_pct": 0.0})
    assert float(ahead["rpm_ordered"]) == pytest.approx(90.0)  # type: ignore[arg-type]
    assert float(astern["rpm_ordered"]) == pytest.approx(-70.0)  # type: ignore[arg-type]
    assert float(stop["rpm_ordered"]) == 0.0  # type: ignore[arg-type]
    # actual rpm carries hunt+sag, so it must NOT equal the clean ordered demand
    assert float(ahead["rpm"]) != pytest.approx(90.0)  # type: ignore[arg-type]


def test_fuel_bunker_gauge_fields() -> None:
    """Ship-panel bunker gauge: fuel_pct in [0,100]; endurance = total/rate/24 (None at STOP);
    range = total/(t per nm) (None below steerage speed)."""
    run = simulate_display_instruments(_state(sog_kn=6.0), overrides={"engine_order_pct": 90.0})
    total = float(run["fuel_total_l"])  # type: ignore[arg-type]
    rate = float(run["fuel_rate_lph"])  # type: ignore[arg-type]
    pernm = float(run["fuel_per_nm_l"])  # type: ignore[arg-type]
    assert 0.0 <= float(run["fuel_pct"]) <= 100.0  # type: ignore[arg-type]
    assert float(run["fuel_endurance_days"]) == pytest.approx(total / rate / 24.0)  # type: ignore[arg-type]
    assert float(run["fuel_range_nm"]) == pytest.approx(total / pernm)  # type: ignore[arg-type]
    # STOP -> zero burn -> endurance undefined (None)
    stopped = simulate_display_instruments(_state(sog_kn=6.0), overrides={"engine_order_pct": 0.0})
    assert stopped["fuel_endurance_days"] is None
    # dead in the water -> economy and range undefined
    adrift = simulate_display_instruments(_state(sog_kn=0.0), overrides={"engine_order_pct": 90.0})
    assert adrift["fuel_range_nm"] is None


def test_load_rises_with_sea_state_at_fixed_order() -> None:
    """Heavy running: at a fixed engine order a higher sea state adds resistance, raising load."""
    when = datetime(2024, 3, 10, 14, 0, 0, tzinfo=UTC)
    calm = simulate_display_instruments(
        _state(sea_state=1, when=when), overrides={"engine_order_pct": 90.0}
    )
    rough = simulate_display_instruments(
        _state(sea_state=6, when=when), overrides={"engine_order_pct": 90.0}
    )
    assert float(rough["load_pct"]) > float(calm["load_pct"])  # type: ignore[arg-type]


def test_shaft_power_tracks_load() -> None:
    """Shaft power (MW) is load_pct/100 x 20 MW MCR."""
    sim = simulate_display_instruments(_state(), overrides={"engine_order_pct": 90.0})
    load = float(sim["load_pct"])  # type: ignore[arg-type]
    assert float(sim["shaft_power_mw"]) == pytest.approx(load / 100.0 * 20.0)  # type: ignore[arg-type]
