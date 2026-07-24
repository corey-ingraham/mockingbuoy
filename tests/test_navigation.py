"""Navigation: coordinate formatting edge cases and dead reckoning vs geographiclib."""

from __future__ import annotations

import pytest
from geographiclib.geodesic import Geodesic

from nmea_sim import navigation


@pytest.mark.parametrize(
    ("value", "is_lat", "expected"),
    [
        (12.8, True, ("1248.0000", "N")),
        (-12.8, True, ("1248.0000", "S")),
        (-40.9, False, ("04054.0000", "W")),
        (40.9, False, ("04054.0000", "E")),
        (0.0, True, ("0000.0000", "N")),
        (0.0, False, ("00000.0000", "E")),
    ],
)
def test_to_ddmm_known_values(value: float, is_lat: bool, expected: tuple[str, str]) -> None:
    assert navigation.to_ddmm(value, is_lat) == expected


def test_to_ddmm_widths() -> None:
    lat_str, _ = navigation.to_ddmm(5.5, is_lat=True)
    lon_str, _ = navigation.to_ddmm(5.5, is_lat=False)
    assert lat_str == "0530.0000"  # 2 degree digits
    assert lon_str == "00530.0000"  # 3 degree digits


def test_to_ddmm_minute_carry_guard() -> None:
    # A value whose minutes round to 60.0000 must carry into the degrees.
    value = 36 + 59.999999 / 60.0
    coord, _ = navigation.to_ddmm(value, is_lat=True)
    assert coord == "3700.0000"


def test_knots_to_mps() -> None:
    assert navigation.knots_to_mps(1.0) == pytest.approx(0.514444, abs=1e-5)


def test_dead_reckon_zero_speed_is_stationary() -> None:
    assert navigation.dead_reckon(25.0, -80.0, 0.0, 90.0, 60.0) == (25.0, -80.0)


def test_dead_reckon_matches_geodesic_inverse() -> None:
    lat0, lon0, sog_kn, cog, dt = 10.0, -40.0, 10.0, 45.0, 120.0
    lat1, lon1 = navigation.dead_reckon(lat0, lon0, sog_kn, cog, dt)

    inv = Geodesic.WGS84.Inverse(lat0, lon0, lat1, lon1)
    expected_dist = navigation.knots_to_mps(sog_kn) * dt

    assert inv["s12"] == pytest.approx(expected_dist, rel=1e-9)
    assert inv["azi1"] % 360.0 == pytest.approx(cog, abs=1e-6)


def test_dead_reckon_due_east_increases_longitude() -> None:
    _, lon1 = navigation.dead_reckon(0.0, 0.0, 20.0, 90.0, 60.0)
    assert lon1 > 0.0
