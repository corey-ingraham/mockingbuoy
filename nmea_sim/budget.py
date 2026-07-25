"""Baud-budget guard: does a channel's emission fit the wire?

A serial line has a fixed character throughput (``baud / bits-per-character``). If a
channel is configured to emit more bytes per second than the wire can carry, sentences
queue and fall behind — a silent, corrupting failure. This module computes, per channel,
the offered load versus capacity and flags anything above a safety threshold (default
80%). The engine consults it at start-up; the config phase reuses it at validation time.

Worked example the plan calls out: on 4800 8N1 (480 char/s), ``HDG`` + ``HDT`` both at
10 Hz is ~100% of capacity — over budget — while ``HDT`` alone at 10 Hz is comfortably
under. (A TCP tap is a separate transport and does **not** count against this budget.)
"""

from __future__ import annotations

from dataclasses import dataclass

# Every serial line is terminated with CRLF on the wire; count those two bytes.
CRLF_BYTES = 2
DEFAULT_THRESHOLD = 0.80


def framing_bits(framing: str) -> int:
    """Bits per character for a framing string like ``8N1`` (start + data + parity + stop)."""
    f = framing.strip().upper()
    # Validate every position before any int() so a malformed framing yields ONE clear,
    # catchable error at validate() time rather than a raw int() ValueError out of the budget
    # calc (which would traceback --validate-only). Data 5-8, parity N/E/O, stop 1-2.
    if len(f) != 3 or f[0] not in "5678" or f[1] not in ("N", "E", "O") or f[2] not in ("1", "2"):
        raise ValueError(f"unsupported framing {framing!r} (expected e.g. 8N1)")
    data_bits = int(f[0])
    parity_bits = 0 if f[1] == "N" else 1
    stop_bits = int(f[2])
    return 1 + data_bits + parity_bits + stop_bits  # leading start bit


def wire_capacity_cps(baud: int, framing: str) -> float:
    """Characters (bytes) per second the wire can carry at this baud and framing."""
    return baud / framing_bits(framing)


def line_bytes(line: str) -> int:
    """On-the-wire byte count of a sentence, including its CRLF terminator."""
    return len(line) + CRLF_BYTES


@dataclass(frozen=True)
class ChannelBudget:
    """Offered load vs. wire capacity for one channel."""

    load_cps: float
    capacity_cps: float
    threshold: float

    @property
    def utilization(self) -> float:
        return self.load_cps / self.capacity_cps if self.capacity_cps else float("inf")

    @property
    def over(self) -> bool:
        return self.utilization > self.threshold


def evaluate(
    baud: int,
    framing: str,
    emissions: list[tuple[float, list[str]]],
    threshold: float = DEFAULT_THRESHOLD,
) -> ChannelBudget:
    """Compute the budget for a channel.

    ``emissions`` is one entry per emitted sentence type: ``(rate_hz, sample_lines)`` where
    ``sample_lines`` is the list of NMEA lines produced by a single emission of that type
    (usually one line; AIS multi-fragment messages contribute several).
    """
    load = 0.0
    for rate_hz, lines in emissions:
        bytes_per_emission = sum(line_bytes(line) for line in lines)
        load += bytes_per_emission * rate_hz
    return ChannelBudget(
        load_cps=load,
        capacity_cps=wire_capacity_cps(baud, framing),
        threshold=threshold,
    )
