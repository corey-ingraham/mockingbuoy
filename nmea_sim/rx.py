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
"""

from __future__ import annotations

from datetime import UTC, datetime

import pynmea2

# Which sentence types contribute which VesselState fields. The extraction below mirrors
# the generators' output so a loopback (TX→RX on the same wire) round-trips cleanly.
_SUPPORTED = ("RMC", "GGA", "VTG", "HDT", "HDG")


def _has(value: object) -> bool:
    """True when a pynmea2 field carries a usable value (not None / empty string)."""
    return value is not None and value != ""


def parse_line(line: str) -> dict[str, float]:
    """Map a verified NMEA line to ``VesselState`` field changes.

    Returns an empty dict for unrecognised sentences. Raises ``pynmea2.ParseError`` if the
    line is not parseable NMEA — the caller counts that as an RX parse error.
    """
    msg = pynmea2.parse(line)
    st = getattr(msg, "sentence_type", "")
    if st not in _SUPPORTED:
        return {}

    out: dict[str, float] = {}
    if st == "RMC":
        if _has(msg.lat) and _has(msg.lon):
            out["lat"] = float(msg.latitude)
            out["lon"] = float(msg.longitude)
        if _has(msg.spd_over_grnd):
            out["sog_kn"] = float(msg.spd_over_grnd)
        if _has(msg.true_course):
            out["cog_deg"] = float(msg.true_course)
        if _has(msg.mag_variation) and _has(msg.mag_var_dir):
            # East-positive to match _magnetic(): pynmea2 gives an unsigned magnitude plus
            # an E/W hemisphere, so West flips the sign. Seeding this lets a LIVE->SIM
            # handover resume magnetic-derived values without a jump.
            var = float(msg.mag_variation)
            out["mag_variation_deg"] = var if msg.mag_var_dir == "E" else -var
    elif st == "GGA":
        if _has(msg.lat) and _has(msg.lon):
            out["lat"] = float(msg.latitude)
            out["lon"] = float(msg.longitude)
        if _has(msg.altitude):
            out["altitude_m"] = float(msg.altitude)
        if _has(msg.gps_qual):
            out["fix_quality"] = int(msg.gps_qual)
        if _has(msg.num_sats):
            out["satellites"] = int(msg.num_sats)
        if _has(msg.horizontal_dil):
            out["hdop"] = float(msg.horizontal_dil)
    elif st == "VTG":
        if _has(msg.true_track):
            out["cog_deg"] = float(msg.true_track)
        if _has(msg.spd_over_grnd_kts):
            out["sog_kn"] = float(msg.spd_over_grnd_kts)
    elif st == "HDT":
        if _has(msg.heading):
            out["heading_true_deg"] = float(msg.heading)
    elif st == "HDG":
        if _has(msg.heading):
            out["heading_mag_deg"] = float(msg.heading)
    return out


def parse_time(line: str) -> datetime | None:
    """Extract a tz-aware UTC ``datetime`` from a time-bearing GNSS sentence, else ``None``.

    The Time Authority unifies clock and position on a single GNSS source, and the ZDA
    carve-out synthesizes a ZDA from an RMC's *exact* time — both need the wall-clock instant
    a sentence carries, which ``parse_line`` (a state-field mapper) deliberately does not
    surface. Only RMC (datestamp + timestamp) and ZDA (day/month/year + timestamp) carry a
    full date; every other sentence, or a blank/missing date or time field, yields ``None`` so
    the caller never fabricates a time. A genuine ``pynmea2.ParseError`` propagates; callers on
    the RX path already suppress it.
    """
    msg = pynmea2.parse(line)
    st = getattr(msg, "sentence_type", "")
    if st == "RMC":
        datestamp = getattr(msg, "datestamp", None)
        timestamp = getattr(msg, "timestamp", None)
        if datestamp is None or timestamp is None:
            return None
        return datetime.combine(datestamp, timestamp, tzinfo=UTC)
    if st == "ZDA":
        timestamp = getattr(msg, "timestamp", None)
        if timestamp is None or not (_has(msg.day) and _has(msg.month) and _has(msg.year)):
            return None
        date = datetime(int(msg.year), int(msg.month), int(msg.day), tzinfo=UTC).date()
        return datetime.combine(date, timestamp, tzinfo=UTC)
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
