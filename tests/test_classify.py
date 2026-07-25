"""classify.sentence_class: talker+formatter -> coarse class, and it never raises on garbage.

The router runs this per input line, so the two properties that matter are correctness of the
class mapping and total robustness: an arbitrary byte string off a live wire must yield a valid
label (or ``None``) and must never raise, and pure garbage must never masquerade as a real class.
"""

from __future__ import annotations

import random
import string

import pytest

from nmea_sim.classify import CLASS_TO_ROLE, sentence_class

_ALLOWED = (None, "gnss", "heading", "ais")


# --- the class mapping ------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A",
        "$GNGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47",
        "$GPGLL,4916.45,N,12311.12,W,225444,A,A*49",
        "$GPVTG,054.7,T,034.4,M,005.5,N,010.2,K,A*29",
        "$GPZDA,201530.00,04,07,2002,00,00*60",
    ],
)
def test_gnss_formatters_classify_as_gnss(line: str) -> None:
    assert sentence_class(line) == "gnss"


@pytest.mark.parametrize(
    "line",
    [
        "$HEHDT,280.0,T*1F",
        "$HCHDG,283.0,0.0,E,3.0,W*11",
        "$HCHDM,283.0,M*22",
        "$HETHS,280.0,A*2B",
        "$TIROT,-15.0,A*2E",
    ],
)
def test_heading_formatters_classify_as_heading(line: str) -> None:
    assert sentence_class(line) == "heading"


@pytest.mark.parametrize(
    "line",
    [
        "!AIVDM,1,1,,A,15M6nP0P00G@J?jE`k4pW?v00<0M,0*7C",
        "!AIVDO,1,1,,,B39i>1000nTu10bp0Fh8wu5kP06,0*4A",
    ],
)
def test_ais_addresses_classify_as_ais(line: str) -> None:
    assert sentence_class(line) == "ais"


@pytest.mark.parametrize(
    "line",
    [
        "",  # empty
        "x",  # single char, no delimiter
        "hello world",  # prose
        "1234",  # digits, no delimiter
        "GPRMC,123519,A",  # missing the '$' delimiter
        "$",  # bare delimiter
        "!",  # bare AIS delimiter
        "$GP",  # too short to hold a formatter
        "$GPXXX,1,2,3",  # '$' but an unknown formatter
        "!BBVDM,1,1,,A,xxxx,0*00",  # '!' but not an AIS address
        "$GPVDM,1,1,,A,xxxx,0*00",  # AIS-looking formatter but '$' start, not '!'
        "*GPRMC,x",  # wrong delimiter
    ],
)
def test_junk_classifies_as_none(line: str) -> None:
    assert sentence_class(line) is None


# --- coupling: every class maps to a role -----------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "!ABVDM,1,1,,A,15M6nP0P00G@J?jE`k4pW?v00<0M,0*7C",  # AIS base-station talker
        "!BSVDM,1,1,,B,15M6nP0P00G@J?jE`k4pW?v00<0M,0*7C",  # AIS base talker
    ],
)
def test_non_ai_vdm_talkers_classify_as_ais(line: str) -> None:
    """DOM7: VDM/VDO from base-station talkers (AB/BS) is real AIS, not dropped."""
    assert sentence_class(line) == "ais"


def test_gns_classifies_as_gnss() -> None:
    """DOM7: GNS (multi-constellation fix) feeds the GPS channel."""
    assert sentence_class("$GNGNS,123519.00,4807.038,N,01131.000,E,AA,08,1.0,0.0,,,*4C") == "gnss"


@pytest.mark.parametrize(
    "line",
    [
        "$PGRMC,1,2,3",  # proprietary $P... must NOT be sliced to formatter 'RMC'
        "$PASHR,123519,280.0,T",  # proprietary attitude
        "$GPRMCX,1,2,3",  # over-length address must NOT slice to 'RMC'
    ],
)
def test_proprietary_and_overlength_never_gnss(line: str) -> None:
    """M2: a $P... or an over-length address must never be misclassified as gnss."""
    assert sentence_class(line) is None


def test_every_class_has_a_role() -> None:
    assert set(CLASS_TO_ROLE) == {"gnss", "heading", "ais"}
    assert CLASS_TO_ROLE == {"gnss": "gps", "heading": "heading", "ais": "ais"}


# --- fuzz: never raise, never mislabel pure garbage -------------------------------


def test_fuzz_random_bytes_never_raise_and_stay_in_domain() -> None:
    """Arbitrary strings (including ones that start with a delimiter) always return a member of
    the allowed domain and never raise — the guard-length-and-slice contract."""
    rng = random.Random(20240724)
    alphabet = string.ascii_letters + string.digits + "$!,.*- \x00\x01\xff"
    for _ in range(4000):
        length = rng.randint(0, 12)
        line = "".join(rng.choice(alphabet) for _ in range(length))
        assert sentence_class(line) in _ALLOWED


def test_fuzz_pure_garbage_never_mislabelled_as_a_real_class() -> None:
    """A line that cannot be a real address (no NMEA delimiter start) must always be ``None`` —
    a garbage line can never be mistaken for a gnss/heading/ais source."""
    rng = random.Random(1312)
    # Deliberately exclude '$' and '!' from the first position so nothing can accidentally form a
    # valid address; the result must therefore always be None.
    non_delims = string.ascii_letters + string.digits + " ,.*-@#%"
    for _ in range(4000):
        length = rng.randint(1, 16)
        line = "".join(rng.choice(non_delims) for _ in range(length))
        assert sentence_class(line) is None
