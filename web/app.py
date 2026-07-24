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
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import janus
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from nmea_sim.config import EngineConfig
from nmea_sim.engine import Engine, HealthReport
from nmea_sim.state import VesselState

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
}


# --- (de)serialization helpers ----------------------------------------------------


def _state_to_dict(state: VesselState) -> dict[str, Any]:
    """Serialize a vessel snapshot to JSON-safe primitives (``utc`` as ISO 8601)."""
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
        "utc": state.utc.isoformat(),
    }


def _health_to_dict(report: HealthReport) -> dict[str, Any]:
    """Serialize a :class:`HealthReport` to a JSON-safe dict (``status`` = ``running``)."""
    return {
        "status": "running",
        "ok": report.ok,
        "physics_alive": report.physics_alive,
        "channels": [
            {
                "channel_id": ch.channel_id,
                "alive": ch.alive,
                "emitted": ch.emitted,
                "build_errors": ch.build_errors,
                "last_emit_age_s": ch.last_emit_age_s,
                "sinks": [{"name": s.name, "down": s.down, "errors": s.errors} for s in ch.sinks],
            }
            for ch in report.channels
        ],
    }


# --- control request model --------------------------------------------------------


class ControlRequest(BaseModel):
    """Body of ``POST /api/control``. ``action`` is required; the numeric fields apply
    only to ``action == "update"`` and are each optional."""

    action: str
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

    def state_changes(self) -> dict[str, float]:
        """Return only the supplied numeric state fields (drop ``action`` and ``None``s)."""
        changes: dict[str, float] = {}
        for field in _UPDATE_RANGES:
            value = getattr(self, field)
            if value is not None:
                changes[field] = float(value)
        return changes


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

    def stop(self) -> None:
        """Stop and drop the engine if running; idempotent."""
        engine = self._engine
        if engine is None:
            return
        self._engine = None
        engine.stop()

    def snapshot(self) -> VesselState | None:
        return self._engine.snapshot() if self._engine is not None else None

    def update_state(self, **changes: float) -> VesselState:
        if self._engine is None:
            raise RuntimeError("engine is not running")
        return self._engine.update_state(**changes)

    def health(self) -> dict[str, Any]:
        """Serialized health dict; ``{"status": "stopped", ...}`` when not running."""
        if self._engine is None:
            return {"status": "stopped", "ok": False, "physics_alive": False, "channels": []}
        return _health_to_dict(self._engine.health())


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

    ``config_path`` defaults to the ``MOCKINGBUOY_CONFIG`` env var, else ``config.json``.
    """
    resolved_path = config_path or os.environ.get("MOCKINGBUOY_CONFIG", "config.json")
    config = EngineConfig.load(resolved_path)

    broker = Broker()
    manager = EngineManager(config, broker)
    auth = _make_auth_dependency()
    # Serializes engine start/stop so concurrent control requests can't double-open ports;
    # the transitions themselves run off the event loop (they join threads / open serial).
    control_lock = asyncio.Lock()

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
        try:
            yield
        finally:
            for task in (pump_task, health_task):
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

    return app


async def _health_broadcast_loop(manager: EngineManager, broker: Broker) -> None:
    """Publish a ``health`` SSE frame roughly every :data:`_HEALTH_INTERVAL_S` seconds."""
    while True:
        broker.publish_health(manager.health())
        await asyncio.sleep(_HEALTH_INTERVAL_S)


# Module-level ASGI entrypoint (``web.app:app``).
app = create_app()
