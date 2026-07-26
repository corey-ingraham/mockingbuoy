"""Deterministic depth-under-keel model (``nmea_sim.depthsim.depth_sim``).

Mirrors ``test_seastate``: the function is pure and deterministic (stdlib ``math`` only, no
randomness, no globals, no wall-clock), so the same ``t_s`` always yields the same depth.
These tests pin determinism, the ``>= min_depth_m`` floor, the distinct-phase layering (the
three sinusoids never peak together), and that the output is alive but bounded.
"""

from __future__ import annotations

import math

import pytest

from nmea_sim.config import DepthSimSpec
from nmea_sim.depthsim import _RIPPLE_PHASE, _SHOAL_PHASE, depth_sim

# Sample densely across the longest (drift) period so peaks are well resolved.
_PERIOD_S = 1800.0
_SAMPLES = 3600


def _spec(**overrides: float | bool) -> DepthSimSpec:
    return DepthSimSpec(**overrides)  # type: ignore[arg-type]


def _sweep(spec: DepthSimSpec, base: float | None = None) -> list[float]:
    b = spec.base_depth_m if base is None else base
    return [depth_sim(b, _PERIOD_S * i / _SAMPLES, spec) for i in range(_SAMPLES + 1)]


def test_deterministic_repeat() -> None:
    """Same base/time/params -> identical output (no randomness, no hidden state)."""
    spec = _spec()
    assert depth_sim(spec.base_depth_m, 42.0, spec) == depth_sim(spec.base_depth_m, 42.0, spec)


def test_distinct_phase_layering_at_zero() -> None:
    """At t=0 the drift term is exactly zero (sin 0) while shoal/ripple sit at their fixed,
    distinct phase offsets -- so the three components are demonstrably out of phase (they never
    peak together). Pins the exact algorithm decomposition."""
    spec = _spec()
    expected = (
        spec.base_depth_m
        + spec.drift_amp_m * math.sin(0.0)
        + spec.shoal_amp_m * math.sin(_SHOAL_PHASE)
        + spec.ripple_amp_m * math.sin(_RIPPLE_PHASE)
    )
    assert depth_sim(spec.base_depth_m, 0.0, spec) == pytest.approx(expected)
    # The phases are the frozen, distinct offsets (drift has none; shoal pi/4; ripple pi/2).
    assert pytest.approx(math.pi / 4.0) == _SHOAL_PHASE
    assert pytest.approx(math.pi / 2.0) == _RIPPLE_PHASE
    assert _SHOAL_PHASE != _RIPPLE_PHASE


def test_floor_is_never_breached() -> None:
    """Depth is floored at ``min_depth_m`` and can never go negative with the default floor."""
    # A spec whose entire amplitude budget cannot reach the floor -> always clamped to it.
    clamped = _spec(
        base_depth_m=0.0,
        min_depth_m=10.0,
        drift_amp_m=1.0,
        shoal_amp_m=1.0,
        ripple_amp_m=1.0,
    )
    for depth in _sweep(clamped, base=0.0):
        assert depth == pytest.approx(10.0)

    # Default floor is 0.0: even the deepest trough stays >= 0 across a full period.
    default = _spec(base_depth_m=5.0, drift_amp_m=20.0, shoal_amp_m=15.0, ripple_amp_m=0.6)
    assert min(_sweep(default)) >= 0.0


def test_alive_but_bounded() -> None:
    """Enabled depth is not a frozen constant: it varies across the period, yet stays within
    base +/- (sum of amplitudes)."""
    spec = _spec()
    samples = _sweep(spec)
    assert max(samples) - min(samples) > 1.0  # demonstrably moving
    span = spec.drift_amp_m + spec.shoal_amp_m + spec.ripple_amp_m
    assert max(samples) <= spec.base_depth_m + span + 1e-6
    assert min(samples) >= max(spec.min_depth_m, spec.base_depth_m - span - 1e-6)


def test_pure_in_time_only() -> None:
    """The base depth is passed as a constant, so depth is a pure function of ``t_s`` alone --
    identical times give identical output regardless of call order."""
    spec = _spec()
    a = [depth_sim(spec.base_depth_m, t, spec) for t in (0.0, 100.0, 250.5, 999.0)]
    b = [depth_sim(spec.base_depth_m, t, spec) for t in (999.0, 250.5, 100.0, 0.0)][::-1]
    assert a == b
