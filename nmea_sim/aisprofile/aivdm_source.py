"""Decode a captured ``!AIVDM`` / ``!AIVDO`` NMEA log into :class:`AisRecord` values.

Reuses the exact ``pyais`` decode idiom the diagnostics inspector already relies on
(``from pyais import decode`` -> ``decode(*fragments)``, imported lazily, degrading rather than
raising), and adds the multi-fragment reassembly a stream needs: a Type-5 static report spans
two sentences, so its fragments are buffered by ``(seq_id, channel)`` and decoded together once
the last fragment arrives — mirroring how :mod:`nmea_sim.ais_generator` emits them.

Handled messages:

* **Position** — Types 1/2/3 (Class A), 18/19 (Class B): yield mmsi + lat/lon/sog/cog. Type 19
  also carries a ship type, so it fills the category too. Other position types carry no ship
  type (``ship_type`` is left "unknown").
* **Static** — Types 5 (Class A), 24 (Class B): yield mmsi + ship type (no position).

Everything else is ignored. This function **never raises**: an unparseable line, an incomplete
fragment set, or outright garbage is skipped, so a truncated or noisy capture degrades cleanly.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .records import _SHIP_TYPE_UNKNOWN, AisRecord

_POSITION_TYPES = frozenset({1, 2, 3, 18, 19})
_STATIC_TYPES = frozenset({5, 24})
# Message types transmitted by a Class A transceiver; the rest are Class B.
_CLASS_A_TYPES = frozenset({1, 2, 3, 5})

# A capture with many never-completed fragment sets must not grow the buffer without bound.
_MAX_PENDING = 4096


def _iter_logs(target: Path) -> list[Path]:
    """One log file, or every ``*.log``/``*.txt``/``*.nmea`` in a directory (sorted)."""
    if target.is_dir():
        files: list[Path] = []
        for pattern in ("*.log", "*.txt", "*.nmea"):
            files.extend(target.glob(pattern))
        return sorted(set(files))
    return [target]


class _Reassembler:
    """Buffers multi-fragment AIVDM/AIVDO sentences until a message is complete.

    ``push`` returns the ordered fragment list when a message is whole (immediately for a
    single-fragment sentence), otherwise ``None``. Fragments are keyed by their sequential
    message-ID and radio channel so two interleaved multi-part messages never cross-contaminate.
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str, int], dict[int, str]] = {}

    def push(self, line: str) -> list[str] | None:
        if line[:1] != "!" or line[1:6] not in ("AIVDM", "AIVDO"):
            return None
        parts = line.split(",")
        if len(parts) < 6:
            return None
        try:
            count = int(parts[1])
            num = int(parts[2])
        except ValueError:
            return None
        if count <= 1:
            return [line]
        if not (1 <= num <= count):
            return None

        key = (parts[3], parts[4], count)
        slots = self._pending.setdefault(key, {})
        slots[num] = line
        if len(slots) < count:
            if len(self._pending) > _MAX_PENDING:
                self._pending.clear()
            return None
        del self._pending[key]
        return [slots[i] for i in range(1, count + 1)]


def _safe_decode(fragments: list[str]) -> Any | None:
    """Decode a complete fragment list via ``pyais``; ``None`` on any failure (never raises)."""
    from pyais import decode as ais_decode

    try:
        return ais_decode(*fragments)
    except Exception:  # noqa: BLE001 - a bad/partial capture must degrade, not crash
        return None


def _f(value: Any) -> float:
    """Coerce an optional numeric AIS field to float, or ``nan`` when absent."""
    if value is None:
        return math.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _ship_type(value: Any) -> int:
    """Coerce an optional ship-type field to int, or the "unknown" sentinel when absent."""
    if value is None:
        return _SHIP_TYPE_UNKNOWN
    try:
        return int(value)
    except (TypeError, ValueError):
        return _SHIP_TYPE_UNKNOWN


def _record_from_msg(msg: Any) -> AisRecord | None:
    """Map a decoded ``pyais`` message to an :class:`AisRecord`, or ``None`` if not of interest."""
    msg_type = getattr(msg, "msg_type", None)
    mmsi = getattr(msg, "mmsi", None)
    if mmsi is None or msg_type is None:
        return None
    cls = "A" if msg_type in _CLASS_A_TYPES else "B"

    if msg_type in _POSITION_TYPES:
        return AisRecord(
            mmsi=int(mmsi),
            ts=None,
            lat=_f(getattr(msg, "lat", None)),
            lon=_f(getattr(msg, "lon", None)),
            sog=_f(getattr(msg, "speed", None)),
            cog=_f(getattr(msg, "course", None)),
            ship_type=_ship_type(getattr(msg, "ship_type", None)),
            transceiver_class=cls,
        )
    if msg_type in _STATIC_TYPES:
        return AisRecord(
            mmsi=int(mmsi),
            ts=None,
            lat=math.nan,
            lon=math.nan,
            sog=math.nan,
            cog=math.nan,
            ship_type=_ship_type(getattr(msg, "ship_type", None)),
            transceiver_class=cls,
        )
    return None


def iter_records(source: Path | str) -> Iterator[AisRecord]:
    """Yield :class:`AisRecord` from a captured AIVDM/AIVDO log file or directory of logs.

    Never raises on line content: unparseable or incomplete sentences are skipped. Raises
    ``FileNotFoundError`` only if ``source`` itself does not exist.
    """
    root = Path(source)
    if not root.exists():
        raise FileNotFoundError(f"AIS capture source not found: {root}")
    reasm = _Reassembler()
    for path in _iter_logs(root):
        with path.open(encoding="latin-1", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                fragments = reasm.push(line)
                if fragments is None:
                    continue
                msg = _safe_decode(fragments)
                if msg is None:
                    continue
                record = _record_from_msg(msg)
                if record is not None:
                    yield record
