"""Deterministic true-wind drift: a gusting speed and a veering direction.

Real wind is never steady — it gusts and lulls a few knots and veers/backs several degrees
over tens of seconds. This module turns a time into a live ``(true_speed, true_dir)`` so the
wind needles breathe instead of sitting frozen; the apparent (relative) wind the UI and the
MWV sentence show is then recomputed from this true wind plus vessel motion, so both dials
move together and stay physically consistent.

Pure and deterministic, mirroring :mod:`nmea_sim.steeringsim` and :mod:`nmea_sim.depthsim`:
no randomness, no global state, no wall-clock. The same ``t_s`` always yields the same value.
Standard-library ``math`` only. The gust and the veer sit at distinct phases (and each carries a
bounded second harmonic, same idiom as ``seastate._oscillate``) so speed and direction never peak
together and the motion reads organic rather than a single clean sine.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import WindSimSpec

# Distinct fixed phase offsets (radians) so the gust and the veer never peak together.
_GUST_PHASE = 0.0
_VEER_PHASE = math.pi / 3.0

# Second-harmonic content for organic feel, same idiom and bound as ``seastate._oscillate``: the
# combined waveform can never exceed ``amplitude * (1 + _HARMONIC_WEIGHT)`` because each sine term
# is bounded by 1.
_HARMONIC_WEIGHT = 0.15
_GUST_HARMONIC_PHASE = math.pi / 4.0
_VEER_HARMONIC_PHASE = math.pi / 2.5


def wind_sim(t_s: float, params: WindSimSpec) -> tuple[float, float]:
    """Return ``(true_speed_kn, true_dir_deg)`` drifting around the spec's base wind.

    Speed = ``base_speed_kn`` + a gust oscillation (amplitude ``gust_amp_kn``, period
    ``gust_period_s``), floored at 0 so a lull can never read negative. Direction =
    ``base_dir_deg`` + a veer oscillation (amplitude ``veer_amp_deg``, period ``veer_period_s``),
    taken modulo 360. Each is a fundamental plus a bounded second harmonic. Pure and deterministic
    in ``t_s`` alone.
    """
    wg = 2.0 * math.pi / params.gust_period_s
    gust = params.gust_amp_kn * (
        math.sin(wg * t_s + _GUST_PHASE)
        + _HARMONIC_WEIGHT * math.sin(2.0 * wg * t_s + _GUST_HARMONIC_PHASE)
    )
    speed = max(0.0, params.base_speed_kn + gust)

    wv = 2.0 * math.pi / params.veer_period_s
    veer = params.veer_amp_deg * (
        math.sin(wv * t_s + _VEER_PHASE)
        + _HARMONIC_WEIGHT * math.sin(2.0 * wv * t_s + _VEER_HARMONIC_PHASE)
    )
    direction = (params.base_dir_deg + veer) % 360.0
    return speed, direction
