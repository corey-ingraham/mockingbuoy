"""Apparent-wind resolution: cardinal cases plus the COG-vs-heading distinction."""

from __future__ import annotations

import pytest

from nmea_sim.wind import apparent_wind


def _bow_distance(angle_deg: float) -> float:
    """Circular distance from dead ahead, so 359.999... reads as ~0."""
    return min(angle_deg % 360.0, 360.0 - (angle_deg % 360.0))


def test_head_on_adds_speeds_and_bears_dead_ahead() -> None:
    # Wind FROM north, vessel steaming due north into it: apparent wind is the
    # sum of the two speeds, arriving from dead ahead.
    speed, angle = apparent_wind(
        true_speed_kn=20.0, true_dir_deg=0.0, heading_deg=0.0, cog_deg=0.0, sog_kn=6.0
    )
    assert speed == pytest.approx(26.0)
    assert _bow_distance(angle) == pytest.approx(0.0, abs=1e-6)


def test_following_wind_subtracts_speeds_and_bears_astern() -> None:
    # Wind FROM directly astern, vessel running with it: apparent speed is the
    # difference, arriving from dead astern (180).
    speed, angle = apparent_wind(
        true_speed_kn=15.0, true_dir_deg=180.0, heading_deg=0.0, cog_deg=0.0, sog_kn=5.0
    )
    assert speed == pytest.approx(10.0)
    assert angle == pytest.approx(180.0)


def test_beam_wind_is_drawn_forward_of_the_beam() -> None:
    # True wind on the starboard beam (FROM east) while making way forward: the
    # vessel's own motion pulls the apparent wind forward of 90.
    speed, angle = apparent_wind(
        true_speed_kn=10.0, true_dir_deg=90.0, heading_deg=0.0, cog_deg=0.0, sog_kn=5.0
    )
    assert angle < 90.0
    assert angle == pytest.approx(63.4349488, abs=1e-6)
    assert speed == pytest.approx(11.1803399, abs=1e-6)


def test_vessel_vector_uses_cog_not_heading() -> None:
    # Bow points north (heading 0) but current sets the vessel's track to COG 045.
    # The apparent wind must be resolved against the COG track; had heading been
    # used for the vessel vector the answer would differ materially (~329.6 vs
    # ~14.6, and a different speed).
    speed, angle = apparent_wind(
        true_speed_kn=10.0, true_dir_deg=0.0, heading_deg=0.0, cog_deg=45.0, sog_kn=5.0
    )
    assert speed == pytest.approx(13.9896633, abs=1e-6)
    assert angle == pytest.approx(14.6388066, abs=1e-6)

    # Sanity: the heading-based (incorrect) resolution lands far away, confirming
    # the two inputs are genuinely distinguished by this case.
    _, heading_based = apparent_wind(
        true_speed_kn=10.0, true_dir_deg=0.0, heading_deg=45.0, cog_deg=45.0, sog_kn=5.0
    )
    assert abs(heading_based - angle) > 30.0
