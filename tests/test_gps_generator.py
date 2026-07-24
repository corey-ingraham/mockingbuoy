"""GPS generator: valid checksums, re-parse round-trips, and COG != heading."""

from __future__ import annotations

import pynmea2
import pytest

from nmea_sim import checksum
from nmea_sim.gps_generator import SUPPORTED, GpsGenerator
from nmea_sim.state import VesselState


@pytest.fixture
def gen() -> GpsGenerator:
    return GpsGenerator(talker="GP")


def test_build_returns_all_requested(gen: GpsGenerator, sample_state: VesselState) -> None:
    lines = gen.build(sample_state)
    assert len(lines) == len(SUPPORTED)


def test_every_sentence_is_valid_and_gp(gen: GpsGenerator, sample_state: VesselState) -> None:
    for line in gen.build(sample_state):
        assert line.startswith("$GP")
        assert "\r" not in line and "\n" not in line  # no line ending baked in
        assert checksum.verify(line), line


def test_all_sentences_reparse(gen: GpsGenerator, sample_state: VesselState) -> None:
    for line in gen.build(sample_state):
        parsed = pynmea2.parse(line)  # raises on malformed/checksum error
        assert parsed.talker == "GP"


def test_gga_position_roundtrips(gen: GpsGenerator, sample_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.gga(sample_state))
    assert parsed.latitude == pytest.approx(sample_state.lat, abs=1e-4)
    assert parsed.longitude == pytest.approx(sample_state.lon, abs=1e-4)
    assert int(parsed.num_sats) == sample_state.satellites
    assert parsed.gps_qual == sample_state.fix_quality


def test_rmc_uses_cog_not_heading(gen: GpsGenerator, sample_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.rmc(sample_state))
    assert float(parsed.true_course) == pytest.approx(sample_state.cog_deg, abs=0.05)
    assert float(parsed.true_course) != pytest.approx(sample_state.heading_true_deg, abs=0.05)


def test_vtg_true_track_uses_cog_not_heading(gen: GpsGenerator, sample_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.vtg(sample_state))
    assert float(parsed.true_track) == pytest.approx(sample_state.cog_deg, abs=0.05)
    assert float(parsed.true_track) != pytest.approx(sample_state.heading_true_deg, abs=0.05)


def test_vtg_magnetic_track_derived_from_variation(
    gen: GpsGenerator, sample_state: VesselState
) -> None:
    parsed = pynmea2.parse(gen.vtg(sample_state))
    expected_mag = (sample_state.cog_deg - sample_state.mag_variation_deg) % 360.0
    assert float(parsed.mag_track) == pytest.approx(expected_mag, abs=0.05)


def test_rmc_speed_and_status(gen: GpsGenerator, sample_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.rmc(sample_state))
    assert float(parsed.spd_over_grnd) == pytest.approx(sample_state.sog_kn, abs=0.05)
    assert parsed.status == "A"  # fix_quality > 0


def test_rmc_status_void_when_no_fix(gen: GpsGenerator, sample_state: VesselState) -> None:
    from dataclasses import replace

    no_fix = replace(sample_state, fix_quality=0)
    parsed = pynmea2.parse(gen.rmc(no_fix))
    assert parsed.status == "V"


def test_zda_date_matches_state(gen: GpsGenerator, sample_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.zda(sample_state))
    assert int(parsed.day) == sample_state.utc.day
    assert int(parsed.month) == sample_state.utc.month
    assert int(parsed.year) == sample_state.utc.year


def test_build_rejects_unknown_sentence(gen: GpsGenerator, sample_state: VesselState) -> None:
    with pytest.raises(ValueError):
        gen.build(sample_state, sentences=("GGA", "ZZZ"))
