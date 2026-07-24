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
from typing import Protocol, cast

import pynmea2

from . import budget, rx
from .ais_generator import AisGenerator
from .config import AisSpec, ChannelSpec, EngineConfig, TimeSourceSpec
from .diagnostics import CaptureSession, PortDiagnostics
from .gps_generator import GpsGenerator, zda_from_datetime
from .heading_generator import HeadingGenerator
from .instrument_generator import InstrumentGenerator
from .navigation import dead_reckon
from .ntpsync import NtpSync
from .realism import RealismProfile, TargetSpawner
from .router import Router
from .seastate import sea_state_motion
from .serialport import SerialPort
from .state import AisTarget, SharedState, VesselState
from .tcp_tap import TcpTap
from .timeauthority import TimeAuthority
from .writers import LogWriter, NullWriter, PtyWriter, Writer

# Internal AIS emission kinds (config models AIS as one emit entry; the engine expands it
# into a position report and an optional periodic static/voyage report).
AIS_POSITION = "AIS_POSITION"
AIS_STATIC = "AIS_STATIC"

# A single in-flight capture may carry at most this many bytes without a newline before the
# runaway tail is flushed verbatim to the file — bounds the per-input capture residual (R28) so a
# newline-less wire can never grow the tee buffer without limit.
_MAX_CAPTURE_RESIDUAL = 4096


# Shutdown sentinel pushed onto every worker inbox by ``Engine.stop``. A module-level singleton
# so identity (``is``) comparison is unambiguous: a worker blocked in ``inbox.get`` wakes at once
# and breaks, rather than depending on a ``stop_event.wait`` a queue put can never interrupt (R40).
_STOP = object()


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
    # Where this channel's output is coming from right now: ``"OFF"`` when disabled, else
    # ``"LIVE:<input_id>"`` when a physical source is winning (auto mode) or ``"SIM"`` when it
    # is generating. Always ``"OFF"``/``"SIM"`` in simulate mode (no router, no live source).
    source: str


@dataclass(frozen=True)
class HealthReport:
    ok: bool
    physics_alive: bool
    channels: list[ChannelHealth]


# --- physics ----------------------------------------------------------------------


class TimeSource:
    """Produces successive UTC values per the configured clock model."""

    def __init__(self, spec: TimeSourceSpec, epoch: datetime | None) -> None:
        # ``mode`` is a public, effectively-read-only attribute (never reassigned post-construction)
        # so the TimeAuthority can honour the base clock's mode (system_utc/simulated/hold) when no
        # GNSS source is live, and so TimeSource structurally satisfies the clock surface the
        # authority wraps (its Protocol expects a settable ``mode: str``, which a property is not).
        self.mode = spec.mode
        self._rate = spec.rate
        self._epoch = epoch

    def initial(self) -> datetime:
        if self.mode in ("simulated", "hold") and self._epoch is not None:
            return self._epoch
        return datetime.now(UTC)

    def advance(self, current: datetime, dt_s: float) -> datetime:
        if self.mode == "system_utc":
            return datetime.now(UTC)
        if self.mode == "hold":
            return current
        return current + timedelta(seconds=dt_s * self._rate)


class _Clock(Protocol):
    """The narrow clock surface ``PhysicsEngine`` drives — just ``advance``.

    Typed as a Protocol so the engine can be handed either the bare ``TimeSource`` (simulate mode)
    or the ``TimeAuthority`` drop-in (auto mode) without either being a subclass of the other; both
    satisfy this structurally, which is exactly what lets the authority be a transparent stand-in.
    """

    def advance(self, current: datetime, dt_s: float) -> datetime: ...


class PhysicsEngine:
    """Pure position/clock integrator — no threading, so it is deterministically testable."""

    def __init__(self, movement_mode: str, time_source: _Clock) -> None:
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


# --- single-source ZDA carve-out (auto mode, GPS channel only) --------------------


def _formatter_of(line: str) -> str:
    """The 3-char NMEA formatter of a ``$``-sentence address (``$GPRMC`` -> ``RMC``), else ``""``.

    A cheap slice (no full parse) mirroring ``classify.sentence_class`` — enough for the ZDA
    carve-out to tell an RMC from a ZDA on a winning line without paying for a pynmea2 parse.
    """
    if not line or line[0] != "$":
        return ""
    address = line[1:].partition(",")[0]
    return address[2:5] if len(address) >= 5 else ""


class ZdaCarveout:
    """Synthesize a ZDA for the GPS channel when the winning GNSS source sends RMC but no ZDA.

    On a real bus a receiver may emit RMC (which carries a full date+time) yet no standalone ZDA.
    Building a ZDA from the projected sim clock would risk a ZDA whose time diverges from the
    winning source's RMC — the single-source invariant (R2) forbids that split. So this watches the
    WINNING gnss line and, only while the source itself has sent no ZDA, emits a ZDA built from that
    RMC's EXACT parsed time, so time and position can never come apart on the wire.

    It is an exemption from passthrough SUPPRESSION only, never from the channel-OFF gate (R55): the
    worker invokes it *after* forwarding a winning line, and ``_on_passthrough`` already returns
    early when the channel is disabled, so a synthesized ZDA is naturally silent on an OFF channel.
    """

    def __init__(self, talker: str) -> None:
        self._talker = talker
        self._seen_zda = False
        self._winner: str | None = None

    def on_forward(self, input_id: str, line: str) -> list[str]:
        """Return synthesized ZDA line(s) to inject after ``line`` (a winning gnss line)."""
        # A change of winning source resets the "has this source sent a ZDA?" memory, so a new
        # source that does send its own ZDA is never shadowed by the previous source's history.
        if input_id != self._winner:
            self._winner = input_id
            self._seen_zda = False
        formatter = _formatter_of(line)
        if formatter == "ZDA":
            # The source sends its own ZDA -> the caller already forwarded it; add nothing so we
            # never double up on the wire, and remember not to synthesize for this source.
            self._seen_zda = True
            return []
        if formatter == "RMC" and not self._seen_zda:
            utc = rx.parse_time(line)
            if utc is not None:
                return [zda_from_datetime(self._talker, utc)]
        return []


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
        router: Router | None = None,
        zda_carveout: ZdaCarveout | None = None,
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
        # Single-source ZDA synthesis, GPS channel only (None on every other worker and in simulate
        # mode). When set, it runs on the WORKER thread after a winning gnss line is forwarded, so
        # the synthesized ZDA is injected by the same single writer — preserving the per-channel
        # single-writer invariant — and inherits this channel's OFF gate for free.
        self._zda_carveout = zda_carveout
        # AUTO-mode wiring. ``router`` is None in simulate mode, in which case the inbox is never
        # fed and every branch below that consults the router is dead — the worker runs its
        # generation schedule exactly as before. ``_channel_class`` is the sentence class this
        # channel's role consumes (None for roles that consume none, e.g. instrument -> never
        # suppressed). ``_inbox`` carries classified passthrough tuples (or ``_STOP``); bounded to
        # match ``status_q`` so a stalled worker cannot grow it without limit (a full inbox drops
        # the line with an ``inbox_full`` status — R50).
        self._router = router
        self._channel_class = router.channel_class(spec.id) if router is not None else None
        self._inbox: queue.Queue[object] = queue.Queue(maxsize=10000)
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
        #
        # ONE path for both modes (R40). The worker sleeps by *blocking on its inbox* until the
        # soonest emitter is due; a timeout (``queue.Empty``) means "nothing arrived, run the due
        # schedule" — byte-identical to the old sleep-then-fire loop when the inbox is never fed
        # (simulate mode). A real message is a classified passthrough tuple handled out-of-band; it
        # does NOT touch the emit schedule, so ``next_fire`` stays pure absolute-monotonic and
        # generation timing is unaffected by passthrough arrivals. ``_STOP`` breaks immediately even
        # when the inbox is otherwise quiet — a plain ``stop_event.wait`` could not be interrupted
        # by a queue put, which is the bug this redesign fixes.
        start = time.monotonic()
        offsets = emission_offsets([em.period for em in self._emitters])
        for em, off in zip(self._emitters, offsets, strict=True):
            em.next_fire = start + off
        while not self._stop_event.is_set():
            now = time.monotonic()
            soonest = min(em.next_fire for em in self._emitters)
            wait = max(0.0, soonest - now)
            try:
                msg: object | None = self._inbox.get(timeout=wait)
            except queue.Empty:
                msg = None
            if msg is _STOP:
                break
            if msg is None:  # timeout -> run the due-emitter schedule exactly as before
                now = time.monotonic()
                for em in self._emitters:
                    if em.next_fire <= now:
                        self._fire(em)
                        em.next_fire = advance_next_fire(em.next_fire, em.period, now)
            else:  # a classified passthrough tuple (input_id, cls, line)
                self._on_passthrough(cast("tuple[str, str, str]", msg))

    # -- emission -----------------------------------------------------------
    def _fire(self, em: _Emitter) -> None:
        # Checked before generation so a muted channel costs nothing and suppresses every
        # consumer at once — serial, TCP tap and the web monitor all hang off _fan_out.
        if not self._enabled.is_set():
            return
        # Suppress generation while a live source is winning this channel's class: passthrough owns
        # the bus this tick, so the generator must not also fire (they would double up on the wire).
        # Instrument channels have channel_class None -> never suppressed; simulate has router None.
        if self._router is not None and self._channel_class is not None:
            now = time.monotonic()
            if self._router.any_live(self._spec.id, self._channel_class, now):
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

    # -- passthrough (auto mode) -------------------------------------------
    def enqueue(self, msg: object) -> None:
        """Hand a classified passthrough tuple (or ``_STOP``) to this worker's inbox.

        Called from the engine's RX-dispatch thread (single producer per message kind). On a full
        inbox the line is DROPPED with a best-effort ``inbox_full`` status — a logged gap, never a
        block, so a wedged worker cannot back-pressure the input reader (R50).
        """
        try:
            self._inbox.put_nowait(msg)
        except queue.Full:
            self._emit_status("inbox_full", self._spec.id)

    def _on_passthrough(self, msg: tuple[str, str, str]) -> None:
        # Single-writer per channel: only this worker thread ever calls ``_fan_out`` for this
        # channel, so the winner check + forward is atomic with respect to generation.
        input_id, cls, line = msg
        # OFF beats everything, including live passthrough (R9/R55): a disabled channel is silent.
        if not self._enabled.is_set():
            return
        now = time.monotonic()
        if self._router is not None and input_id == self._router.winner(self._spec.id, cls, now):
            self._inject(line)
            self._feed_passthrough_state(line)
            # Single-source ZDA carve-out (GPS channel only): if the winning source sent an RMC but
            # no ZDA, synthesize one from the RMC's exact time and inject it here on the WORKER
            # thread, so time and position never split and the single-writer invariant holds.
            if self._zda_carveout is not None:
                for synth in self._zda_carveout.on_forward(input_id, line):
                    self._inject(synth)
        # else: a higher-priority source is currently live -> drop this line.

    def _inject(self, line: str) -> None:
        """Forward a winning source's line VERBATIM to every sink (and the monitor)."""
        self._fan_out(line)
        self._emitted += 1
        self._last_emit = time.monotonic()

    def _feed_passthrough_state(self, line: str) -> None:
        # Seed shared state from the live line so that when the source dies the generator resumes
        # from the last real values (seamless failover). NO rx_accept whitelist here: in auto the
        # router is the trust boundary and the line is already checksum-verified. Unparseable lines
        # are simply not seeded (they still went out verbatim above).
        with contextlib.suppress(pynmea2.ParseError):
            changes = rx.parse_line(line)
            if changes:
                self._shared.update(**changes)

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
        now = time.monotonic()
        age = None if self._last_emit is None else now - self._last_emit
        if not self._enabled.is_set():
            source = "OFF"
        elif self._router is not None and self._channel_class is not None:
            source = self._router.source_label(self._spec.id, self._channel_class, now)
        else:
            source = "SIM"
        return ChannelHealth(
            channel_id=self._spec.id,
            alive=self._alive,
            emitted=self._emitted,
            build_errors=self._build_errors,
            sinks=[SinkHealth(s.name, s.down, s.errors) for s in self._sinks],
            last_emit_age_s=age,
            enabled=self._enabled.is_set(),
            source=source,
        )


# --- active-diagnostics port gating (R17) -----------------------------------------


def targetable_slots(config: EngineConfig) -> set[str]:
    """Input-slot ids eligible as an active-diagnostics target (send/loopback/baud-sweep).

    The R17 rule, expressed once and shared by the engine accessor and the web layer: a slot is a
    legal target ONLY if its :class:`InputSpec` declares ``function == "unused"`` AND no channel
    names it in ``sources``. Everything carrying real traffic — an assigned input, an input a
    channel draws from, an output channel — is excluded, so a bench action can never drive a wire
    the running config depends on. Pure config read; empty when nothing qualifies.
    """
    referenced = {src for ch in config.channels for src in ch.sources}
    return {
        inp.id for inp in config.inputs if inp.function == "unused" and inp.id not in referenced
    }


def port_is_operational(config: EngineConfig, slot: str) -> bool:
    """Whether ``slot`` names an operationally in-use port that an active action must refuse.

    True for any output-channel id, any input slot with a real ``function`` (not ``"unused"``), or
    any input a channel references in ``sources``. False for an unused/unreferenced input slot (a
    legal target) and for an unknown id (which the caller still refuses as non-targetable). This is
    the security-sensitive half of R17: an operational port is never a send/loopback/sweep target.
    """
    if any(ch.id == slot for ch in config.channels):
        return True
    referenced = {src for ch in config.channels for src in ch.sources}
    for inp in config.inputs:
        if inp.id == slot:
            return inp.function != "unused" or inp.id in referenced
    return False


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

        # AUTO mode gets an arbiter that classifies input lines and picks winners; simulate mode
        # has none, so every worker's router-consulting branch is inert and the inbox is never fed.
        self._router = Router(config) if config.mode == "auto" else None

        # Single-source Time Authority (auto only): wraps the base TimeSource + the SAME router that
        # feeds the GPS output, so the GNSS winner supplies BOTH position and time and they can
        # never split across sources. In simulate mode it stays None and PhysicsEngine keeps driving
        # the bare TimeSource, byte-for-byte as before — no NtpSync/authority is even built.
        self._time_authority: TimeAuthority | None = None
        clock: _Clock = self._time_source
        if config.mode == "auto" and self._router is not None:
            # The GPS output channel id whose GNSS winner also owns the clock ("" if none defined).
            gps_channel_id = next((c.id for c in config.channels if c.role == "gps"), "")
            # GNSS-capable inputs tagged by function: gps->"gps", sat->"sat"; others omitted so
            # note_time ignores them and only a real GNSS wire can ever become the time source.
            input_tag = {
                i.id: ("gps" if i.function == "gps" else "sat")
                for i in config.inputs
                if i.function in ("gps", "sat")
            }
            self._time_authority = TimeAuthority(
                self._time_source, self._router, gps_channel_id, input_tag, NtpSync()
            )
            clock = self._time_authority

        self._physics_engine = PhysicsEngine(config.movement.mode, clock)
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
            # Only the GPS channel in auto mode carries a ZDA carve-out; every other worker (and all
            # of simulate mode) gets None, so the carve-out branch in _on_passthrough stays inert.
            carveout = (
                ZdaCarveout(spec.talker or "GP")
                if config.mode == "auto" and spec.role == "gps"
                else None
            )
            self._workers.append(
                _ChannelWorker(
                    spec,
                    source,
                    sinks,
                    self._shared,
                    self._status,
                    self._stop_event,
                    monitor,
                    self._router,
                    carveout,
                )
            )
        self._worker_by_id = {w.channel_id: w for w in self._workers}

        # AUTO mode: one input reader per InputSpec. Each is a receive-only serial port whose
        # verified lines are dispatched to the router, which decides the target channel. Kept
        # separate from ``_startables`` (the sinks) because start ORDER matters: readers must come
        # up only after their target workers are draining (R50), see ``start``.
        self._input_readers: list[SerialPort] = []
        # AUTO-only bench diagnostics: one rolling per-input PortDiagnostics (keyed by input id,
        # scored against that input's declared baud) fed from the reader's raw-bytes tap. Read-only
        # — it observes the exact same chunks the dispatcher already processes and changes no
        # emission, framing, or liveness. Empty in simulate mode, so simulate is untouched.
        self._diagnostics: dict[str, PortDiagnostics] = {}
        # Optional bounded raw captures, one active session per input at most (web layer enforces
        # max-concurrent + total-data quota). Guarded because the tee runs on the reader thread
        # while start/stop run on request handlers.
        self._captures: dict[str, CaptureSession] = {}
        self._capture_residual: dict[str, bytes] = {}
        self._capture_lock = threading.Lock()
        if config.mode == "auto":
            for inp in config.inputs:
                self._diagnostics[inp.id] = PortDiagnostics(inp.id, inp.baud)
                self._input_readers.append(
                    SerialPort(
                        inp.path,
                        inp.baud,
                        framing=inp.framing,
                        direction="rx",
                        read_timeout=inp.read_timeout_s,
                        on_rx=self._make_dispatch(inp.id),
                        on_raw=self._make_raw_feed(inp.id),
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

    def _make_dispatch(self, input_id: str) -> Callable[[str], None]:
        """Bind an input id to the RX dispatcher so a reader's lines route to the right channel."""

        def dispatch(line: str) -> None:
            self._dispatch_rx(input_id, line)

        return dispatch

    def _dispatch_rx(self, input_id: str, line: str) -> None:
        """Route one verified input line to its target channel's inbox.

        The cross-platform test seam: ``test_auto_mode`` drives this directly with fabricated lines,
        no serial/pty needed (R35). The router classifies + records liveness + names the target
        channel; the engine only enqueues. All arbitration (winner selection, suppression) is the
        router's and the owning worker's job — this method never touches a sink.
        """
        if self._router is None:
            return
        now = time.monotonic()
        routed = self._router.note_rx(input_id, line, now)
        if routed is not None:
            target_id, cls, routed_line = routed
            worker = self._worker_by_id.get(target_id)
            if worker is not None:
                worker.enqueue((input_id, cls, routed_line))
        # ALSO feed the single-source Time Authority: parse the wall-clock instant off a
        # time-bearing GNSS sentence (RMC/ZDA) and stamp a fix. note_time ignores non-GNSS inputs,
        # and parse_time returns None for sentences without a full date+time, so this is safe to run
        # on every line. A genuine ParseError is suppressed exactly as the RX path already does.
        if self._time_authority is not None:
            utc: datetime | None = None
            with contextlib.suppress(pynmea2.ParseError):
                utc = rx.parse_time(line)
            if utc is not None:
                self._time_authority.note_time(input_id, utc, now)

    def _make_raw_feed(self, input_id: str) -> Callable[[bytes], None]:
        """Bind an input id to its PortDiagnostics + capture tee for the reader's raw-bytes tap.

        Runs on the input reader thread for every chunk, before the dispatcher sees it. It only
        folds bytes into the rolling scorer and, if a capture is armed for this slot, tees complete
        lines into it — never touches a sink, the router, state, or liveness.
        """
        diag = self._diagnostics[input_id]

        def feed(chunk: bytes) -> None:
            now = time.monotonic()
            diag.feed_bytes(chunk, now)
            self._tee_capture(input_id, chunk, now)

        return feed

    def _tee_capture(self, input_id: str, chunk: bytes, now: float) -> None:
        """Append newline-complete lines of ``chunk`` to this slot's active capture, if any.

        Bounded (R28): a per-input residual holds at most one in-flight partial line, flushed
        verbatim once it exceeds ``_MAX_CAPTURE_RESIDUAL`` (a newline-less runaway). When the
        session trips its own byte/wall-clock cap it auto-stops and is dropped from the registry.
        """
        with self._capture_lock:
            cap = self._captures.get(input_id)
            if cap is None or not cap.active:
                return
            buf = self._capture_residual.get(input_id, b"") + chunk
            *lines, residual = buf.split(b"\n")
            if len(residual) > _MAX_CAPTURE_RESIDUAL:
                lines.append(residual)
                residual = b""
            self._capture_residual[input_id] = residual
            for line in lines:
                if not cap.write_line(line + b"\n", now):
                    self._captures.pop(input_id, None)
                    self._capture_residual.pop(input_id, None)
                    break

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
        # START ORDER (R50): sinks -> workers -> input readers. Open the I/O sinks (serial ports,
        # TCP taps) first so the first sentence has somewhere to go and taps accept subscribers;
        # start the channel workers (the inbox drainers) next; only THEN start the input readers,
        # so no reader can enqueue a passthrough line before its target worker is draining.
        for startable in self._startables:
            start = getattr(startable, "start", None)
            if callable(start):
                start()
        self._physics.start()
        for worker in self._workers:
            worker.start()
        for reader in self._input_readers:
            reader.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop_event.set()
        # Wake any worker blocked in ``inbox.get`` at once — a queue put interrupts the block a
        # bare ``stop_event`` could not (R40). Best-effort: a full inbox still drains on the
        # ``stop_event`` guard at the top of the loop.
        for worker in self._workers:
            worker.enqueue(_STOP)
        # Close input readers first so no further lines are dispatched into workers that are winding
        # down; then join physics + workers; then tear down the sinks.
        for reader in self._input_readers:
            with contextlib.suppress(Exception):
                reader.close()
        # Flush + close any active raw captures so their files land intact on shutdown (the tee
        # thread is winding down; do this after the readers stop so no further lines race in).
        with self._capture_lock:
            sessions = list(self._captures.values())
            self._captures.clear()
            self._capture_residual.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                session.stop()
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

    def time_source(self) -> str:
        """The tier currently supplying the clock: gps/sat/ntp/system/simulated/hold.

        In auto mode this is the Time Authority's last resolved tag (thread-safe str read); in
        simulate mode there is no authority, so it is a static label off the configured clock
        (``system_utc`` -> ``"system"``, else the mode verbatim). Web surfacing lands in Phase C.
        """
        if self._time_authority is not None:
            return self._time_authority.source_tag()
        mode = self._config.time_source.mode
        return "system" if mode == "system_utc" else mode

    def input_status(self) -> list[dict[str, object]]:
        """One read-only entry per configured input slot for the web ``/api/inputs`` view.

        Each entry is ``{"id", "function", "detected_class", "live"}``. In auto mode the detection
        pair comes from the router's per-input liveness: if any sentence class is currently live on
        the slot, ``detected_class`` is that class and ``live`` is True; else ``None``/False. In
        simulate mode there is no router, so every slot reports ``detected_class=None, live=False``.
        The device path is deliberately withheld here — the web layer owns the info-leak decision
        about what of a slot is safe to reveal (R19), and it never gets the path from this surface.
        """
        now = time.monotonic()
        entries: list[dict[str, object]] = []
        for inp in self._config.inputs:
            detected = (
                self._router.live_class_for_input(inp.id, now) if self._router is not None else None
            )
            entries.append(
                {
                    "id": inp.id,
                    "function": inp.function,
                    "detected_class": detected,
                    "live": detected is not None,
                }
            )
        return entries

    def diagnostics_snapshot(self) -> list[dict[str, object]]:
        """One rolling PortDiagnostics snapshot per input slot (read-only; auto mode only).

        Ordered to match ``config.inputs``. Empty in simulate mode and whenever no inputs are
        configured — there are no per-input scorers to report. Purely observational: reading it
        never touches emission, the router, or state.
        """
        now = time.monotonic()
        return [
            self._diagnostics[inp.id].snapshot(now)
            for inp in self._config.inputs
            if inp.id in self._diagnostics
        ]

    def is_operational_port(self, slot: str) -> bool:
        """R17 accessor: whether ``slot`` is an operational port an active action must refuse."""
        return port_is_operational(self._config, slot)

    def targetable_slots(self) -> set[str]:
        """R17 accessor: the set of input-slot ids eligible as an active-diagnostics target."""
        return targetable_slots(self._config)

    def start_capture(
        self, slot: str, *, data_dir: str, max_bytes: int, max_seconds: float
    ) -> dict[str, object]:
        """Arm a bounded raw capture on an input slot; the reader tee then fills it.

        Server-owned lifecycle: the caller never supplies a filename (the session generates one
        under ``data_dir``). Returns the session status. Idempotent — re-arming an already-active
        slot returns the running session unchanged. Raises ``KeyError`` for a slot that is not a
        read input (nothing feeds a capture on a port the engine does not read).
        """
        with self._capture_lock:
            existing = self._captures.get(slot)
            if existing is not None and existing.active:
                return existing.status()
            if slot not in self._diagnostics:
                raise KeyError(slot)
            session = CaptureSession(
                slot,
                data_dir,
                time.monotonic(),
                max_bytes=max_bytes,
                max_seconds=max_seconds,
            )
            self._captures[slot] = session
            self._capture_residual[slot] = b""
            return session.status()

    def stop_capture(self, slot: str) -> dict[str, object] | None:
        """Stop and deregister the capture on ``slot``; return its final status, or None if none."""
        with self._capture_lock:
            session = self._captures.pop(slot, None)
            self._capture_residual.pop(slot, None)
        if session is None:
            return None
        session.stop()
        return session.status()

    def capture_status(self) -> list[dict[str, object]]:
        """Status of every registered capture session (read-only snapshot)."""
        with self._capture_lock:
            sessions = list(self._captures.values())
        return [s.status() for s in sessions]

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
