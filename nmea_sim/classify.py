"""Classify a raw NMEA line into a coarse sentence *class* without a full parse.

The AUTO router runs this on every line that arrives on an input port, so it must be
cheap and it must never raise on garbage bytes. Rather than pay for a ``pynmea2`` parse
just to route, we slice the formatter out of the address field and match it against small
sets. The address is the token between the start delimiter and the first comma: for
``$``/``!`` sentences it is a 2-char talker followed by a 3-char formatter (``$GPRMC`` →
talker ``GP``, formatter ``RMC``). AIS is the exception — its 5-char address ``AIVDM`` /
``AIVDO`` (talker ``AI``, formatter ``VDM``) is matched whole, because the whole marker is
what identifies the encapsulated payload.

Classes are intentionally coarse — ``gnss`` (position/time), ``heading``, ``ais`` — because
the router only needs to know which OUTPUT channel a line belongs to, not what it says. The
per-field meaning is decoded later, and only for the winning line, by ``rx.parse_line``.
Anything unrecognised, malformed, or empty is ``None`` so the router simply drops it.
"""

from __future__ import annotations

# Formatters that carry position/time — these feed the GPS channel.
_GNSS_FORMATTERS = frozenset({"RMC", "GGA", "GLL", "VTG", "ZDA"})
# Formatters that carry heading/rate-of-turn — these feed the heading channel.
_HEADING_FORMATTERS = frozenset({"HDT", "HDG", "HDM", "THS", "ROT"})
# AIS is matched on the whole 5-char address, not a 3-char formatter (see module docstring).
_AIS_ADDRESSES = frozenset({"AIVDM", "AIVDO"})

# The router maps a sentence class to the output channel role that consumes it.
CLASS_TO_ROLE = {"gnss": "gps", "heading": "heading", "ais": "ais"}


def sentence_class(line: str) -> str | None:
    """Return ``"gnss"``/``"heading"``/``"ais"``/``None`` from a line's address field.

    Deliberately lenient: any length guard failure, unknown formatter, or missing delimiter
    yields ``None`` instead of raising, because this is fed arbitrary bytes off a live wire.
    """
    if not line:
        return None

    start = line[0]
    if start not in ("$", "!"):
        return None

    # The address is everything up to the first comma; strip the leading delimiter. Some
    # lines may arrive without a comma yet (partial frame) — ``partition`` still gives us the
    # address token, and the length guards below reject anything too short to classify.
    address = line[1:].partition(",")[0]

    if start == "!":
        return "ais" if address in _AIS_ADDRESSES else None

    # "$" sentences: talker is 2 chars, formatter is the next 3.
    if len(address) < 5:
        return None
    formatter = address[2:5]
    if formatter in _GNSS_FORMATTERS:
        return "gnss"
    if formatter in _HEADING_FORMATTERS:
        return "heading"
    return None
