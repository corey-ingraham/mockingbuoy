"""Deep cross-field validation for an :class:`~nmea_sim.config.EngineConfig`.

The config dataclasses do only *local* structural checks (a bad ``physics_hz``, an unknown
``mode``). This module does the **cross-field** validation the engine cannot express in a
single dataclass: a channel emitting a sentence its role can't build, two channels fighting
over the same device or TCP-tap port, an over-budget wire, an out-of-range starting fix.

It is intentionally **pure and side-effect free**: :func:`validate` returns a list of
human-readable problem strings (empty means valid), so callers can print them (headless
``--validate-only``), reject a bad save, or surface them in the web UI without catching
exceptions. :func:`validate_or_raise` is the throwing convenience wrapper.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from . import budget
from .gps_generator import SUPPORTED as _GPS_SENTENCES
from .heading_generator import SUPPORTED as _HEADING_SENTENCES
from .instrument_generator import SUPPORTED as _INSTRUMENT_SENTENCES

if TYPE_CHECKING:
    from .config import ChannelSpec, EngineConfig, InputSpec

# Sentence names each role's generator can actually build. AIS is modelled in config as a
# single ``AIVDM`` emit entry (own-ship reports go out as !AIVDO on the wire).
_AIS_SENTENCES = ("AIVDM", "AIVDO")
_ROLE_SENTENCES: dict[str, tuple[str, ...]] = {
    "gps": _GPS_SENTENCES,
    "heading": _HEADING_SENTENCES,
    "instrument": _INSTRUMENT_SENTENCES,
    "ais": _AIS_SENTENCES,
}
_VALID_ROLES = tuple(_ROLE_SENTENCES)
_VALID_DIRECTIONS = ("tx", "rx", "both")

# Writer backends the engine can construct (see ``Engine._make_backend_writer``). A backend
# outside this set has no sink and would fail at engine build, so reject it at validate time.
_VALID_WRITER_BACKENDS = ("log", "null", "pty", "serial")

# Roles arbitrated by the AUTO-mode router / TimeAuthority. At most one channel may own each: the
# router maps role->channel and (defensively) keeps the last, while other consumers keep the
# first, so two channels sharing a role make them disagree on the winner. Instrument channels
# consume no live class and are not arbitrated, so duplicates of them are harmless and allowed.
_ARBITRATED_ROLES = ("gps", "heading", "ais")

# AIS own-ship identity bounds. Values outside these are silently corrupted by pyais on the wire
# (MMSI wraps at 30 bits, ship_type truncates to 0, over-long name/call_sign are clipped).
_MAX_MMSI = 999_999_999
_MAX_SHIP_TYPE = 99
_MAX_AIS_NAME = 20
_MAX_AIS_CALLSIGN = 7
# AIS 6-bit ASCII character set (ITU-R M.1371): the 64 characters payload text is armored into.
# name/call_sign characters outside it are mangled on the wire, so reject them at validate time.
_AIS_SIXBIT_CHARS = frozenset("@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?")

# Operating modes the engine can honour. "simulate" emits synthetic sentences; "auto" passes
# through live NMEA and falls back to simulating; "replay" re-injects a recorded capture. A mode
# outside this set is rejected loudly here rather than silently degrading to simulate.
_VALID_MODES = ("simulate", "auto", "replay")

# What an operator can declare an input slot is wired to. "sat" = satellite compass, which
# carries both heading and GNSS position/time, so it can feed more than one output channel.
_VALID_FUNCTIONS = ("gps", "sat", "ais", "unused")

# VesselState fields an RX channel is allowed to feed back into shared state.
_STATE_FIELDS = frozenset(
    {
        "lat",
        "lon",
        "sog_kn",
        "cog_deg",
        "heading_true_deg",
        "heading_mag_deg",
        "mag_variation_deg",
        "altitude_m",
        "fix_quality",
        "satellites",
        "hdop",
        "stw_kn",
        "depth_m",
        "rot_dpm",
        "wind_speed_kn",
        "wind_dir_deg",
        "sea_state",
        "pitch_deg",
        "roll_deg",
        "rudder_angle_deg",
        "set_deg",
        "drift_kn",
    }
)

# A path containing any of these is an unset placeholder — not a real device, so it is
# excluded from duplicate-path and same-device baud-conflict checks.
_PLACEHOLDER_MARKERS = ("change-me", "none", "placeholder")

# Numeric ranges for the initial fix. ``None`` bound = unbounded on that side.
_STATE_RANGES: dict[str, tuple[float | None, float | None]] = {
    "lat": (-90.0, 90.0),
    "lon": (-180.0, 180.0),
    "sog_kn": (0.0, None),
    "cog_deg": (0.0, 360.0),
    "heading_true_deg": (0.0, 360.0),
    "heading_mag_deg": (0.0, 360.0),
    "mag_variation_deg": (-180.0, 180.0),
    "fix_quality": (0.0, None),
    "satellites": (0.0, None),
    "hdop": (0.0, None),
    "stw_kn": (0.0, 100.0),
    "depth_m": (0.0, 12000.0),
    "rot_dpm": (-720.0, 720.0),
    "wind_speed_kn": (0.0, 200.0),
    "wind_dir_deg": (0.0, 360.0),
    "sea_state": (0.0, 9.0),
    "pitch_deg": (-90.0, 90.0),
    "roll_deg": (-90.0, 90.0),
    "rudder_angle_deg": (-45.0, 45.0),
    "set_deg": (0.0, 360.0),
    "drift_kn": (0.0, 100.0),
}

_MIN_PORT = 1
_MAX_PORT = 65535


class ConfigError(ValueError):
    """Raised by :func:`validate_or_raise` when a config fails deep validation."""


def _is_placeholder(path: str) -> bool:
    p = path.strip().lower()
    return p == "" or any(marker in p for marker in _PLACEHOLDER_MARKERS)


def _is_valid_framing(framing: str) -> bool:
    """True iff ``framing`` is a buildable serial framing string (e.g. ``8N1``).

    Mirrors the strict parser the engine uses at build time (``serialport._serial_kwargs``):
    5-8 data bits, N/E/O parity, 1-2 stop bits. The looser budget parser accepts ``9N1``/``8N3``
    and then either raises a raw ``ValueError`` out of ``validate()`` (``8X1``) or crashes at
    engine build, so validation rejects every unbuildable framing here instead.
    """
    f = framing.strip().upper()
    return len(f) == 3 and f[0] in "5678" and f[1] in "NEO" and f[2] in "12"


def _budget_samples(spec: ChannelSpec) -> list[tuple[float, list[str]]]:
    """Build representative NMEA lines for each emission so the wire load can be estimated.

    Imports the engine lazily to avoid a config<-engine import cycle. A build failure for a
    sample (e.g. an unbuildable sentence) is left to the sentence-legality check and simply
    omitted from the load estimate.
    """
    from datetime import UTC, datetime

    from .engine import build_source, emitters_for  # local: engine imports config
    from .state import VesselState  # local: only needed to fabricate a sample state

    sample = VesselState(
        lat=0.0,
        lon=0.0,
        sog_kn=10.0,
        cog_deg=90.0,
        heading_true_deg=90.0,
        heading_mag_deg=90.0,
        mag_variation_deg=0.0,
        altitude_m=0.0,
        fix_quality=1,
        satellites=10,
        hdop=1.0,
        utc=datetime(2024, 1, 1, tzinfo=UTC),
        sea_state=2,
        depth_m=10.0,
    )
    try:
        source = build_source(spec)
    except Exception:
        return []
    samples: list[tuple[float, list[str]]] = []
    for em in emitters_for(spec):
        try:
            lines = source.build(em.sentence, sample)
        except Exception:
            continue
        if em.period > 0:
            samples.append((1.0 / em.period, lines))
    return samples


def _validate_channel(spec: ChannelSpec, errors: list[str]) -> None:
    where = f"channel {spec.id!r}"

    if spec.role not in _VALID_ROLES:
        errors.append(f"{where}: unknown role {spec.role!r} (expected {'|'.join(_VALID_ROLES)})")
    if spec.direction not in _VALID_DIRECTIONS:
        errors.append(
            f"{where}: direction {spec.direction!r} invalid "
            f"(expected {'|'.join(_VALID_DIRECTIONS)})"
        )
    if spec.baud <= 0:
        errors.append(f"{where}: baud must be > 0, got {spec.baud}")

    # Framing must be buildable, or the engine crashes at start; reject it loudly (M5). Checked
    # before the budget estimate below, which would otherwise raise a raw ValueError on bad framing.
    if not _is_valid_framing(spec.framing):
        errors.append(
            f"{where}: framing {spec.framing!r} is not buildable "
            "(expected e.g. 8N1: 5-8 data bits, N/E/O parity, 1-2 stop bits)"
        )

    # Talker is required for the roles whose sentences carry one (AIS uses !AI* directly).
    if spec.role in ("gps", "heading") and not spec.talker:
        errors.append(f"{where}: role {spec.role!r} requires a 'talker' (e.g. GP, HE)")

    # Sentence x role legality: every emitted sentence must be one the role can build.
    legal = _ROLE_SENTENCES.get(spec.role, ())
    for em in spec.emit:
        if em.sentence not in legal:
            errors.append(
                f"{where}: role {spec.role!r} cannot emit {em.sentence!r} "
                f"(supported: {', '.join(legal) or 'none'})"
            )

    # A transmitting channel needs something to transmit; a pure-RX channel must not. "Something"
    # means at least one ENABLED emit entry — an all-disabled list schedules no emitters and its
    # worker crashes on an empty min() at start (H2), so it is rejected the same as an empty list.
    if spec.direction in ("tx", "both") and not spec.emit:
        errors.append(f"{where}: direction {spec.direction!r} but no 'emit' entries")
    elif spec.direction in ("tx", "both") and not any(em.enabled for em in spec.emit):
        errors.append(
            f"{where}: direction {spec.direction!r} but every 'emit' entry is disabled "
            "(a transmitting channel needs at least one enabled sentence to schedule)"
        )
    if spec.direction == "rx" and spec.emit:
        errors.append(f"{where}: direction 'rx' cannot have 'emit' entries")

    # RX feed whitelist must name real VesselState fields.
    if spec.rx_feeds_state and not spec.rx_accept:
        errors.append(f"{where}: rx_feeds_state is true but rx_accept is empty (accepts nothing)")
    for fieldname in spec.rx_accept:
        if fieldname not in _STATE_FIELDS:
            errors.append(f"{where}: rx_accept field {fieldname!r} is not a VesselState field")

    if spec.role == "ais" and spec.ais is None:
        errors.append(f"{where}: role 'ais' requires an 'ais' block")

    _validate_ais_identity(spec, errors)
    _validate_ais_traffic(spec, errors)

    if spec.tcp_tap is not None and spec.tcp_tap.enabled:
        port = spec.tcp_tap.port
        if not (_MIN_PORT <= port <= _MAX_PORT):
            errors.append(f"{where}: tcp_tap.port {port} out of range {_MIN_PORT}-{_MAX_PORT}")

    # Baud budget: does the offered load fit the wire? Skipped on unbuildable framing (reported
    # above), which would otherwise raise a raw ValueError out of budget.evaluate.
    if _is_valid_framing(spec.framing):
        samples = _budget_samples(spec)
        if samples:
            result = budget.evaluate(spec.baud, spec.framing, samples)
            if result.over:
                errors.append(
                    f"{where}: over baud budget — {result.utilization * 100:.0f}% of "
                    f"{spec.baud} {spec.framing} (limit {result.threshold * 100:.0f}%)"
                )


def _validate_ais_identity(spec: ChannelSpec, errors: list[str]) -> None:
    """Reject AIS own-ship identity values pyais would silently corrupt on the wire (H8).

    The config dataclass coerces these to int/str with no range or charset check, so an
    out-of-range MMSI wraps (30-bit), an oversized ship_type truncates to 0, and a name or
    call_sign outside the AIS 6-bit ASCII set is mangled by the payload armor — all from a config
    that would otherwise validate clean. Also rejects a non-positive Type-5 period, which either
    divides by zero (0) or floods the wire (<0) after validation (M6).
    """
    if spec.ais is None:
        return
    where = f"channel {spec.id!r}"
    own = spec.ais.own_ship

    if not (0 < own.mmsi <= _MAX_MMSI):
        errors.append(
            f"{where}: ais.own_ship.mmsi {own.mmsi} out of range "
            f"(expected 1-{_MAX_MMSI}, a 9-digit MMSI)"
        )
    if not (0 <= own.ship_type <= _MAX_SHIP_TYPE):
        errors.append(
            f"{where}: ais.own_ship.ship_type {own.ship_type} out of range "
            f"(expected 0-{_MAX_SHIP_TYPE})"
        )
    if len(own.name) > _MAX_AIS_NAME:
        errors.append(
            f"{where}: ais.own_ship.name {own.name!r} is too long "
            f"({len(own.name)} chars, AIS allows at most {_MAX_AIS_NAME})"
        )
    if len(own.call_sign) > _MAX_AIS_CALLSIGN:
        errors.append(
            f"{where}: ais.own_ship.call_sign {own.call_sign!r} is too long "
            f"({len(own.call_sign)} chars, AIS allows at most {_MAX_AIS_CALLSIGN})"
        )
    for label, text in (("name", own.name), ("call_sign", own.call_sign)):
        bad = sorted({ch for ch in text if ch not in _AIS_SIXBIT_CHARS})
        if bad:
            errors.append(
                f"{where}: ais.own_ship.{label} {text!r} has character(s) {bad} outside the "
                "AIS 6-bit ASCII set (upper-case A-Z, 0-9, space and @[\\]^_!\"#$%&'()*+,-./:;<=>?)"
            )

    if spec.ais.type5_period_s <= 0:
        errors.append(f"{where}: ais.type5_period_s must be > 0, got {spec.ais.type5_period_s}")


def _validate_ais_traffic(spec: ChannelSpec, errors: list[str]) -> None:
    """Sanity-check an AIS channel's optional synthetic-traffic seam.

    Leaving ``profile_path`` null is fine — the neutral built-in default profile is used. But a
    *set* ``profile_path`` must exist and load: the engine loads it eagerly at construction
    (``RealismProfile.from_path``), so a missing or malformed file would crash start-up. We fail
    loudly here instead, matching the ``fail loudly`` rule — a set-but-missing path is a hard error.
    """
    if spec.ais is None or spec.ais.traffic is None:
        return
    traffic = spec.ais.traffic
    if not traffic.enabled:
        return
    where = f"channel {spec.id!r}"
    path = traffic.profile_path
    if path is None:
        return  # no path => the neutral default profile is used; nothing to check

    from pathlib import Path

    from .realism import RealismProfile

    if not Path(path).exists():
        errors.append(
            f"{where}: ais.traffic.profile_path {path!r} does not exist "
            "(set it to a profile present on this host, or null to use the neutral default)"
        )
        return
    try:
        RealismProfile.from_path(path)
    except Exception as exc:  # surface a malformed/unreadable profile as a readable problem
        errors.append(
            f"{where}: ais.traffic.profile_path {path!r} is not a loadable profile ({exc})"
        )


def _validate_input(spec: InputSpec, errors: list[str]) -> None:
    """Local structural checks for one input slot (belt-and-braces alongside the dataclass guard).

    Bounds mirror the channel-baud idiom (``baud > 0``); the two timeouts must be strictly
    positive because a zero/negative liveness window would never let a source count as dead, and a
    non-positive read timeout is not a valid poll bound.
    """
    where = f"input {spec.id!r}"

    if spec.function not in _VALID_FUNCTIONS:
        errors.append(
            f"{where}: unknown function {spec.function!r} (expected {'|'.join(_VALID_FUNCTIONS)})"
        )
    if spec.baud <= 0:
        errors.append(f"{where}: baud must be > 0, got {spec.baud}")
    if not _is_valid_framing(spec.framing):
        errors.append(
            f"{where}: framing {spec.framing!r} is not buildable "
            "(expected e.g. 8N1: 5-8 data bits, N/E/O parity, 1-2 stop bits)"
        )
    if spec.liveness_timeout_s <= 0:
        errors.append(f"{where}: liveness_timeout_s must be > 0, got {spec.liveness_timeout_s}")
    if spec.read_timeout_s <= 0:
        errors.append(f"{where}: read_timeout_s must be > 0, got {spec.read_timeout_s}")


def _validate_sources(config: EngineConfig, errors: list[str]) -> None:
    """Cross-check each channel's ``sources`` against the top-level input registry.

    A source names an input slot the channel may draw from, so every id must resolve to a defined
    ``inputs[].id``. Separately, in auto mode a channel that both lists ``sources`` and still has
    the older ``rx_feeds_state`` set would have two subsystems writing shared vessel state from the
    same wire and racing — the ``inputs`` registry supersedes that path, so it is a hard error.
    """
    defined = {inp.id for inp in config.inputs}
    auto = config.mode == "auto"

    for spec in config.channels:
        for sid in spec.sources:
            if sid not in defined:
                errors.append(
                    f"channel {spec.id!r}: source {sid!r} does not match any inputs[].id "
                    "(define an input with that id, or remove it from this channel's sources)"
                )
        if auto and spec.sources and spec.rx_feeds_state:
            errors.append(
                f"channel {spec.id!r}: cannot set both 'sources' and rx_feeds_state=true in auto "
                "mode — both would feed shared vessel state from the same wire and race; the "
                "top-level 'inputs' registry supersedes the per-channel rx_feeds_state path, so "
                "drop rx_feeds_state on this channel"
            )


def _validate_cross_channel(config: EngineConfig, errors: list[str]) -> None:
    ids: dict[str, int] = {}
    # Device paths share ONE namespace across outputs and inputs: an output port and an input port
    # are both physical ttys, so no two may name the same device. Value is (kind, id) of the first
    # claimant so a collision can name what it clashed with.
    paths: dict[str, tuple[str, str]] = {}  # real device path -> (kind, id) that first claimed it
    tap_ports: dict[int, str] = {}  # tap port -> channel id

    for spec in config.channels:
        ids[spec.id] = ids.get(spec.id, 0) + 1

        if not _is_placeholder(spec.path):
            key = spec.path.strip()
            if key in paths:
                kind, owner = paths[key]
                if kind == "channel":
                    errors.append(
                        f"channel {spec.id!r}: device path {spec.path!r} already used by "
                        f"channel {owner!r} (each channel needs its own port)"
                    )
                else:
                    errors.append(
                        f"channel {spec.id!r}: device path {spec.path!r} already used by "
                        f"input {owner!r} (an output and an input may not share a device path)"
                    )
            else:
                paths[key] = ("channel", spec.id)

        if spec.tcp_tap is not None and spec.tcp_tap.enabled:
            port = spec.tcp_tap.port
            if port in tap_ports:
                errors.append(
                    f"channel {spec.id!r}: tcp_tap.port {port} collides with "
                    f"channel {tap_ports[port]!r}"
                )
            else:
                tap_ports[port] = spec.id

    # Inputs join the same path namespace (checked after channels so a clash is reported against
    # the channel that claimed the path first) and get their own duplicate-id check.
    input_ids: dict[str, int] = {}
    for inp in config.inputs:
        input_ids[inp.id] = input_ids.get(inp.id, 0) + 1

        if not _is_placeholder(inp.path):
            key = inp.path.strip()
            if key in paths:
                kind, owner = paths[key]
                errors.append(
                    f"input {inp.id!r}: device path {inp.path!r} already used by "
                    f"{kind} {owner!r} (each port needs its own device path)"
                )
            else:
                paths[key] = ("input", inp.id)

    for cid, count in ids.items():
        if count > 1:
            errors.append(f"duplicate channel id {cid!r} appears {count} times")

    for iid, count in input_ids.items():
        if count > 1:
            errors.append(f"duplicate input id {iid!r} appears {count} times")

    # M1: at most one channel may own each arbitrated role. The router keeps the last such channel
    # and other consumers keep the first, so a duplicate role makes them disagree over the winner
    # and silently strands the losing channel — reject it here instead.
    role_owners: dict[str, list[str]] = {}
    for spec in config.channels:
        if spec.role in _ARBITRATED_ROLES:
            role_owners.setdefault(spec.role, []).append(spec.id)
    for role, owners in role_owners.items():
        if len(owners) > 1:
            errors.append(
                f"duplicate role {role!r}: channels {owners} all claim it "
                "(at most one channel may own an arbitrated gps/heading/ais role)"
            )


def _validate_initial_state(config: EngineConfig, errors: list[str]) -> None:
    raw = config.initial_state_raw
    for required in ("lat", "lon"):
        if required not in raw:
            errors.append(f"initial_state: missing required field {required!r}")

    for name, (lo, hi) in _STATE_RANGES.items():
        if name not in raw:
            continue
        try:
            value = float(raw[name])
        except (TypeError, ValueError):
            errors.append(f"initial_state.{name}: {raw[name]!r} is not a number")
            continue
        # NaN/Infinity slip past both bound comparisons below (both are False for NaN), so gate on
        # finiteness first — a NaN own-ship field poisons position on every channel (H9).
        if not math.isfinite(value):
            errors.append(f"initial_state.{name}: {raw[name]!r} is not a finite number")
            continue
        if lo is not None and value < lo:
            errors.append(f"initial_state.{name}: {value} below minimum {lo}")
        if hi is not None and value > hi:
            errors.append(f"initial_state.{name}: {value} above maximum {hi}")


def _validate_route_replay(config: EngineConfig, errors: list[str]) -> None:
    """Cross-field rules for the route-playback (F1) and record-and-replay (F2) seams.

    Both drive own-ship position, so they are mutually exclusive. Route playback only makes sense
    while simulating with dead-reckoning on; replay mode needs a real capture that exists on this
    host (checked eagerly so a missing file fails at validate/start, not mid-run).
    """
    route = config.route
    replay = config.replay

    # F1 (R53/R54): route.enabled preconditions.
    if route is not None and route.enabled:
        if config.mode != "simulate":
            errors.append(
                f"route.enabled requires mode 'simulate', got {config.mode!r} — route playback "
                "drives synthetic own-ship motion, which only applies when simulating"
            )
        if config.movement.mode != "underway":
            errors.append(
                "route.enabled requires movement.mode 'underway' so dead-reckoning can drive "
                f"own-ship along the route (got movement.mode {config.movement.mode!r})"
            )
        if len(route.waypoints) < 2:
            errors.append(
                f"route.enabled requires at least 2 waypoints, got {len(route.waypoints)}"
            )
        if route.speed_kn <= 0:
            errors.append(
                f"route.enabled requires speed_kn > 0 to advance along the route, got "
                f"{route.speed_kn}"
            )
        # A non-finite or out-of-range waypoint makes Geodesic.Inverse return NaN, poisoning
        # own-ship position on every channel (H9) — reject each one loudly.
        for idx, (lat, lon) in enumerate(route.waypoints):
            if not (math.isfinite(lat) and math.isfinite(lon)):
                errors.append(f"route.waypoints[{idx}] ({lat}, {lon}) is not finite")
            elif not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                errors.append(
                    f"route.waypoints[{idx}] ({lat}, {lon}) out of range "
                    "(lat -90..90, lon -180..180)"
                )

    # F2 (R54): replay mode needs an enabled replay block naming a capture that exists.
    if config.mode == "replay":
        if replay is None or not replay.enabled:
            errors.append(
                "mode 'replay' requires a 'replay' block with enabled=true (the capture file is "
                "the source of truth in replay mode)"
            )
        elif not replay.file.strip():
            errors.append(
                "mode 'replay' requires replay.file to name a capture to replay (got empty)"
            )
        else:
            from pathlib import Path

            if not Path(replay.file).exists():
                errors.append(
                    f"replay.file {replay.file!r} does not exist "
                    "(set it to an NMEA capture present on this host)"
                )

    # Scope enum + ais-only precondition. Enum-checked whenever a replay block exists so a bad value
    # fails loud regardless of mode. Under scope 'ais-only' own-ship is simulated and only the AIS
    # contacts are replayed, so an AIS output channel MUST exist for those contacts to land on.
    if replay is not None:
        if replay.scope not in ("full", "ais-only"):
            errors.append(f"replay.scope {replay.scope!r} invalid (expected full|ais-only)")
        elif replay.scope == "ais-only" and not any(ch.role == "ais" for ch in config.channels):
            errors.append(
                "replay.scope 'ais-only' requires a channel with role 'ais' to receive the "
                "replayed AIS contacts (own-ship is simulated, so only AIS comes from the capture)"
            )

    # F2 (R54): replay and route both own position — reject enabling both.
    route_on = route is not None and route.enabled
    replay_on = config.mode == "replay" or (replay is not None and replay.enabled)
    if route_on and replay_on:
        errors.append(
            "route.enabled is incompatible with replay — both drive own-ship position; enable at "
            "most one (disable route playback, or leave replay mode)"
        )


def _validate_globals(config: EngineConfig, errors: list[str]) -> None:
    host = config.tcp_tap_host.strip()
    if host in ("", "0.0.0.0"):  # noqa: S104 - explicitly rejecting the wildcard bind
        errors.append(
            f"tcp_tap_host {config.tcp_tap_host!r} is not allowed — bind taps to a specific "
            "LAN IP (or 127.0.0.1), never the 0.0.0.0 wildcard"
        )

    if config.writer_backend not in _VALID_WRITER_BACKENDS:
        errors.append(
            f"writer_backend {config.writer_backend!r} invalid "
            f"(expected {'|'.join(_VALID_WRITER_BACKENDS)})"
        )

    # time_source.rate is uncoerced by the dataclass, so a string (or NaN) rate reaches here and
    # would kill the physics thread at run time — reject anything non-finite or non-positive (M7).
    rate = config.time_source.rate
    try:
        rate_ok = math.isfinite(rate) and rate > 0
    except TypeError:
        rate_ok = False
    if not rate_ok:
        errors.append(f"time_source.rate must be a finite number > 0, got {rate!r}")

    if config.time_source.mode == "simulated" and not config.time_source.epoch:
        errors.append("time_source.mode 'simulated' requires an 'epoch' (ISO 8601)")
    if config.time_source.epoch:
        try:
            config.epoch_datetime()
        except ValueError:
            errors.append(f"time_source.epoch {config.time_source.epoch!r} is not valid ISO 8601")

    if not config.channels:
        errors.append("config has no channels")

    # Belt-and-braces mode guard: the dataclass __post_init__ already rejects a bad mode, but a
    # test (or any caller) can construct EngineConfig directly, so re-check here.
    if config.mode not in _VALID_MODES:
        errors.append(f"mode {config.mode!r} invalid (expected {'|'.join(_VALID_MODES)})")

    # In auto mode a channel first tries to pass through real NMEA from a physical input, falling
    # back to simulating only when the input goes dead. Two preconditions make that meaningful:
    if config.mode == "auto":
        if config.writer_backend != "serial":
            errors.append(
                f"mode 'auto' requires writer_backend 'serial', got {config.writer_backend!r} — "
                "the log/pty/null backends have no real input port, so auto would silently be "
                "identical to simulate (use 'serial', or set mode 'simulate')"
            )
        if not config.inputs:
            errors.append(
                "mode 'auto' requires at least one entry in 'inputs' — auto passes through live "
                "input, so define the input slots your channels draw from (or set mode 'simulate')"
            )
        if config.movement.mode != "static":
            errors.append(
                "auto mode requires movement.mode 'static' so simulated dead-reckoning cannot "
                "clobber live passthrough position — with a live GNSS source owning position, a "
                f"dead-reckoning physics engine would fight it (got movement.mode "
                f"{config.movement.mode!r})"
            )


def validate(config: EngineConfig) -> list[str]:
    """Return a list of human-readable problems (empty list == the config is valid)."""
    errors: list[str] = []
    _validate_globals(config, errors)
    _validate_initial_state(config, errors)
    for spec in config.channels:
        _validate_channel(spec, errors)
    for inp in config.inputs:
        _validate_input(inp, errors)
    _validate_cross_channel(config, errors)
    _validate_sources(config, errors)
    _validate_route_replay(config, errors)
    return errors


def validate_or_raise(config: EngineConfig) -> None:
    """Raise :class:`ConfigError` (message lists every problem) if ``config`` is invalid."""
    problems = validate(config)
    if problems:
        joined = "\n  - ".join(problems)
        raise ConfigError(f"invalid configuration ({len(problems)} problem(s)):\n  - {joined}")
