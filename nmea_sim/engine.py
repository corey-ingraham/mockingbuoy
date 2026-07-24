"""The runtime engine: one physics thread + one sender thread per channel.

Topology (all threads share one ``stop_event``):

* **PhysicsThread** advances the single authoritative ``VesselState`` (position by
  dead-reckoning, clock by the configured time source) at ``movement.physics_hz``.
* **One ChannelWorker per channel** runs a **drift-free** schedule: each configured
  emission has its own next-fire time on ``time.monotonic``; the worker sleeps until the
  soonest, fires it, and advances ``next_fire += period`` (never ``now + period``) so
  timing does not drift. Emissions are **spread** across their period at start so a
  channel does not burst everything at ``t=0``.
* Each emitted sentence is **fanned out** to every sink for that channel (log/serial/tap
  in later phases) with **per-sink failure isolation**: one sink raising marks only that
  sink down and never stops the others or the worker. Emitted lines also go to an optional
  ``monitor`` callback (the web SSE seam) and lifecycle events to a ``status_queue``.

The engine consults the **baud-budget guard** before starting and refuses (or warns) if a
channel is over budget. It never imports the web/serial layers — those plug in as sinks.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from . import budget
from .ais_generator import AisGenerator
from .config import AisSpec, ChannelSpec, EngineConfig, TimeSourceSpec
from .gps_generator import GpsGenerator
from .heading_generator import HeadingGenerator
from .navigation import dead_reckon
from .state import AisTarget, SharedState, VesselState
from .writers import LogWriter, NullWriter, Writer

# Internal AIS emission kinds (config models AIS as one emit entry; the engine expands it
# into a position report and an optional periodic static/voyage report).
AIS_POSITION = "AIS_POSITION"
AIS_STATIC = "AIS_STATIC"


class BudgetExceeded(RuntimeError):
    """Raised at start-up when one or more channels exceed the baud budget (strict mode)."""


# --- sentence sources -------------------------------------------------------------


class SentenceSource(Protocol):
    """Builds the NMEA line(s) for a named emission from a vessel snapshot."""

    def build(self, sentence: str, state: VesselState) -> list[str]: ...


class _GpsSource:
    def __init__(self, talker: str) -> None:
        self._gen = GpsGenerator(talker or "GP")

    def build(self, sentence: str, state: VesselState) -> list[str]:
        return [self._gen.build(state, (sentence,))[0]]


class _HeadingSource:
    def __init__(self, talker: str) -> None:
        self._gen = HeadingGenerator(talker or "HE")

    def build(self, sentence: str, state: VesselState) -> list[str]:
        return [self._gen.build(state, (sentence,))[0]]


class _AisSource:
    def __init__(self, ais: AisSpec) -> None:
        self._gen = AisGenerator("AI")
        self._own = ais.own_ship

    def build(self, sentence: str, state: VesselState) -> list[str]:
        if sentence == AIS_POSITION:
            return self._gen.own_ship(state, self._own.mmsi, class_type=self._own.klass)
        if sentence == AIS_STATIC:
            target = AisTarget(
                mmsi=self._own.mmsi,
                lat=state.lat,
                lon=state.lon,
                sog_kn=state.sog_kn,
                cog_deg=state.cog_deg,
                heading_deg=int(round(state.heading_true_deg)) % 360,
                class_type=self._own.klass,
                ship_type=self._own.ship_type,
                name=self._own.name,
                callsign=self._own.call_sign,
                imo=self._own.imo,
            )
            return self._gen.static(target)
        raise ValueError(f"unknown AIS emission {sentence!r}")


def build_source(spec: ChannelSpec) -> SentenceSource:
    """Pick the generator-backed source for a channel's role."""
    if spec.role == "gps":
        return _GpsSource(spec.talker)
    if spec.role == "heading":
        return _HeadingSource(spec.talker)
    if spec.role == "ais":
        if spec.ais is None:
            raise ValueError(f"channel {spec.id!r} has role 'ais' but no ais config")
        return _AisSource(spec.ais)
    raise ValueError(f"channel {spec.id!r} has unknown role {spec.role!r}")


def emitters_for(spec: ChannelSpec) -> list[_Emitter]:
    """Expand a channel's config into scheduled emitters (periods in seconds)."""
    if spec.role == "ais":
        pos_rate = spec.emit[0].rate_hz if spec.emit else 0.2
        out = [_Emitter(AIS_POSITION, 1.0 / pos_rate)]
        if spec.ais is not None and spec.ais.include_type5:
            out.append(_Emitter(AIS_STATIC, spec.ais.type5_period_s))
        return out
    return [_Emitter(e.sentence, 1.0 / e.rate_hz) for e in spec.emit]


def emission_offsets(periods: list[float]) -> list[float]:
    """Deterministic start offsets that spread emissions across their periods.

    Emission ``i`` of ``n`` starts at fraction ``i/n`` of its own period, so equal-rate
    sentences on a channel do not all fire at the same instant.
    """
    n = len(periods)
    return [(i / n) * periods[i] for i in range(n)] if n else []


# --- scheduling + fan-out state ---------------------------------------------------


@dataclass
class _Emitter:
    sentence: str
    period: float
    next_fire: float = 0.0


@dataclass
class _Sink:
    name: str
    writer: Writer
    down: bool = False
    errors: int = 0


@dataclass(frozen=True)
class StatusMsg:
    channel_id: str
    kind: str  # started | stopped | build_error | sink_error | budget_warning
    detail: str = ""
    at: float = 0.0


@dataclass(frozen=True)
class SinkHealth:
    name: str
    down: bool
    errors: int


@dataclass(frozen=True)
class ChannelHealth:
    channel_id: str
    alive: bool
    emitted: int
    build_errors: int
    sinks: list[SinkHealth]
    last_emit_age_s: float | None


@dataclass(frozen=True)
class HealthReport:
    ok: bool
    physics_alive: bool
    channels: list[ChannelHealth]


# --- physics ----------------------------------------------------------------------


class TimeSource:
    """Produces successive UTC values per the configured clock model."""

    def __init__(self, spec: TimeSourceSpec, epoch: datetime | None) -> None:
        self._mode = spec.mode
        self._rate = spec.rate
        self._epoch = epoch

    def initial(self) -> datetime:
        if self._mode in ("simulated", "hold") and self._epoch is not None:
            return self._epoch
        return datetime.now(UTC)

    def advance(self, current: datetime, dt_s: float) -> datetime:
        if self._mode == "system_utc":
            return datetime.now(UTC)
        if self._mode == "hold":
            return current
        return current + timedelta(seconds=dt_s * self._rate)


class PhysicsEngine:
    """Pure position/clock integrator — no threading, so it is deterministically testable."""

    def __init__(self, movement_mode: str, time_source: TimeSource) -> None:
        self._mode = movement_mode
        self._time = time_source

    def advance(self, state: VesselState, dt_s: float) -> dict[str, object]:
        """Return the field changes for advancing ``state`` by ``dt_s`` seconds."""
        changes: dict[str, object] = {"utc": self._time.advance(state.utc, dt_s)}
        if self._mode == "underway" and state.sog_kn != 0.0:
            lat, lon = dead_reckon(state.lat, state.lon, state.sog_kn, state.cog_deg, dt_s)
            changes["lat"] = lat
            changes["lon"] = lon
        return changes


class _PhysicsThread(threading.Thread):
    def __init__(
        self, shared: SharedState, physics: PhysicsEngine, hz: float, stop: threading.Event
    ) -> None:
        super().__init__(name="physics", daemon=True)
        self._shared = shared
        self._physics = physics
        self._period = 1.0 / hz
        self._stop = stop

    def run(self) -> None:
        prev = time.monotonic()
        next_tick = prev + self._period
        while not self._stop.is_set():
            wait = next_tick - time.monotonic()
            if wait > 0 and self._stop.wait(wait):
                break
            now = time.monotonic()
            dt = now - prev
            prev = now
            changes = self._physics.advance(self._shared.snapshot(), dt)
            self._shared.update(**changes)
            next_tick += self._period
            if next_tick <= now:  # fell behind; resync without a catch-up burst
                next_tick = now + self._period


# --- per-channel sender -----------------------------------------------------------


class _ChannelWorker(threading.Thread):
    def __init__(
        self,
        spec: ChannelSpec,
        source: SentenceSource,
        sinks: list[_Sink],
        shared: SharedState,
        status_q: queue.Queue[StatusMsg],
        stop: threading.Event,
        monitor: Callable[[str, str], None] | None,
    ) -> None:
        super().__init__(name=f"channel-{spec.id}", daemon=True)
        self._spec = spec
        self._source = source
        self._sinks = sinks
        self._shared = shared
        self._status = status_q
        self._stop = stop
        self._monitor = monitor
        self._emitters = emitters_for(spec)
        self._alive = False
        self._emitted = 0
        self._build_errors = 0
        self._last_emit: float | None = None

    # -- lifecycle ----------------------------------------------------------
    def run(self) -> None:
        self._alive = True
        self._emit_status("started")
        try:
            self._loop()
        finally:
            self._alive = False
            self._emit_status("stopped")

    def _loop(self) -> None:
        start = time.monotonic()
        offsets = emission_offsets([em.period for em in self._emitters])
        for em, off in zip(self._emitters, offsets, strict=True):
            em.next_fire = start + off
        while not self._stop.is_set():
            now = time.monotonic()
            soonest = min(em.next_fire for em in self._emitters)
            wait = soonest - now
            if wait > 0:
                if self._stop.wait(wait):
                    break
                continue
            now = time.monotonic()
            for em in self._emitters:
                if em.next_fire <= now:
                    self._fire(em)
                    em.next_fire += em.period
                    if em.next_fire <= now:  # behind by >1 period: resync, no burst
                        em.next_fire = now + em.period

    # -- emission -----------------------------------------------------------
    def _fire(self, em: _Emitter) -> None:
        state = self._shared.snapshot()
        try:
            lines = self._source.build(em.sentence, state)
        except Exception as exc:  # a build failure must not kill the channel
            self._build_errors += 1
            self._emit_status("build_error", f"{em.sentence}: {exc!r}")
            return
        for line in lines:
            self._fan_out(line)
        self._emitted += len(lines)
        self._last_emit = time.monotonic()

    def _fan_out(self, line: str) -> None:
        for sink in self._sinks:
            if sink.down:
                continue
            try:
                sink.writer.write_line(line)
            except Exception as exc:  # isolate: one bad sink never blocks the others
                sink.down = True
                sink.errors += 1
                self._emit_status("sink_error", f"{sink.name}: {exc!r}")
        if self._monitor is not None:
            # a slow/broken monitor must never break emission
            with contextlib.suppress(Exception):
                self._monitor(self._spec.id, line)

    # -- reporting ----------------------------------------------------------
    def _emit_status(self, kind: str, detail: str = "") -> None:
        # status is best-effort; a full queue must never block a sender
        with contextlib.suppress(queue.Full):
            self._status.put_nowait(StatusMsg(self._spec.id, kind, detail, time.monotonic()))

    def close_sinks(self) -> None:
        for sink in self._sinks:
            with contextlib.suppress(Exception):  # best-effort teardown
                sink.writer.close()

    def health(self) -> ChannelHealth:
        age = None if self._last_emit is None else time.monotonic() - self._last_emit
        return ChannelHealth(
            channel_id=self._spec.id,
            alive=self._alive,
            emitted=self._emitted,
            build_errors=self._build_errors,
            sinks=[SinkHealth(s.name, s.down, s.errors) for s in self._sinks],
            last_emit_age_s=age,
        )


# --- the engine -------------------------------------------------------------------

SinkHook = Callable[[ChannelSpec], Iterable[Writer]]


class Engine:
    """Owns shared state, physics, and one worker per channel; start/stop/health."""

    def __init__(
        self,
        config: EngineConfig,
        *,
        sink_hook: SinkHook | None = None,
        monitor: Callable[[str, str], None] | None = None,
        strict_budget: bool = True,
    ) -> None:
        self._config = config
        self._stop = threading.Event()
        self._status: queue.Queue[StatusMsg] = queue.Queue(maxsize=10000)

        self._time_source = TimeSource(config.time_source, config.epoch_datetime())
        self._shared = SharedState(config.build_initial_state(self._time_source.initial()))
        self._physics_engine = PhysicsEngine(config.movement.mode, self._time_source)
        self._physics = _PhysicsThread(
            self._shared, self._physics_engine, config.movement.physics_hz, self._stop
        )

        self._workers: list[_ChannelWorker] = []
        for spec in config.channels:
            source = build_source(spec)
            sinks = self._build_sinks(spec, sink_hook)
            self._check_budget(spec, source, strict_budget)
            self._workers.append(
                _ChannelWorker(spec, source, sinks, self._shared, self._status, self._stop, monitor)
            )

    # -- construction helpers ----------------------------------------------
    def _make_backend_writer(self, spec: ChannelSpec) -> Writer:
        backend = self._config.writer_backend
        if backend == "log":
            return LogWriter()
        if backend == "null":
            return NullWriter()
        # serial and pty backends arrive with the serial layer.
        raise NotImplementedError(f"writer backend {backend!r} is not available yet")

    def _build_sinks(self, spec: ChannelSpec, sink_hook: SinkHook | None) -> list[_Sink]:
        sinks = [_Sink(self._config.writer_backend, self._make_backend_writer(spec))]
        if sink_hook is not None:
            for i, writer in enumerate(sink_hook(spec)):
                sinks.append(_Sink(f"extra{i}:{type(writer).__name__}", writer))
        return sinks

    def _check_budget(self, spec: ChannelSpec, source: SentenceSource, strict: bool) -> None:
        state = self._shared.snapshot()
        emissions: list[tuple[float, list[str]]] = []
        for em in emitters_for(spec):
            try:
                lines = source.build(em.sentence, state)
            except Exception:
                continue  # unbuildable sample can't be budgeted; leave to config validation
            emissions.append((1.0 / em.period, lines))
        result = budget.evaluate(spec.baud, spec.framing, emissions)
        if result.over:
            detail = (
                f"{result.utilization * 100:.0f}% of {spec.baud} {spec.framing} "
                f"(> {result.threshold * 100:.0f}%)"
            )
            if strict:
                raise BudgetExceeded(f"channel {spec.id!r} over baud budget: {detail}")
            self._status.put_nowait(StatusMsg(spec.id, "budget_warning", detail, 0.0))

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._physics.start()
        for worker in self._workers:
            worker.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        deadline = time.monotonic() + timeout
        for thread in (self._physics, *self._workers):
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        for worker in self._workers:
            worker.close_sinks()

    # -- accessors ----------------------------------------------------------
    @property
    def status_queue(self) -> queue.Queue[StatusMsg]:
        return self._status

    def snapshot(self) -> VesselState:
        return self._shared.snapshot()

    def update_state(self, **changes: object) -> VesselState:
        """Apply an external state edit (the web control seam)."""
        return self._shared.update(**changes)

    def health(self) -> HealthReport:
        channels = [w.health() for w in self._workers]
        physics_alive = self._physics.is_alive()
        ok = (
            physics_alive
            and all(c.alive for c in channels)
            and all(not s.down for c in channels for s in c.sinks)
        )
        return HealthReport(ok=ok, physics_alive=physics_alive, channels=channels)
