"""Deterministic depth-under-keel model: a wire-backed ``depth_m`` from three sinusoids.

The buoy sits over a seabed whose depth is never perfectly flat: slow bathymetric drift
as the hull swings on its watch circle, gentler shoaling/deepening runs, and a small
swell ripple on top. This module turns a fixed base depth plus a time into a single
``depth_m`` value so DPT/DBT and the depth chart always have a *live but honest* number
instead of a frozen constant.

Pure and deterministic, mirroring :mod:`nmea_sim.seastate`: no randomness, no global
state, no wall-clock. The same ``t_s`` always yields the same depth. Standard-library
``math`` only. The three sinusoids sit at distinct phases so they never peak together,
and the sum is floored at ``params.min_depth_m`` (>= 0) so depth can never go negative.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import DepthSimSpec

# Distinct fixed phase offsets (radians) so the three components never peak together, keeping
# the summed depth organic rather than a single clean sine. The drift term carries no phase
# offset (it anchors t=0 at the base depth); shoal and ripple are offset apart from it.
_SHOAL_PHASE = math.pi / 4.0
_RIPPLE_PHASE = math.pi / 2.0


def depth_sim(base_depth_m: float, t_s: float, params: DepthSimSpec) -> float:
    """Return depth in metres at time ``t_s`` seconds, floored at ``params.min_depth_m``.

    A fixed base depth with three summed sinusoids at distinct periods/phases: a slow
    bathymetric drift, a gentler shoaling/deepening run, and a small swell ripple. Pure and
    deterministic in ``t_s`` alone (identical inputs give identical output), exactly like
    :func:`nmea_sim.seastate.sea_state_motion`.
    """
    w_drift = 2.0 * math.pi / params.drift_period_s
    w_shoal = 2.0 * math.pi / params.shoal_period_s
    w_ripple = 2.0 * math.pi / params.ripple_period_s
    drift = params.drift_amp_m * math.sin(w_drift * t_s)  # slow bathymetric drift
    shoal = params.shoal_amp_m * math.sin(w_shoal * t_s + _SHOAL_PHASE)  # gentle shoaling runs
    ripple = params.ripple_amp_m * math.sin(w_ripple * t_s + _RIPPLE_PHASE)  # small swell ripple
    depth = base_depth_m + drift + shoal + ripple
    return max(params.min_depth_m, depth)
