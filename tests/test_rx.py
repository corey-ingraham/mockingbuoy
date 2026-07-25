"""RX parse: generator output round-trips back to VesselState fields; whitelist gates it."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pynmea2
import pytest

from nmea_sim import checksum, rx
from nmea_sim.gps_generator import GpsGenerator
from nmea_sim.heading_generator import HeadingGenerator
from nmea_sim.state import VesselState


def _mk(body: str) -> str:
    """Wrap an NMEA body in ``$…*HH`` with a VALID checksum, so the garbage is in the fields."""
    return "$" + body + "*" + checksum.compute(body)


def test_rmc_round_trips_cog_not_heading(sample_state: VesselState) -> None:
    line = GpsGenerator("GP").rmc(sample_state)
    changes = rx.parse_line(line)
    assert changes["lat"] == pytest.approx(sample_state.lat, abs=1e-4)
    assert changes["lon"] == pytest.approx(sample_state.lon, abs=1e-4)
    assert changes["sog_kn"] == pytest.approx(sample_state.sog_kn, abs=0.05)
    # RMC carries course-over-ground, never heading — the codebase's #1 invariant.
    assert changes["cog_deg"] == pytest.approx(sample_state.cog_deg, abs=0.05)
    assert "heading_true_deg" not in changes


def test_rmc_mag_variation_east_is_positive(sample_state: VesselState) -> None:
    """RMC magnetic variation round-trips East-positive (the codebase's East-positive convention),
    so a LIVE->SIM handover seeds variation without a sign flip."""
    east = replace(sample_state, mag_variation_deg=6.5)  # East variation
    line = GpsGenerator("GP").rmc(east)
    changes = rx.parse_line(line)
    assert changes["mag_variation_deg"] == pytest.approx(6.5, abs=0.05)


def test_rmc_mag_variation_west_is_negative(sample_state: VesselState) -> None:
    # sample_state carries a West (-3.0) variation; the parsed sign must stay negative.
    line = GpsGenerator("GP").rmc(sample_state)
    changes = rx.parse_line(line)
    assert changes["mag_variation_deg"] == pytest.approx(sample_state.mag_variation_deg, abs=0.05)
    assert changes["mag_variation_deg"] < 0


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


# --- parse_time: the Time Authority / ZDA-synthesis feed --------------------------


def test_parse_time_rmc_recovers_tz_aware_utc_with_subsecond(sample_state: VesselState) -> None:
    """RMC's datestamp + sub-second timestamp round-trip to the exact tz-aware UTC — this is the
    instant the single-source ZDA carve-out re-emits, so it must survive to centisecond precision.
    """
    line = GpsGenerator("GP").rmc(sample_state)
    parsed = rx.parse_time(line)
    # sample_state.utc carries 420000 microseconds; the generator writes centiseconds (.42).
    assert parsed == datetime(2024, 6, 21, 12, 35, 19, 420000, tzinfo=UTC)


def test_parse_time_zda_recovers_tz_aware_utc(sample_state: VesselState) -> None:
    line = GpsGenerator("GP").zda(sample_state)
    parsed = rx.parse_time(line)
    assert parsed == datetime(2024, 6, 21, 12, 35, 19, 420000, tzinfo=UTC)


def test_parse_time_non_time_sentence_is_none(sample_state: VesselState) -> None:
    # GLL carries a time but no date, and HDT carries neither: neither is a full-date source.
    assert rx.parse_time(GpsGenerator("GP").gll(sample_state)) is None
    assert rx.parse_time(HeadingGenerator("HE").hdt(sample_state)) is None


def test_parse_time_blank_date_time_fields_is_none() -> None:
    """An RMC with empty timestamp/datestamp fields must never fabricate a time -> None."""
    blank = str(pynmea2.RMC("GP", "RMC", ("", "V", "", "", "", "", "", "", "", "", "", "N")))
    assert rx.parse_time(blank) is None


# --- whitelist gate --------------------------------------------------------------


def test_accepted_changes_keeps_only_whitelisted(sample_state: VesselState) -> None:
    line = GpsGenerator("GP").rmc(sample_state)
    accepted = rx.accepted_changes(line, ["cog_deg"])
    assert set(accepted) == {"cog_deg"}
    assert accepted["cog_deg"] == pytest.approx(sample_state.cog_deg, abs=0.05)


def test_empty_whitelist_accepts_nothing(sample_state: VesselState) -> None:
    line = GpsGenerator("GP").rmc(sample_state)
    assert rx.accepted_changes(line, []) == {}


# --- H1: total per-field parsing on a checksum-valid line ------------------------
# A checksum-valid sentence with one garbage field must skip that field and keep the rest,
# never raising — one bad wire line can never kill the reader/worker thread.


def test_bad_speed_field_is_skipped_not_raised() -> None:
    """RMC speed '1.2.3' -> float() ValueError on the field; sog_kn is dropped, the rest stays."""
    line = _mk("GPRMC,123519.42,A,4807.038,N,01131.000,E,1.2.3,084.4,210624,003.1,W,A")
    changes = rx.parse_line(line)  # must not raise
    assert "sog_kn" not in changes
    assert changes["cog_deg"] == pytest.approx(84.4, abs=0.05)
    assert changes["lat"] == pytest.approx(48.1173, abs=1e-3)


def test_garbage_lat_field_is_skipped_not_raised() -> None:
    """An unparseable coordinate raises AttributeError/ValueError on access; lat/lon are dropped."""
    line = _mk("GPRMC,123519.42,A,ABCD,N,01131.000,E,022.4,084.4,210624,003.1,W,A")
    changes = rx.parse_line(line)  # must not raise
    assert "lat" not in changes and "lon" not in changes
    assert changes["sog_kn"] == pytest.approx(22.4, abs=0.05)


def test_non_finite_speed_field_is_skipped() -> None:
    """A checksum-valid NaN speed must never poison state -> the field is dropped (finite gate)."""
    line = _mk("GPRMC,123519.42,A,4807.038,N,01131.000,E,NaN,084.4,210624,003.1,W,A")
    changes = rx.parse_line(line)  # must not raise
    assert "sog_kn" not in changes
    assert changes["lat"] == pytest.approx(48.1173, abs=1e-3)


def test_non_numeric_zda_int_field_does_not_raise() -> None:
    """A non-numeric ZDA day would raise int() ValueError; parse_line must stay total."""
    line = _mk("GPZDA,123519.00,AB,07,2024,00,00")
    assert rx.parse_line(line) == {}  # ZDA maps no state fields, and must not raise


# --- H9: hemisphere-present + finite gates ---------------------------------------


def test_blank_hemisphere_position_is_absent_not_zero_zero() -> None:
    """pynmea2 yields latitude/longitude 0.0 when the hemisphere is blank; a blank-hemisphere
    RMC must be treated as ABSENT position, never seeding a spurious (0, 0) fix."""
    line = _mk("GPRMC,123519.42,A,4807.038,,01131.000,,022.4,084.4,210624,003.1,W,A")
    changes = rx.parse_line(line)
    assert "lat" not in changes and "lon" not in changes
    assert changes["sog_kn"] == pytest.approx(22.4, abs=0.05)  # non-position fields still map


# --- parse_time totality + DOM4 status gate --------------------------------------


def test_parse_time_bad_datestamp_is_none_not_raised() -> None:
    """A '990013' datestamp makes datetime.combine raise TypeError; parse_time must return None."""
    line = _mk("GPRMC,123519.42,A,4807.038,N,01131.000,E,022.4,084.4,990013,003.1,W,A")
    assert rx.parse_time(line) is None  # must not raise


def test_parse_time_leap_second_time_is_none_not_raised() -> None:
    """A documented leap-second '235960' time is unparseable; parse_time returns None, no raise."""
    line = _mk("GPRMC,235960,A,4807.038,N,01131.000,E,022.4,084.4,210624,003.1,W,A")
    assert rx.parse_time(line) is None


def test_parse_time_non_numeric_zda_is_none_not_raised() -> None:
    line = _mk("GPZDA,123519.00,AB,07,2024,00,00")
    assert rx.parse_time(line) is None


def test_parse_time_rmc_status_void_is_none(sample_state: VesselState) -> None:
    """RMC status V (void/no-fix) is a free-running RTC, not a GNSS-tier time -> None even with a
    valid datestamp/timestamp, so it can never outrank a real time source."""
    line = _mk("GPRMC,123519.42,V,4807.038,N,01131.000,E,022.4,084.4,210624,003.1,W,N")
    assert rx.parse_time(line) is None


# --- DOM7: THS / HDM / GLL / ROT now seed failover state -------------------------


def test_ths_maps_true_heading(sample_state: VesselState) -> None:
    """A THS-only satellite compass must seed heading_true_deg for failover."""
    line = HeadingGenerator("HE").ths(sample_state)
    changes = rx.parse_line(line)
    assert changes["heading_true_deg"] == pytest.approx(sample_state.heading_true_deg, abs=0.05)


def test_hdm_maps_magnetic_heading(sample_state: VesselState) -> None:
    line = HeadingGenerator("HE").hdm(sample_state)
    changes = rx.parse_line(line)
    assert changes["heading_mag_deg"] == pytest.approx(sample_state.heading_mag_deg, abs=0.05)


def test_gll_maps_position(sample_state: VesselState) -> None:
    line = GpsGenerator("GP").gll(sample_state)
    changes = rx.parse_line(line)
    assert changes["lat"] == pytest.approx(sample_state.lat, abs=1e-4)
    assert changes["lon"] == pytest.approx(sample_state.lon, abs=1e-4)


def test_rot_maps_rate_of_turn() -> None:
    line = _mk("TIROT,-15.0,A")
    changes = rx.parse_line(line)
    assert changes["rot_dpm"] == pytest.approx(-15.0, abs=0.05)
