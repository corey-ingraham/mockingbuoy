"""Parse a received NMEA line into candidate ``VesselState`` field changes.

The RX path is deliberately split from the transport (``serialport.py``): a serial or
pty reader hands verified lines here, and this pure function turns a recognised sentence
into a dict of ``VesselState`` field names → values. It performs **no** whitelisting or
state mutation itself — the caller decides whether any given field is allowed to feed the
sim (``rx_feeds_state`` + the ``rx_accept`` whitelist). Keeping it pure makes the mapping
testable without a serial port.

Only the sentences the sim itself understands are mapped; anything else yields an empty
dict (silently ignored, not an error). Checksum verification happens upstream, before a
line reaches here.

**Total on a checksum-valid line.** pynmea2 converts fields lazily, so a garbage field in
an otherwise-parseable sentence (e.g. RMC speed ``1.2.3``, a ``990013`` datestamp, a
non-numeric ZDA day, a leap-second ``235960`` time, or an unparseable coordinate) raises a
plain ``ValueError``/``TypeError``/``AttributeError`` on *access*, not at parse time. Every
per-field conversion below is wrapped so a single bad field is skipped (omitted from the
returned dict) rather than raising — a device whose job is tolerating bad wire data must
never let one malformed field kill the reader/worker thread. Non-finite (NaN/inf) numerics
are treated the same as a bad field. A structurally-unparseable line still raises
``pynmea2.ParseError`` from ``pynmea2.parse`` itself; callers on the RX path suppress it.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pynmea2

# Which sentence types contribute which VesselState fields. The extraction below mirrors
# the generators' output so a loopback (TX→RX on the same wire) round-trips cleanly. THS/HDM/
# GLL/ROT are mapped too (though the sim does not emit them) so a live receiver that speaks
# only those — e.g. a THS-only satellite compass — still seeds a failover value.
_SUPPORTED = ("RMC", "GGA", "VTG", "HDT", "HDG", "THS", "HDM", "GLL", "ROT")


def _has(value: object) -> bool:
    """True when a pynmea2 field carries a usable value (not None / empty string)."""
    return value is not None and value != ""


def _num(msg: object, attr: str, conv: Callable[[Any], float]) -> float | None:
    """Read ``attr`` off ``msg`` and convert it, returning ``None`` on any bad/absent field.

    Both the attribute access (a pynmea2 coordinate property can raise) and the conversion
    are guarded. Non-finite results (NaN/inf) are rejected so they can never poison state.
    """
    try:
        raw = getattr(msg, attr)
    except (ValueError, TypeError, AttributeError):
        return None
    if not _has(raw):
        return None
    try:
        result = conv(raw)
    except (ValueError, TypeError, AttributeError):
        return None
    if isinstance(result, float) and not math.isfinite(result):
        return None
    return result


def _put(
    out: dict[str, float], key: str, msg: object, attr: str, conv: Callable[[Any], float]
) -> None:
    """Assign ``out[key]`` from a converted field, skipping absent/bad/non-finite values."""
    value = _num(msg, attr, conv)
    if value is not None:
        out[key] = value


def _latlon(out: dict[str, float], msg: object) -> None:
    """Seed lat/lon only when both hemispheres are present and both values are finite.

    A blank hemisphere means the receiver reported no position: pynmea2 then yields
    ``latitude``/``longitude`` of ``0.0``, so a blank ``lat_dir``/``lon_dir`` is treated as
    ABSENT and never seeds a spurious (0, 0) fix.
    """
    if not (_has(getattr(msg, "lat_dir", "")) and _has(getattr(msg, "lon_dir", ""))):
        return
    lat = _num(msg, "latitude", float)
    lon = _num(msg, "longitude", float)
    if lat is not None and lon is not None:
        out["lat"] = lat
        out["lon"] = lon


def parse_line(line: str) -> dict[str, float]:
    """Map a verified NMEA line to ``VesselState`` field changes.

    Returns an empty dict for unrecognised sentences. Never raises on a checksum-valid line:
    each field is converted defensively and a bad field is simply omitted. Raises
    ``pynmea2.ParseError`` only if the line is not parseable NMEA at all — the caller counts
    that as an RX parse error.
    """
    msg = pynmea2.parse(line)
    st = getattr(msg, "sentence_type", "")
    if st not in _SUPPORTED:
        return {}

    out: dict[str, float] = {}
    if st == "RMC":
        _latlon(out, msg)
        _put(out, "sog_kn", msg, "spd_over_grnd", float)
        _put(out, "cog_deg", msg, "true_course", float)
        if _has(getattr(msg, "mag_variation", None)) and _has(getattr(msg, "mag_var_dir", None)):
            # East-positive to match _magnetic(): pynmea2 gives an unsigned magnitude plus
            # an E/W hemisphere, so West flips the sign. Seeding this lets a LIVE->SIM
            # handover resume magnetic-derived values without a jump.
            var = _num(msg, "mag_variation", float)
            if var is not None:
                out["mag_variation_deg"] = var if msg.mag_var_dir == "E" else -var
    elif st == "GGA":
        _latlon(out, msg)
        _put(out, "altitude_m", msg, "altitude", float)
        _put(out, "fix_quality", msg, "gps_qual", int)
        _put(out, "satellites", msg, "num_sats", int)
        _put(out, "hdop", msg, "horizontal_dil", float)
    elif st == "GLL":
        _latlon(out, msg)
    elif st == "VTG":
        _put(out, "cog_deg", msg, "true_track", float)
        _put(out, "sog_kn", msg, "spd_over_grnd_kts", float)
    elif st in ("HDT", "THS"):
        _put(out, "heading_true_deg", msg, "heading", float)
    elif st in ("HDG", "HDM"):
        _put(out, "heading_mag_deg", msg, "heading", float)
    elif st == "ROT":
        _put(out, "rot_dpm", msg, "rate_of_turn", float)
    return out


def parse_time(line: str) -> datetime | None:
    """Extract a tz-aware UTC ``datetime`` from a time-bearing GNSS sentence, else ``None``.

    The Time Authority unifies clock and position on a single GNSS source, and the ZDA
    carve-out synthesizes a ZDA from an RMC's *exact* time — both need the wall-clock instant
    a sentence carries, which ``parse_line`` (a state-field mapper) deliberately does not
    surface. Only RMC (datestamp + timestamp) and ZDA (day/month/year + timestamp) carry a
    full date; every other sentence, or a blank/missing/garbage date or time field, yields
    ``None`` so the caller never fabricates a time. An RMC whose status is not ``A`` (a
    void/no-fix free-running RTC) is rejected too, so it can never outrank a real time tier.
    A genuine ``pynmea2.ParseError`` propagates; callers on the RX path already suppress it.
    """
    msg = pynmea2.parse(line)
    st = getattr(msg, "sentence_type", "")
    if st == "RMC":
        # Status V = void/no-fix: the receiver's free-running RTC is not a GNSS-tier clock.
        if getattr(msg, "status", "") != "A":
            return None
        try:
            datestamp = msg.datestamp
            timestamp = msg.timestamp
            if datestamp is None or timestamp is None:
                return None
            return datetime.combine(datestamp, timestamp, tzinfo=UTC)
        except (ValueError, TypeError, AttributeError):
            return None
    if st == "ZDA":
        try:
            timestamp = msg.timestamp
            if timestamp is None or not (_has(msg.day) and _has(msg.month) and _has(msg.year)):
                return None
            date = datetime(int(msg.year), int(msg.month), int(msg.day), tzinfo=UTC).date()
            return datetime.combine(date, timestamp, tzinfo=UTC)
        except (ValueError, TypeError, AttributeError):
            return None
    return None


def accepted_changes(line: str, rx_accept: list[str]) -> dict[str, float]:
    """Parse ``line`` and keep only fields present in the ``rx_accept`` whitelist.

    This is the gate that stops a loopback or a rogue talker from rewriting state the
    operator did not opt in to. An empty whitelist accepts nothing.
    """
    if not rx_accept:
        return {}
    allow = set(rx_accept)
    return {k: v for k, v in parse_line(line).items() if k in allow}
