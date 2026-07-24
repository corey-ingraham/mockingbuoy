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

from typing import TYPE_CHECKING

from . import budget
from .gps_generator import SUPPORTED as _GPS_SENTENCES
from .heading_generator import SUPPORTED as _HEADING_SENTENCES

if TYPE_CHECKING:
    from .config import ChannelSpec, EngineConfig

# Sentence names each role's generator can actually build. AIS is modelled in config as a
# single ``AIVDM`` emit entry (own-ship reports go out as !AIVDO on the wire).
_AIS_SENTENCES = ("AIVDM", "AIVDO")
_ROLE_SENTENCES: dict[str, tuple[str, ...]] = {
    "gps": _GPS_SENTENCES,
    "heading": _HEADING_SENTENCES,
    "ais": _AIS_SENTENCES,
}
_VALID_ROLES = tuple(_ROLE_SENTENCES)
_VALID_DIRECTIONS = ("tx", "rx", "both")

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
}

_MIN_PORT = 1
_MAX_PORT = 65535


class ConfigError(ValueError):
    """Raised by :func:`validate_or_raise` when a config fails deep validation."""


def _is_placeholder(path: str) -> bool:
    p = path.strip().lower()
    return p == "" or any(marker in p for marker in _PLACEHOLDER_MARKERS)


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

    # A transmitting channel needs something to transmit; a pure-RX channel must not.
    if spec.direction in ("tx", "both") and not spec.emit:
        errors.append(f"{where}: direction {spec.direction!r} but no 'emit' entries")
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

    _validate_ais_traffic(spec, errors)

    if spec.tcp_tap is not None and spec.tcp_tap.enabled:
        port = spec.tcp_tap.port
        if not (_MIN_PORT <= port <= _MAX_PORT):
            errors.append(f"{where}: tcp_tap.port {port} out of range {_MIN_PORT}-{_MAX_PORT}")

    # Baud budget: does the offered load fit the wire?
    samples = _budget_samples(spec)
    if samples:
        result = budget.evaluate(spec.baud, spec.framing, samples)
        if result.over:
            errors.append(
                f"{where}: over baud budget — {result.utilization * 100:.0f}% of "
                f"{spec.baud} {spec.framing} (limit {result.threshold * 100:.0f}%)"
            )


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


def _validate_cross_channel(config: EngineConfig, errors: list[str]) -> None:
    ids: dict[str, int] = {}
    paths: dict[str, str] = {}  # real device path -> channel id that first claimed it
    tap_ports: dict[int, str] = {}  # tap port -> channel id

    for spec in config.channels:
        ids[spec.id] = ids.get(spec.id, 0) + 1

        if not _is_placeholder(spec.path):
            key = spec.path.strip()
            if key in paths:
                errors.append(
                    f"channel {spec.id!r}: device path {spec.path!r} already used by "
                    f"channel {paths[key]!r} (each channel needs its own port)"
                )
            else:
                paths[key] = spec.id

        if spec.tcp_tap is not None and spec.tcp_tap.enabled:
            port = spec.tcp_tap.port
            if port in tap_ports:
                errors.append(
                    f"channel {spec.id!r}: tcp_tap.port {port} collides with "
                    f"channel {tap_ports[port]!r}"
                )
            else:
                tap_ports[port] = spec.id

    for cid, count in ids.items():
        if count > 1:
            errors.append(f"duplicate channel id {cid!r} appears {count} times")


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
        if lo is not None and value < lo:
            errors.append(f"initial_state.{name}: {value} below minimum {lo}")
        if hi is not None and value > hi:
            errors.append(f"initial_state.{name}: {value} above maximum {hi}")


def _validate_globals(config: EngineConfig, errors: list[str]) -> None:
    host = config.tcp_tap_host.strip()
    if host in ("", "0.0.0.0"):  # noqa: S104 - explicitly rejecting the wildcard bind
        errors.append(
            f"tcp_tap_host {config.tcp_tap_host!r} is not allowed — bind taps to a specific "
            "LAN IP (or 127.0.0.1), never the 0.0.0.0 wildcard"
        )

    if config.time_source.mode == "simulated" and not config.time_source.epoch:
        errors.append("time_source.mode 'simulated' requires an 'epoch' (ISO 8601)")
    if config.time_source.epoch:
        try:
            config.epoch_datetime()
        except ValueError:
            errors.append(f"time_source.epoch {config.time_source.epoch!r} is not valid ISO 8601")

    if not config.channels:
        errors.append("config has no channels")


def validate(config: EngineConfig) -> list[str]:
    """Return a list of human-readable problems (empty list == the config is valid)."""
    errors: list[str] = []
    _validate_globals(config, errors)
    _validate_initial_state(config, errors)
    for spec in config.channels:
        _validate_channel(spec, errors)
    _validate_cross_channel(config, errors)
    return errors


def validate_or_raise(config: EngineConfig) -> None:
    """Raise :class:`ConfigError` (message lists every problem) if ``config`` is invalid."""
    problems = validate(config)
    if problems:
        joined = "\n  - ".join(problems)
        raise ConfigError(f"invalid configuration ({len(problems)} problem(s)):\n  - {joined}")
