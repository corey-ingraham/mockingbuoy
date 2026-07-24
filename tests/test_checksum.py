"""Checksum: known-answer, format round-trip, and verification."""

from __future__ import annotations

import pytest

from nmea_sim import checksum


def test_known_answer_gga() -> None:
    # The canonical NMEA example sentence has checksum 0x47.
    body = "GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
    assert checksum.compute(body) == "47"


def test_compute_empty_body_is_zero() -> None:
    assert checksum.compute("") == "00"


def test_format_sentence_roundtrips_through_verify() -> None:
    sentence = checksum.format_sentence("GPGGA,123519,4807.038,N")
    assert sentence.startswith("$GPGGA")
    assert sentence.endswith("*" + checksum.compute("GPGGA,123519,4807.038,N"))
    assert checksum.verify(sentence)


def test_format_sentence_ais_bang_delimiter() -> None:
    sentence = checksum.format_sentence("AIVDM,1,1,,A,15M,0", start="!")
    assert sentence.startswith("!AIVDM")
    assert checksum.verify(sentence)


def test_format_sentence_rejects_bad_delimiter() -> None:
    with pytest.raises(ValueError):
        checksum.format_sentence("GPGGA", start="#")


def test_verify_detects_corruption() -> None:
    good = checksum.format_sentence("GPGGA,123519,4807.038,N")
    corrupted = good.replace("4807.038", "4807.039")
    assert not checksum.verify(corrupted)


def test_verify_is_case_insensitive() -> None:
    body = "GPRMC,123519,A,4807.038,N"
    lower = f"${body}*{checksum.compute(body).lower()}"
    assert checksum.verify(lower)


@pytest.mark.parametrize("junk", ["", "no-star-here", "$missingchecksum*", "plaintext*4"])
def test_verify_rejects_malformed(junk: str) -> None:
    assert not checksum.verify(junk)


def test_verify_tolerates_trailing_crlf() -> None:
    sentence = checksum.format_sentence("GPGGA,123519") + "\r\n"
    assert checksum.verify(sentence)
