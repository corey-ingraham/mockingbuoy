"""Deterministic steering models: a small helm oscillation and a gentle heading wander.

A vessel holding a course never sits perfectly still on the helm: the autopilot (or a
helmsman) makes small rudder corrections, and the head swings a degree or so either side of
the ordered course. This module turns a time into those two live-but-honest values so the
rudder-angle and heading readouts always breathe instead of sitting frozen.

Pure and deterministic, mirroring :mod:`nmea_sim.seastate` and :mod:`nmea_sim.depthsim`:
no randomness, no global state, no wall-clock. The same ``t_s`` always yields the same value.
Standard-library ``math`` only. Each component sits at a distinct phase so the rudder and the
heading wander never peak together, and both carry a bounded second harmonic (same idiom as
``seastate._oscillate``) so the motion reads organic rather than a single clean sine.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import HeadingSimSpec, RudderSimSpec

# Distinct fixed phase offsets (radians) so the rudder oscillation and the heading wander never
# peak together, keeping the two readouts visibly independent.
_RUDDER_PHASE = 0.0
_HEADING_PHASE = math.pi / 6.0  # distinct from rudder so the two never peak together

# Second-harmonic content for organic feel, same idiom and bound as ``seastate._oscillate``: the
# combined waveform can never exceed ``amplitude * (1 + _HARMONIC_WEIGHT)`` because each sine term
# is bounded by 1.
_HARMONIC_WEIGHT = 0.12
_RUDDER_HARMONIC_PHASE = math.pi / 5.0
_HEADING_HARMONIC_PHASE = math.pi / 2.0

# Hard mechanical clamp on the rudder (matches the -45..45 rudder bound in engine._STATE_RANGES).
_RUDDER_LIMIT_DEG = 45.0


def rudder_sim(t_s: float, params: RudderSimSpec) -> float:
    """Helm oscillation about 0 deg (small hold corrections), clamped to +/-45.

    A fundamental plus a bounded second harmonic, amplitude ``params.amp_deg`` and period
    ``params.period_s``. The absolute value is <= ``amp_deg * (1 + _HARMONIC_WEIGHT)`` before the
    clamp, so with the default 1.5 deg it never nears the 45 clamp; the clamp is defensive for a
    hand-tuned large amp. Pure and deterministic in ``t_s`` alone.
    """
    w = 2.0 * math.pi / params.period_s
    fundamental = math.sin(w * t_s + _RUDDER_PHASE)
    harmonic = _HARMONIC_WEIGHT * math.sin(2.0 * w * t_s + _RUDDER_HARMONIC_PHASE)
    val = params.amp_deg * (fundamental + harmonic)
    return max(-_RUDDER_LIMIT_DEG, min(_RUDDER_LIMIT_DEG, val))


def heading_sim(setpoint_deg: float, t_s: float, params: HeadingSimSpec) -> float:
    """Setpoint + gentle wander, returned modulo 360. NEVER re-seeded from its own output.

    The wander is a fundamental plus a bounded second harmonic, amplitude ``params.amp_deg``
    (~1 deg) and period ``params.period_s``, added to the fixed ``setpoint_deg``. The engine
    derives ``heading_mag_deg = (result - mag_variation_deg) % 360`` (convention verified in
    test_integration.py:59-61). Pure and deterministic in ``(setpoint_deg, t_s)``.
    """
    w = 2.0 * math.pi / params.period_s
    fundamental = math.sin(w * t_s + _HEADING_PHASE)
    harmonic = _HARMONIC_WEIGHT * math.sin(2.0 * w * t_s + _HEADING_HARMONIC_PHASE)
    wander = params.amp_deg * (fundamental + harmonic)
    return (setpoint_deg + wander) % 360.0
