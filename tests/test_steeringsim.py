"""Deterministic steering models (``nmea_sim.steeringsim.rudder_sim`` / ``heading_sim``).

Mirrors ``test_depthsim`` / ``test_seastate``: the two functions are pure and deterministic
(stdlib ``math`` only, no randomness, no globals, no wall-clock), so the same ``t_s`` always
yields the same value. These tests pin determinism, the bounded harmonic amplitude, the
distinct-phase decomposition at ``t=0``, the defensive +/-45 rudder clamp for an over-large
amp, and that the heading wander stays wrapped into ``[0, 360)`` around its setpoint.
"""

from __future__ import annotations

import math

import pytest

from nmea_sim.config import HeadingSimSpec, RudderSimSpec
from nmea_sim.steeringsim import (
    _HARMONIC_WEIGHT,
    _HEADING_HARMONIC_PHASE,
    _HEADING_PHASE,
    _RUDDER_HARMONIC_PHASE,
    _RUDDER_LIMIT_DEG,
    _RUDDER_PHASE,
    heading_sim,
    rudder_sim,
)

# Sample densely across a period so peaks are well resolved.
_SAMPLES = 4000


def _rudder_sweep(spec: RudderSimSpec) -> list[float]:
    return [rudder_sim(spec.period_s * i / _SAMPLES, spec) for i in range(_SAMPLES + 1)]


def _heading_sweep(setpoint: float, spec: HeadingSimSpec) -> list[float]:
    return [heading_sim(setpoint, spec.period_s * i / _SAMPLES, spec) for i in range(_SAMPLES + 1)]


def test_deterministic_repeat() -> None:
    """Same time/params -> identical output (no randomness, no hidden state)."""
    rspec = RudderSimSpec(enabled=True)
    hspec = HeadingSimSpec(enabled=True)
    assert rudder_sim(42.0, rspec) == rudder_sim(42.0, rspec)
    assert heading_sim(280.0, 42.0, hspec) == heading_sim(280.0, 42.0, hspec)


def test_pure_in_time_only() -> None:
    """Pure functions of their inputs alone -> identical inputs give identical output
    regardless of call order."""
    rspec = RudderSimSpec(enabled=True)
    hspec = HeadingSimSpec(enabled=True)
    times = (0.0, 3.5, 100.0, 250.5, 999.0)
    ra = [rudder_sim(t, rspec) for t in times]
    rb = [rudder_sim(t, rspec) for t in reversed(times)][::-1]
    assert ra == rb
    ha = [heading_sim(90.0, t, hspec) for t in times]
    hb = [heading_sim(90.0, t, hspec) for t in reversed(times)][::-1]
    assert ha == hb


def test_rudder_decomposition_at_zero() -> None:
    """At t=0 the rudder value is exactly its fundamental+harmonic decomposition at the frozen,
    distinct phase offsets. Pins the algorithm and the phase constants."""
    spec = RudderSimSpec(enabled=True)
    expected = spec.amp_deg * (
        math.sin(_RUDDER_PHASE) + _HARMONIC_WEIGHT * math.sin(_RUDDER_HARMONIC_PHASE)
    )
    assert rudder_sim(0.0, spec) == pytest.approx(expected)
    # Rudder fundamental has no phase offset; the harmonic sits at a distinct fixed phase.
    assert _RUDDER_PHASE == 0.0
    assert _RUDDER_PHASE != _RUDDER_HARMONIC_PHASE


def test_heading_decomposition_at_zero() -> None:
    """At t=0 heading == (setpoint + fundamental + harmonic) % 360 at the frozen phase offsets.
    Pins the algorithm, the setpoint anchoring, and the phase constants."""
    setpoint = 280.0
    spec = HeadingSimSpec(enabled=True)
    wander = spec.amp_deg * (
        math.sin(_HEADING_PHASE) + _HARMONIC_WEIGHT * math.sin(_HEADING_HARMONIC_PHASE)
    )
    assert heading_sim(setpoint, 0.0, spec) == pytest.approx((setpoint + wander) % 360.0)
    # The heading fundamental sits at a distinct phase from the rudder (they never peak together).
    assert _HEADING_PHASE != _RUDDER_PHASE
    assert pytest.approx(math.pi / 6.0) == _HEADING_PHASE


def test_rudder_amplitude_bounded_by_harmonic() -> None:
    """The rudder oscillation about 0 never exceeds ``amp_deg * (1 + _HARMONIC_WEIGHT)`` (each
    sine term is bounded by 1) and is demonstrably alive (not frozen)."""
    spec = RudderSimSpec(enabled=True)
    samples = _rudder_sweep(spec)
    bound = spec.amp_deg * (1.0 + _HARMONIC_WEIGHT)
    assert max(abs(v) for v in samples) <= bound + 1e-9
    assert max(samples) - min(samples) > 0.5  # moving
    # With the 1.5 deg default it stays comfortably clear of the 45 clamp.
    assert max(abs(v) for v in samples) < _RUDDER_LIMIT_DEG


def test_rudder_clamped_to_45_for_overlarge_amp() -> None:
    """A hand-tuned over-large amp is defensively clamped to the +/-45 mechanical limit."""
    spec = RudderSimSpec(enabled=True, amp_deg=200.0, period_s=10.0)
    samples = _rudder_sweep(spec)
    assert max(samples) == pytest.approx(_RUDDER_LIMIT_DEG)
    assert min(samples) == pytest.approx(-_RUDDER_LIMIT_DEG)
    for v in samples:
        assert -_RUDDER_LIMIT_DEG <= v <= _RUDDER_LIMIT_DEG


def test_heading_wraps_into_0_360_around_setpoint() -> None:
    """Heading is always returned in ``[0, 360)`` and stays within ``amp*(1+harmonic)`` of the
    setpoint (accounting for the wrap), for setpoints straddling 0/360."""
    spec = HeadingSimSpec(enabled=True)
    bound = spec.amp_deg * (1.0 + _HARMONIC_WEIGHT)
    for setpoint in (0.0, 1.0, 90.0, 280.0, 359.5):
        samples = _heading_sweep(setpoint, spec)
        for v in samples:
            assert 0.0 <= v < 360.0
            # Smallest signed angular distance from the setpoint must be within the bound.
            diff = ((v - setpoint + 180.0) % 360.0) - 180.0
            assert abs(diff) <= bound + 1e-9
        # Demonstrably alive.
        assert max(samples) != min(samples)


def test_heading_never_reseeds_from_output() -> None:
    """Feeding a prior heading output back as the setpoint would drift; the contract is that the
    setpoint is fixed, so the same setpoint always yields the same wander band. This pins that the
    setpoint argument is authoritative (never derived from a previous return)."""
    spec = HeadingSimSpec(enabled=True)
    # Two independent evaluations at the same setpoint/time are identical -> no accumulated state.
    first = heading_sim(90.0, 12.0, spec)
    second = heading_sim(90.0, 12.0, spec)
    assert first == second
    # A different setpoint simply shifts the band by the setpoint delta (mod 360), proving the
    # wander is added to the setpoint rather than fed back from output.
    shifted = heading_sim(90.0 + 30.0, 12.0, spec)
    assert shifted == pytest.approx((first + 30.0) % 360.0)
