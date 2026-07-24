"""Heading generator: valid checksums, re-parse, and heading != COG."""

from __future__ import annotations

import pynmea2
import pytest

from nmea_sim import checksum
from nmea_sim.heading_generator import SUPPORTED, HeadingGenerator
from nmea_sim.state import VesselState


@pytest.fixture
def gen() -> HeadingGenerator:
    return HeadingGenerator(talker="HE")


def test_build_returns_all_requested(gen: HeadingGenerator, sample_state: VesselState) -> None:
    lines = gen.build(sample_state)
    assert len(lines) == len(SUPPORTED)


def test_every_sentence_is_valid_and_he(gen: HeadingGenerator, sample_state: VesselState) -> None:
    for line in gen.build(sample_state):
        assert line.startswith("$HE")
        assert "\r" not in line and "\n" not in line
        assert checksum.verify(line), line


def test_hdt_uses_true_heading_not_cog(gen: HeadingGenerator, sample_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.hdt(sample_state))
    assert float(parsed.heading) == pytest.approx(sample_state.heading_true_deg, abs=0.05)
    # The whole point: heading is NOT course-over-ground.
    assert float(parsed.heading) != pytest.approx(sample_state.cog_deg, abs=0.05)


def test_hdm_uses_magnetic_heading(gen: HeadingGenerator, sample_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.hdm(sample_state))
    assert float(parsed.heading) == pytest.approx(sample_state.heading_mag_deg, abs=0.05)


def test_hdg_carries_variation(gen: HeadingGenerator, sample_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.hdg(sample_state))
    assert float(parsed.heading) == pytest.approx(sample_state.heading_mag_deg, abs=0.05)
    assert float(parsed.variation) == pytest.approx(abs(sample_state.mag_variation_deg), abs=0.05)
    assert parsed.var_dir == "W"  # sample variation is negative (West)


def test_build_rejects_unknown_sentence(gen: HeadingGenerator, sample_state: VesselState) -> None:
    with pytest.raises(ValueError):
        gen.build(sample_state, sentences=("HDT", "ZZZ"))
