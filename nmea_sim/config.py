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

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .state import VesselState

# JSON keys a comment/schema convention may carry without being a real config field — a key
# starting with either is tolerated (and dropped) rather than rejected as unknown.
_COMMENT_KEY_PREFIXES = ("$", "_")

# Top-level JSON keys ``EngineConfig.from_dict`` accepts. Kept explicit (not derived from the
# dataclass) because a couple of JSON keys differ from their field names (``initial_state`` ->
# ``initial_state_raw``); anything else is a typo we now reject loudly rather than silently drop.
_ENGINE_CONFIG_KEYS = frozenset(
    {
        "writer_backend",
        "movement",
        "time_source",
        "initial_state",
        "channels",
        "ais_targets",
        "tcp_tap_host",
        "mode",
        "inputs",
        "voltage_sense",
        "route",
        "replay",
        "aggregate_tap",
        "display_overrides",
        "depth_sim",
        "rudder_sim",
        "heading_sim",
        "wind_sim",
    }
)


def _reject_unknown_keys(data: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    """Raise ``ValueError`` naming any key in ``data`` outside ``allowed`` (comment keys apart).

    Fail-loud replacement for the old silent-drop: a typo'd top-level key used to be ignored on
    load and deleted on save. Keys prefixed ``$``/``_`` are treated as comments and tolerated.
    """
    unknown = sorted(
        k for k in data if k not in allowed and not k.startswith(_COMMENT_KEY_PREFIXES)
    )
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {unknown} (expected any of {sorted(allowed)})")


def _spec_from_mapping(cls: type[Any], data: dict[str, Any], where: str) -> Any:
    """Build a small dataclass from a JSON mapping, rejecting unknown keys loudly.

    ``from_dict`` used to splat ``**data`` straight into MovementSpec/TimeSourceSpec, so a typo'd
    key raised a raw ``TypeError`` that escaped ``main._load``'s ``(ValueError, KeyError)`` catch.
    Surface it as a ``ValueError`` naming the offending keys instead — the same fail-loud handling
    the top-level and every other unknown-key case now gets.
    """
    known = {f.name for f in fields(cls)}
    _reject_unknown_keys(data, frozenset(known), where)
    filtered = {k: v for k, v in data.items() if not k.startswith(_COMMENT_KEY_PREFIXES)}
    return cls(**filtered)


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
    """One emitted sentence type at a fixed rate.

    ``enabled`` is a per-sentence on/off switch applied when ``emitters_for`` expands the emit
    list: a disabled entry is skipped (frees baud budget) but stays in config so it can be
    flipped back on. Absent in JSON means ``True`` — configs written before this switch existed
    keep emitting every listed sentence exactly as before.
    """

    sentence: str
    rate_hz: float
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.rate_hz <= 0:
            raise ValueError(f"emit.rate_hz must be > 0 for {self.sentence!r}, got {self.rate_hz}")


@dataclass(frozen=True)
class RouteSpec:
    """Optional waypoint-playback seam (simulate mode only).

    When ``enabled`` is false (the default) nothing drives own-ship and behaviour is byte-identical
    to a config with no route block. When enabled, the physics thread walks own-ship through
    ``waypoints`` (ordered ``(lat, lon)`` pairs) at ``speed_kn``, steering ``cog_deg`` toward the
    active waypoint; on reaching the last it stops, or wraps when ``loop``. Requires simulate mode
    with dead-reckoning on (``movement.mode == 'underway'``) and at least two waypoints — enforced
    in :mod:`nmea_sim.validate`, not here, so a partially-built config can still be constructed.
    """

    enabled: bool = False
    waypoints: list[tuple[float, float]] = field(default_factory=list)
    speed_kn: float = 0.0
    loop: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RouteSpec:
        # JSON has no tuples: waypoints arrive as [lat, lon] lists and are normalised to tuples.
        return cls(
            enabled=bool(data.get("enabled", False)),
            waypoints=[(float(wp[0]), float(wp[1])) for wp in data.get("waypoints", [])],
            speed_kn=float(data.get("speed_kn", 0.0)),
            loop=bool(data.get("loop", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            # Emit as [lat, lon] lists so from_dict round-trips them back to identical tuples.
            "waypoints": [[lat, lon] for lat, lon in self.waypoints],
            "speed_kn": self.speed_kn,
            "loop": self.loop,
        }


@dataclass(frozen=True)
class ReplaySpec:
    """Optional record-and-replay source (active only under ``mode == 'replay'``).

    Inert unless the top-level mode is ``replay``. ``file`` names an NMEA capture the engine
    re-injects line-by-line preserving inter-line timing (scaled by ``speed``); ``loop`` restarts
    at EOF. In replay mode the generators are suppressed and the file is the source of truth. The
    ``file``-exists precondition is enforced in :mod:`nmea_sim.validate` so a missing capture fails
    at validate/start, not mid-run.
    """

    enabled: bool = False
    file: str = ""
    loop: bool = False
    speed: float = 1.0
    # Replay scope. "full" (the default, today's behaviour) treats the capture as the entire source
    # of truth — own-ship AND AIS are replayed and every generator is suppressed. "ais-only" replays
    # just the AIS contacts while own-ship is SIMULATED from config/route (the gps/heading channels
    # generate own-ship nav and physics owns own-ship position). Allowed values: "full"|"ais-only".
    scope: str = "full"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplaySpec:
        return cls(
            enabled=bool(data.get("enabled", False)),
            file=str(data.get("file", "")),
            loop=bool(data.get("loop", False)),
            speed=float(data.get("speed", 1.0)),
            scope=str(data.get("scope", "full")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "file": self.file,
            "loop": self.loop,
            "speed": self.speed,
            "scope": self.scope,
        }


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "mmsi": self.mmsi,
            "class": self.klass,
            "name": self.name,
            "call_sign": self.call_sign,
            "ship_type": self.ship_type,
            "imo": self.imo,
        }


@dataclass(frozen=True)
class AisTrafficSpec:
    """Optional synthetic-traffic seam for an AIS channel.

    When ``enabled`` is false (the default) the channel emits **own-ship only** — behaviour
    is byte-identical to a channel with no traffic block at all. When enabled, the engine
    loads a region-neutral realism profile (from ``profile_path`` if set, else a built-in
    neutral default), spawns synthetic contacts, and interleaves their position/static
    reports with own-ship's. The profile file is **user-supplied, local, and optional** — it
    carries every location-specific value, keeping them out of tracked code and config.
    """

    enabled: bool = False
    profile_path: str | None = None
    target_count: int | None = None  # override the profile's own target_count when set
    seed: int | None = None
    # Anti-teleport ceiling: caps the real elapsed time applied per target advance, so a
    # process stall can't jump a contact across the region in one step. NOT an update cadence
    # (targets advance every position build); keep it >= the AIS position emit period.
    max_advance_s: float = 10.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AisTrafficSpec:
        raw_count = data.get("target_count")
        raw_seed = data.get("seed")
        raw_path = data.get("profile_path")
        return cls(
            enabled=bool(data.get("enabled", False)),
            profile_path=str(raw_path) if raw_path is not None else None,
            target_count=int(raw_count) if raw_count is not None else None,
            seed=int(raw_seed) if raw_seed is not None else None,
            max_advance_s=float(data.get("max_advance_s", 10.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "profile_path": self.profile_path,
            "target_count": self.target_count,
            "seed": self.seed,
            "max_advance_s": self.max_advance_s,
        }


@dataclass(frozen=True)
class AisSpec:
    """AIS behaviour for an AIS channel."""

    own_ship: AisOwnShip
    mode: str = "ownship"
    channel_alternation: bool = True
    include_type5: bool = True
    type5_period_s: float = 360.0
    traffic: AisTrafficSpec | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AisSpec:
        traffic_data = data.get("traffic")
        return cls(
            own_ship=AisOwnShip.from_dict(data["own_ship"]),
            mode=str(data.get("mode", "ownship")),
            channel_alternation=bool(data.get("channel_alternation", True)),
            include_type5=bool(data.get("include_type5", True)),
            type5_period_s=float(data.get("type5_period_s", 360.0)),
            traffic=AisTrafficSpec.from_dict(traffic_data) if traffic_data else None,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "own_ship": self.own_ship.to_dict(),
            "mode": self.mode,
            "channel_alternation": self.channel_alternation,
            "include_type5": self.include_type5,
            "type5_period_s": self.type5_period_s,
        }
        # Emit "traffic" only when present, so configs that never opted into the seam
        # round-trip unchanged.
        if self.traffic is not None:
            out["traffic"] = self.traffic.to_dict()
        return out


@dataclass(frozen=True)
class TcpTapSpec:
    """Per-channel raw NMEA-over-TCP tap. Port collision checks land in the config phase."""

    enabled: bool = False
    port: int = 10110

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TcpTapSpec:
        return cls(
            enabled=bool(data.get("enabled", False)),
            port=int(data.get("port", 10110)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "port": self.port}


@dataclass(frozen=True)
class VoltageSenseSpec:
    """Optional differential line-voltage sensing for the diagnostics surface.

    A wholly opt-in hardware seam: when ``enabled`` is false (the default) nothing touches an
    ADC and the app runs identically on a box with no sensing hardware. When enabled, the
    diagnostics layer reads idle A/B line voltages through the named ADC ``driver`` so an
    INFERRED reversed-A/B verdict can be upgraded to a MEASURED one. The ADC library is
    lazy-imported at use time, so this block is inert config until something actually reads it.

    ``channels`` maps a logical slot id to its ADC-input wiring (opaque here — the driver
    interprets it), and ``divider_ratio`` scales a resistor-divider'd reading back to the true
    line voltage. This block is deliberately EXCLUDED from the persist allow-list: sensing wiring
    is a local hardware fact, not something the running app should write back into config.
    """

    enabled: bool = False
    driver: str = ""
    i2c_address: int = 0
    channels: dict[str, Any] = field(default_factory=dict)
    divider_ratio: float = 1.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VoltageSenseSpec:
        return cls(
            enabled=bool(data.get("enabled", False)),
            driver=str(data.get("driver", "")),
            i2c_address=int(data.get("i2c_address", 0)),
            channels=dict(data.get("channels", {})),
            divider_ratio=float(data.get("divider_ratio", 1.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "driver": self.driver,
            "i2c_address": self.i2c_address,
            "channels": dict(self.channels),
            "divider_ratio": self.divider_ratio,
        }


@dataclass(frozen=True)
class DisplayOverridesSpec:
    """Operator overrides for the seven display-only cosmetic instruments.

    These keys are NOT wire-backed — they ride the SSE ``sim`` frame only (no NMEA sentence).
    Each field is optional: an absent (``None``) key means "auto" for that instrument (the pure
    :func:`web.display_sim.simulate_display_instruments` value is used unchanged). ``to_dict``
    emits ONLY the set keys, so a block that overrode nothing round-trips as ``{}``.
    """

    water_temp_c: float | None = None
    air_temp_c: float | None = None
    humidity_pct: float | None = None
    pressure_hpa: float | None = None
    fuel_total_l: float | None = None
    fuel_rate_lph: float | None = None
    prop_pitch_pct: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DisplayOverridesSpec:
        def _opt(key: str) -> float | None:
            v = data.get(key)
            return float(v) if v is not None else None

        return cls(
            water_temp_c=_opt("water_temp_c"),
            air_temp_c=_opt("air_temp_c"),
            humidity_pct=_opt("humidity_pct"),
            pressure_hpa=_opt("pressure_hpa"),
            fuel_total_l=_opt("fuel_total_l"),
            fuel_rate_lph=_opt("fuel_rate_lph"),
            prop_pitch_pct=_opt("prop_pitch_pct"),
        )

    def to_dict(self) -> dict[str, Any]:
        # Emit only non-None keys so a config that overrode nothing round-trips as {} and the
        # manager can seed its live dict straight from this (dict[str, float], no None values).
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is not None:
                out[f.name] = value
        return out


@dataclass(frozen=True)
class DepthSimSpec:
    """Deterministic depth-under-keel simulation (see :mod:`nmea_sim.depthsim`).

    When ``enabled`` is false (the default) nothing drives ``depth_m`` and behaviour is
    byte-identical to a config with no depth-sim block. When enabled, the physics tick writes a
    live ``depth_m`` computed by :func:`nmea_sim.depthsim.depth_sim` from three summed sinusoids
    around ``base_depth_m``. All values are cross-checked in :mod:`nmea_sim.validate`.
    """

    enabled: bool = False
    base_depth_m: float = 50.0  # mean depth the sim oscillates around
    drift_amp_m: float = 20.0  # slow bathymetric drift amplitude
    drift_period_s: float = 1800.0  # 30 min
    shoal_amp_m: float = 15.0  # gentle shoaling/deepening runs
    shoal_period_s: float = 600.0  # 10 min
    ripple_amp_m: float = 0.6  # small swell ripple
    ripple_period_s: float = 8.0
    min_depth_m: float = 0.0  # hard floor (bounded >= 0)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DepthSimSpec:
        return cls(
            enabled=bool(data.get("enabled", False)),
            base_depth_m=float(data.get("base_depth_m", 50.0)),
            drift_amp_m=float(data.get("drift_amp_m", 20.0)),
            drift_period_s=float(data.get("drift_period_s", 1800.0)),
            shoal_amp_m=float(data.get("shoal_amp_m", 15.0)),
            shoal_period_s=float(data.get("shoal_period_s", 600.0)),
            ripple_amp_m=float(data.get("ripple_amp_m", 0.6)),
            ripple_period_s=float(data.get("ripple_period_s", 8.0)),
            min_depth_m=float(data.get("min_depth_m", 0.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "base_depth_m": self.base_depth_m,
            "drift_amp_m": self.drift_amp_m,
            "drift_period_s": self.drift_period_s,
            "shoal_amp_m": self.shoal_amp_m,
            "shoal_period_s": self.shoal_period_s,
            "ripple_amp_m": self.ripple_amp_m,
            "ripple_period_s": self.ripple_period_s,
            "min_depth_m": self.min_depth_m,
        }


@dataclass(frozen=True)
class RudderSimSpec:
    """Deterministic helm oscillation (see :mod:`nmea_sim.steeringsim`).

    ``enabled`` default is ``False`` so a directly-constructed spec is inert and the config layer
    round-trips byte-identically; default-ON in simulate mode is resolved by
    :func:`effective_rudder_sim`, NOT by this dataclass. When effective+enabled the physics tick
    writes a small ``rudder_angle_deg`` oscillation about 0 deg. Cross-checked in
    :mod:`nmea_sim.validate`.
    """

    enabled: bool = False
    amp_deg: float = 1.5
    period_s: float = 10.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RudderSimSpec:
        return cls(
            enabled=bool(data.get("enabled", False)),
            amp_deg=float(data.get("amp_deg", 1.5)),
            period_s=float(data.get("period_s", 10.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "amp_deg": self.amp_deg,
            "period_s": self.period_s,
        }


@dataclass(frozen=True)
class HeadingSimSpec:
    """Deterministic heading wander about a setpoint (see :mod:`nmea_sim.steeringsim`).

    ``enabled`` default is ``False`` so a directly-constructed spec is inert and the config layer
    round-trips byte-identically; default-ON in simulate mode is resolved by
    :func:`effective_heading_sim`, NOT by this dataclass. When effective+enabled the physics tick
    writes ``heading_true_deg``/``heading_mag_deg`` as the setpoint plus a gentle ~1 deg wander.
    Cross-checked in :mod:`nmea_sim.validate`.
    """

    enabled: bool = False
    amp_deg: float = 1.0
    period_s: float = 45.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HeadingSimSpec:
        return cls(
            enabled=bool(data.get("enabled", False)),
            amp_deg=float(data.get("amp_deg", 1.0)),
            period_s=float(data.get("period_s", 45.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "amp_deg": self.amp_deg,
            "period_s": self.period_s,
        }


@dataclass(frozen=True)
class WindSimSpec:
    """Deterministic true-wind drift (see :mod:`nmea_sim.windsim`).

    ``enabled`` default is ``False`` so a directly-constructed spec is inert and the config layer
    round-trips byte-identically; default-ON in simulate mode is resolved by
    :func:`effective_wind_sim`, NOT by this dataclass. ``base_speed_kn``/``base_dir_deg`` seed the
    drift and are supplied from the initial wind state by ``effective_wind_sim`` when the block is
    absent. When effective+enabled the physics tick drives ``wind_speed_kn``/``wind_dir_deg`` (the
    apparent wind, MWV and MWD all follow). Cross-checked in :mod:`nmea_sim.validate`.
    """

    enabled: bool = False
    base_speed_kn: float = 0.0
    base_dir_deg: float = 0.0
    gust_amp_kn: float = 2.0
    gust_period_s: float = 30.0
    veer_amp_deg: float = 8.0
    veer_period_s: float = 60.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WindSimSpec:
        return cls(
            enabled=bool(data.get("enabled", False)),
            base_speed_kn=float(data.get("base_speed_kn", 0.0)),
            base_dir_deg=float(data.get("base_dir_deg", 0.0)),
            gust_amp_kn=float(data.get("gust_amp_kn", 2.0)),
            gust_period_s=float(data.get("gust_period_s", 30.0)),
            veer_amp_deg=float(data.get("veer_amp_deg", 8.0)),
            veer_period_s=float(data.get("veer_period_s", 60.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "base_speed_kn": self.base_speed_kn,
            "base_dir_deg": self.base_dir_deg,
            "gust_amp_kn": self.gust_amp_kn,
            "gust_period_s": self.gust_period_s,
            "veer_amp_deg": self.veer_amp_deg,
            "veer_period_s": self.veer_period_s,
        }


def effective_depth_sim(cfg: EngineConfig, initial_depth_m: float) -> DepthSimSpec | None:
    """Simulate-only resolved depth-sim spec (default-ON mechanism; NOT a config-layer change).

    None outside simulate mode (protects auto RX / replay). In simulate: an explicit block is used
    as-is (tuned ``base_depth_m`` preserved); an ABSENT block becomes an enabled default whose
    ``base_depth_m`` is SEEDED from the initial depth so depth oscillates around where she started.
    """
    if cfg.mode != "simulate":
        return None
    if cfg.depth_sim is not None:
        return cfg.depth_sim
    return DepthSimSpec(enabled=True, base_depth_m=initial_depth_m)


def effective_rudder_sim(cfg: EngineConfig) -> RudderSimSpec | None:
    """Simulate-only resolved rudder-sim spec (default-ON; NOT a config-layer change).

    None outside simulate mode (protects auto RX / replay). In simulate: an explicit block is used
    as-is; an ABSENT block becomes an enabled default.
    """
    if cfg.mode != "simulate":
        return None
    if cfg.rudder_sim is not None:
        return cfg.rudder_sim
    return RudderSimSpec(enabled=True)


def effective_heading_sim(cfg: EngineConfig) -> HeadingSimSpec | None:
    """Simulate-only resolved heading-sim spec (default-ON; NOT a config-layer change).

    None outside simulate mode (protects auto RX / replay). In simulate: an explicit block is used
    as-is; an ABSENT block becomes an enabled default.
    """
    if cfg.mode != "simulate":
        return None
    if cfg.heading_sim is not None:
        return cfg.heading_sim
    return HeadingSimSpec(enabled=True)


def effective_wind_sim(
    cfg: EngineConfig, initial_speed_kn: float, initial_dir_deg: float
) -> WindSimSpec | None:
    """Simulate-only resolved wind-sim spec (default-ON; NOT a config-layer change).

    None outside simulate mode (protects auto RX / replay live wind). In simulate: an explicit
    block is used as-is; an ABSENT block becomes an enabled default whose base speed/direction are
    SEEDED from the initial wind so the drift centres on where she started.
    """
    if cfg.mode != "simulate":
        return None
    if cfg.wind_sim is not None:
        return cfg.wind_sim
    return WindSimSpec(enabled=True, base_speed_kn=initial_speed_kn, base_dir_deg=initial_dir_deg)


@dataclass(frozen=True)
class InputSpec:
    """One physical INPUT slot a channel may draw live NMEA from in ``auto`` mode.

    Inputs are a separate top-level registry (not a channel field) because a single wire can
    feed MORE than one output: a satellite compass carries heading sentences (for the heading
    output) AND GNSS position/time sentences (for the GPS output). So the model is N input
    slots, each ``ChannelSpec.sources`` naming an ordered priority list of these ids.

    This is a pure config seam — nothing here opens a port. The router/arbiter that consumes it
    lands in a later phase; until then every field is inert.
    """

    id: str
    path: str
    # What the operator says is wired here. "sat" = satellite compass (heading AND position),
    # so one sat input legitimately appears in both the heading and gps channels' sources.
    function: str = "unused"
    baud: int = 4800
    framing: str = "8N1"
    # How long without a valid sentence before this source counts as dead and the channel
    # falls back to simulating.
    liveness_timeout_s: float = 3.0
    # Deliberately much shorter than the 0.5 s the output-side serial reader uses: on an input
    # port every read blocks the passthrough path, so a tight timeout bounds passthrough
    # latency (and how fast a channel notices the source went dead) rather than TX cadence.
    read_timeout_s: float = 0.03

    def __post_init__(self) -> None:
        if self.function not in ("gps", "sat", "ais", "unused"):
            raise ValueError(f"input.function must be gps|sat|ais|unused, got {self.function!r}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InputSpec:
        return cls(
            id=str(data["id"]),
            path=str(data["path"]),
            function=str(data.get("function", "unused")),
            baud=int(data.get("baud", 4800)),
            framing=str(data.get("framing", "8N1")),
            liveness_timeout_s=float(data.get("liveness_timeout_s", 3.0)),
            read_timeout_s=float(data.get("read_timeout_s", 0.03)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "function": self.function,
            "baud": self.baud,
            "framing": self.framing,
            "liveness_timeout_s": self.liveness_timeout_s,
            "read_timeout_s": self.read_timeout_s,
        }


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
    # Startup default for the channel's runtime on/off flag. The engine owns the live
    # value once running, so this only decides whether the channel emits from the first
    # tick; an operator can flip it later without a restart and without touching config.
    enabled: bool = True
    rx_accept: list[str] = field(default_factory=list)
    # Ordered INPUT-slot ids (see ``InputSpec.id``) this channel may draw live NMEA from in
    # ``auto`` mode, highest priority first. This channel's own ``path`` stays its OUTPUT port;
    # ``sources`` names INPUT slots, never output ports. Empty (the default) means "always
    # simulate" — which is exactly what every pre-``auto`` config gets.
    sources: list[str] = field(default_factory=list)
    emit: list[EmitSpec] = field(default_factory=list)
    ais: AisSpec | None = None
    tcp_tap: TcpTapSpec | None = None
    # A tap-only channel has no backend writer: it publishes solely over ``tcp_tap`` (a software
    # feed with no serial adapter). Under the global ``serial`` backend this stops the engine from
    # opening a port it hasn't got — which would otherwise mark the channel down and flip /healthz
    # to 503. Its ``path`` is unused. Validation requires an enabled ``tcp_tap`` when this is set.
    tap_only: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChannelSpec:
        ais_data = data.get("ais")
        tap_data = data.get("tcp_tap")
        return cls(
            id=str(data["id"]),
            role=str(data["role"]),
            path=str(data["path"]),
            baud=int(data["baud"]),
            framing=str(data.get("framing", "8N1")),
            direction=str(data.get("direction", "tx")),
            talker=str(data.get("talker", "")),
            rx_feeds_state=bool(data.get("rx_feeds_state", False)),
            # Absent key means "on": configs written before per-channel toggling existed
            # must keep emitting exactly as they did.
            enabled=bool(data.get("enabled", True)),
            rx_accept=[str(x) for x in data.get("rx_accept", [])],
            # Absent key -> empty list, i.e. "always simulate".
            sources=[str(x) for x in data.get("sources", [])],
            emit=[
                EmitSpec(
                    str(e["sentence"]),
                    float(e["rate_hz"]),
                    # Absent -> True: pre-switch configs emit every listed sentence unchanged.
                    enabled=bool(e.get("enabled", True)),
                )
                for e in data.get("emit", [])
            ],
            ais=AisSpec.from_dict(ais_data) if ais_data else None,
            tcp_tap=TcpTapSpec.from_dict(tap_data) if tap_data else None,
            tap_only=bool(data.get("tap_only", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "role": self.role,
            "path": self.path,
            "baud": self.baud,
            "framing": self.framing,
            "direction": self.direction,
            "talker": self.talker,
            "rx_feeds_state": self.rx_feeds_state,
            "enabled": self.enabled,
            "rx_accept": list(self.rx_accept),
            "sources": list(self.sources),
            "emit": [
                {"sentence": e.sentence, "rate_hz": e.rate_hz, "enabled": e.enabled}
                for e in self.emit
            ],
        }
        if self.ais is not None:
            out["ais"] = self.ais.to_dict()
        if self.tcp_tap is not None:
            out["tcp_tap"] = self.tcp_tap.to_dict()
        if self.tap_only:
            out["tap_only"] = True
        return out


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
    # Host that TCP taps bind to — a LAN IP in production, never the 0.0.0.0 wildcard.
    tcp_tap_host: str = "127.0.0.1"
    # Operating mode. "simulate" (default, today's behaviour) emits synthetic sentences on
    # every channel; "auto" lets each channel pass through live NMEA from its ``sources`` and
    # fall back to simulating when the source goes dead; "replay" re-injects a recorded NMEA
    # capture (see ``replay``) as the source of truth, suppressing the generators. All new fields
    # carry defaults so every positional or partial EngineConfig(...) construction keeps working
    # unchanged.
    mode: str = "simulate"
    # Top-level INPUT-slot registry, referenced by ``ChannelSpec.sources``. Empty by default,
    # so simulate-only configs are unaffected. Inert until the router phase consumes it.
    inputs: list[InputSpec] = field(default_factory=list)
    # Optional differential line-voltage sensing seam for diagnostics. None (the default) means
    # "no sensing hardware", byte-identical to a config that never mentioned it. Excluded from
    # the persist allow-list — sensing wiring is a local hardware fact, not persisted state.
    voltage_sense: VoltageSenseSpec | None = None
    # Optional waypoint-playback seam (simulate mode only). None (the default) means "no route",
    # byte-identical to a config that never mentioned it. Cross-field rules live in ``validate``.
    route: RouteSpec | None = None
    # Optional record-and-replay source, active only under mode 'replay'. None (the default) means
    # "no replay". Its file-exists precondition is enforced eagerly in ``validate``.
    replay: ReplaySpec | None = None
    # Optional CONSOLIDATED tap: one TCP port that every channel fans out to (in addition to its
    # own writer/tap), so a single client gets the full merged NMEA stream — the classic
    # multiplexer feed. None (the default) means "no aggregate tap". Binds on ``tcp_tap_host``;
    # its port must not collide with any per-channel ``tcp_tap`` port (enforced in ``validate``).
    aggregate_tap: TcpTapSpec | None = None
    # Optional operator overrides for the six display-only cosmetic instruments (temps/fuel/etc).
    # None (the default) means "no overrides" — every instrument tracks its auto value. Applied at
    # the SSE assembly site only; never wire-backed. Round-trips byte-identically when absent.
    display_overrides: DisplayOverridesSpec | None = None
    # Optional deterministic depth-under-keel simulation. None (the default) means "no depth sim",
    # byte-identical to a config that never mentioned it. When enabled the physics tick drives the
    # wire-backed ``depth_m`` (DPT/DBT/chart track it). Cross-field rules live in ``validate``.
    depth_sim: DepthSimSpec | None = None
    # Optional deterministic helm-hold / heading-hold simulations. None (the default) means "no
    # sim block", byte-identical to a config that never mentioned it — default-ON in simulate mode
    # is resolved by effective_rudder_sim/effective_heading_sim, never by these fields. When
    # effective+enabled the physics tick drives rudder_angle_deg / heading_true_deg (+ mag).
    rudder_sim: RudderSimSpec | None = None
    heading_sim: HeadingSimSpec | None = None
    # Optional deterministic true-wind drift. None (the default) means "no wind sim", byte-identical
    # to a config that never mentioned it — default-ON in simulate mode is resolved by
    # effective_wind_sim, never by this field. When effective+enabled the physics tick drives
    # wind_speed_kn / wind_dir_deg (apparent wind, MWV and MWD all follow).
    wind_sim: WindSimSpec | None = None

    # Numeric own-ship fields expected in ``initial_state`` (utc is supplied by the engine).
    _STATE_INT_FIELDS = ("fix_quality", "satellites")

    def __post_init__(self) -> None:
        # Belt-and-braces with validate._validate_globals; construction should fail loudly on a
        # mode the engine cannot honour.
        if self.mode not in ("simulate", "auto", "replay"):
            raise ValueError(f"mode must be simulate|auto|replay, got {self.mode!r}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EngineConfig:
        _reject_unknown_keys(data, _ENGINE_CONFIG_KEYS, "config")
        return cls(
            writer_backend=str(data.get("writer_backend", "log")),
            movement=_spec_from_mapping(MovementSpec, data.get("movement", {}), "movement"),
            time_source=_spec_from_mapping(
                TimeSourceSpec, data.get("time_source", {}), "time_source"
            ),
            initial_state_raw=dict(data.get("initial_state", {})),
            channels=[ChannelSpec.from_dict(c) for c in data.get("channels", [])],
            ais_targets=[dict(t) for t in data.get("ais_targets", [])],
            tcp_tap_host=str(data.get("tcp_tap_host", "127.0.0.1")),
            # Absent -> "simulate" / empty, so configs written before this seam are unchanged.
            mode=str(data.get("mode", "simulate")),
            inputs=[InputSpec.from_dict(i) for i in data.get("inputs", [])],
            # Absent -> None (disabled), so configs written before this seam are unchanged.
            voltage_sense=(
                VoltageSenseSpec.from_dict(vs)
                if (vs := data.get("voltage_sense")) is not None
                else None
            ),
            # Absent -> None (disabled), so configs written before these seams are unchanged.
            route=(RouteSpec.from_dict(rt) if (rt := data.get("route")) is not None else None),
            replay=(ReplaySpec.from_dict(rp) if (rp := data.get("replay")) is not None else None),
            aggregate_tap=(
                TcpTapSpec.from_dict(at) if (at := data.get("aggregate_tap")) is not None else None
            ),
            # Absent -> None (no overrides / no depth sim), so configs written before these seams
            # are unchanged and round-trip byte-identically.
            display_overrides=(
                DisplayOverridesSpec.from_dict(do)
                if (do := data.get("display_overrides")) is not None
                else None
            ),
            depth_sim=(
                DepthSimSpec.from_dict(ds) if (ds := data.get("depth_sim")) is not None else None
            ),
            rudder_sim=(
                RudderSimSpec.from_dict(rs) if (rs := data.get("rudder_sim")) is not None else None
            ),
            heading_sim=(
                HeadingSimSpec.from_dict(hs)
                if (hs := data.get("heading_sim")) is not None
                else None
            ),
            wind_sim=(
                WindSimSpec.from_dict(ws) if (ws := data.get("wind_sim")) is not None else None
            ),
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
            # stw defaults to SOG; depth to 10 m; pitch/roll are overwritten by physics each tick.
            stw_kn=float(r.get("stw_kn", r.get("sog_kn", 0.0))),
            depth_m=float(r.get("depth_m", 10.0)),
            rot_dpm=float(r.get("rot_dpm", 0.0)),
            wind_speed_kn=float(r.get("wind_speed_kn", 0.0)),
            wind_dir_deg=float(r.get("wind_dir_deg", 0.0)),
            sea_state=int(r.get("sea_state", 1)),
            pitch_deg=float(r.get("pitch_deg", 0.0)),
            roll_deg=float(r.get("roll_deg", 0.0)),
            rudder_angle_deg=float(r.get("rudder_angle_deg", 0.0)),
            set_deg=float(r.get("set_deg", 0.0)),
            drift_kn=float(r.get("drift_kn", 0.0)),
        )

    def epoch_datetime(self) -> datetime | None:
        """Parse the configured simulated-clock epoch, if any (ISO 8601, assumed UTC)."""
        if not self.time_source.epoch:
            return None
        dt = datetime.fromisoformat(self.time_source.epoch)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    # -- serialisation ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Serialise back to the JSON shape ``from_dict`` accepts (round-trips ``load``)."""
        out: dict[str, Any] = {
            "writer_backend": self.writer_backend,
            "movement": {"mode": self.movement.mode, "physics_hz": self.movement.physics_hz},
            "time_source": {
                "mode": self.time_source.mode,
                "epoch": self.time_source.epoch,
                "rate": self.time_source.rate,
            },
            "initial_state": dict(self.initial_state_raw),
            "channels": [c.to_dict() for c in self.channels],
            "ais_targets": [dict(t) for t in self.ais_targets],
            "tcp_tap_host": self.tcp_tap_host,
            "mode": self.mode,
            "inputs": [i.to_dict() for i in self.inputs],
        }
        # Emit "voltage_sense" only when present, so configs that never opted into the seam
        # round-trip byte-identically.
        if self.voltage_sense is not None:
            out["voltage_sense"] = self.voltage_sense.to_dict()
        # Likewise for the route/replay seams: absent unless opted into, so a config that never
        # named them round-trips byte-identically.
        if self.route is not None:
            out["route"] = self.route.to_dict()
        if self.replay is not None:
            out["replay"] = self.replay.to_dict()
        if self.aggregate_tap is not None:
            out["aggregate_tap"] = self.aggregate_tap.to_dict()
        # Emit the display-overrides/depth-sim seams only when present, so a config that never
        # opted into either round-trips byte-identically.
        if self.display_overrides is not None:
            out["display_overrides"] = self.display_overrides.to_dict()
        if self.depth_sim is not None:
            out["depth_sim"] = self.depth_sim.to_dict()
        # Likewise the steering seams: emit only when present so an untouched config round-trips
        # byte-identically (the simulate-mode default-ON lives in the effective_* helpers, not
        # here).
        if self.rudder_sim is not None:
            out["rudder_sim"] = self.rudder_sim.to_dict()
        if self.heading_sim is not None:
            out["heading_sim"] = self.heading_sim.to_dict()
        if self.wind_sim is not None:
            out["wind_sim"] = self.wind_sim.to_dict()
        return out

    def save(self, path: str | Path) -> None:
        """Atomically write the config as JSON.

        Writes to a temp file in the destination directory and ``os.replace``-s it into place,
        so a crash mid-write can never leave a half-written config that fails to parse on the
        next boot.
        """
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # allow_nan=False: reject NaN/Infinity outright rather than emit the non-standard JSON
        # literals ``json.loads`` would happily read back, so a poisoned own-ship value cannot
        # round-trip through a saved config (H9).
        payload = json.dumps(self.to_dict(), indent=2, allow_nan=False) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            dir=str(dest.parent), prefix=f".{dest.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, dest)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty == valid). See ``validate`` module."""
        from . import validate as _validate  # local import avoids a config<-validate cycle

        return _validate.validate(self)

    def validate_or_raise(self) -> None:
        """Raise ``validate.ConfigError`` if the config fails deep cross-field validation."""
        from . import validate as _validate

        _validate.validate_or_raise(self)
