"""aisprofile: CSV + AIVDM ingestion distil into a valid, statistics-only realism profile.

Everything is in-memory / tmp-file and cross-platform. AIVDM sentences are built with the same
``pyais`` encoder the engine uses, so the fixtures are real, checksum-valid sentences rather than
hand-typed literals. All coordinates/MMSIs are synthetic.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

import pytest
from pyais.encode import encode_dict

from nmea_sim.aisprofile import aivdm_source, build_profile, csv_source
from nmea_sim.aisprofile.__main__ import _sniff_format, main
from nmea_sim.realism import RealismProfile

# --- CSV fixtures -----------------------------------------------------------------

_CSV_HEADER = [
    "MMSI",
    "BaseDateTime",
    "LAT",
    "LON",
    "SOG",
    "COG",
    "Heading",
    "VesselType",
    "TransceiverClass",
]

# Five distinct synthetic vessels: 2 cargo, 1 fishing, 1 pleasure, 1 "other".
# Two 30-min buckets: 00:00-00:30 holds {1,2,3}; 00:30-01:00 holds {1,2,3,4,5}.
# -> per-bucket distinct counts [3, 5] -> median 4 -> target_count 4.
_CSV_ROWS = [
    # mmsi, ts, lat, lon, sog, cog, heading, vessel_type, class
    (111111111, "2024-01-01T00:05:00", 10.10, -30.30, 12.0, 90.0, 90, 70, "A"),
    (222222222, "2024-01-01T00:06:00", 10.20, -30.20, 14.0, 80.0, 80, 70, "A"),
    (333333333, "2024-01-01T00:07:00", 10.05, -30.35, 4.0, 10.0, 15, 30, "A"),
    (111111111, "2024-01-01T00:40:00", 10.12, -30.28, 11.0, 92.0, 92, 70, "A"),
    (222222222, "2024-01-01T00:41:00", 10.22, -30.18, 13.5, 82.0, 82, 70, "A"),
    (333333333, "2024-01-01T00:42:00", 10.06, -30.34, 3.5, 12.0, 511, 30, "A"),
    (444444444, "2024-01-01T00:43:00", 10.30, -30.10, 6.0, 45.0, 45, 37, "B"),
    (555555555, "2024-01-01T00:44:00", 10.00, -30.40, 2.0, 20.0, 20, 0, "B"),
]


def _write_csv(path: Path, rows: list[tuple] = _CSV_ROWS) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_CSV_HEADER)
        writer.writerows(rows)
    return path


def test_csv_profile_is_valid_and_has_expected_shape(tmp_path: Path) -> None:
    src = _write_csv(tmp_path / "ais.csv")
    profile = build_profile(csv_source.iter_records(src))

    # Output validates through the canonical schema (round-trip is also done inside build_profile).
    loaded = RealismProfile.from_dict(profile)
    assert isinstance(loaded, RealismProfile)

    # type_mix normalises to 1 over distinct vessels: cargo 2/5, fishing/pleasure/other 1/5 each.
    mix = profile["type_mix"]
    assert sum(mix.values()) == pytest.approx(1.0, abs=0.01)
    assert mix["cargo"] == pytest.approx(0.4)
    assert mix["fishing"] == pytest.approx(0.2)

    # target_count is the median concurrent count over the two 30-min buckets ([3, 5] -> 4).
    assert profile["target_count"] == 4

    # class_a_fraction: 3 of 5 vessels are Class A.
    assert profile["class_a_fraction"] == pytest.approx(0.6)


def test_csv_bbox_is_percentile_bounded(tmp_path: Path) -> None:
    src = _write_csv(tmp_path / "ais.csv")
    profile = build_profile(csv_source.iter_records(src))
    region = profile["region"]
    # 1st/99th percentile box sits inside the raw min/max extent of the fixture points.
    assert region["min_lat"] >= 10.00
    assert region["max_lat"] <= 10.30
    assert region["min_lon"] >= -30.40
    assert region["max_lon"] <= -30.10
    assert region["min_lat"] <= region["max_lat"]
    assert region["min_lon"] <= region["max_lon"]


def test_csv_bbox_filter_excludes_out_of_box_rows(tmp_path: Path) -> None:
    rows = [
        (111111111, "2024-01-01T00:05:00", 10.10, -30.30, 12.0, 90.0, 90, 70, "A"),
        (999999999, "2024-01-01T00:06:00", 50.00, 10.00, 5.0, 0.0, 0, 30, "A"),  # far outside
    ]
    src = _write_csv(tmp_path / "ais.csv", rows)
    profile = build_profile(
        csv_source.iter_records(src, bbox=(10.0, 10.5, -30.5, -30.0)),
    )
    # Only the in-box cargo vessel survives -> type_mix is entirely cargo.
    assert profile["type_mix"] == {"cargo": pytest.approx(1.0)}


def test_csv_column_override_maps_alternate_headers(tmp_path: Path) -> None:
    path = tmp_path / "alt.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["UserID", "Latitude", "Longitude", "Type"])
        writer.writerow([111111111, 10.1, -30.3, 70])
    records = list(
        csv_source.iter_records(
            path,
            columns={"mmsi": "UserID", "lat": "Latitude", "lon": "Longitude", "ship_type": "Type"},
        )
    )
    assert len(records) == 1
    assert records[0].mmsi == 111111111
    assert records[0].ship_type == 70


def test_csv_missing_required_column_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["MMSI", "SOG"])  # no LAT/LON
        writer.writerow([111111111, 5.0])
    with pytest.raises(ValueError):
        list(csv_source.iter_records(path))


def test_csv_bad_rows_are_skipped_not_fatal(tmp_path: Path) -> None:
    rows = [
        ("notanumber", "2024-01-01T00:00:00", "nope", "nope", "x", "y", "", "", ""),
        (111111111, "2024-01-01T00:00:00", 10.1, -30.3, 12.0, 90.0, 90, 70, "A"),
    ]
    src = _write_csv(tmp_path / "ais.csv", rows)
    records = list(csv_source.iter_records(src))
    assert [r.mmsi for r in records] == [111111111]


def test_empty_record_stream_raises() -> None:
    with pytest.raises(ValueError):
        build_profile(iter(()))


# --- AIVDM fixtures ---------------------------------------------------------------


def _position_a(mmsi: int, lat: float, lon: float, sog: float, cog: float) -> list[str]:
    return encode_dict(
        {"mmsi": mmsi, "type": 1, "lat": lat, "lon": lon, "speed": sog, "course": cog},
        talker_id="AI",
        sentence_type="VDM",
        radio_channel="A",
    )


def _static5(mmsi: int, ship_type: int, seq_id: int = 0) -> list[str]:
    return encode_dict(
        {"mmsi": mmsi, "type": 5, "shipname": "SYNTH", "callsign": "SYN", "ship_type": ship_type},
        talker_id="AI",
        sentence_type="VDM",
        radio_channel="A",
        seq_id=seq_id,
    )


def _write_lines(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_aivdm_profile_reassembles_multifragment_static(tmp_path: Path) -> None:
    static = _static5(111111111, ship_type=70)  # cargo, Class A, spans 2 fragments
    assert len(static) == 2

    lines = [
        *_position_a(111111111, 10.10, -30.30, 12.0, 90.0),  # position for the same vessel
        *static,
        *_position_a(222222222, 10.20, -30.20, 4.0, 45.0),  # a second vessel, no static -> other
        *_static5(333333333, ship_type=30, seq_id=1),  # fishing static, no position
    ]
    src = _write_lines(tmp_path / "capture.log", lines)

    records = list(aivdm_source.iter_records(src))
    # The reassembled Type-5 yields exactly one static record carrying the ship type.
    static_records = [r for r in records if r.ship_type == 70]
    assert len(static_records) == 1

    profile = build_profile(aivdm_source.iter_records(src))
    RealismProfile.from_dict(profile)
    # ship_type 70 -> cargo, 30 -> fishing (from statics); position-only vessel -> other.
    assert "cargo" in profile["type_mix"]
    assert "fishing" in profile["type_mix"]
    assert sum(profile["type_mix"].values()) == pytest.approx(1.0, abs=0.01)


def test_aivdm_lone_fragment_is_skipped_never_raises(tmp_path: Path) -> None:
    static = _static5(111111111, ship_type=70)
    lines = [
        static[0],  # only the FIRST fragment of a 2-part message -> incomplete, must be skipped
        *_position_a(222222222, 10.20, -30.20, 4.0, 45.0),
    ]
    src = _write_lines(tmp_path / "capture.log", lines)
    records = list(aivdm_source.iter_records(src))
    # No record carries ship_type 70 (the static never completed); the position still comes through.
    assert all(r.ship_type != 70 for r in records)
    assert any(r.mmsi == 222222222 for r in records)


def test_aivdm_garbage_and_partial_lines_never_raise(tmp_path: Path) -> None:
    lines = [
        "",
        "not a sentence at all",
        "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,*1F",  # a non-AIS sentence
        "!AIVDM,2,1,5,A,truncated",  # partial fragment, junk payload
        "!AIVDM,9,9,,A,@@@@,0*00",  # bad fragment indices / payload
        *_position_a(111111111, 10.10, -30.30, 12.0, 90.0),  # one good line so a profile builds
    ]
    src = _write_lines(tmp_path / "capture.log", lines)
    records = list(aivdm_source.iter_records(src))  # must not raise
    assert any(r.mmsi == 111111111 for r in records)
    profile = build_profile(aivdm_source.iter_records(src))
    RealismProfile.from_dict(profile)


def test_aivdm_seeded_random_bytes_fuzz_never_raises(tmp_path: Path) -> None:
    rng = random.Random(1337)
    good = _position_a(111111111, 10.10, -30.30, 12.0, 90.0)
    lines: list[str] = list(good)
    for _ in range(300):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40)))
        lines.append(raw.decode("latin-1"))
        # Also fuzz sentences that merely LOOK like AIVDM so the fragment parser is exercised.
        fields = ",".join(str(rng.randrange(-5, 12)) for _ in range(rng.randrange(1, 8)))
        lines.append("!AIVDM," + fields)
    rng.shuffle(lines)
    src = _write_lines(tmp_path / "fuzz.log", lines)

    records = list(aivdm_source.iter_records(src))  # never raises on any line
    assert any(r.mmsi == 111111111 for r in records)


# --- format sniffing + CLI --------------------------------------------------------


def test_sniff_format_picks_aivdm(tmp_path: Path) -> None:
    src = _write_lines(tmp_path / "capture.log", _position_a(111111111, 10.1, -30.3, 12.0, 90.0))
    assert _sniff_format(src) == "aivdm"


def test_sniff_format_picks_csv(tmp_path: Path) -> None:
    src = _write_csv(tmp_path / "ais.csv")
    assert _sniff_format(src) == "csv"


def test_sniff_format_skips_leading_blank_lines(tmp_path: Path) -> None:
    src = tmp_path / "capture.log"
    body = "\n".join(_position_a(111111111, 10.1, -30.3, 12.0, 90.0))
    src.write_text("\n\n   \n" + body + "\n", encoding="utf-8")
    assert _sniff_format(src) == "aivdm"


def test_cli_auto_writes_profile_json(tmp_path: Path) -> None:
    src = _write_csv(tmp_path / "ais.csv")
    out = tmp_path / "profile.json"
    rc = main([str(src), "--out", str(out), "--format", "auto"])
    assert rc == 0
    assert out.exists()
    # The written JSON loads straight back into a RealismProfile.
    loaded = RealismProfile.from_path(out)
    assert loaded.target_count == 4


def test_cli_reports_error_on_empty_input(tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    with empty.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow(_CSV_HEADER)  # header only, no rows
    rc = main([str(empty), "--out", str(tmp_path / "p.json"), "--format", "csv"])
    assert rc == 1


def test_cli_aivdm_with_bbox_fails_loud(tmp_path: Path) -> None:
    """A bounding box is a CSV-only filter; passing it with an aivdm capture must fail loudly
    rather than being silently dropped (which would also strip static-only records)."""
    src = _write_lines(tmp_path / "capture.log", _position_a(111111111, 10.1, -30.3, 12.0, 90.0))
    out = tmp_path / "p.json"
    rc = main(
        [
            str(src),
            "--out",
            str(out),
            "--format",
            "aivdm",
            "--min-lat",
            "10.0",
            "--max-lat",
            "10.5",
            "--min-lon",
            "-30.5",
            "--max-lon",
            "-30.0",
        ]
    )
    assert rc == 2
    assert not out.exists()


def test_cli_aivdm_with_columns_fails_loud(tmp_path: Path) -> None:
    """``--columns`` is meaningless for a decoded capture; it must fail loudly, not be ignored."""
    src = _write_lines(tmp_path / "capture.log", _position_a(111111111, 10.1, -30.3, 12.0, 90.0))
    out = tmp_path / "p.json"
    rc = main([str(src), "--out", str(out), "--format", "aivdm", "--columns", "lat=Latitude"])
    assert rc == 2
    assert not out.exists()


# --- reassembly edge cases: reversed / interleaved fragments -----------------------


def test_aivdm_reversed_fragments_reassemble(tmp_path: Path) -> None:
    """A multi-part message whose fragments arrive out of order still reassembles (the
    ``_Reassembler`` orders by fragment number, not by arrival)."""
    static = _static5(111111111, ship_type=70)
    assert len(static) == 2
    src = _write_lines(tmp_path / "capture.log", [static[1], static[0]])  # 2nd fragment first
    records = list(aivdm_source.iter_records(src))
    assert any(r.ship_type == 70 for r in records)


def test_aivdm_interleaved_multipart_do_not_crosscontaminate(tmp_path: Path) -> None:
    """Two multi-part statics interleaved on the same channel (distinct seq-ids) each reassemble
    to their own vessel, never splicing a fragment of one into the other."""
    a = _static5(111111111, ship_type=70, seq_id=0)  # cargo
    b = _static5(222222222, ship_type=30, seq_id=1)  # fishing
    src = _write_lines(tmp_path / "capture.log", [a[0], b[0], a[1], b[1]])
    records = list(aivdm_source.iter_records(src))
    types = {r.mmsi: r.ship_type for r in records if r.ship_type >= 0}
    assert types[111111111] == 70
    assert types[222222222] == 30


# --- clamp bounds, MMSI sanity, legacy types, static-only fail-loud ----------------


def test_target_count_clamped_up_to_min_three(tmp_path: Path) -> None:
    rows = [(111111111, "2024-01-01T00:05:00", 10.1, -30.3, 12.0, 90.0, 90, 70, "A")]
    src = _write_csv(tmp_path / "ais.csv", rows)
    profile = build_profile(csv_source.iter_records(src))
    assert profile["target_count"] == 3  # median concurrent 1 -> clamped up to the floor of 3


def test_target_count_clamped_down_to_max_forty(tmp_path: Path) -> None:
    rows = [
        (100_000_000 + i, "2024-01-01T00:05:00", 10.1, -30.3, 12.0, 90.0, 90, 70, "A")
        for i in range(45)
    ]
    src = _write_csv(tmp_path / "many.csv", rows)
    profile = build_profile(csv_source.iter_records(src))
    assert profile["target_count"] == 40  # 45 concurrent -> clamped down to the ceiling of 40


def test_csv_zero_and_invalid_mmsi_skipped(tmp_path: Path) -> None:
    """MMSI 0 (and other non-9-digit values) merge many distinct vessels into one bucket, so
    they are filtered out."""
    rows = [
        (0, "2024-01-01T00:05:00", 10.1, -30.3, 12.0, 90.0, 90, 70, "A"),  # MMSI 0
        (42, "2024-01-01T00:06:00", 10.2, -30.2, 4.0, 45.0, 45, 30, "A"),  # too short
        (111111111, "2024-01-01T00:07:00", 10.15, -30.25, 8.0, 60.0, 60, 70, "A"),  # valid
    ]
    src = _write_csv(tmp_path / "ais.csv", rows)
    records = list(csv_source.iter_records(src))
    assert [r.mmsi for r in records] == [111111111]


def test_csv_legacy_vessel_types_are_categorised(tmp_path: Path) -> None:
    """Marine-Cadastre legacy VesselType codes 1001-1025 map onto real categories, not 'other'."""
    rows = [
        (111111111, "2024-01-01T00:05:00", 10.1, -30.3, 12.0, 90.0, 90, 1004, "A"),  # legacy cargo
        (222222222, "2024-01-01T00:06:00", 10.2, -30.2, 4.0, 45.0, 45, 1001, "A"),  # legacy fishing
        (
            333333333,
            "2024-01-01T00:07:00",
            10.15,
            -30.25,
            8.0,
            60.0,
            60,
            1005,
            "B",
        ),  # legacy tanker
    ]
    src = _write_csv(tmp_path / "ais.csv", rows)
    profile = build_profile(csv_source.iter_records(src))
    mix = profile["type_mix"]
    assert set(mix) == {"cargo", "fishing", "tanker"}  # none fell through to 'other'


def test_aivdm_static_only_stream_fails_loud(tmp_path: Path) -> None:
    """A capture with only static reports carries no position, so no region can be derived — it
    must fail loudly instead of silently collapsing the region to Null Island (0, 0)."""
    lines = [
        *_static5(111111111, ship_type=70, seq_id=0),
        *_static5(222222222, ship_type=30, seq_id=1),
    ]
    src = _write_lines(tmp_path / "capture.log", lines)
    with pytest.raises(ValueError):
        build_profile(aivdm_source.iter_records(src))
