"""Deterministic true-wind drift (``nmea_sim.windsim.wind_sim``).

Mirrors ``test_steeringsim`` / ``test_depthsim``: the function is pure and deterministic (stdlib
``math`` only, no randomness, no globals, no wall-clock), so the same ``t_s`` always yields the same
``(speed, dir)``. These tests pin determinism, the bounded harmonic amplitudes, the distinct-phase
decomposition at ``t=0``, the non-negative speed floor at a deep lull, and that the direction stays
wrapped into ``[0, 360)`` around its base as the base straddles 0/360.
"""

from __future__ import annotations

import math

import pytest

from nmea_sim.config import WindSimSpec
from nmea_sim.windsim import (
    _GUST_HARMONIC_PHASE,
    _GUST_PHASE,
    _HARMONIC_WEIGHT,
    _VEER_HARMONIC_PHASE,
    _VEER_PHASE,
    wind_sim,
)

# Sample densely across the longer period so both oscillations are well resolved.
_SAMPLES = 4000


def _sweep(spec: WindSimSpec) -> list[tuple[float, float]]:
    horizon = max(spec.gust_period_s, spec.veer_period_s)
    return [wind_sim(horizon * i / _SAMPLES, spec) for i in range(_SAMPLES + 1)]


def test_deterministic_repeat() -> None:
    """Same time/params -> identical output (no randomness, no hidden state)."""
    spec = WindSimSpec(enabled=True, base_speed_kn=8.0, base_dir_deg=45.0)
    assert wind_sim(42.0, spec) == wind_sim(42.0, spec)


def test_pure_in_time_only() -> None:
    """Pure function of its inputs alone -> identical inputs give identical output regardless of
    call order."""
    spec = WindSimSpec(enabled=True, base_speed_kn=12.0, base_dir_deg=200.0)
    times = (0.0, 3.5, 100.0, 250.5, 999.0)
    a = [wind_sim(t, spec) for t in times]
    b = [wind_sim(t, spec) for t in reversed(times)][::-1]
    assert a == b


def test_decomposition_at_zero() -> None:
    """At t=0 speed/dir are exactly their fundamental+harmonic decomposition at the frozen, distinct
    phase offsets. Pins the algorithm and the phase constants."""
    spec = WindSimSpec(enabled=True, base_speed_kn=10.0, base_dir_deg=100.0)
    gust = spec.gust_amp_kn * (
        math.sin(_GUST_PHASE) + _HARMONIC_WEIGHT * math.sin(_GUST_HARMONIC_PHASE)
    )
    veer = spec.veer_amp_deg * (
        math.sin(_VEER_PHASE) + _HARMONIC_WEIGHT * math.sin(_VEER_HARMONIC_PHASE)
    )
    speed, direction = wind_sim(0.0, spec)
    assert speed == pytest.approx(max(0.0, spec.base_speed_kn + gust))
    assert direction == pytest.approx((spec.base_dir_deg + veer) % 360.0)
    # Gust and veer sit at distinct phases so speed and direction never peak together.
    assert _GUST_PHASE == 0.0
    assert _VEER_PHASE != _GUST_PHASE


def test_speed_bounded_and_alive() -> None:
    """Speed stays within ``base +/- gust_amp*(1+harmonic)`` and is demonstrably moving."""
    spec = WindSimSpec(enabled=True, base_speed_kn=8.0, base_dir_deg=45.0)
    speeds = [s for s, _ in _sweep(spec)]
    bound = spec.gust_amp_kn * (1.0 + _HARMONIC_WEIGHT)
    assert max(speeds) <= spec.base_speed_kn + bound + 1e-9
    assert min(speeds) >= spec.base_speed_kn - bound - 1e-9
    assert max(speeds) - min(speeds) > 0.5  # moving


def test_speed_floored_at_zero_in_deep_lull() -> None:
    """A base below the gust amplitude cannot drive speed negative (a lull floors at 0)."""
    spec = WindSimSpec(enabled=True, base_speed_kn=1.0, base_dir_deg=0.0, gust_amp_kn=5.0)
    speeds = [s for s, _ in _sweep(spec)]
    assert min(speeds) >= 0.0
    assert min(speeds) == pytest.approx(0.0)  # the deep lull actually reaches the floor


def test_direction_wraps_into_0_360_around_base() -> None:
    """Direction is always in ``[0, 360)`` and stays within ``veer_amp*(1+harmonic)`` of the base
    (accounting for the wrap), for bases straddling 0/360."""
    bound = 8.0 * (1.0 + _HARMONIC_WEIGHT)
    for base in (0.0, 5.0, 90.0, 200.0, 357.0):
        spec = WindSimSpec(enabled=True, base_speed_kn=8.0, base_dir_deg=base)
        dirs = [d for _, d in _sweep(spec)]
        for d in dirs:
            assert 0.0 <= d < 360.0
            diff = ((d - base + 180.0) % 360.0) - 180.0
            assert abs(diff) <= bound + 1e-9
        assert max(dirs) != min(dirs)  # alive
