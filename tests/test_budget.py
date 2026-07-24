"""Baud-budget guard: framing math, wire capacity, and the HDG+HDT-over-4800 case."""

from __future__ import annotations

import pytest

from nmea_sim import budget
from nmea_sim.heading_generator import HeadingGenerator
from nmea_sim.state import VesselState


def test_framing_bits() -> None:
    assert budget.framing_bits("8N1") == 10  # start + 8 + 0 + 1
    assert budget.framing_bits("8E1") == 11  # parity adds a bit
    assert budget.framing_bits("7E1") == 10


def test_framing_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        budget.framing_bits("banana")


def test_wire_capacity() -> None:
    assert budget.wire_capacity_cps(4800, "8N1") == pytest.approx(480.0)
    assert budget.wire_capacity_cps(38400, "8N1") == pytest.approx(3840.0)


def test_line_bytes_counts_crlf() -> None:
    assert budget.line_bytes("$HEHDT,280.0,T*1F") == len("$HEHDT,280.0,T*1F") + 2


def test_hdt_only_is_within_budget(sample_state: VesselState) -> None:
    hg = HeadingGenerator("HE")
    result = budget.evaluate(4800, "8N1", [(10.0, [hg.hdt(sample_state)])])
    assert not result.over
    assert result.utilization < 0.8


def test_hdg_plus_hdt_at_10hz_is_over_budget(sample_state: VesselState) -> None:
    hg = HeadingGenerator("HE")
    emissions = [
        (10.0, [hg.hdt(sample_state)]),
        (10.0, [hg.hdg(sample_state)]),
    ]
    result = budget.evaluate(4800, "8N1", emissions)
    assert result.over  # the plan's canonical over-budget case
    assert result.utilization > 0.8


def test_utilization_and_threshold() -> None:
    # 240 char/s offered against 480 char/s = 50%.
    result = budget.evaluate(4800, "8N1", [(10.0, ["x" * 22])])  # (22+2)*10 = 240
    assert result.utilization == pytest.approx(0.5)
    assert not result.over
