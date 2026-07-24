"""Instrument generator: valid checksums, re-parse round-trips, apparent vs true wind."""

from __future__ import annotations

from dataclasses import replace

import pynmea2
import pytest

from nmea_sim import checksum
from nmea_sim.instrument_generator import SUPPORTED, InstrumentGenerator
from nmea_sim.state import VesselState
from nmea_sim.wind import apparent_wind


@pytest.fixture
def gen() -> InstrumentGenerator:
    return InstrumentGenerator(talker="II")


@pytest.fixture
def inst_state(sample_state: VesselState) -> VesselState:
    """``sample_state`` with every instrument field driven to a non-zero value.

    The base fixture leaves STW/ROT/attitude/rudder/current at their defaults; round-trip
    tests need distinct non-zero values so a dropped or cross-wired field is visible.
    """
    return replace(
        sample_state,
        stw_kn=11.7,
        depth_m=25.0,
        rot_dpm=4.5,
        wind_speed_kn=10.0,
        wind_dir_deg=200.0,
        pitch_deg=1.5,
        roll_deg=-2.3,
        rudder_angle_deg=3.2,
        set_deg=120.0,
        drift_kn=1.1,
    )


def test_build_returns_all_requested(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    lines = gen.build(inst_state)
    assert len(lines) == len(SUPPORTED)


def test_every_sentence_is_valid(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    for line in gen.build(inst_state):
        assert "\r" not in line and "\n" not in line  # no line ending baked in
        assert checksum.verify(line), line


def test_standard_sentences_reparse_as_ii(
    gen: InstrumentGenerator, inst_state: VesselState
) -> None:
    standard = tuple(name for name in SUPPORTED if name != "PASHR")
    for line in gen.build(inst_state, sentences=standard):
        assert line.startswith("$II")
        parsed = pynmea2.parse(line)  # raises on malformed/checksum error
        assert parsed.talker == "II"


def test_vhw_speed_and_heading(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.vhw(inst_state))
    assert float(parsed.heading_true) == pytest.approx(inst_state.heading_true_deg, abs=0.05)
    assert float(parsed.heading_magnetic) == pytest.approx(inst_state.heading_mag_deg, abs=0.05)
    assert float(parsed.water_speed_knots) == pytest.approx(inst_state.stw_kn, abs=0.05)
    assert float(parsed.water_speed_km) == pytest.approx(inst_state.stw_kn * 1.852, abs=0.05)


def test_dpt_depth_and_zero_offset(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.dpt(inst_state))
    assert float(parsed.depth) == pytest.approx(inst_state.depth_m, abs=0.05)
    assert float(parsed.offset) == pytest.approx(0.0, abs=0.05)


def test_dbt_units_roundtrip(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.dbt(inst_state))
    assert float(parsed.depth_meters) == pytest.approx(inst_state.depth_m, abs=0.05)
    assert float(parsed.depth_feet) == pytest.approx(inst_state.depth_m / 0.3048, abs=0.05)
    assert float(parsed.depth_fathoms) == pytest.approx(inst_state.depth_m / 1.8288, abs=0.05)
    assert parsed.unit_feet == "f"
    assert parsed.unit_meters == "M"
    assert parsed.unit_fathoms == "F"


def test_mwv_is_apparent_wind(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.mwv(inst_state))
    exp_speed, exp_angle = apparent_wind(
        inst_state.wind_speed_kn,
        inst_state.wind_dir_deg,
        inst_state.heading_true_deg,
        inst_state.cog_deg,
        inst_state.sog_kn,
    )
    assert float(parsed.wind_angle) == pytest.approx(exp_angle, abs=0.05)
    assert float(parsed.wind_speed) == pytest.approx(exp_speed, abs=0.05)
    assert parsed.reference == "R"  # relative = apparent
    assert parsed.status == "A"
    # Apparent angle is bow-relative and must differ from the true FROM bearing.
    assert float(parsed.wind_angle) != pytest.approx(inst_state.wind_dir_deg, abs=0.05)


def test_mwd_is_true_wind(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.mwd(inst_state))
    assert float(parsed.direction_true) == pytest.approx(inst_state.wind_dir_deg, abs=0.05)
    exp_mag = (inst_state.wind_dir_deg - inst_state.mag_variation_deg) % 360.0
    assert float(parsed.direction_magnetic) == pytest.approx(exp_mag, abs=0.05)
    assert float(parsed.wind_speed_knots) == pytest.approx(inst_state.wind_speed_kn, abs=0.05)
    assert float(parsed.wind_speed_meters) == pytest.approx(
        inst_state.wind_speed_kn * 0.514444, abs=0.05
    )


def test_rot_rate_and_status(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.rot(inst_state))
    assert float(parsed.rate_of_turn) == pytest.approx(inst_state.rot_dpm, abs=0.05)
    assert parsed.status == "A"


def test_xdr_pitch_and_roll(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.xdr(inst_state))
    # data = [type, value, units, name] x 2 (pitch group then roll group).
    assert parsed.data[3] == "PTCH"
    assert float(parsed.data[1]) == pytest.approx(inst_state.pitch_deg, abs=0.05)
    assert parsed.data[7] == "ROLL"
    assert float(parsed.data[5]) == pytest.approx(inst_state.roll_deg, abs=0.05)
    assert parsed.data[2] == "D" and parsed.data[6] == "D"


def test_rsa_starboard_angle(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.rsa(inst_state))
    assert float(parsed.rsa_starboard) == pytest.approx(inst_state.rudder_angle_deg, abs=0.05)
    assert parsed.rsa_starboard_status == "A"
    assert not parsed.rsa_port  # single-rudder: port field empty


def test_vdr_set_and_drift(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    parsed = pynmea2.parse(gen.vdr(inst_state))
    assert float(parsed.deg_t) == pytest.approx(inst_state.set_deg, abs=0.05)
    exp_mag = (inst_state.set_deg - inst_state.mag_variation_deg) % 360.0
    assert float(parsed.deg_m) == pytest.approx(exp_mag, abs=0.05)
    assert float(parsed.current) == pytest.approx(inst_state.drift_kn, abs=0.05)


def test_pashr_checksum_and_fields(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    line = gen.pashr(inst_state)
    assert line.startswith("$PASHR,")
    assert "\r" not in line and "\n" not in line
    assert checksum.verify(line), line
    body, _ = checksum.split(line)
    fields = body.split(",")
    assert fields[0] == "PASHR"
    # Layout: PASHR, time, heading, T, roll, pitch, heave, ...
    assert fields[3] == "T"
    assert float(fields[2]) == pytest.approx(inst_state.heading_true_deg, abs=0.05)
    assert float(fields[4]) == pytest.approx(inst_state.roll_deg, abs=0.05)
    assert float(fields[5]) == pytest.approx(inst_state.pitch_deg, abs=0.05)
    assert float(fields[6]) == pytest.approx(0.0, abs=0.005)  # heave


def test_build_rejects_unknown_sentence(gen: InstrumentGenerator, inst_state: VesselState) -> None:
    with pytest.raises(ValueError):
        gen.build(inst_state, sentences=("VHW", "ZZZ"))
