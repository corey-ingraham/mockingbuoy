"""FastAPI front end for the NMEA simulator engine.

This is the only web-aware module in the project. It imports :mod:`nmea_sim` (the pure,
hardware-free engine) but the engine never imports this layer — the dependency arrow points
one way (``web`` -> ``nmea_sim``), and ``tkinter`` is never imported anywhere.

The app wraps a single :class:`~nmea_sim.engine.Engine` behind an
:class:`EngineManager`, fans every emitted sentence out to browser clients over
Server-Sent Events via a thread-safe :class:`Broker` (a ``janus.Queue`` bridging the
engine's worker threads to the asyncio event loop), and exposes a small control API.

Key seams:

* ``Engine(config, monitor=broker.publish)`` — the engine calls ``monitor(channel_id, line)``
  from its worker threads for every emitted line; that is the SSE source.
* The Broker's ``janus.Queue`` is constructed **inside the running event loop** (lifespan
  startup), never at import time.
* Engine threads are one-shot: :meth:`EngineManager.start` always builds a **fresh** Engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import janus
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict

from nmea_sim.config import EngineConfig, InputSpec
from nmea_sim.diagnostics import decode_line, score_baud
from nmea_sim.engine import Engine, HealthReport, port_is_operational, targetable_slots
from nmea_sim.serialport import SerialPort
from nmea_sim.state import VesselState
from nmea_sim.wind import apparent_wind

# --- constants --------------------------------------------------------------------

#: Directory holding the static single-page UI, resolved relative to this file so it
#: works regardless of the process working directory.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"

#: Per-subscriber SSE queue depth. On overflow the oldest frame is dropped so a slow
#: browser can never apply back-pressure to the engine threads.
_SUBSCRIBER_MAXSIZE = 1000

#: Hard cap on concurrent SSE subscribers. Bounds total memory (each subscriber holds up
#: to ``_SUBSCRIBER_MAXSIZE`` frames) and the per-frame fan-out cost. Caddy's per-IP
#: connection limits are the outer guard; this is the in-app backstop (over-limit -> 503).
_MAX_SUBSCRIBERS = 64

#: Bridge queue depth between engine threads and the event loop. On overflow
#: :meth:`Broker.publish` drops the line (never blocks an engine thread).
_BRIDGE_MAXSIZE = 10_000

#: How often the health-broadcast task emits a ``health`` SSE frame, in seconds.
_HEALTH_INTERVAL_S = 1.0

#: How often the state-broadcast task emits a ``state`` SSE frame, in seconds (~4 Hz). Fast enough
#: to drive a smooth conning display; the drop-oldest subscriber queue keeps the NEWEST frames, so a
#: state frame is never starved by the nmea flood under normal load. A fuller interest-filtered /
#: latest-wins broker redesign (a separate diagnostics stream) is deferred to the diagnostics phase.
_STATE_INTERVAL_S = 0.25

#: Minimum seconds between two active diagnostics actions on the SAME slot. A deliberate friction
#: (single-flight is enforced separately): a second action inside the cooldown is refused (429) so a
#: bench port can't be hammered with back-to-back send/loopback/sweep drives.
_DIAG_COOLDOWN_S = 5.0

#: Where diagnostics captures are written — the sole writable path (``data/``, git-ignored). Every
#: capture filename is SERVER-generated under here (R18); a caller can never supply a path.
_DIAG_DATA_DIR = "data"

#: At most this many raw captures may run at once (web-layer cap; per-file byte + wall-clock caps
#: live in ``CaptureSession``, the total-``data/`` quota is checked below).
_CAPTURE_MAX_CONCURRENT = 2

#: Per-capture-file hard byte cap (handed to ``CaptureSession``; it auto-stops on reaching it).
_CAPTURE_MAX_BYTES = 1 << 20  # 1 MiB

#: Per-capture wall-clock ceiling in seconds (``CaptureSession`` auto-stops on reaching it).
_CAPTURE_MAX_SECONDS = 300.0

#: Total-``data/`` byte quota. A new capture is refused once the directory already holds this much,
#: so runaway captures can never fill the only writable path.
_CAPTURE_DATA_QUOTA_BYTES = 64 << 20  # 64 MiB

#: Accepted numeric state-edit fields and their inclusive ``(min, max)`` ranges.
#: ``None`` means unbounded on that side. Mirrors ``nmea_sim.validate`` where sensible.
_UPDATE_RANGES: dict[str, tuple[float | None, float | None]] = {
    "lat": (-90.0, 90.0),
    "lon": (-180.0, 180.0),
    "sog_kn": (0.0, None),
    "cog_deg": (0.0, 360.0),
    "heading_true_deg": (0.0, 360.0),
    "heading_mag_deg": (0.0, 360.0),
    "mag_variation_deg": (-180.0, 180.0),
    "altitude_m": (None, None),
    "fix_quality": (0.0, None),
    "satellites": (0.0, None),
    "hdop": (0.0, None),
    # Manual (operator-editable) instrument/nav fields. Nothing DERIVED lives here: pitch_deg and
    # roll_deg are owned by the physics tick (sea-state motion model) and must never be set by hand.
    "stw_kn": (0.0, 100.0),
    "depth_m": (0.0, 12000.0),
    "rot_dpm": (-720.0, 720.0),
    "wind_speed_kn": (0.0, 200.0),
    "wind_dir_deg": (0.0, 360.0),
    "sea_state": (0.0, 9.0),
    "rudder_angle_deg": (-45.0, 45.0),
    "set_deg": (0.0, 360.0),
    "drift_kn": (0.0, 100.0),
}

#: The subset of :data:`_UPDATE_RANGES` that the Save-as-defaults persist endpoint (R15 allow-list)
#: accepts as own-ship initial-state overrides — the newly-added manual fields only (``sea_state``
#: included). Position/GNSS defaults and every DERIVED field are deliberately excluded.
_INITIAL_STATE_MANUAL_FIELDS: tuple[str, ...] = (
    "stw_kn",
    "depth_m",
    "rot_dpm",
    "wind_speed_kn",
    "wind_dir_deg",
    "sea_state",
    "rudder_angle_deg",
    "set_deg",
    "drift_kn",
)


# --- (de)serialization helpers ----------------------------------------------------


def _state_to_dict(state: VesselState) -> dict[str, Any]:
    """Serialize a vessel snapshot to JSON-safe primitives (``utc`` as ISO 8601).

    Includes the derived ``pitch_deg``/``roll_deg`` (physics-owned) for display, plus the apparent
    (relative) wind computed ON READ from the true wind and the vessel's motion over ground — the
    conning display wants bow-relative wind, which the stored state never holds, so it is resolved
    here rather than carried as a persisted field that could drift out of sync with the true wind.
    """
    app_speed, app_angle = apparent_wind(
        state.wind_speed_kn,
        state.wind_dir_deg,
        state.heading_true_deg,
        state.cog_deg,
        state.sog_kn,
    )
    return {
        "lat": state.lat,
        "lon": state.lon,
        "sog_kn": state.sog_kn,
        "cog_deg": state.cog_deg,
        "heading_true_deg": state.heading_true_deg,
        "heading_mag_deg": state.heading_mag_deg,
        "mag_variation_deg": state.mag_variation_deg,
        "altitude_m": state.altitude_m,
        "fix_quality": state.fix_quality,
        "satellites": state.satellites,
        "hdop": state.hdop,
        "stw_kn": state.stw_kn,
        "depth_m": state.depth_m,
        "rot_dpm": state.rot_dpm,
        "wind_speed_kn": state.wind_speed_kn,
        "wind_dir_deg": state.wind_dir_deg,
        "sea_state": state.sea_state,
        "pitch_deg": state.pitch_deg,
        "roll_deg": state.roll_deg,
        "rudder_angle_deg": state.rudder_angle_deg,
        "set_deg": state.set_deg,
        "drift_kn": state.drift_kn,
        "app_wind_speed_kn": app_speed,
        "app_wind_angle_deg": app_angle,
        "utc": state.utc.isoformat(),
    }


def _health_to_dict(
    report: HealthReport,
    *,
    mode: str | None = None,
    time_source: str | None = None,
) -> dict[str, Any]:
    """Serialize a :class:`HealthReport` to a JSON-safe dict (``status`` = ``running``).

    Each channel carries its ``source`` badge (``OFF``/``LIVE:<id>``/``SIM``). ``mode`` and
    ``time_source`` are top-level UI badges supplied by the caller (which alone knows the running
    config's mode and the engine's resolved time tag); each is included only when provided, so a
    stopped-engine health dict can carry ``mode`` from config while omitting ``time_source``.
    """
    out: dict[str, Any] = {
        "status": "running",
        "ok": report.ok,
        "physics_alive": report.physics_alive,
        "channels": [
            {
                "channel_id": ch.channel_id,
                "alive": ch.alive,
                "enabled": ch.enabled,
                "emitted": ch.emitted,
                "build_errors": ch.build_errors,
                "last_emit_age_s": ch.last_emit_age_s,
                "source": ch.source,
                "sinks": [{"name": s.name, "down": s.down, "errors": s.errors} for s in ch.sinks],
            }
            for ch in report.channels
        ],
    }
    if mode is not None:
        out["mode"] = mode
    if time_source is not None:
        out["time_source"] = time_source
    return out


def _input_mismatch(function: str, detected_class: str | None) -> bool:
    """Whether a slot's detected sentence class contradicts its declared ``function``.

    A read-only sanity flag for the operator: ``gps`` expects ``gnss``; ``sat`` (a satellite
    compass) legitimately carries ``gnss`` *or* ``heading``, so it only conflicts when the detected
    class is ``ais``; ``ais`` expects ``ais``; ``unused`` never conflicts. No detection (``None``)
    is never a mismatch — an idle slot is not a wiring error.
    """
    if detected_class is None or function == "unused":
        return False
    if function == "gps":
        return detected_class != "gnss"
    if function == "sat":
        return detected_class == "ais"
    if function == "ais":
        return detected_class != "ais"
    return False


# --- diagnostics active-action IO (bounded, best-effort) --------------------------


def _nmea_checksum(body: str) -> str:
    """XOR the sentence body (between ``$`` and ``*``) into the two-hex-digit ``*HH`` suffix."""
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"{cs:02X}"


def _loopback_sentence() -> str:
    """A well-formed, vendor-neutral self-test sentence written during a loopback probe."""
    body = "PMBLOOPBACK,1"
    return f"${body}*{_nmea_checksum(body)}"


def _tx_probe(path: str, baud: int, framing: str, line: str) -> dict[str, Any]:
    """Best-effort single-line TX on a bench port, then close. Bounded and tolerant.

    Uses the ordinary tolerant :class:`SerialPort`: a missing/absent device never raises — it
    reports ``present=False`` and the write is silently dropped. On a dev box with no hardware the
    port never opens, so ``sent`` is ``False``. Runs off the event loop (the caller offloads it).
    The device ``path`` is consumed here and never returned (R19 info-leak hygiene).
    """
    port = SerialPort(path, baud, framing=framing, direction="tx")
    port.start()
    try:
        port.write_line(line)
        return {"sent": port.present, "present": port.present}
    finally:
        port.close()


def _data_dir_bytes(data_dir: str) -> int:
    """Total size in bytes of regular files directly under ``data_dir`` (0 if it does not exist)."""
    root = Path(data_dir)
    if not root.is_dir():
        return 0
    total = 0
    for child in root.iterdir():
        if child.is_file():
            with contextlib.suppress(OSError):
                total += child.stat().st_size
    return total


# --- control request model --------------------------------------------------------


class ControlRequest(BaseModel):
    """Body of ``POST /api/control``. ``action`` is required; the numeric fields apply
    only to ``action == "update"`` and are each optional, while ``channel_id``/``enabled``
    apply only to ``action == "channel"``. The two groups are kept in one model because
    :meth:`state_changes` reads *only* the keys in :data:`_UPDATE_RANGES`, so a channel
    toggle can never leak into a vessel-state update."""

    action: str
    channel_id: str | None = None
    enabled: bool | None = None
    lat: float | None = None
    lon: float | None = None
    sog_kn: float | None = None
    cog_deg: float | None = None
    heading_true_deg: float | None = None
    heading_mag_deg: float | None = None
    mag_variation_deg: float | None = None
    altitude_m: float | None = None
    fix_quality: float | None = None
    satellites: float | None = None
    hdop: float | None = None
    # Manual instrument/nav fields (mirroring the new ``_UPDATE_RANGES`` keys so ``state_changes``
    # picks them up). ``sea_state`` is edited as a float and applied as-is — the engine clamps and
    # rounds it to the integer WMO scale — kept ``float | None`` for uniformity with the rest.
    stw_kn: float | None = None
    depth_m: float | None = None
    rot_dpm: float | None = None
    wind_speed_kn: float | None = None
    wind_dir_deg: float | None = None
    sea_state: float | None = None
    rudder_angle_deg: float | None = None
    set_deg: float | None = None
    drift_kn: float | None = None

    def state_changes(self) -> dict[str, float]:
        """Return only the supplied numeric state fields (drop ``action`` and ``None``s)."""
        changes: dict[str, float] = {}
        for field in _UPDATE_RANGES:
            value = getattr(self, field)
            if value is not None:
                changes[field] = float(value)
        return changes


# --- persist (Save-as-defaults) request models ------------------------------------


class ChannelDefault(BaseModel):
    """One per-channel enable default in a persist request. Extras are forbidden so a request can
    never smuggle an unrelated channel field (path, baud, framing, ...) through this narrow seam."""

    model_config = ConfigDict(extra="forbid")

    id: str
    enabled: bool


class InputDefault(BaseModel):
    """One per-slot input-function assignment in a persist request. Extras forbidden: the device
    ``path`` and every other InputSpec field are deliberately NOT reachable from here (R15/R19)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    function: str


class InitialStateRequest(BaseModel):
    """Body of ``POST /api/config/initial-state`` — the Save-as-defaults persist allow-list (R15).

    This is a DEDICATED model, not :class:`ControlRequest`: it exposes ONLY the operator-editable
    manual own-ship fields, an optional operating ``mode``, per-channel enable defaults, and
    per-slot input-function assignments. Every field an attacker might use to redirect I/O or leak
    paths —
    ``path``, ``tcp_tap*``, ``baud``, ``writer_backend``, ``direction``, ``framing``,
    ``voltage_sense``, ``rx_feeds_state``, ``emit`` — is simply absent, and ``extra="forbid"`` turns
    any unknown key into a 422 rather than a silently-ignored write. ``mode`` is validated in the
    handler (not a pydantic ``Literal``) so a bad value is a 400 with a clear message; ``"replay"``
    is rejected — it is a later feature the engine cannot yet honour.
    """

    model_config = ConfigDict(extra="forbid")

    stw_kn: float | None = None
    depth_m: float | None = None
    rot_dpm: float | None = None
    wind_speed_kn: float | None = None
    wind_dir_deg: float | None = None
    sea_state: float | None = None
    rudder_angle_deg: float | None = None
    set_deg: float | None = None
    drift_kn: float | None = None
    mode: str | None = None
    channels: list[ChannelDefault] | None = None
    inputs: list[InputDefault] | None = None


# --- diagnostics request models ---------------------------------------------------


class DecodeRequest(BaseModel):
    """Body of ``POST /api/diag/decode``. Read-only single-line inspector — no port is touched."""

    model_config = ConfigDict(extra="forbid")

    line: str


class BaudSweepRequest(BaseModel):
    """Body of ``POST /api/diag/baud-sweep``. ``confirm`` must echo the slot id (R17 friction)."""

    model_config = ConfigDict(extra="forbid")

    slot: str
    confirm: str


class SendRequest(BaseModel):
    """Body of ``POST /api/diag/send``. ``confirm`` must echo the slot id (R17 friction)."""

    model_config = ConfigDict(extra="forbid")

    slot: str
    line: str
    confirm: str


class LoopbackRequest(BaseModel):
    """Body of ``POST /api/diag/loopback``. ``confirm`` must echo the slot id (R17 friction)."""

    model_config = ConfigDict(extra="forbid")

    slot: str
    confirm: str


class CaptureRequest(BaseModel):
    """Body of ``POST /api/diag/capture``. ``action`` is ``start`` or ``stop``; the filename is
    always server-generated under ``data/`` (R18) — a caller can never supply a path."""

    model_config = ConfigDict(extra="forbid")

    slot: str
    action: str


# --- broker: engine threads <-> event loop <-> SSE clients -------------------------


class SubscriberLimitError(RuntimeError):
    """Raised by :meth:`Broker.subscribe` when the concurrent-subscriber cap is reached."""


class Broker:
    """Fans engine-thread output out to any number of SSE subscribers.

    A single ``janus.Queue`` bridges the (synchronous, multi-threaded) engine side to the
    (async) event-loop side. Engine threads call :meth:`publish`; the :meth:`pump`
    coroutine drains the async end and copies each frame into every subscriber's bounded
    :class:`asyncio.Queue`, dropping the oldest frame on overflow.
    """

    def __init__(self) -> None:
        self._queue: janus.Queue[dict[str, Any]] | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def bind(self, queue: janus.Queue[dict[str, Any]]) -> None:
        """Attach the janus queue (built inside the running loop during lifespan startup)."""
        self._queue = queue

    # -- producer side (called from engine worker threads) --------------------

    def publish(self, channel_id: str, line: str) -> None:
        """Thread-safe: enqueue one emitted NMEA line. Drops on a full bridge (never blocks).

        This exact signature is the engine's ``monitor(channel_id, line)`` seam.
        """
        self._emit({"event": "nmea", "data": {"channel": channel_id, "line": line}})

    def publish_health(self, health: dict[str, Any]) -> None:
        """Enqueue a health frame for fan-out. Drops on a full bridge."""
        self._emit({"event": "health", "data": health})

    def publish_state(self, state: dict[str, Any]) -> None:
        """Enqueue a vessel-state frame for fan-out (the smooth conning stream). Drops on a full
        bridge — a missed state frame is harmless, the next one carries the whole snapshot."""
        self._emit({"event": "state", "data": state})

    def _emit(self, frame: dict[str, Any]) -> None:
        queue = self._queue
        if queue is None:
            return
        with contextlib.suppress(janus.SyncQueueFull):
            queue.sync_q.put_nowait(frame)

    # -- consumer side (event loop) ------------------------------------------

    async def pump(self) -> None:
        """Drain the bridge and fan every frame out to subscribers (drop-oldest on full)."""
        queue = self._queue
        if queue is None:
            return
        while True:
            frame = await queue.async_q.get()
            for sub in list(self._subscribers):
                _put_drop_oldest(sub, frame)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new SSE client and return its bounded frame queue.

        Raises :class:`SubscriberLimitError` when :data:`_MAX_SUBSCRIBERS` is reached so the
        caller can reject the connection (503) rather than grow unbounded.
        """
        if len(self._subscribers) >= _MAX_SUBSCRIBERS:
            raise SubscriberLimitError(f"subscriber limit reached ({_MAX_SUBSCRIBERS})")
        sub: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SUBSCRIBER_MAXSIZE)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: asyncio.Queue[dict[str, Any]]) -> None:
        """Deregister an SSE client (idempotent)."""
        self._subscribers.discard(sub)

    @property
    def subscriber_count(self) -> int:
        """Current number of registered SSE subscribers (read-only, for the posture summary)."""
        return len(self._subscribers)

    def close(self) -> None:
        """Close the underlying janus queue, if bound."""
        if self._queue is not None:
            self._queue.close()


def _put_drop_oldest(sub: asyncio.Queue[dict[str, Any]], frame: dict[str, Any]) -> None:
    """Enqueue ``frame`` into ``sub``; if full, drop the oldest frame to make room."""
    try:
        sub.put_nowait(frame)
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            sub.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            sub.put_nowait(frame)


# --- engine manager ---------------------------------------------------------------


class EngineManager:
    """Owns the config, the (optional) running Engine, and the Broker.

    Every :meth:`start` builds a **fresh** Engine because engine threads are one-shot and
    cannot be restarted after :meth:`Engine.stop`.
    """

    def __init__(self, config: EngineConfig, broker: Broker) -> None:
        self._config = config
        self._broker = broker
        self._engine: Engine | None = None
        # Monotonic instant of the last successful start (None while stopped), for uptime_s.
        self._started_at: float | None = None

    @property
    def config(self) -> EngineConfig:
        return self._config

    @property
    def running(self) -> bool:
        return self._engine is not None

    def start(self) -> None:
        """Start the engine if stopped; no-op if already running."""
        if self._engine is not None:
            return
        engine = Engine(self._config, monitor=self._broker.publish)
        engine.start()
        self._engine = engine
        self._started_at = time.monotonic()

    def stop(self) -> None:
        """Stop and drop the engine if running; idempotent."""
        engine = self._engine
        if engine is None:
            return
        self._engine = None
        self._started_at = None
        engine.stop()

    def uptime_s(self) -> float:
        """Seconds since the running engine last started, or ``0.0`` when stopped."""
        return time.monotonic() - self._started_at if self._started_at is not None else 0.0

    def snapshot(self) -> VesselState | None:
        return self._engine.snapshot() if self._engine is not None else None

    def update_state(self, **changes: float) -> VesselState:
        if self._engine is None:
            raise RuntimeError("engine is not running")
        return self._engine.update_state(**changes)

    def set_channel_enabled(self, channel_id: str, enabled: bool) -> bool:
        """Toggle one output channel on/off; ``False`` when the id is unknown.

        A flag write only — the worker thread and its drift-free schedule are untouched, so
        this needs no engine restart and cannot block the caller.
        """
        if self._engine is None:
            raise RuntimeError("engine is not running")
        return self._engine.set_channel_enabled(channel_id, enabled)

    def input_status(self) -> list[dict[str, object]]:
        """Per-slot detection view. When running, delegates to :meth:`Engine.input_status` (which
        never exposes the device path — R19); when stopped, reports every configured slot idle
        (``detected_class=None``, ``live=False``) so the UI can still render the slot list."""
        if self._engine is not None:
            return self._engine.input_status()
        return [
            {"id": inp.id, "function": inp.function, "detected_class": None, "live": False}
            for inp in self._config.inputs
        ]

    # -- diagnostics (read-only + gated active actions) --------------------
    def _input_spec(self, slot: str) -> InputSpec | None:
        """The :class:`InputSpec` for ``slot`` (or None). Its ``path`` stays internal — R19."""
        return next((inp for inp in self._config.inputs if inp.id == slot), None)

    def diagnostics_snapshot(self) -> list[dict[str, Any]]:
        """Per-input PortDiagnostics snapshots (empty when stopped or in simulate mode)."""
        return self._engine.diagnostics_snapshot() if self._engine is not None else []

    def is_operational_port(self, slot: str) -> bool:
        """R17 gate half: whether ``slot`` is an operational port an active action must refuse."""
        return port_is_operational(self._config, slot)

    def targetable_slots(self) -> set[str]:
        """R17 gate half: input-slot ids eligible as an active-diagnostics target."""
        return targetable_slots(self._config)

    def baud_sweep(self, slot: str) -> dict[str, Any]:
        """Score a baud sweep on a bench slot. No hardware retune/capture is wired on this box, so
        the scorer runs over an empty sample set and returns ``winner=None`` (R29: no printable
        structure at any rate implicates polarity/wiring, not baud). Real per-baud capture IO is
        the hardware extension point."""
        if self._input_spec(slot) is None:
            raise KeyError(slot)
        return score_baud({})

    def send_test(self, slot: str, line: str) -> dict[str, Any]:
        """Best-effort single-line TX on a bench slot (bounded, tolerant). Blocking — offload it."""
        spec = self._input_spec(slot)
        if spec is None:
            raise KeyError(slot)
        return _tx_probe(spec.path, spec.baud, spec.framing, line)

    def loopback_test(self, slot: str) -> dict[str, Any]:
        """TX a canned self-test sentence on a bench slot (bounded, tolerant). Blocking; offload."""
        spec = self._input_spec(slot)
        if spec is None:
            raise KeyError(slot)
        line = _loopback_sentence()
        result = _tx_probe(spec.path, spec.baud, spec.framing, line)
        return {"probe": "loopback", "sent_line": line, **result}

    def start_capture(self, slot: str) -> dict[str, Any]:
        """Arm a bounded raw capture (server-named file under ``data/``). Requires a running engine
        and a slot the engine reads; raises ``RuntimeError``/``KeyError`` otherwise."""
        if self._engine is None:
            raise RuntimeError("engine is not running")
        return self._engine.start_capture(
            slot,
            data_dir=_DIAG_DATA_DIR,
            max_bytes=_CAPTURE_MAX_BYTES,
            max_seconds=_CAPTURE_MAX_SECONDS,
        )

    def stop_capture(self, slot: str) -> dict[str, Any] | None:
        """Stop + deregister the capture on ``slot``; None if none was active."""
        if self._engine is None:
            raise RuntimeError("engine is not running")
        return self._engine.stop_capture(slot)

    def capture_status(self) -> list[dict[str, Any]]:
        """Status of every registered capture (empty when stopped)."""
        return self._engine.capture_status() if self._engine is not None else []

    def health(self) -> dict[str, Any]:
        """Serialized health dict; ``{"status": "stopped", ...}`` when not running.

        When running the dict carries the engine's resolved ``time_source`` tag and the running
        ``mode``; when stopped only ``mode`` (from config) is reported and ``time_source`` is
        omitted — there is no live clock to name.
        """
        if self._engine is None:
            return {
                "status": "stopped",
                "ok": False,
                "physics_alive": False,
                "channels": [],
                "mode": self._config.mode,
            }
        return _health_to_dict(
            self._engine.health(),
            mode=self._config.mode,
            time_source=self._engine.time_source(),
        )


# --- optional in-app HTTP Basic (defense in depth, default OFF) --------------------


def _make_auth_dependency() -> Callable[..., Awaitable[None]]:
    """Return an auth dependency.

    This is an OPTIONAL defense-in-depth layer, off by default. Caddy is the primary auth
    layer. These env vars are deliberately distinct from Caddy's ``MOCKINGBUOY_BASIC_*``
    (which carry a *bcrypt* hash from ``caddy hash-password``): if the app reused those, the
    service would load Caddy's credential and try to verify a bcrypt hash with the argon2
    scheme below, rejecting every already-Caddy-authenticated request. To turn this layer on,
    set both ``MOCKINGBUOY_APP_BASIC_USER`` and ``MOCKINGBUOY_APP_BASIC_HASH`` — a passlib
    **argon2** hash (``from passlib.hash import argon2; argon2.hash("...")``). No credential
    is ever embedded here.
    """
    user = os.environ.get("MOCKINGBUOY_APP_BASIC_USER")
    password_hash = os.environ.get("MOCKINGBUOY_APP_BASIC_HASH")

    # Fail closed on a half-configured auth: setting exactly one env var must NOT silently
    # serve every endpoint unauthenticated while the operator believes Basic is enabled.
    if bool(user) != bool(password_hash):
        raise RuntimeError(
            "in-app Basic auth is half-configured: set BOTH MOCKINGBUOY_APP_BASIC_USER and "
            "MOCKINGBUOY_APP_BASIC_HASH, or neither"
        )

    if not user or not password_hash:

        async def _noop() -> None:
            return None

        return _noop

    from passlib.context import CryptContext

    security = HTTPBasic()
    pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

    # `security` is captured as a default-argument value (evaluated eagerly at def time),
    # not inside the annotation itself — with `from __future__ import annotations` every
    # annotation is a lazily-evaluated string, and a closure-local name like `security`
    # is not visible to that later `eval()` (which only sees module globals). Putting the
    # `Depends(...)` call in the annotation (e.g. `Annotated[T, Depends(security)]`) would
    # silently fail to resolve as a security dependency and mis-parse the request instead.
    async def _verify(credentials: HTTPBasicCredentials = Depends(security)) -> None:  # noqa: B008
        user_ok = secrets.compare_digest(credentials.username, user)
        pass_ok = pwd_context.verify(credentials.password, password_hash)
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=401,
                detail="unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )

    return _verify


# --- app factory ------------------------------------------------------------------


def create_app(config_path: str | None = None) -> FastAPI:
    """Build the FastAPI app.

    Load order (R49 first-boot fallback): the explicit ``config_path`` arg, else the
    ``MOCKINGBUOY_CONFIG`` env var, else the operator-written ``data/config.local.json`` if it
    already exists, else the tracked ``config.json`` baseline. So a fresh install with no local file
    boots from the read-only baseline, and once a Save-as-defaults write has landed the local file
    is preferred. Because :meth:`EngineConfig.save` is atomic (temp file + ``os.replace``), a
    concurrent read here can only ever see the whole-old or whole-new file, never a torn one.

    The persist target is resolved separately (it must never default to the read-only baseline):
    the ``config_path`` arg, else ``MOCKINGBUOY_CONFIG``, else ``data/config.local.json`` — never
    ``config.json``.
    """
    resolved_path = (
        config_path
        or os.environ.get("MOCKINGBUOY_CONFIG")
        or ("data/config.local.json" if Path("data/config.local.json").exists() else "config.json")
    )
    config = EngineConfig.load(resolved_path)

    #: Where Save-as-defaults writes. Deliberately does NOT fall back to ``config.json``: that file
    #: is the tracked baseline and, under a strict sandbox, its directory is read-only.
    persist_path = config_path or os.environ.get("MOCKINGBUOY_CONFIG") or "data/config.local.json"

    broker = Broker()
    manager = EngineManager(config, broker)
    auth = _make_auth_dependency()
    # Serializes engine start/stop so concurrent control requests can't double-open ports;
    # the transitions themselves run off the event loop (they join threads / open serial).
    control_lock = asyncio.Lock()
    # Serializes Save-as-defaults writes so two concurrent persists can't interleave their
    # validate -> save; the atomic writer handles crash safety, this handles request concurrency.
    persist_lock = asyncio.Lock()
    # Active-diagnostics single-flight + per-slot cooldown state. ``diag_lock`` guards both maps so
    # the check-and-reserve is atomic across concurrent requests; ``diag_inflight`` holds slots with
    # an action running now (reject a second with 429), ``diag_last_action`` the monotonic finish
    # time per slot (reject a follow-up inside the cooldown with 429).
    diag_lock = asyncio.Lock()
    diag_inflight: set[str] = set()
    diag_last_action: dict[str, float] = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Build the janus queue inside the running loop, then wire the broker.
        queue: janus.Queue[dict[str, Any]] = janus.Queue(maxsize=_BRIDGE_MAXSIZE)
        broker.bind(queue)

        # Auto-start the engine so the UI shows live data immediately. Run the (blocking)
        # start off the loop so opening serial ports / TCP taps can't stall startup.
        async with control_lock:
            await asyncio.to_thread(manager.start)

        pump_task = asyncio.create_task(broker.pump())
        health_task = asyncio.create_task(_health_broadcast_loop(manager, broker))
        state_task = asyncio.create_task(_state_broadcast_loop(manager, broker))
        try:
            yield
        finally:
            for task in (pump_task, health_task, state_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # Engine.stop() joins worker threads (up to its timeout); keep it off the loop
            # so a stalled sink can't freeze pending SSE flushes during shutdown.
            async with control_lock:
                await asyncio.to_thread(manager.stop)  # joins threads -> no leaked threads
            broker.close()

    app = FastAPI(title="mockingbuoy", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index(_: None = Depends(auth)) -> HTMLResponse:
        return HTMLResponse(_INDEX_HTML.read_text(encoding="utf-8"))

    @app.get("/healthz")
    async def healthz(_: None = Depends(auth)) -> Response:
        health = manager.health()
        ok = bool(health.get("ok")) and health.get("status") == "running"
        status_code = 200 if ok else 503
        return JSONResponse(health, status_code=status_code)

    @app.get("/api/config")
    async def api_config(_: None = Depends(auth)) -> dict[str, Any]:
        return manager.config.to_dict()

    @app.get("/api/state")
    async def api_state(_: None = Depends(auth)) -> dict[str, Any]:
        state = manager.snapshot()
        if state is None:
            return {"running": False}
        return _state_to_dict(state)

    @app.post("/api/control")
    async def api_control(body: ControlRequest, _: None = Depends(auth)) -> dict[str, Any]:
        action = body.action.strip().lower()

        if action == "start":
            async with control_lock:
                await asyncio.to_thread(manager.start)
            return {"running": True}

        if action == "stop":
            async with control_lock:
                await asyncio.to_thread(manager.stop)
            return {"running": False}

        if action == "update":
            if not manager.running:
                raise HTTPException(status_code=409, detail="engine is not running")
            changes = body.state_changes()
            for field, value in changes.items():
                low, high = _UPDATE_RANGES[field]
                if (low is not None and value < low) or (high is not None and value > high):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{field}={value} out of range [{low}, {high}]",
                    )
            state = manager.update_state(**changes)
            return {"running": True, "state": _state_to_dict(state)}

        if action == "channel":
            if not manager.running:
                raise HTTPException(status_code=409, detail="engine is not running")
            if body.channel_id is None or body.enabled is None:
                raise HTTPException(
                    status_code=400, detail="channel action requires channel_id and enabled"
                )
            # Setting a flag on a live worker: no lock and no worker thread needed, unlike
            # start/stop (which join threads and open ports) — offloading would only add
            # latency to what is a single atomic write.
            if not manager.set_channel_enabled(body.channel_id, body.enabled):
                raise HTTPException(status_code=404, detail=f"unknown channel: {body.channel_id!r}")
            # The toggle reaches every client through the existing 1 Hz health broadcast.
            return {"running": True, "channel_id": body.channel_id, "enabled": body.enabled}

        raise HTTPException(status_code=400, detail=f"unknown action: {body.action!r}")

    @app.get("/api/stream")
    async def api_stream(_: None = Depends(auth)) -> StreamingResponse:
        try:
            sub = broker.subscribe()
        except SubscriberLimitError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

        async def event_source() -> AsyncIterator[str]:
            # Disconnect handling is delegated to StreamingResponse: on client disconnect it
            # cancels this generator, so the ``finally`` runs and the subscriber is dropped.
            # We deliberately do NOT poll ``request.is_disconnected()`` here — under the
            # Starlette TestClient that consumes the same ASGI receive channel the response's
            # own disconnect listener uses and deadlocks the stream.
            try:
                while True:
                    frame = await sub.get()
                    yield f"event: {frame['event']}\ndata: {json.dumps(frame['data'])}\n\n"
            finally:
                broker.unsubscribe(sub)

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/config/initial-state")
    async def api_persist_initial_state(
        body: InitialStateRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        # Serialize the whole endpoint: two concurrent saves must not interleave their
        # validate -> save against the same file (the atomic writer only guarantees no torn file).
        async with persist_lock:
            # 1. Range-check every provided manual field with the SAME check the "update" action
            #    uses, so the persisted defaults can never be values a live update would reject.
            for field in _INITIAL_STATE_MANUAL_FIELDS:
                value = getattr(body, field)
                if value is None:
                    continue
                low, high = _UPDATE_RANGES[field]
                if (low is not None and value < low) or (high is not None and value > high):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{field}={value} out of range [{low}, {high}]",
                    )

            # 2. Merge onto a COPY of the running config's dict (never mutate the live config).
            merged = manager.config.to_dict()
            initial_state = dict(merged.get("initial_state", {}))
            for field in _INITIAL_STATE_MANUAL_FIELDS:
                value = getattr(body, field)
                if value is not None:
                    initial_state[field] = value
            merged["initial_state"] = initial_state

            if body.mode is not None:
                if body.mode not in ("simulate", "auto"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"mode must be simulate|auto, got {body.mode!r}",
                    )
                merged["mode"] = body.mode

            if body.channels is not None:
                by_id = {c["id"]: c for c in merged["channels"]}
                for ch in body.channels:
                    target = by_id.get(ch.id)
                    if target is None:
                        raise HTTPException(status_code=400, detail=f"unknown channel: {ch.id!r}")
                    target["enabled"] = ch.enabled

            if body.inputs is not None:
                by_id = {i["id"]: i for i in merged["inputs"]}
                for slot in body.inputs:
                    target = by_id.get(slot.id)
                    if target is None:
                        raise HTTPException(status_code=400, detail=f"unknown input: {slot.id!r}")
                    target["function"] = slot.function

            # 3. Rebuild + deep-validate the merged config; any structural or cross-field problem
            #    (incl. a bad input function) becomes a 400 with the validator's own message.
            try:
                merged_config = EngineConfig.from_dict(merged)
                merged_config.validate_or_raise()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            # 4. Persist to the local override file (never the tracked baseline) via the ATOMIC
            #    writer, off the event loop (it fsyncs). save() mkdirs the parent if needed.
            await asyncio.to_thread(merged_config.save, persist_path)

        # 5. Echo the saved defaults. This does NOT hot-reload the running engine: mode / input /
        #    channel-default changes take effect on the next engine (re)start, consistent with how
        #    writer_backend and other structural fields are treated.
        return {
            "saved": True,
            "initial_state": merged_config.initial_state_raw,
            "mode": merged_config.mode,
            "hot_reloaded": False,
        }

    @app.get("/api/inputs")
    async def api_inputs(_: None = Depends(auth)) -> list[dict[str, Any]]:
        # Read-only slot view. Info-leak hygiene (R19): only the slot id, declared function, and the
        # detection booleans are exposed — never the device path / by-id link / adapter serial.
        out: list[dict[str, Any]] = []
        for entry in manager.input_status():
            function = str(entry["function"])
            detected = entry["detected_class"]
            detected_class = str(detected) if detected is not None else None
            out.append(
                {
                    "id": entry["id"],
                    "function": function,
                    "detected_class": detected_class,
                    "live": bool(entry["live"]),
                    "mismatch": _input_mismatch(function, detected_class),
                }
            )
        return out

    @app.get("/api/security")
    async def api_security(_: None = Depends(auth)) -> dict[str, Any]:
        # Non-secret posture only (R19). Each *_basic is a bare presence probe computed at the
        # narrowest scope; the *_HASH vars are NEVER read and no environment VALUE is ever returned.
        cfg = manager.config
        taps = [
            {"channel": ch.id, "port": ch.tcp_tap.port}
            for ch in cfg.channels
            if ch.tcp_tap is not None and ch.tcp_tap.enabled
        ]
        return {
            "tls": "internal",
            "caddy_basic": bool(os.environ.get("MOCKINGBUOY_BASIC_USER")),
            "app_basic": bool(os.environ.get("MOCKINGBUOY_APP_BASIC_USER")),
            # Report the actual bind so the posture is honest for the shipped unix-socket
            # bind. The systemd unit sets MOCKINGBUOY_APP_BIND to the socket path; default
            # stays "127.0.0.1" so dev / loopback-TCP output is unchanged.
            "app_bind": os.environ.get("MOCKINGBUOY_APP_BIND", "127.0.0.1"),
            "taps": taps,
            "tap_host": cfg.tcp_tap_host,
            "subscribers": broker.subscriber_count,
            "max_subscribers": _MAX_SUBSCRIBERS,
            "uptime_s": manager.uptime_s(),
            "running": manager.running,
            # The app sets no global security response headers of its own (TLS + security headers
            # are the reverse proxy's job); report the empty set honestly, not something misleading.
            "headers": [],
        }

    # -- diagnostics (Maintenance tab backend) ------------------------------
    # R23/R25 future home: a SAMPLED live raw-diag SSE stream (~10-20 lines/s/port, its OWN stream
    # separate from /api/stream) lands with the Maintenance UI phase. For now the monitor POLLS
    # GET /api/diag for rolling stats; we deliberately do NOT flood /api/stream with per-line diag
    # frames here, so the conning stream is never starved by a diagnostics flood.

    def _reject_non_target(slot: str) -> None:
        """R17 refusal for an ACTIVE action: an operational port, or any slot that is not a free
        unused target, is refused with 409 (the port is busy/ineligible, not a malformed request).
        This is the security boundary — a bench TX/reconfigure can never reach a live wire."""
        if manager.is_operational_port(slot):
            raise HTTPException(status_code=409, detail=f"slot {slot!r} is an operational port")
        if slot not in manager.targetable_slots():
            raise HTTPException(
                status_code=409, detail=f"slot {slot!r} is not a free unused target"
            )

    def _check_confirm(slot: str, confirm: str) -> None:
        """The confirm token is the slot id echoed back — a deliberate friction, never a secret.
        Missing token -> 400; present but wrong -> 403."""
        if not confirm:
            raise HTTPException(status_code=400, detail="confirm token required")
        if confirm != slot:
            raise HTTPException(status_code=403, detail="confirm token does not match slot")

    @contextlib.asynccontextmanager
    async def _single_flight(slot: str) -> AsyncIterator[None]:
        """Reserve ``slot`` for one in-flight action under a per-slot cooldown; release on exit.

        A second concurrent action on the same slot, or a follow-up inside the cooldown, is refused
        with 429. The check-and-reserve is atomic under ``diag_lock`` so two racing requests cannot
        both slip through.
        """
        async with diag_lock:
            now = time.monotonic()
            if slot in diag_inflight:
                raise HTTPException(status_code=429, detail=f"action already running on {slot!r}")
            last = diag_last_action.get(slot)
            if last is not None and now - last < _DIAG_COOLDOWN_S:
                raise HTTPException(status_code=429, detail=f"slot {slot!r} in cooldown")
            diag_inflight.add(slot)
        try:
            yield
        finally:
            async with diag_lock:
                diag_inflight.discard(slot)
                diag_last_action[slot] = time.monotonic()

    @app.get("/api/diag")
    async def api_diag(_: None = Depends(auth)) -> dict[str, Any]:
        # Read-only per-input rolling diagnostics. Empty ports when stopped or in simulate mode.
        return {"running": manager.running, "ports": manager.diagnostics_snapshot()}

    @app.post("/api/diag/decode")
    async def api_diag_decode(body: DecodeRequest, _: None = Depends(auth)) -> dict[str, Any]:
        # Read-only single-line inspector (click-to-decode). Never touches a port; never raises.
        return decode_line(body.line)

    @app.post("/api/diag/baud-sweep")
    async def api_diag_baud_sweep(
        body: BaudSweepRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        _reject_non_target(body.slot)
        _check_confirm(body.slot, body.confirm)
        async with _single_flight(body.slot):
            result = await asyncio.to_thread(manager.baud_sweep, body.slot)
        return {"slot": body.slot, **result}

    @app.post("/api/diag/send")
    async def api_diag_send(body: SendRequest, _: None = Depends(auth)) -> dict[str, Any]:
        _reject_non_target(body.slot)
        _check_confirm(body.slot, body.confirm)
        async with _single_flight(body.slot):
            result = await asyncio.to_thread(manager.send_test, body.slot, body.line)
        return {"slot": body.slot, **result}

    @app.post("/api/diag/loopback")
    async def api_diag_loopback(body: LoopbackRequest, _: None = Depends(auth)) -> dict[str, Any]:
        _reject_non_target(body.slot)
        _check_confirm(body.slot, body.confirm)
        async with _single_flight(body.slot):
            result = await asyncio.to_thread(manager.loopback_test, body.slot)
        return {"slot": body.slot, **result}

    @app.post("/api/diag/capture")
    async def api_diag_capture(body: CaptureRequest, _: None = Depends(auth)) -> dict[str, Any]:
        # Capture is READ-ONLY recording, so it is NOT gated to unused targets (recording a live
        # feed is legitimate) and takes no confirm token — only quotas and a server-owned filename.
        action = body.action.strip().lower()
        if action == "stop":
            if not manager.running:
                raise HTTPException(status_code=409, detail="engine is not running")
            status = manager.stop_capture(body.slot)
            if status is None:
                raise HTTPException(status_code=404, detail=f"no active capture on {body.slot!r}")
            return {"action": "stop", **status}
        if action != "start":
            raise HTTPException(
                status_code=400, detail=f"action must be start|stop, got {body.action!r}"
            )
        if not manager.running:
            raise HTTPException(status_code=409, detail="engine is not running")
        # Web-layer quotas (R18): bound concurrent captures and the total data/ footprint before
        # arming; the per-file byte + wall-clock caps are the CaptureSession's own responsibility.
        active = [s for s in manager.capture_status() if s.get("active")]
        if len(active) >= _CAPTURE_MAX_CONCURRENT:
            raise HTTPException(status_code=429, detail="max concurrent captures reached")
        if _data_dir_bytes(_DIAG_DATA_DIR) >= _CAPTURE_DATA_QUOTA_BYTES:
            raise HTTPException(status_code=507, detail="data/ capture quota exhausted")
        try:
            status = await asyncio.to_thread(manager.start_capture, body.slot)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"unknown or non-read slot: {body.slot!r}"
            ) from None
        return {"action": "start", **status}

    return app


async def _health_broadcast_loop(manager: EngineManager, broker: Broker) -> None:
    """Publish a ``health`` SSE frame roughly every :data:`_HEALTH_INTERVAL_S` seconds."""
    while True:
        broker.publish_health(manager.health())
        await asyncio.sleep(_HEALTH_INTERVAL_S)


async def _state_broadcast_loop(manager: EngineManager, broker: Broker) -> None:
    """Publish a ``state`` SSE frame roughly every :data:`_STATE_INTERVAL_S` seconds (~4 Hz).

    Skips a tick when the engine is stopped (no snapshot). The drop-oldest subscriber queue keeps
    the NEWEST frames, so this fast conning stream is never starved by the nmea flood under normal
    load; a latest-wins / interest-filtered broker (a separate diag stream) is deferred to C3.
    """
    while True:
        snapshot = manager.snapshot()
        if snapshot is not None:
            broker.publish_state(_state_to_dict(snapshot))
        await asyncio.sleep(_STATE_INTERVAL_S)


# Module-level ASGI entrypoint (``web.app:app``).
app = create_app()
