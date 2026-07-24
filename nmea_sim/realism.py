"""Generic, area-neutral realism knobs for synthetic AIS traffic.

The synthetic AIS generator produces contacts shaped by a **profile** — a set of
region-neutral statistics that can make traffic *resemble* any area without naming one:

* ``region`` — a lat/lon bounding box the targets stay inside.
* ``target_count`` — how many contacts to maintain.
* ``type_mix`` — weights over generic ship-type categories.
* ``speed_profiles`` — a per-category speed distribution (mean/spread, clamped).
* ``motion_model`` — ``anchored`` / ``transiting`` / ``drifting``.
* ``class_a_fraction`` — share of contacts that are Class A (vs Class B).

Public defaults describe a neutral open-water scenario and contain **no** region-specific
values. A profile may be loaded from an external JSON file (its contents are user-supplied)
— this is the seam an area profile plugs into, keeping every location value out of the code.
Nothing here reads or writes real vessel identities: MMSIs are synthetic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from typing import Any

from .navigation import dead_reckon
from .state import AisTarget

# Generic category -> representative AIS ship-type code. Categories are deliberately
# broad and area-neutral; the code is what goes on the wire (Type 5 / Type 24).
CATEGORY_SHIP_TYPE: dict[str, int] = {
    "fishing": 30,
    "passenger": 60,
    "cargo": 70,
    "tanker": 80,
    "pleasure": 37,
    "other": 0,
}

MOTION_MODELS = ("anchored", "transiting", "drifting")


@dataclass(frozen=True)
class Region:
    """A lat/lon bounding box. Neutral defaults sit on the equator/prime-meridian."""

    min_lat: float = -0.5
    max_lat: float = 0.5
    min_lon: float = -0.5
    max_lon: float = 0.5

    def contains(self, lat: float, lon: float) -> bool:
        return self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon

    def clamp(self, lat: float, lon: float) -> tuple[float, float]:
        return (
            min(max(lat, self.min_lat), self.max_lat),
            min(max(lon, self.min_lon), self.max_lon),
        )


@dataclass(frozen=True)
class SpeedProfile:
    """A clamped normal speed distribution (knots)."""

    mean_kn: float = 8.0
    std_kn: float = 3.0
    min_kn: float = 0.0
    max_kn: float = 25.0

    def sample(self, rng: Random) -> float:
        return min(max(rng.gauss(self.mean_kn, self.std_kn), self.min_kn), self.max_kn)


@dataclass(frozen=True)
class RealismProfile:
    """A complete, area-neutral traffic profile consumed by the target spawner."""

    region: Region = field(default_factory=Region)
    target_count: int = 6
    type_mix: dict[str, float] = field(
        default_factory=lambda: {"cargo": 0.4, "fishing": 0.3, "pleasure": 0.2, "other": 0.1}
    )
    speed_profiles: dict[str, SpeedProfile] = field(default_factory=dict)
    motion_model: str = "transiting"
    class_a_fraction: float = 0.5

    def __post_init__(self) -> None:
        if self.motion_model not in MOTION_MODELS:
            raise ValueError(
                f"motion_model must be one of {MOTION_MODELS}, got {self.motion_model!r}"
            )
        if not self.type_mix or any(w < 0 for w in self.type_mix.values()):
            raise ValueError("type_mix must be non-empty with non-negative weights")
        if sum(self.type_mix.values()) <= 0:
            raise ValueError("type_mix weights must sum to a positive value")

    def speed_for(self, category: str) -> SpeedProfile:
        """The speed profile for a category, or a neutral default."""
        return self.speed_profiles.get(category, SpeedProfile())

    @classmethod
    def default(cls) -> RealismProfile:
        """A neutral open-water profile with no region-specific values."""
        return cls()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RealismProfile:
        """Build a profile from a plain dict (e.g. a loaded JSON profile file)."""
        region = Region(**data["region"]) if "region" in data else Region()
        speeds = {
            category: SpeedProfile(**params)
            for category, params in data.get("speed_profiles", {}).items()
        }
        return cls(
            region=region,
            target_count=int(data.get("target_count", 6)),
            type_mix=dict(data.get("type_mix", cls().type_mix)),
            speed_profiles=speeds,
            motion_model=str(data.get("motion_model", "transiting")),
            class_a_fraction=float(data.get("class_a_fraction", 0.5)),
        )

    @classmethod
    def from_path(cls, path: str | Path) -> RealismProfile:
        """Load a profile from a JSON file. The file's *contents* are user-supplied."""
        with Path(path).open(encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


class TargetSpawner:
    """Produces and advances synthetic ``AisTarget`` contacts from a profile.

    Deterministic given a seed, so tests can assert statistical properties (targets stay
    inside ``region``, the category mix tracks ``type_mix``, speeds stay within profile
    bounds) without flaking.
    """

    def __init__(self, profile: RealismProfile, seed: int | None = None) -> None:
        self.profile = profile
        self._rng = Random(seed)

    def _pick_category(self) -> str:
        categories = list(self.profile.type_mix.keys())
        weights = list(self.profile.type_mix.values())
        return self._rng.choices(categories, weights=weights, k=1)[0]

    def _random_position(self) -> tuple[float, float]:
        r = self.profile.region
        return (
            self._rng.uniform(r.min_lat, r.max_lat),
            self._rng.uniform(r.min_lon, r.max_lon),
        )

    def spawn_one(self) -> AisTarget:
        category = self._pick_category()
        lat, lon = self._random_position()
        speed = self.profile.speed_for(category).sample(self._rng)
        cog = self._rng.uniform(0.0, 360.0)
        class_type = "A" if self._rng.random() < self.profile.class_a_fraction else "B"
        # Synthetic 9-digit MMSI; never a real registered identity.
        mmsi = self._rng.randint(200_000_000, 799_999_999)
        return AisTarget(
            mmsi=mmsi,
            lat=lat,
            lon=lon,
            sog_kn=speed,
            cog_deg=cog,
            heading_deg=int(round(cog)) % 360,
            class_type=class_type,
            ship_type=CATEGORY_SHIP_TYPE.get(category, 0),
        )

    def spawn(self, count: int | None = None) -> list[AisTarget]:
        n = self.profile.target_count if count is None else count
        return [self.spawn_one() for _ in range(n)]

    def advance(self, target: AisTarget, dt_s: float) -> AisTarget:
        """Move a target forward ``dt_s`` seconds per the profile's motion model.

        * ``anchored`` — position held.
        * ``transiting`` — dead-reckon along COG at current speed.
        * ``drifting`` — slow set/drift: dead-reckon at a small random speed.

        The result is clamped back inside ``region`` so contacts never wander out.
        """
        from dataclasses import replace

        model = self.profile.motion_model
        if model == "anchored" or target.sog_kn == 0.0:
            return target
        if model == "drifting":
            drift_kn = self._rng.uniform(0.1, 0.6)
            lat, lon = dead_reckon(target.lat, target.lon, drift_kn, target.cog_deg, dt_s)
        else:  # transiting
            lat, lon = dead_reckon(target.lat, target.lon, target.sog_kn, target.cog_deg, dt_s)
        lat, lon = self.profile.region.clamp(lat, lon)
        return replace(target, lat=lat, lon=lon)
