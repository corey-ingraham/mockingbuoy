"""Deterministic sea-state motion model: pitch and roll from a WMO sea state.

A hull rolls and pitches on the waves. This module turns a WMO sea state (0 = calm
glassy, 9 = phenomenal) plus a time into a ``(pitch_deg, roll_deg)`` pair, so the rest
of the sim can gently perturb attitude-derived values without ever sitting perfectly
still — a floating hull never does.

The physics kept here are deliberately simple but honest:

* **Amplitude scales with sea state; period does not.** Roll and pitch periods are hull
  properties (a resonance the sea excites), so they stay fixed while only the *amount* of
  motion grows with the sea state. A fixed table maps each sea state to a roll amplitude
  and a (smaller) pitch amplitude, both rising monotonically. The calm end is tiny but
  non-zero; the top end is capped at realistic values (see ``ROLL_CAP_DEG`` /
  ``PITCH_CAP_DEG``).
* **Roll and pitch do not move in lockstep.** They use distinct natural periods and phase
  offsets, plus a small second harmonic, so the combined motion reads as organic rather
  than a single clean sine.

Pure and deterministic: no randomness, no global state, no wall-clock. The same
``(sea_state, t_s)`` always yields the same tuple. Standard-library math only.
"""

from __future__ import annotations

import math

# WMO sea state -> (roll amplitude deg, pitch amplitude deg). Both rise monotonically.
# Calm end is tiny-but-non-zero; the top end stays inside the caps below even after the
# second harmonic is added (see the harmonic bound note in _oscillate).
_AMPLITUDE_DEG: tuple[tuple[float, float], ...] = (
    (0.30, 0.15),  # 0 — calm (glassy)
    (1.00, 0.50),  # 1 — calm (rippled)
    (2.50, 1.20),  # 2 — smooth (wavelets)
    (5.00, 2.20),  # 3 — slight
    (8.00, 3.50),  # 4 — moderate
    (12.00, 5.00),  # 5 — rough
    (18.00, 7.00),  # 6 — very rough
    (24.00, 9.00),  # 7 — high
    (30.00, 11.00),  # 8 — very high
    (35.00, 12.00),  # 9 — phenomenal
)

# Fixed natural periods (seconds) — hull resonances, independent of sea state.
_ROLL_PERIOD_S = 12.0
_PITCH_PERIOD_S = 7.0

# Distinct phase offsets (radians) so roll and pitch never peak together.
_ROLL_PHASE = 0.0
_PITCH_PHASE = math.pi / 3.0

# Second-harmonic content for organic feel. Its relative weight is bounded so the
# combined waveform can never exceed ``amplitude * (1 + _HARMONIC_WEIGHT)``.
_HARMONIC_WEIGHT = 0.12
_ROLL_HARMONIC_PHASE = math.pi / 5.0
_PITCH_HARMONIC_PHASE = math.pi / 2.0

# Documented worst-case bounds at sea state 9. The SS9 amplitudes above times the
# harmonic bound (1 + _HARMONIC_WEIGHT) stay strictly under these.
ROLL_CAP_DEG = 40.0
PITCH_CAP_DEG = 15.0

_MAX_SEA_STATE = len(_AMPLITUDE_DEG) - 1


def _clamp_sea_state(sea_state: int) -> int:
    """Round and clamp an arbitrary sea state into the supported 0..9 range."""
    return max(0, min(_MAX_SEA_STATE, round(sea_state)))


def _oscillate(
    amplitude: float, period_s: float, phase: float, harmonic_phase: float, t_s: float
) -> float:
    """A fixed-shape fundamental-plus-second-harmonic oscillation at ``t_s``.

    The absolute value is bounded by ``amplitude * (1 + _HARMONIC_WEIGHT)`` because each
    sine term is bounded by 1, which is what keeps the caps honest.
    """
    w = 2.0 * math.pi / period_s
    fundamental = math.sin(w * t_s + phase)
    harmonic = _HARMONIC_WEIGHT * math.sin(2.0 * w * t_s + harmonic_phase)
    return amplitude * (fundamental + harmonic)


def sea_state_motion(sea_state: int, t_s: float) -> tuple[float, float]:
    """Return (pitch_deg, roll_deg) for a WMO sea state 0-9 at time t_s seconds."""
    roll_amp, pitch_amp = _AMPLITUDE_DEG[_clamp_sea_state(sea_state)]
    pitch_deg = _oscillate(pitch_amp, _PITCH_PERIOD_S, _PITCH_PHASE, _PITCH_HARMONIC_PHASE, t_s)
    roll_deg = _oscillate(roll_amp, _ROLL_PERIOD_S, _ROLL_PHASE, _ROLL_HARMONIC_PHASE, t_s)
    return (pitch_deg, roll_deg)
