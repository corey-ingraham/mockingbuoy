"""Sea-state motion model: monotonic amplitude, bounded, deterministic, calm-but-alive."""

from __future__ import annotations

import pytest

from nmea_sim.seastate import (
    PITCH_CAP_DEG,
    ROLL_CAP_DEG,
    sea_state_motion,
)

# Sample densely across the longer (roll) period so peaks are well resolved.
_PERIOD_S = 12.0
_SAMPLES = 2000


def _samples(sea_state: int) -> list[tuple[float, float]]:
    return [sea_state_motion(sea_state, _PERIOD_S * i / _SAMPLES) for i in range(_SAMPLES + 1)]


def _peak_roll(sea_state: int) -> float:
    return max(abs(roll) for _pitch, roll in _samples(sea_state))


def _peak_pitch(sea_state: int) -> float:
    return max(abs(pitch) for pitch, _roll in _samples(sea_state))


@pytest.mark.parametrize("lower", list(range(9)))
def test_roll_amplitude_rises_with_sea_state(lower: int) -> None:
    assert _peak_roll(lower) < _peak_roll(lower + 1)


@pytest.mark.parametrize("lower", list(range(9)))
def test_pitch_amplitude_rises_with_sea_state(lower: int) -> None:
    assert _peak_pitch(lower) < _peak_pitch(lower + 1)


def test_bounded_at_sea_state_nine() -> None:
    assert _peak_roll(9) <= ROLL_CAP_DEG
    assert _peak_pitch(9) <= PITCH_CAP_DEG


def test_deterministic() -> None:
    assert sea_state_motion(5, 42.0) == sea_state_motion(5, 42.0)


def test_sea_state_zero_is_tiny_but_alive() -> None:
    peak_roll = _peak_roll(0)
    peak_pitch = _peak_pitch(0)
    # Strictly non-zero somewhere across the period...
    assert peak_roll > 0.0
    assert peak_pitch > 0.0
    # ...yet unmistakably calm.
    assert peak_roll < 1.0
    assert peak_pitch < 1.0


def test_out_of_range_sea_state_clamps() -> None:
    assert sea_state_motion(-3, 3.0) == sea_state_motion(0, 3.0)
    assert sea_state_motion(50, 3.0) == sea_state_motion(9, 3.0)
