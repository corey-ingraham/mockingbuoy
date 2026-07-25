"""Diagnostics core tests: the rolling scorer, the pure fault advisor, the baud scorer, and the
click-to-decode inspector.

Everything here is fed crafted BYTES (or crafted stat dicts) and read back through the public
surface — no serial IO, no sleeps, no wall-clock (``now`` is passed in explicitly), so the whole
file is deterministic and cross-platform. ``classify_fault`` is exercised one verdict per R29 rule,
including the negative case that a mere plurality of talker ids is normal and must NOT read as a
collision. The closing fuzz loop pins the R37 contract: garbage never raises anywhere on the path,
and a fully-valid stream is never mislabelled a reversed pair.
"""

from __future__ import annotations

import random
import threading
from typing import Any

from nmea_sim.checksum import format_sentence
from nmea_sim.diagnostics import (
    PortDiagnostics,
    classify_fault,
    decode_line,
    score_baud,
)

# --- crafted-line helpers ----------------------------------------------------------

_RMC = format_sentence("GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W")
_GGA = format_sentence("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,")
_HDT = format_sentence("HEHDT,123.4,T")


def _corrupt_checksum(line: str) -> str:
    """A well-framed line whose trailing checksum no longer matches -> counts as bad_checksum."""
    return line[:-2] + "00"


def _joined(lines: list[str]) -> bytes:
    """Newline-terminate a list of lines so ``feed_bytes`` sees only complete frames (no tail)."""
    return ("\n".join(lines) + "\n").encode("latin-1")


# --- PortDiagnostics rolling counters ----------------------------------------------


def test_port_diagnostics_counts_valid_bad_and_malformed() -> None:
    """One window of a mixed stream folds into the right per-kind counters, talker set, and
    per-formatter inventory, and the derived per-second/bus-load rates match the fixed window."""
    port = PortDiagnostics("p1", 4800, window_s=10.0)
    now = 1000.0
    port.feed_bytes(
        _joined(
            [
                _RMC,
                _GGA,
                _HDT,
                _corrupt_checksum(_RMC),  # bad checksum, still GP/RMC-framed
                _corrupt_checksum(_GGA),  # bad checksum, still GP/GGA-framed
                "NODOLLAR,1,2,3",  # no $/! start -> malformed
            ]
        ),
        now,
    )
    snap = port.snapshot(now)

    assert snap["port_id"] == "p1"
    assert snap["baud"] == 4800
    assert snap["valid"] == 3
    assert snap["bad_checksum"] == 2
    assert snap["malformed"] == 1
    assert snap["lines"] == 6

    # Only well-framed lines contribute a talker/formatter (malformed line does not).
    assert set(snap["talkers"]) == {"GP", "HE"}
    assert {"RMC", "GGA", "HDT"} <= set(snap["inventory"])
    for entry in snap["inventory"].values():
        assert entry["rate_hz"] > 0.0
        assert entry["last_seen_s"] is not None

    # Rates divide by the OBSERVED span, not the full window (EFF7). Fed and snapshotted at the
    # same instant, the span floors at 1 s, so six lines read as 6/s -- an honest warm-up rate
    # rather than the ~0.6/s a full-10-s-window divisor would understate it to.
    assert snap["sentences_per_s"] == 6.0
    assert snap["bytes"] > 0
    assert snap["bytes_per_s"] > 0.0
    assert snap["bus_load_pct"] > 0.0
    assert 0.0 <= snap["printable_ratio"] <= 1.0


def test_port_diagnostics_ages_old_bytes_out_of_window() -> None:
    """Bytes fed before the window are pruned: a snapshot far in the future sees an empty window."""
    port = PortDiagnostics("p1", 4800, window_s=10.0)
    port.feed_bytes(_joined([_RMC, _GGA]), 1000.0)
    assert port.snapshot(1000.0)["valid"] == 2

    # 30 s later the earlier bucket is well past the 10 s window and has been dropped.
    later = port.snapshot(1030.0)
    assert later["bytes"] == 0
    assert later["valid"] == 0
    assert later["verdict"] == "no-data"


def test_warmup_rates_use_observed_span_not_full_window() -> None:
    """EFF7: during warm-up the rates divide by the observed span, not the full window. ~1 s of an
    ~80%-loaded 4800-baud bus must report ~80% load, not the ~8% a full-10-s divisor would give
    (which would suppress the collision rule exactly when the operator is watching)."""
    port = PortDiagnostics("p1", 4800, window_s=10.0)
    # 480 bytes/s == 100% at 4800 baud / 10 bits per char; 384 bytes in ~1 s is ~80% load.
    port.feed_bytes(b"x" * 384, 1000.0)
    snap = port.snapshot(1001.0)
    assert snap["bus_load_pct"] > 50.0  # ~80 with observed span; would be ~8 against full window


def test_proprietary_talker_is_not_fabricated() -> None:
    """DOM10: a proprietary ``$P`` sentence has no standard 2-char talker / 3-char formatter, so the
    inventory must not fabricate talker 'PG' / formatter 'RME' from ``$PGRME`` — it is talker 'P'
    with the manufacturer id as the formatter."""
    port = PortDiagnostics("p1", 4800)
    port.feed_bytes(_joined([format_sentence("PGRME,15.0,M,45.0,M,25.0,M")]), 1000.0)
    snap = port.snapshot(1000.0)
    assert snap["valid"] == 1
    assert "PG" not in snap["talkers"]
    assert "P" in snap["talkers"]
    assert "RME" not in snap["inventory"]
    assert "GRM" in snap["inventory"]


def test_concurrent_feed_and_snapshot_never_raise() -> None:
    """H7: feed_bytes (reader thread) and snapshot (web/CLI thread) run concurrently under one lock.
    Without it, snapshot iterating _buckets while a feed inserts/prunes raises 'dictionary changed
    size during iteration'. A short window forces active pruning to maximise the race."""
    port = PortDiagnostics("stress", 4800, window_s=2.0)
    blob = _joined([_RMC, _GGA, _HDT] * 4)
    errors: list[BaseException] = []

    def feeder() -> None:
        try:
            for i in range(3000):
                port.feed_bytes(blob, 1000.0 + i * 0.001)
        except (
            BaseException
        ) as exc:  # noqa: BLE001 - the whole point is to catch a race RuntimeError
            errors.append(exc)

    def snapper() -> None:
        try:
            for i in range(3000):
                port.snapshot(1000.0 + i * 0.001)
        except BaseException as exc:  # noqa: BLE001 - ditto
            errors.append(exc)

    threads = [
        threading.Thread(target=feeder),
        threading.Thread(target=snapper),
        threading.Thread(target=feeder),
        threading.Thread(target=snapper),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors, errors


# --- classify_fault: one verdict per R29 rule --------------------------------------


def test_classify_no_data_on_zero_bytes() -> None:
    verdict, advice = classify_fault({"bytes": 0})
    assert verdict == "no-data"
    assert "no bytes" in advice


def test_classify_reversed_ab_on_nonprintable_bytes() -> None:
    """Bytes present but no printable framing at all: a ranked wiring/polarity differential."""
    port = PortDiagnostics("p1", 4800)
    port.feed_bytes(bytes([0x80, 0x81, 0x82, 0x83, 0xFE, 0xFF]) * 20, 1000.0)
    snap = port.snapshot(1000.0)
    assert snap["bytes"] > 0
    assert snap["valid"] + snap["bad_checksum"] == 0
    assert snap["verdict"] == "reversed-ab"
    # It is offered as a RANKED differential, never a definitive "swap the pair" instruction.
    assert "likely reversed" in snap["advice"]


def test_classify_wrong_baud_on_printable_failing_checksums() -> None:
    """Printable framing present but the checksums (nearly) all fail -> wrong baud / line noise."""
    port = PortDiagnostics("p1", 4800)
    port.feed_bytes(_joined([_corrupt_checksum(_RMC)] * 8), 1000.0)
    snap = port.snapshot(1000.0)
    assert snap["bad_checksum"] == 8
    assert snap["valid"] == 0
    assert snap["verdict"] == "wrong-baud"
    assert "wrong baud" in snap["advice"]


def test_classify_noise_on_minority_checksum_failures() -> None:
    """A real but minority checksum-fail rate over an otherwise-valid stream reads as noise, not
    wrong-baud (which requires the checksums to MOSTLY fail)."""
    verdict, advice = classify_fault(
        {
            "bytes": 4000,
            "printable_ratio": 1.0,
            "valid": 190,
            "bad_checksum": 10,
            "malformed": 0,
            "bus_load_pct": 20.0,
            "talkers": ["GP"],
        }
    )
    assert verdict == "noise"
    assert "checksum errors" in advice


def test_classify_collision_on_saturated_bus_with_truncation() -> None:
    """Checksum/framing errors CORRELATED with a saturated bus, truncation, and more than one
    talker -> a collision diagnosis."""
    verdict, advice = classify_fault(
        {
            "bytes": 6000,
            "printable_ratio": 0.9,
            "valid": 2,
            "bad_checksum": 3,
            "malformed": 8,
            "bus_load_pct": 95.0,
            "talkers": ["GP", "AI"],
        }
    )
    assert verdict == "collision"
    assert "multiplexer" in advice


def test_multiple_talkers_alone_is_not_a_collision() -> None:
    """R29: more than one distinct talker id on a healthy bus is NORMAL and must never, on its own,
    be reported as a collision."""
    verdict, _ = classify_fault(
        {
            "bytes": 500,
            "printable_ratio": 1.0,
            "valid": 60,
            "bad_checksum": 0,
            "malformed": 0,
            "bus_load_pct": 25.0,
            "talkers": ["GP", "AI", "HE", "II"],
        }
    )
    assert verdict == "valid"


def test_classify_device_fault_when_expected_sentence_stale() -> None:
    """Clean frames but a sentence the caller expected is absent -> a device/config fault, not
    wiring. (The expected set is caller-supplied; snapshot never sets it, so the rule is opt-in.)"""
    verdict, advice = classify_fault(
        {
            "bytes": 500,
            "printable_ratio": 1.0,
            "valid": 50,
            "bad_checksum": 0,
            "malformed": 0,
            "bus_load_pct": 20.0,
            "talkers": ["GP"],
            "expected_missing": ["ZDA"],
        }
    )
    assert verdict == "device-fault"
    assert "device/config fault" in advice


def test_classify_valid_on_clean_stream() -> None:
    port = PortDiagnostics("p1", 4800)
    port.feed_bytes(_joined([_RMC, _GGA] * 10), 1000.0)
    snap = port.snapshot(1000.0)
    assert snap["valid"] == 20
    assert snap["bad_checksum"] == 0
    assert snap["verdict"] == "valid"
    assert snap["advice"] == "valid NMEA at this baud"


def test_classify_printable_non_nmea_is_not_blessed_valid() -> None:
    """M13: a 100%-printable stream that never frames a checksum-valid sentence (plain ASCII, wrong
    protocol) must NOT be reported green as 'valid NMEA at this baud'. Zero valid lines -> not-nmea,
    not healthy."""
    port = PortDiagnostics("p1", 4800)
    port.feed_bytes(
        _joined(["hello world this is not nmea", "plain ascii, no dollar frame"] * 6), 1000.0
    )
    snap = port.snapshot(1000.0)
    assert snap["valid"] == 0
    assert snap["malformed"] > 0
    assert snap["verdict"] == "not-nmea"
    assert snap["verdict"] != "valid"


def test_classify_not_nmea_on_sparse_bad_checksums() -> None:
    """A handful of bad-checksum lines (below the wrong-baud threshold) with zero valid lines is
    still not a healthy port: it falls through to not-nmea rather than the green 'valid' verdict."""
    verdict, _ = classify_fault(
        {
            "bytes": 200,
            "printable_ratio": 1.0,
            "valid": 0,
            "bad_checksum": 2,
            "malformed": 0,
            "bus_load_pct": 5.0,
            "talkers": ["GP"],
        }
    )
    assert verdict == "not-nmea"


# --- score_baud --------------------------------------------------------------------


def test_score_baud_picks_the_best_valid_ratio() -> None:
    result = score_baud(
        {
            4800: (_RMC + "\n" + _GGA + "\n").encode("latin-1"),
            9600: b"garbage-with-no-checksum\n",
        }
    )
    assert result["ratios"][4800] == 1.0
    assert result["ratios"][9600] == 0.0
    assert result["winner"] == 4800


def test_score_baud_winner_none_when_no_rate_is_printable() -> None:
    """No rate yields any checksum-valid structure -> winner None, which implicates polarity/wiring
    rather than a baud the driver should blindly pick."""
    result = score_baud({4800: bytes([0x00, 0x01, 0x02]), 9600: b""})
    assert result["winner"] is None
    assert all(ratio == 0.0 for ratio in result["ratios"].values())


def test_score_baud_ignores_trailing_partial_fragment() -> None:
    """M14: a capture cut at the dwell boundary ends mid-sentence. That trailing fragment (no
    closing newline) must not count as a failed line and drag the valid ratio below 1.0."""
    # Two whole valid sentences, then a truncated third with no terminating newline.
    raw = (_RMC + "\n" + _GGA + "\n" + _RMC[:20]).encode("latin-1")
    result = score_baud({4800: raw})
    assert result["ratios"][4800] == 1.0  # 2/2, the fragment is dropped rather than scored as bad
    assert result["winner"] == 4800


# --- decode_line -------------------------------------------------------------------


def test_decode_line_reflects_rmc_fields() -> None:
    decoded = decode_line(_RMC)
    assert decoded["sentence_type"] == "RMC"
    assert decoded["talker"] == "GP"
    assert decoded["checksum_ok"] is True
    assert "lat" in decoded["fields"]
    assert "spd_over_grnd" in decoded["fields"]


def test_decode_line_reflects_gga_fields() -> None:
    decoded = decode_line(_GGA)
    assert decoded["sentence_type"] == "GGA"
    assert decoded["talker"] == "GP"
    assert "lat" in decoded["fields"]
    assert "num_sats" in decoded["fields"] or "gps_qual" in decoded["fields"]


def test_decode_line_decodes_aivdm_fragment_via_pyais() -> None:
    decoded = decode_line("!AIVDM,1,1,,B,177KQJ5000G?tO`K>RA1wUbN0TKH,0*5C")
    assert "error" not in decoded
    assert str(decoded["sentence_type"]).startswith("AIS")
    assert decoded["talker"] == "AI"
    assert decoded["fields"]["msg_type"] == 1


def test_decode_line_degrades_proprietary_sentence() -> None:
    """A proprietary ``$P...`` has no public grammar, so it degrades to reflected raw comma fields
    rather than a parsed field map."""
    decoded = decode_line(format_sentence("PGRME,15.0,M,45.0,M,25.0,M"))
    assert decoded["proprietary"] is True
    assert decoded["raw_fields"][0] == "PGRME"
    assert "checksum_ok" in decoded


def test_decode_line_returns_error_dict_on_malformed_input() -> None:
    """Malformed input never raises; it returns an ``{error, checksum_ok}`` dict."""
    not_a_sentence = decode_line("just some random text")
    assert "error" in not_a_sentence
    assert not_a_sentence["checksum_ok"] is False

    unparseable = decode_line("$GPRMC,*ZZ")
    assert "error" in unparseable


# --- fuzz (R37): never raise, and never mislabel a valid stream --------------------


def test_fuzz_feed_and_decode_and_classify_never_raise() -> None:
    """A seeded loop of random garbage must never raise in ``feed_bytes``, ``decode_line``, or
    ``classify_fault`` — the whole diagnostics path degrades, it never crashes."""
    rng = random.Random(0xC3D1A6)
    port = PortDiagnostics("fuzz", 4800)
    now = 5000.0
    for _ in range(500):
        chunk = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 64)))
        now += rng.random()
        port.feed_bytes(chunk, now)  # must not raise
        port.snapshot(now)  # must not raise
        decode_line(chunk.decode("latin-1"))  # must not raise
        stats: dict[str, Any] = {
            "bytes": rng.randrange(-10, 10_000),
            "printable_ratio": rng.uniform(-1.0, 2.0),
            "valid": rng.randrange(-5, 500),
            "bad_checksum": rng.randrange(-5, 500),
            "malformed": rng.randrange(-5, 500),
            "bus_load_pct": rng.uniform(-10.0, 200.0),
            "talkers": ["GP", "AI"][: rng.randrange(0, 3)],
        }
        verdict, advice = classify_fault(stats)  # must not raise
        assert isinstance(verdict, str) and isinstance(advice, str)


def test_fuzz_fully_valid_stream_is_never_reversed_ab() -> None:
    """R37: however the valid frames are chunked across feeds, a fully-valid stream must never be
    flagged as a reversed A/B pair."""
    rng = random.Random(0x5EED)
    port = PortDiagnostics("valid", 4800)
    now = 2000.0
    blob = _joined([_RMC, _GGA, _HDT] * 30)
    # Deliver the same valid bytes in randomly-sized chunks to exercise the residual/partial path.
    i = 0
    while i < len(blob):
        step = rng.randrange(1, 17)
        port.feed_bytes(blob[i : i + step], now)
        i += step
    snap = port.snapshot(now)
    assert snap["verdict"] != "reversed-ab"
    assert snap["verdict"] == "valid"
