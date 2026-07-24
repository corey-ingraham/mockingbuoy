"""Typed configuration model consumed by the engine.

This module parses the JSON config (see ``config.json``) into frozen dataclasses the
engine can rely on. It performs only **light** structural parsing and range checks;
the deep cross-field validation the plan calls for (duplicate device paths, talker x
sentence legality, TCP-tap port collisions, mixed-baud RX+TX, atomic save/reload) lands
in the config phase and layers on top of these same dataclasses.

Nothing here is location- or hardware-specific: a channel is defined purely by its
logical/electrical capabilities (``baud``, ``framing``, ``direction``, ``path``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .state import VesselState

# --- individual spec pieces -------------------------------------------------------


@dataclass(frozen=True)
class MovementSpec:
    """How own-ship position advances. ``static`` holds position; ``underway`` dead-reckons."""

    mode: str = "static"
    physics_hz: float = 10.0

    def __post_init__(self) -> None:
        if self.physics_hz <= 0:
            raise ValueError(f"movement.physics_hz must be > 0, got {self.physics_hz}")
        if self.mode not in ("static", "underway"):
            raise ValueError(f"movement.mode must be static|underway, got {self.mode!r}")


@dataclass(frozen=True)
class TimeSourceSpec:
    """Clock model: ``system_utc`` tracks the host clock, ``simulated`` advances an epoch
    at ``rate``, ``hold`` freezes time."""

    mode: str = "system_utc"
    epoch: str | None = None
    rate: float = 1.0

    def __post_init__(self) -> None:
        if self.mode not in ("system_utc", "simulated", "hold"):
            raise ValueError(f"time_source.mode invalid: {self.mode!r}")


@dataclass(frozen=True)
class EmitSpec:
    """One emitted sentence type at a fixed rate."""

    sentence: str
    rate_hz: float

    def __post_init__(self) -> None:
        if self.rate_hz <= 0:
            raise ValueError(f"emit.rate_hz must be > 0 for {self.sentence!r}, got {self.rate_hz}")


@dataclass(frozen=True)
class AisOwnShip:
    """Own-ship identity for AIS position/static reports."""

    mmsi: int
    klass: str = "A"  # "class" is reserved; JSON key is "class"
    name: str = ""
    call_sign: str = ""
    ship_type: int = 0
    imo: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AisOwnShip:
        return cls(
            mmsi=int(data["mmsi"]),
            klass=str(data.get("class", "A")),
            name=str(data.get("name", "")),
            call_sign=str(data.get("call_sign", "")),
            ship_type=int(data.get("ship_type", 0)),
            imo=int(data.get("imo", 0)),
        )


@dataclass(frozen=True)
class AisSpec:
    """AIS behaviour for an AIS channel."""

    own_ship: AisOwnShip
    mode: str = "ownship"
    channel_alternation: bool = True
    include_type5: bool = True
    type5_period_s: float = 360.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AisSpec:
        return cls(
            own_ship=AisOwnShip.from_dict(data["own_ship"]),
            mode=str(data.get("mode", "ownship")),
            channel_alternation=bool(data.get("channel_alternation", True)),
            include_type5=bool(data.get("include_type5", True)),
            type5_period_s=float(data.get("type5_period_s", 360.0)),
        )


@dataclass(frozen=True)
class ChannelSpec:
    """One output channel — a generic serial-capable stream, defined by capability only."""

    id: str
    role: str  # gps | heading | ais
    path: str
    baud: int
    framing: str = "8N1"
    direction: str = "tx"  # tx | rx | both
    talker: str = ""
    rx_feeds_state: bool = False
    rx_accept: list[str] = field(default_factory=list)
    emit: list[EmitSpec] = field(default_factory=list)
    ais: AisSpec | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChannelSpec:
        ais_data = data.get("ais")
        return cls(
            id=str(data["id"]),
            role=str(data["role"]),
            path=str(data["path"]),
            baud=int(data["baud"]),
            framing=str(data.get("framing", "8N1")),
            direction=str(data.get("direction", "tx")),
            talker=str(data.get("talker", "")),
            rx_feeds_state=bool(data.get("rx_feeds_state", False)),
            rx_accept=[str(x) for x in data.get("rx_accept", [])],
            emit=[EmitSpec(str(e["sentence"]), float(e["rate_hz"])) for e in data.get("emit", [])],
            ais=AisSpec.from_dict(ais_data) if ais_data else None,
        )


# --- top-level config -------------------------------------------------------------


@dataclass(frozen=True)
class EngineConfig:
    """The complete engine configuration."""

    writer_backend: str = "log"
    movement: MovementSpec = field(default_factory=MovementSpec)
    time_source: TimeSourceSpec = field(default_factory=TimeSourceSpec)
    initial_state_raw: dict[str, Any] = field(default_factory=dict)
    channels: list[ChannelSpec] = field(default_factory=list)
    ais_targets: list[dict[str, Any]] = field(default_factory=list)

    # Numeric own-ship fields expected in ``initial_state`` (utc is supplied by the engine).
    _STATE_INT_FIELDS = ("fix_quality", "satellites")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EngineConfig:
        return cls(
            writer_backend=str(data.get("writer_backend", "log")),
            movement=MovementSpec(**data.get("movement", {})),
            time_source=TimeSourceSpec(**data.get("time_source", {})),
            initial_state_raw=dict(data.get("initial_state", {})),
            channels=[ChannelSpec.from_dict(c) for c in data.get("channels", [])],
            ais_targets=[dict(t) for t in data.get("ais_targets", [])],
        )

    @classmethod
    def load(cls, path: str | Path) -> EngineConfig:
        with Path(path).open(encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def build_initial_state(self, utc: datetime) -> VesselState:
        """Construct the initial ``VesselState`` from the raw config plus a clock value."""
        r = self.initial_state_raw
        return VesselState(
            lat=float(r["lat"]),
            lon=float(r["lon"]),
            sog_kn=float(r.get("sog_kn", 0.0)),
            cog_deg=float(r.get("cog_deg", 0.0)),
            heading_true_deg=float(r.get("heading_true_deg", 0.0)),
            heading_mag_deg=float(r.get("heading_mag_deg", 0.0)),
            mag_variation_deg=float(r.get("mag_variation_deg", 0.0)),
            altitude_m=float(r.get("altitude_m", 0.0)),
            fix_quality=int(r.get("fix_quality", 1)),
            satellites=int(r.get("satellites", 0)),
            hdop=float(r.get("hdop", 1.0)),
            utc=utc,
        )

    def epoch_datetime(self) -> datetime | None:
        """Parse the configured simulated-clock epoch, if any (ISO 8601, assumed UTC)."""
        if not self.time_source.epoch:
            return None
        dt = datetime.fromisoformat(self.time_source.epoch)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
