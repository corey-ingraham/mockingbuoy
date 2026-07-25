"""Aggregate a stream of :class:`AisRecord` into a statistics-only realism profile dict.

Ports the aggregation the local ``build_profile`` tooling performs — 1st/99th-percentile
bounding box, distinct-vessel type mix, per-category clamped speed stats, median concurrent
count, Class-A fraction — but consumes the source-neutral :class:`AisRecord` stream instead of
raw CSV rows, so a CSV export and a decoded NMEA capture distil identically.

**Only aggregate statistics are emitted.** No MMSI, name, call sign, or individual position ever
appears in the output; the result is a *shape*, not a re-broadcast of identifiable vessels.
Category and speed are correlated per MMSI (a static report supplies the type, a position report
supplies the motion), then the identities are dropped and only the distribution survives.

The output is validated by round-tripping through :meth:`nmea_sim.realism.RealismProfile.from_dict`
before it is returned, so a malformed aggregate fails loudly here rather than at load time.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from random import Random

from ..realism import RealismProfile
from .records import AisRecord

# Generic category buckets — these keys match nmea_sim.realism.CATEGORY_SHIP_TYPE exactly.
_CATEGORIES = ("fishing", "passenger", "cargo", "tanker", "pleasure", "other")

# AIS 102.3 kn = "not available"; anything wildly above this is noise, not a real speed.
_SOG_MAX_KN = 40.0

MOTION_MODELS = ("anchored", "transiting", "drifting")

# Marine-Cadastre legacy "VesselType" group codes (1001-1025) carry the same broad meaning as
# the standard AIS ship-and-cargo codes; without an explicit map they would ALL collapse to
# "other" and wipe out the type mix. Codes not listed (tug/tow, law/military, unknown groups)
# have no equivalent among the six area-neutral categories and stay "other".
_LEGACY_VESSEL_TYPE: dict[int, str] = {
    1001: "fishing",
    1002: "fishing",
    1003: "cargo",
    1004: "cargo",
    1005: "tanker",
    1006: "tanker",
    1012: "passenger",
    1013: "passenger",
    1014: "passenger",
    1015: "passenger",
    1016: "cargo",
    1017: "tanker",
    1018: "cargo",
    1019: "pleasure",
    1024: "tanker",
}

# Reservoir caps keep memory bounded by distinct vessels, not by row count: a daily
# Marine-Cadastre export streams through in constant memory instead of buffering every row.
_RESERVOIR_CAP = 50_000
_SPEED_CAP_PER_MMSI = 64


def _category(ship_type: int) -> str:
    """Map an AIS ship-and-cargo-type code to a broad, area-neutral category."""
    if ship_type in _LEGACY_VESSEL_TYPE:
        return _LEGACY_VESSEL_TYPE[ship_type]
    if ship_type == 30:
        return "fishing"
    if ship_type in (36, 37):
        return "pleasure"
    if 60 <= ship_type <= 69:
        return "passenger"
    if 70 <= ship_type <= 79:
        return "cargo"
    if 80 <= ship_type <= 89:
        return "tanker"
    return "other"


def _reservoir_add(sample: list[float], value: float, seen: int, cap: int, rng: Random) -> None:
    """Algorithm-R reservoir insert. ``seen`` is how many values were offered before this one.

    Below the cap every value is kept (so small inputs are exact); at the cap a uniform random
    subset is retained, bounding memory without biasing the sampled distribution.
    """
    if len(sample) < cap:
        sample.append(value)
        return
    j = rng.randint(0, seen)
    if j < cap:
        sample[j] = value


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile (q in [0, 1]) over a pre-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _speed_profile(speeds: list[float]) -> dict[str, float]:
    """Clamped normal params for a category's speed distribution."""
    if not speeds:
        return {"mean_kn": 6.0, "std_kn": 3.0, "min_kn": 0.0, "max_kn": 25.0}
    mean = statistics.fmean(speeds)
    std = statistics.pstdev(speeds) if len(speeds) > 1 else 2.0
    p99 = _percentile(sorted(speeds), 0.99)
    max_kn = max(round(max(p99, mean + 3 * std), 1), 1.0)
    return {
        "mean_kn": round(mean, 2),
        "std_kn": round(max(std, 0.5), 2),
        "min_kn": 0.0,
        "max_kn": max_kn,
    }


def _bucket_key(ts: datetime) -> str:
    """Coarse 30-minute wall-clock bucket key for a concurrent-traffic estimate."""
    half = 0 if ts.minute < 30 else 30
    return ts.replace(minute=half, second=0, microsecond=0).isoformat()


@dataclass
class _Accumulator:
    """Distinct-MMSI aggregates. Category and speed are correlated per MMSI, then discarded.

    Positions and per-vessel speeds are reservoir-sampled, so memory stays bounded by the
    number of distinct vessels rather than the number of rows — a multi-million-row daily
    export is streamed in constant memory, matching the ``csv_source`` streaming contract.
    """

    vessels: set[int] = field(default_factory=set)
    lats: list[float] = field(default_factory=list)
    lons: list[float] = field(default_factory=list)
    speeds_by_mmsi: dict[int, list[float]] = field(default_factory=lambda: defaultdict(list))
    vessel_category: dict[int, str] = field(default_factory=dict)
    vessel_class: dict[int, str] = field(default_factory=dict)
    bucket_mmsis: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    # Deterministic RNG for reservoir sampling; a fixed seed keeps the distiller reproducible.
    _rng: Random = field(default_factory=lambda: Random(0))
    _pos_count: int = 0
    _speed_count: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, rec: AisRecord) -> None:
        self.vessels.add(rec.mmsi)
        if rec.ship_type >= 0:
            self.vessel_category[rec.mmsi] = _category(rec.ship_type)  # last-seen wins
        cls = rec.transceiver_class.strip().upper()
        if cls in ("A", "B"):
            self.vessel_class[rec.mmsi] = cls
        if (
            math.isfinite(rec.lat)
            and math.isfinite(rec.lon)
            and -90.0 <= rec.lat <= 90.0
            and -180.0 <= rec.lon <= 180.0
        ):
            _reservoir_add(self.lats, rec.lat, self._pos_count, _RESERVOIR_CAP, self._rng)
            _reservoir_add(self.lons, rec.lon, self._pos_count, _RESERVOIR_CAP, self._rng)
            self._pos_count += 1
        if math.isfinite(rec.sog) and 0.0 <= rec.sog <= _SOG_MAX_KN:
            seen = self._speed_count[rec.mmsi]
            _reservoir_add(
                self.speeds_by_mmsi[rec.mmsi], rec.sog, seen, _SPEED_CAP_PER_MMSI, self._rng
            )
            self._speed_count[rec.mmsi] += 1
        if rec.ts is not None:
            self.bucket_mmsis[_bucket_key(rec.ts)].add(rec.mmsi)


def _aggregate(acc: _Accumulator, motion_model: str) -> dict[str, object]:
    if not acc.vessels:
        raise ValueError("no usable AIS records to build a profile from")
    if not acc.lats or not acc.lons:
        raise ValueError(
            "no usable position reports: cannot derive a region "
            "(a static-only stream would silently collapse to Null Island)"
        )

    lat_sorted = sorted(acc.lats)
    lon_sorted = sorted(acc.lons)
    # 1st/99th percentile bbox trims stray/erroneous fixes without hand-tuning.
    region = {
        "min_lat": round(_percentile(lat_sorted, 0.01), 4),
        "max_lat": round(_percentile(lat_sorted, 0.99), 4),
        "min_lon": round(_percentile(lon_sorted, 0.01), 4),
        "max_lon": round(_percentile(lon_sorted, 0.99), 4),
    }

    # type_mix weighted by distinct vessels (unknown-type vessels fall to "other"), sum 1.0.
    cat_counts: dict[str, int] = defaultdict(int)
    for mmsi in acc.vessels:
        cat_counts[acc.vessel_category.get(mmsi, "other")] += 1
    total_vessels = sum(cat_counts.values()) or 1
    type_mix = {
        cat: round(cat_counts[cat] / total_vessels, 4) for cat in _CATEGORIES if cat_counts.get(cat)
    }

    # speeds grouped by each vessel's category (correlated per MMSI across the two report kinds).
    speeds_by_cat: dict[str, list[float]] = defaultdict(list)
    for mmsi, speeds in acc.speeds_by_mmsi.items():
        speeds_by_cat[acc.vessel_category.get(mmsi, "other")].extend(speeds)
    speed_profiles = {cat: _speed_profile(speeds_by_cat.get(cat, [])) for cat in type_mix}

    # concurrent-traffic estimate: median distinct vessels per 30-min bucket, clamped sane.
    per_bucket = [len(s) for s in acc.bucket_mmsis.values()]
    median_concurrent = int(statistics.median(per_bucket)) if per_bucket else 6
    target_count = max(3, min(median_concurrent, 40))

    classed = [c for c in acc.vessel_class.values() if c in ("A", "B")]
    class_a_fraction = (
        round(sum(1 for c in classed if c == "A") / len(classed), 4) if classed else 0.5
    )

    return {
        "region": region,
        "target_count": target_count,
        "type_mix": type_mix,
        "speed_profiles": speed_profiles,
        "motion_model": motion_model,
        "class_a_fraction": class_a_fraction,
    }


def build_profile(
    records: Iterable[AisRecord],
    *,
    motion_model: str = "transiting",
) -> dict[str, object]:
    """Distil an iterable of :class:`AisRecord` into a validated realism profile dict.

    Raises ``ValueError`` if ``motion_model`` is not one of :data:`MOTION_MODELS`, if the stream
    yields no usable records, or if the resulting aggregate fails
    :meth:`nmea_sim.realism.RealismProfile.from_dict` validation.
    """
    if motion_model not in MOTION_MODELS:
        raise ValueError(f"motion_model must be one of {MOTION_MODELS}, got {motion_model!r}")

    acc = _Accumulator()
    for rec in records:
        acc.add(rec)
    profile = _aggregate(acc, motion_model)

    # Round-trip through the canonical schema: fail loudly here, not at load time.
    RealismProfile.from_dict(profile)
    return profile
