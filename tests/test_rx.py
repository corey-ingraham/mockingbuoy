"""RX parse: generator output round-trips back to VesselState fields; whitelist gates it."""

from __future__ import annotations

import pynmea2
import pytest

from nmea_sim import rx
from nmea_sim.gps_generator import GpsGenerator
from nmea_sim.heading_generator import HeadingGenerator
from nmea_sim.state import VesselState


def test_rmc_round_trips_cog_not_heading(sample_state: VesselState) -> None:
    line = GpsGenerator("GP").rmc(sample_state)
    changes = rx.parse_line(line)
    assert changes["lat"] == pytest.approx(sample_state.lat, abs=1e-4)
    assert changes["lon"] == pytest.approx(sample_state.lon, abs=1e-4)
    assert changes["sog_kn"] == pytest.approx(sample_state.sog_kn, abs=0.05)
    # RMC carries course-over-ground, never heading — the codebase's #1 invariant.
    assert changes["cog_deg"] == pytest.approx(sample_state.cog_deg, abs=0.05)
    assert "heading_true_deg" not in changes


def test_gga_round_trips_fix_fields(sample_state: VesselState) -> None:
    line = GpsGenerator("GP").gga(sample_state)
    changes = rx.parse_line(line)
    assert changes["fix_quality"] == sample_state.fix_quality
    assert changes["satellites"] == sample_state.satellites
    assert changes["hdop"] == pytest.approx(sample_state.hdop, abs=0.05)
    assert changes["altitude_m"] == pytest.approx(sample_state.altitude_m, abs=0.05)


def test_hdt_round_trips_true_heading(sample_state: VesselState) -> None:
    line = HeadingGenerator("HE").hdt(sample_state)
    changes = rx.parse_line(line)
    assert changes["heading_true_deg"] == pytest.approx(sample_state.heading_true_deg, abs=0.05)
    assert "cog_deg" not in changes


def test_hdg_round_trips_magnetic_heading(sample_state: VesselState) -> None:
    line = HeadingGenerator("HE").hdg(sample_state)
    changes = rx.parse_line(line)
    assert changes["heading_mag_deg"] == pytest.approx(sample_state.heading_mag_deg, abs=0.05)


def test_vtg_round_trips_cog(sample_state: VesselState) -> None:
    line = GpsGenerator("GP").vtg(sample_state)
    changes = rx.parse_line(line)
    assert changes["cog_deg"] == pytest.approx(sample_state.cog_deg, abs=0.05)
    assert changes["sog_kn"] == pytest.approx(sample_state.sog_kn, abs=0.05)


def test_unrecognised_sentence_yields_nothing(sample_state: VesselState) -> None:
    # ZDA is valid NMEA the sim doesn't map to a state field.
    line = GpsGenerator("GP").zda(sample_state)
    assert rx.parse_line(line) == {}


def test_garbage_raises_parse_error() -> None:
    with pytest.raises(pynmea2.ParseError):
        rx.parse_line("not a sentence at all")


# --- whitelist gate --------------------------------------------------------------


def test_accepted_changes_keeps_only_whitelisted(sample_state: VesselState) -> None:
    line = GpsGenerator("GP").rmc(sample_state)
    accepted = rx.accepted_changes(line, ["cog_deg"])
    assert set(accepted) == {"cog_deg"}
    assert accepted["cog_deg"] == pytest.approx(sample_state.cog_deg, abs=0.05)


def test_empty_whitelist_accepts_nothing(sample_state: VesselState) -> None:
    line = GpsGenerator("GP").rmc(sample_state)
    assert rx.accepted_changes(line, []) == {}
