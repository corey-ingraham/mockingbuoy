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
from .instrument_generator import InstrumentGenerator
from .navigation import dead_reckon
from .realism import RealismProfile, TargetSpawner
from .seastate import sea_state_motion
from .serialport import SerialPort
from .state import AisTarget, SharedState, VesselState
from .tcp_tap import TcpTap
from .writers import LogWriter, NullWriter, PtyWriter, Writer

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


class _InstrumentSource:
    def __init__(self, talker: str) -> None:
        self._gen = InstrumentGenerator(talker or "II")

    def build(self, sentence: str, state: VesselState) -> list[str]:
        return [self._gen.build(state, (sentence,))[0]]


class _AisSource:
    """Own-ship AIS, plus optional profile-driven synthetic target traffic.

    Traffic disabled (the default) => output is own-ship only, byte-identical to before.
    Traffic enabled => on construction we load a region-neutral :class:`RealismProfile`
    (from a local ``profile_path`` if set, else the neutral default), spawn a deterministic
    set of contacts, and interleave their reports with own-ship's. Targets are advanced by
    the **real elapsed time** between successive position builds, so their motion tracks the
    same wall clock as own-ship without a separate scheduler.
    """

    def __init__(self, ais: AisSpec) -> None:
        self._gen = AisGenerator("AI")
        self._own = ais.own_ship
        self._spawner: TargetSpawner | None = None
        self._targets: list[AisTarget] = []
        self._last_advance: float | None = None
        self._max_dt: float = 0.0
        traffic = ais.traffic
        if traffic is not None and traffic.enabled:
            profile = (
                RealismProfile.from_path(traffic.profile_path)
                if traffic.profile_path
                else RealismProfile.default()
            )
            self._spawner = TargetSpawner(profile, traffic.seed)
            count = (
                traffic.target_count if traffic.target_count is not None else profile.target_count
            )
            self._targets = self._spawner.spawn(count)
            self._max_dt = traffic.max_advance_s

    def _advance_targets(self) -> None:
        """Step every target forward by the real time since the last advance.

        The first call (the construction-time baud-budget probe) only records the baseline
        clock and moves nothing — so the budget guard's double-call at ~t0 is a no-op and
        consumes no RNG. Elapsed time is capped so a stalled process can't teleport a target.
        """
        if self._spawner is None:
            return
        now = time.monotonic()
        if self._last_advance is None:
            self._last_advance = now
            return
        dt = now - self._last_advance
        self._last_advance = now
        if dt > self._max_dt:
            dt = self._max_dt
        if dt <= 0.0:
            return
        self._targets = [self._spawner.advance(t, dt) for t in self._targets]

    def build(self, sentence: str, state: VesselState) -> list[str]:
        if sentence == AIS_POSITION:
            lines = self._gen.own_ship(state, self._own.mmsi, class_type=self._own.klass)
            if self._spawner is not None:
                self._advance_targets()
                for target in self._targets:
                    lines.extend(self._gen.position(target, own_ship=False))
            return lines
        if sentence == AIS_STATIC:
            own = AisTarget(
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
            lines = self._gen.static(own)
            if self._spawner is not None:
                for target in self._targets:
                    lines.extend(self._gen.static(target))
            return lines
        raise ValueError(f"unknown AIS emission {sentence!r}")


def build_source(spec: ChannelSpec) -> SentenceSource:
    """Pick the generator-backed source for a channel's role."""
    if spec.role == "gps":
        return _GpsSource(spec.talker)
    if spec.role == "heading":
        return _HeadingSource(spec.talker)
    if spec.role == "instrument":
        return _InstrumentSource(spec.talker)
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


def advance_next_fire(next_fire: float, period: float, now: float) -> float:
    """Advance a per-emitter ``next_fire`` clock by one ``period``, drift-free.

    Normally returns ``next_fire + period``. If the scheduler fell behind (the naive
    advance would still be due, i.e. ``<= now``), resync to ``now + period`` instead of
    letting the caller fire a catch-up burst — the "fell behind, resync without a burst"
    rule shared by the physics tick and every channel worker's emission schedule.
    """
    advanced = next_fire + period
    if advanced <= now:
        return now + period
    return advanced


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
    # A muted channel is still a live, scheduled thread: ``alive`` reports the thread,
    # ``enabled`` reports whether it is currently allowed to emit. They are independent.
    enabled: bool


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
        new_utc = self._time.advance(state.utc, dt_s)
        changes: dict[str, object] = {"utc": new_utc}
        if self._mode == "underway" and state.sog_kn != 0.0:
            lat, lon = dead_reckon(state.lat, state.lon, state.sog_kn, state.cog_deg, dt_s)
            changes["lat"] = lat
            changes["lon"] = lon
        # Pitch/roll are derived each tick from a deterministic sea-state motion model keyed
        # on the (absolute) clock — a hull rolls at anchor too, so this runs in both modes.
        # advance stays pure: identical inputs (same state, same clock) give identical output.
        pitch, roll = sea_state_motion(state.sea_state, new_utc.timestamp())
        changes["pitch_deg"] = pitch
        changes["roll_deg"] = roll
        return changes


class _PhysicsThread(threading.Thread):
    def __init__(
        self, shared: SharedState, physics: PhysicsEngine, hz: float, stop: threading.Event
    ) -> None:
        super().__init__(name="physics", daemon=True)
        self._shared = shared
        self._physics = physics
        self._period = 1.0 / hz
        self._stop_event = stop

    def run(self) -> None:
        prev = time.monotonic()
        next_tick = prev + self._period
        while not self._stop_event.is_set():
            wait = next_tick - time.monotonic()
            if wait > 0 and self._stop_event.wait(wait):
                break
            now = time.monotonic()
            dt = now - prev
            prev = now
            changes = self._physics.advance(self._shared.snapshot(), dt)
            self._shared.update(**changes)
            next_tick = advance_next_fire(next_tick, self._period, now)


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
        self._stop_event = stop
        self._monitor = monitor
        self._emitters = emitters_for(spec)
        # Mute switch. An Event is used rather than a plain bool because it is read by the
        # sender thread and written by the control seam; set == the channel may emit.
        self._enabled = threading.Event()
        if spec.enabled:
            self._enabled.set()
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

    # -- runtime mute -------------------------------------------------------
    @property
    def channel_id(self) -> str:
        return self._spec.id

    def set_enabled(self, enabled: bool) -> None:
        """Mute or unmute this channel without touching its thread or schedule."""
        if enabled:
            self._enabled.set()
        else:
            self._enabled.clear()

    def enabled(self) -> bool:
        return self._enabled.is_set()

    def _loop(self) -> None:
        # Invariant: the drift-free schedule keeps advancing while the channel is muted —
        # ``_fire`` returns early but ``next_fire`` is still advanced below, so re-enabling
        # resumes on the original cadence with no catch-up burst and no thread rebuild.
        start = time.monotonic()
        offsets = emission_offsets([em.period for em in self._emitters])
        for em, off in zip(self._emitters, offsets, strict=True):
            em.next_fire = start + off
        while not self._stop_event.is_set():
            now = time.monotonic()
            soonest = min(em.next_fire for em in self._emitters)
            wait = soonest - now
            if wait > 0:
                if self._stop_event.wait(wait):
                    break
                continue
            now = time.monotonic()
            for em in self._emitters:
                if em.next_fire <= now:
                    self._fire(em)
                    em.next_fire = advance_next_fire(em.next_fire, em.period, now)

    # -- emission -----------------------------------------------------------
    def _fire(self, em: _Emitter) -> None:
        # Checked before generation so a muted channel costs nothing and suppresses every
        # consumer at once — serial, TCP tap and the web monitor all hang off _fan_out.
        if not self._enabled.is_set():
            return
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
            enabled=self._enabled.is_set(),
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
        self._monitor = monitor
        self._stop_event = threading.Event()
        self._status: queue.Queue[StatusMsg] = queue.Queue(maxsize=10000)

        self._time_source = TimeSource(config.time_source, config.epoch_datetime())
        self._shared = SharedState(config.build_initial_state(self._time_source.initial()))
        self._physics_engine = PhysicsEngine(config.movement.mode, self._time_source)
        self._physics = _PhysicsThread(
            self._shared, self._physics_engine, config.movement.physics_hz, self._stop_event
        )

        # Sinks that own I/O resources (serial ports, TCP taps) and must be started before
        # emission begins. Duck-typed on a ``start()`` method.
        self._startables: list[object] = []

        self._workers: list[_ChannelWorker] = []
        for spec in config.channels:
            source = build_source(spec)
            sinks = self._build_sinks(spec, sink_hook)
            self._check_budget(spec, source, strict_budget)
            self._workers.append(
                _ChannelWorker(
                    spec, source, sinks, self._shared, self._status, self._stop_event, monitor
                )
            )

    # -- construction helpers ----------------------------------------------
    def _make_backend_writer(self, spec: ChannelSpec) -> Writer:
        backend = self._config.writer_backend
        if backend == "log":
            return LogWriter()
        if backend == "null":
            return NullWriter()
        if backend == "pty":
            return PtyWriter()
        if backend == "serial":
            return SerialPort(
                spec.path,
                spec.baud,
                framing=spec.framing,
                direction=spec.direction,
                on_rx=self._rx_monitor(spec),
                state_feed=self._feed_state,
                rx_feeds_state=spec.rx_feeds_state,
                rx_accept=spec.rx_accept,
            )
        raise NotImplementedError(f"writer backend {backend!r} is not available yet")

    def _feed_state(self, changes: dict[str, float]) -> None:
        """RX state seam: apply whitelisted, checksum-verified fields to shared state."""
        self._shared.update(**changes)

    def _rx_monitor(self, spec: ChannelSpec) -> Callable[[str], None] | None:
        """Forward a received line to the web monitor seam, tagged with the channel id."""
        if self._monitor is None:
            return None
        monitor = self._monitor

        def forward(line: str) -> None:
            monitor(spec.id, line)

        return forward

    def _build_sinks(self, spec: ChannelSpec, sink_hook: SinkHook | None) -> list[_Sink]:
        backend_writer = self._make_backend_writer(spec)
        self._register_startable(backend_writer)
        sinks = [_Sink(self._config.writer_backend, backend_writer)]
        if spec.tcp_tap is not None and spec.tcp_tap.enabled:
            tap = TcpTap(self._config.tcp_tap_host, spec.tcp_tap.port)
            self._register_startable(tap)
            sinks.append(_Sink(f"tcp_tap:{spec.tcp_tap.port}", tap))
        if sink_hook is not None:
            for i, writer in enumerate(sink_hook(spec)):
                self._register_startable(writer)
                sinks.append(_Sink(f"extra{i}:{type(writer).__name__}", writer))
        return sinks

    def _register_startable(self, writer: object) -> None:
        if callable(getattr(writer, "start", None)):
            self._startables.append(writer)

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
        # Open I/O sinks (serial ports, TCP taps) before any emission so the first
        # sentence has somewhere to go and taps are already accepting subscribers.
        for startable in self._startables:
            start = getattr(startable, "start", None)
            if callable(start):
                start()
        self._physics.start()
        for worker in self._workers:
            worker.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop_event.set()
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

    def set_channel_enabled(self, channel_id: str, enabled: bool) -> bool:
        """Mute/unmute one channel at runtime; False when no channel has that id.

        Deliberately a flag write and nothing more: no worker is started, stopped or
        rebuilt, so a toggle is cheap enough to serve straight from a request handler.
        """
        for worker in self._workers:
            if worker.channel_id == channel_id:
                worker.set_enabled(enabled)
                return True
        return False

    def health(self) -> HealthReport:
        channels = [w.health() for w in self._workers]
        physics_alive = self._physics.is_alive()
        ok = (
            physics_alive
            and all(c.alive for c in channels)
            and all(not s.down for c in channels for s in c.sinks)
        )
        return HealthReport(ok=ok, physics_alive=physics_alive, channels=channels)
