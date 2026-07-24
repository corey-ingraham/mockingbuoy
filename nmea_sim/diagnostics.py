"""Bench NMEA diagnostics: one rolling per-port validity scorer plus a pure fault advisor.

This is the analysis *core* for the maintenance surface. It is deliberately free of any
serial IO so it fuzz-tests cleanly: every observation enters through ``feed_bytes`` and
every conclusion leaves through ``snapshot``. The web/engine layer owns the wires and the
clock; it hands us raw chunks and a monotonic ``now`` and reads JSON-safe dicts back.

Why a scorer and not a parser: the router already proved (see ``classify``) that slicing the
address field is an order of magnitude cheaper than a full ``pynmea2`` parse, and on a
diagnostics path we may be staring at reversed pairs or wrong-baud garble where a parse would
just raise on every line. So the rolling counters reuse ``checksum.verify`` and a formatter
*slice*; a real parse (``decode_line``) is reserved for the single line an operator clicks.

Everything is bounded (R28): bytes are folded into per-second buckets and dropped as they age
out of the window, and the only carried-over bytes are one in-flight partial line capped at
``_MAX_LINE_BYTES``. Raw bytes are never accumulated for the life of the port.

``classify_fault`` is a pure differential advisor (R29): it ranks the likely physical fault
from the window stats and never claims certainty it does not have — "no printable structure"
is reported as a *ranked* reversed-A/B differential, not a definitive swap, and more than one
talker on the bus is treated as normal, never as a collision on its own. A fully valid stream
must never be flagged (R37).
"""

from __future__ import annotations

import contextlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .checksum import verify

# --- tunables ---------------------------------------------------------------------

# Bytes below this printable fraction with no framing at all read as a wiring/polarity fault,
# not a baud problem — printable garble is a *different* diagnosis (see classify_fault).
_PRINTABLE_MIN = 0.5
# A single in-flight partial line may never grow without bound; past this it is a runaway
# frame (no newline on the wire) and is flushed as malformed.
_MAX_LINE_BYTES = 1024
# NMEA async framing is 10 bits per character (1 start + 8 data + 1 stop, no parity).
_BITS_PER_CHAR = 10

# classify_fault thresholds — see per-rule comments.
_BUS_LOAD_HIGH = 80.0
_COLLISION_MALFORMED_MIN = 5
_MOSTLY_FAIL_RATIO = 0.5
_WRONG_BAUD_BAD_MIN = 5
_NOISE_BAD_MIN = 10
_NOISE_RATIO = 0.05

# Standard NMEA/serial baud rates the auto-baud scorer walks.
STANDARD_BAUDS: tuple[int, ...] = (4800, 9600, 19200, 38400, 57600, 115200)

_ADVICE_REVERSED = (
    "no printable structure — likely reversed A/B pair, wrong electrical standard "
    "(RS-232 vs RS-422), or open line; try swapping the data pair"
)


def _is_printable(byte: int) -> bool:
    """A byte counts as printable if it is ASCII text or ordinary NMEA whitespace (\\t\\r\\n)."""
    return 0x20 <= byte <= 0x7E or byte in (0x09, 0x0A, 0x0D)


def _is_hex2(token: str) -> bool:
    """True if ``token`` is exactly the two hex digits a ``*HH`` checksum requires."""
    return len(token) == 2 and all(c in "0123456789abcdefABCDEF" for c in token)


def _formatter_of(line: str) -> str | None:
    """Slice the formatter out of a line's address (``$GPRMC`` -> ``RMC``, ``!AIVDM`` -> ``VDM``).

    Mirrors ``classify.sentence_class``'s slicing rather than paying for a parse: the address is
    the token before the first comma, talker is 2 chars, formatter the next 3. ``None`` when the
    address is too short to carry a formatter.
    """
    address = line[1:].partition(",")[0]
    if len(address) < 5:
        return None
    return address[2:5]


def _classify_line(line: str) -> tuple[str, str | None, str | None]:
    """Bucket one decoded line as ``valid``/``bad_checksum``/``malformed`` and return its address.

    Malformed means the frame lacks the shape a checksum could even apply to: no ``$``/``!``
    start, no ``*``, or no two-hex-digit token after it (R-per-line: no ``$|!`` or no ``*HH``).
    Only well-framed lines yield a ``(talker, formatter)`` for the inventory.
    """
    if not line or line[0] not in ("$", "!"):
        return "malformed", None, None
    if "*" not in line:
        return "malformed", None, None
    _, _, tail = line[1:].partition("*")
    if not _is_hex2(tail[:2]):
        return "malformed", None, None
    talker = line[1:3]
    formatter = _formatter_of(line)
    kind = "valid" if verify(line) else "bad_checksum"
    return kind, talker, formatter


@dataclass
class _Bucket:
    """One second's worth of folded counters; the window is a bounded ring of these."""

    total_bytes: int = 0
    printable: int = 0
    lines: int = 0
    valid: int = 0
    bad_checksum: int = 0
    malformed: int = 0
    talkers: set[str] = field(default_factory=set)
    formatters: Counter[str] = field(default_factory=Counter)


class PortDiagnostics:
    """Rolling per-port validity scorer over the last ``window_s`` seconds.

    Fed raw chunks off one input port; folds them into per-second buckets so old data ages out
    for free. ``snapshot`` reduces the live window to a JSON-safe stats dict and hangs the
    advisor's verdict off it. Nothing here touches a serial port — the engine attaches
    ``feed_bytes`` as a port's ``on_raw`` hook and polls ``snapshot``.
    """

    def __init__(self, port_id: str, baud: int, window_s: float = 10.0) -> None:
        self.port_id = port_id
        self.baud = baud
        self.window_s = window_s
        self._buckets: dict[int, _Bucket] = {}
        self._last_seen: dict[str, float] = {}
        self._residual = b""

    # -- ingest ------------------------------------------------------------------

    def _bucket(self, now: float) -> _Bucket:
        key = int(now)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket()
            self._buckets[key] = bucket
        return bucket

    def _prune(self, now: float) -> None:
        """Drop buckets and last-seen marks aged past the window (keeps memory O(window))."""
        cutoff = now - self.window_s
        for key in [k for k in self._buckets if k <= cutoff]:
            del self._buckets[key]
        for name in [n for n, seen in self._last_seen.items() if seen <= cutoff]:
            del self._last_seen[name]

    def feed_bytes(self, chunk: bytes, now: float) -> None:
        """Fold one raw chunk into the current window; split completed lines and score each.

        Byte and printable counts are attributed immediately; line-level counts wait for a full
        line, so a chunk that splits a sentence carries only the tail forward (bounded). Never
        raises: garbage decodes to malformed, a newline-less runaway is flushed and dropped.
        """
        self._prune(now)
        if not chunk:
            return
        bucket = self._bucket(now)
        bucket.total_bytes += len(chunk)
        bucket.printable += sum(1 for b in chunk if _is_printable(b))

        self._residual += chunk
        *complete, self._residual = self._residual.split(b"\n")
        if len(self._residual) > _MAX_LINE_BYTES:
            # No newline in sight — treat the runaway as a malformed frame and reset the buffer.
            bucket.lines += 1
            bucket.malformed += 1
            self._residual = b""
        for raw in complete:
            self._ingest_line(raw, now, bucket)

    def _ingest_line(self, raw: bytes, now: float, bucket: _Bucket) -> None:
        text = raw.rstrip(b"\r")
        if not text:
            return
        bucket.lines += 1
        line = text.decode("latin-1")
        kind, talker, formatter = _classify_line(line)
        if kind == "malformed":
            bucket.malformed += 1
            return
        if kind == "valid":
            bucket.valid += 1
        else:
            bucket.bad_checksum += 1
        if talker:
            bucket.talkers.add(talker)
        if formatter:
            bucket.formatters[formatter] += 1
            self._last_seen[formatter] = now

    # -- read --------------------------------------------------------------------

    def snapshot(self, now: float) -> dict[str, Any]:
        """Reduce the live window to a JSON-safe stats dict with the advisor's verdict attached."""
        self._prune(now)
        buckets = self._buckets.values()
        total_bytes = sum(b.total_bytes for b in buckets)
        printable = sum(b.printable for b in buckets)
        lines = sum(b.lines for b in buckets)
        valid = sum(b.valid for b in buckets)
        bad_checksum = sum(b.bad_checksum for b in buckets)
        malformed = sum(b.malformed for b in buckets)

        talkers: set[str] = set()
        formatters: Counter[str] = Counter()
        for b in buckets:
            talkers |= b.talkers
            formatters.update(b.formatters)

        printable_ratio = printable / total_bytes if total_bytes else 0.0
        bytes_per_s = total_bytes / self.window_s
        sentences_per_s = lines / self.window_s
        bus_load_pct = (bytes_per_s * _BITS_PER_CHAR / self.baud * 100.0) if self.baud else 0.0

        inventory = {
            formatter: {
                "last_seen_s": (
                    round(now - self._last_seen[formatter], 3)
                    if formatter in self._last_seen
                    else None
                ),
                "rate_hz": round(count / self.window_s, 3),
            }
            for formatter, count in sorted(formatters.items())
        }

        stats: dict[str, Any] = {
            "port_id": self.port_id,
            "baud": self.baud,
            "bytes": total_bytes,
            "printable_ratio": round(printable_ratio, 4),
            "lines": lines,
            "valid": valid,
            "bad_checksum": bad_checksum,
            "malformed": malformed,
            "sentences_per_s": round(sentences_per_s, 3),
            "bytes_per_s": round(bytes_per_s, 3),
            "bus_load_pct": round(bus_load_pct, 3),
            "talkers": sorted(talkers),
            "inventory": inventory,
        }
        verdict, advice = classify_fault(stats)
        stats["verdict"] = verdict
        stats["advice"] = advice
        return stats


def classify_fault(stats: dict[str, Any]) -> tuple[str, str]:
    """Rank the likely physical fault from one window's stats. Pure, deterministic, never raises.

    Rules are ordered most-specific first so the ranked reversed-A/B differential (no printable
    structure) is decided before a wrong-baud (printable garble) call, per the R29 remap. A bare
    plurality of talkers is normal and never triggers a collision. Returns ``(verdict, advice)``.
    """
    total_bytes = int(stats.get("bytes", 0) or 0)
    printable_ratio = float(stats.get("printable_ratio", 0.0) or 0.0)
    valid = int(stats.get("valid", 0) or 0)
    bad = int(stats.get("bad_checksum", 0) or 0)
    malformed = int(stats.get("malformed", 0) or 0)
    bus_load = float(stats.get("bus_load_pct", 0.0) or 0.0)
    talkers = stats.get("talkers", []) or []
    structured = valid + bad  # lines well-framed enough to carry a checksum

    # 1. Port powered but silent: nothing arrived at all this window.
    if total_bytes <= 0:
        return ("no-data", "no bytes — TX/RX swapped, open line, or dead talker")

    # 2. Bytes but no printable framing: a wiring/polarity differential, not a baud problem.
    if printable_ratio < _PRINTABLE_MIN and structured == 0:
        return ("reversed-ab", _ADVICE_REVERSED)

    # 3. Collision: errors that track a saturated bus AND truncation, with more than one talker.
    #    (More than one talker on its own is normal and is deliberately not enough here.)
    if (
        bus_load >= _BUS_LOAD_HIGH
        and len(talkers) > 1
        and malformed >= _COLLISION_MALFORMED_MIN
        and (bad + malformed) > valid
    ):
        return ("collision", "multiple talkers colliding — use a multiplexer")

    # 4. Wrong baud: printable framing is present but the checksums mostly fail.
    if (
        structured > 0
        and bad >= _WRONG_BAUD_BAD_MIN
        and (valid == 0 or valid / structured < _MOSTLY_FAIL_RATIO)
    ):
        return (
            "wrong-baud",
            "printable but checksums fail — wrong baud (run the sweep) or line noise",
        )

    # 5. Noise: a real but minority checksum-fail rate over a stream that is otherwise valid.
    if bad >= _NOISE_BAD_MIN and structured > 0 and bad / structured >= _NOISE_RATIO:
        return ("noise", "checksum errors — EMI/noise, grounding, or a talker bug")

    # 6. Device fault: clean frames, but a sentence the caller expected is absent/stale. The
    #    expected set may be empty (nothing to expect) -> this rule is simply skipped.
    if valid > 0 and stats.get("expected_missing"):
        return (
            "device-fault",
            "valid data but expected sentence absent — device/config fault, not wiring",
        )

    # 7. Nothing wrong.
    return ("valid", "valid NMEA at this baud")


def score_baud(samples: dict[int, bytes]) -> dict[str, Any]:
    """Pure auto-baud scorer: per candidate baud, the checksum-valid yield of its captured bytes.

    The IO (actually retuning and reading the port) lives in the web/engine driver; here we just
    score the raw bytes it captured at each rate. If no rate yields any checksum-valid structure
    the winner is ``None`` — which implicates polarity/wiring, not baud (R29), and the driver
    should say so rather than pick a bogus "best" rate.
    """
    ratios: dict[int, float] = {}
    for baud, raw in samples.items():
        lines = [ln for ln in raw.split(b"\n") if ln.strip()]
        if not lines:
            ratios[baud] = 0.0
            continue
        valid = sum(1 for ln in lines if verify(ln.rstrip(b"\r").decode("latin-1")))
        ratios[baud] = valid / len(lines)
    winner = max(ratios, key=lambda b: ratios[b], default=None)
    if winner is not None and ratios[winner] <= 0.0:
        winner = None
    return {"ratios": ratios, "winner": winner}


def _json_safe(value: Any) -> Any:
    """Coerce a decoded value into something ``json.dumps`` accepts (enums/bytes/etc -> str)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def decode_line(line: str) -> dict[str, Any]:
    """Full-fidelity single-line inspector for click-to-decode. Never raises on bad input.

    ``$`` sentences go through ``pynmea2`` (proprietary ``$P`` degrade to reflected raw fields);
    ``!AIVDM``/``!AIVDO`` through ``pyais``. A malformed or half-fragment line returns an
    ``{error, checksum_ok}`` dict rather than raising — this is fed whatever an operator clicked.
    """
    text = (line or "").strip()
    if not text or text[0] not in ("$", "!"):
        return {"error": "not an NMEA sentence", "checksum_ok": False}

    checksum_ok = verify(text)
    try:
        if text[0] == "!":
            return _decode_ais(text, checksum_ok)
        if len(text) > 1 and text[1] == "P":
            # Proprietary $P... — no public grammar, so reflect the raw comma fields verbatim.
            body = text[1:].partition("*")[0]
            return {
                "proprietary": True,
                "raw_fields": body.split(","),
                "checksum_ok": checksum_ok,
            }
        return _decode_nmea(text, checksum_ok)
    except Exception as exc:  # noqa: BLE001 - inspector must degrade, never raise
        return {"error": str(exc), "checksum_ok": checksum_ok}


def _decode_nmea(text: str, checksum_ok: bool) -> dict[str, Any]:
    import pynmea2

    msg = pynmea2.parse(text, check=False)
    fields: dict[str, Any] = {}
    for spec in msg.fields:
        attr = spec[1]
        with contextlib.suppress(Exception):
            fields[attr] = _json_safe(getattr(msg, attr))
    return {
        "sentence_type": getattr(msg, "sentence_type", None),
        "talker": getattr(msg, "talker", None),
        "checksum_ok": checksum_ok,
        "fields": fields,
    }


def _decode_ais(text: str, checksum_ok: bool) -> dict[str, Any]:
    from pyais import decode as ais_decode

    try:
        msg = ais_decode(text)
    except Exception as exc:  # noqa: BLE001 - single fragment of a multi-part message, or junk
        return {
            "error": str(exc),
            "checksum_ok": checksum_ok,
            "note": "single line only — multi-fragment AIVDM needs the full fragment list",
        }
    data = _json_safe(msg.asdict())
    return {
        "sentence_type": f"AIS-{data.get('msg_type')}" if isinstance(data, dict) else "AIS",
        "talker": "AI",
        "checksum_ok": checksum_ok,
        "fields": data,
    }


class CaptureSession:
    """A bounded raw-line capture to a server-named file under ``data/`` (R18).

    The caller never supplies a path or filename — one is generated from the slot and a UTC
    timestamp — and the session enforces a hard per-file byte cap and a max wall-clock, auto
    stopping on either. ``data_dir`` is injected so tests point it at a tmp dir; the global
    total-``data/`` quota and max-concurrent limits are the web layer's job, not this class's.
    """

    def __init__(
        self,
        slot: str,
        data_dir: Path | str,
        start_time: float,
        max_bytes: int = 1 << 20,
        max_seconds: float = 300.0,
    ) -> None:
        self.slot = slot
        self.data_dir = Path(data_dir)
        self.start_time = start_time
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds
        self.bytes_written = 0
        self._closed = False

        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe_slot = "".join(c for c in slot if c.isalnum() or c in "-_") or "port"
        self.filename = f"capture-{safe_slot}-{ts}.log"
        self.path = self.data_dir / self.filename
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("ab")

    @property
    def active(self) -> bool:
        return not self._closed

    def write_line(self, line: bytes, now: float) -> bool:
        """Append one raw line (newline-terminated). Returns False once a bound stops it."""
        if self._closed:
            return False
        if now - self.start_time >= self.max_seconds:
            self.stop()
            return False
        payload = line if line.endswith(b"\n") else line + b"\n"
        if self.bytes_written + len(payload) > self.max_bytes:
            self.stop()
            return False
        self._handle.write(payload)
        self.bytes_written += len(payload)
        return True

    def stop(self) -> None:
        """Flush and close idempotently; safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._handle.flush()
            self._handle.close()

    def status(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "filename": self.filename,
            "bytes_written": self.bytes_written,
            "active": self.active,
        }
