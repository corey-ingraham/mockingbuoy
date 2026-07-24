"""NMEA 0183 XOR checksum: compute, format, and verify sentences.

A sentence looks like ``$GPGGA,123519,...*47`` (or ``!AIVDM,...*5C`` for AIS).
The checksum is the XOR of every character *between* the start delimiter (``$`` or
``!``) and the ``*``, rendered as two upper-case hex digits.

``pynmea2``/``pyais`` compute checksums themselves; this module is the hand-rolled
fallback and — more importantly — the verifier used on the RX path.
"""

from __future__ import annotations

START_DELIMITERS = ("$", "!")


def compute(body: str) -> str:
    """XOR-checksum ``body`` (the chars between the delimiter and ``*``) as 2 hex digits."""
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"{checksum:02X}"


def format_sentence(body: str, start: str = "$") -> str:
    """Wrap a sentence ``body`` into a full delimited, checksummed sentence (no CRLF).

    ``body`` is the talker+type+fields, e.g. ``"GPGGA,123519,4807.038,N,..."``.
    The serial layer appends ``\\r\\n``; this function never adds a line ending.
    """
    if start not in START_DELIMITERS:
        raise ValueError(f"start delimiter must be one of {START_DELIMITERS}, got {start!r}")
    return f"{start}{body}*{compute(body)}"


def split(sentence: str) -> tuple[str, str]:
    """Split a full sentence into ``(body, checksum_hex)``; raise if malformed."""
    text = sentence.strip()
    if not text or text[0] not in START_DELIMITERS:
        raise ValueError(f"sentence must start with {START_DELIMITERS}: {sentence!r}")
    if "*" not in text:
        raise ValueError(f"sentence has no '*' checksum delimiter: {sentence!r}")
    body_with_star, _, checksum = text[1:].partition("*")
    if len(checksum) < 2:
        raise ValueError(f"sentence has no 2-digit checksum: {sentence!r}")
    # Only the first two chars after '*' are the checksum (some talkers append CRLF/junk).
    return body_with_star, checksum[:2]


def verify(sentence: str) -> bool:
    """Return True if ``sentence``'s trailing checksum matches its body (case-insensitive)."""
    try:
        body, checksum = split(sentence)
    except ValueError:
        return False
    return compute(body).upper() == checksum.upper()
