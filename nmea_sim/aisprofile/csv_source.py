"""Stream a tabular AIS export (a Marine-Cadastre-style CSV, or a directory of them).

Ports the streaming CSV reader and bounding-box filter from the local ``ingest_ais`` /
``build_profile`` tooling into the public package so a profile can be built straight from a
CSV without a separate pre-filter pass. Pure standard library, streamed row-by-row, so a
multi-million-row day fits in modest memory.

The default column names match a Marine-Cadastre-style export
(``MMSI,BaseDateTime,LAT,LON,SOG,COG,Heading,VesselType,TransceiverClass``); a
``columns`` override maps those logical names onto whatever an export actually uses. Individual
rows that cannot be parsed (bad/blank ``MMSI``/``LAT``/``LON``) are skipped — data cleanliness
is a data problem, not a crash — but a header that is *missing the required columns entirely*
is a configuration error and raises, per the fail-loud convention.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterator, Mapping
from pathlib import Path

from .records import _SHIP_TYPE_UNKNOWN, AisRecord

# Logical field -> default CSV column. ``heading`` is carried for override completeness only;
# it is not part of the statistics-only ``AisRecord``.
DEFAULT_COLUMNS: dict[str, str] = {
    "mmsi": "MMSI",
    "ts": "BaseDateTime",
    "lat": "LAT",
    "lon": "LON",
    "sog": "SOG",
    "cog": "COG",
    "heading": "Heading",
    "ship_type": "VesselType",
    "transceiver_class": "TransceiverClass",
}

# Logical fields whose column must be present in the header or the file cannot be ingested.
_REQUIRED = ("mmsi", "lat", "lon")

# A valid ship-station MMSI is nine digits. Values outside this range (notably 0, and
# coast/base-station identifiers) are not individual vessels and would merge many distinct
# contacts into one bucket, so they are skipped as unusable data.
_MIN_MMSI = 100_000_000
_MAX_MMSI = 999_999_999

# A lat/lon bounding box: ``(min_lat, max_lat, min_lon, max_lon)``.
BBox = tuple[float, float, float, float]


def _iter_csvs(target: Path) -> list[Path]:
    """One CSV, or every ``*.csv`` in a directory (sorted for a deterministic order)."""
    if target.is_dir():
        return sorted(p for p in target.glob("*.csv"))
    return [target]


def _to_float(raw: str) -> float:
    """Parse a float, or ``nan`` when the cell is blank/garbage (a "not reported" marker)."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return math.nan


def _to_ship_type(raw: str) -> int:
    """Parse an AIS ship-and-cargo-type code, or the "unknown" sentinel when unparseable."""
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return _SHIP_TYPE_UNKNOWN


def _one_file(
    src: Path,
    columns: Mapping[str, str],
    bbox: BBox | None,
) -> Iterator[AisRecord]:
    from datetime import datetime

    with src.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        header = set(reader.fieldnames or ())
        missing = [columns[f] for f in _REQUIRED if columns[f] not in header]
        if missing:
            raise ValueError(f"{src}: required column(s) not found in header: {missing}")

        for row in reader:
            mmsi_s = (row.get(columns["mmsi"]) or "").strip()
            lat = _to_float((row.get(columns["lat"]) or "").strip())
            lon = _to_float((row.get(columns["lon"]) or "").strip())
            if not mmsi_s or math.isnan(lat) or math.isnan(lon):
                continue
            try:
                mmsi = int(mmsi_s)
            except ValueError:
                continue
            if not (_MIN_MMSI <= mmsi <= _MAX_MMSI):
                continue
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                continue
            if bbox is not None:
                min_lat, max_lat, min_lon, max_lon = bbox
                if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
                    continue

            ts_col = columns.get("ts")
            ts: datetime | None = None
            if ts_col:
                raw_ts = (row.get(ts_col) or "").strip()
                if raw_ts:
                    try:
                        ts = datetime.fromisoformat(raw_ts)
                    except ValueError:
                        ts = None

            cls = (row.get(columns.get("transceiver_class", "")) or "").strip().upper()
            yield AisRecord(
                mmsi=mmsi,
                ts=ts,
                lat=lat,
                lon=lon,
                sog=_to_float((row.get(columns.get("sog", "")) or "").strip()),
                cog=_to_float((row.get(columns.get("cog", "")) or "").strip()),
                ship_type=_to_ship_type((row.get(columns.get("ship_type", "")) or "").strip()),
                transceiver_class=cls,
            )


def iter_records(
    source: Path | str,
    *,
    columns: Mapping[str, str] | None = None,
    bbox: BBox | None = None,
) -> Iterator[AisRecord]:
    """Yield :class:`AisRecord` from a CSV file or a directory of CSVs.

    ``columns`` overrides individual entries of :data:`DEFAULT_COLUMNS` (logical field -> the
    actual column name in this export); ``bbox`` is an optional ``(min_lat, max_lat, min_lon,
    max_lon)`` filter. Raises ``FileNotFoundError`` if ``source`` does not exist, or
    ``ValueError`` if a file's header lacks the required MMSI/LAT/LON columns.
    """
    root = Path(source)
    if not root.exists():
        raise FileNotFoundError(f"AIS CSV source not found: {root}")
    resolved = {**DEFAULT_COLUMNS, **(columns or {})}
    sources = _iter_csvs(root)
    if not sources:
        raise FileNotFoundError(f"no CSV files found under {root}")
    for src in sources:
        yield from _one_file(src, resolved, bbox)
