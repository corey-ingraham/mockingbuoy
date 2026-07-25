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
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import pynmea2
from geographiclib.geodesic import Geodesic

from . import budget, rx
from .ais_generator import AisGenerator
from .classify import CLASS_TO_ROLE, sentence_class
from .config import AisSpec, ChannelSpec, EngineConfig, RouteSpec, TimeSourceSpec
from .diagnostics import CaptureSession, PortDiagnostics
from .gps_generator import GpsGenerator, zda_from_datetime
from .heading_generator import HeadingGenerator
from .instrument_generator import InstrumentGenerator
from .navigation import dead_reckon, knots_to_mps
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

# WGS-84 geodesic, shared for the route driver's bearing/distance to the active waypoint (the same
# model ``navigation.dead_reckon`` uses to step along that bearing, so steer and step stay in sync).
_GEOD = Geodesic.WGS84


def _bearing_and_distance(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> tuple[float, float]:
    """Initial great-circle bearing (degrees, 0..360) and distance (metres) from p1 to p2."""
    g = _GEOD.Inverse(lat1, lon1, lat2, lon2)
    return g["azi1"] % 360.0, g["s12"]


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
    """Expand a channel's config into scheduled emitters (periods in seconds).

    A per-sentence ``EmitSpec.enabled == False`` frees its slot: the emitter is not scheduled (F4),
    so a disabled sentence stops emitting and returns its baud budget. Absent/``True`` keeps every
    listed sentence exactly as before, so pre-switch configs are unchanged.
    """
    if spec.role == "ais":
        # AIS models its position rate via the first emit entry. If every configured emit is
        # explicitly disabled the channel goes silent (no position, no static); with none configured
        # the historical 0.2 Hz position default stands.
        active = [e for e in spec.emit if e.enabled]
        if spec.emit and not active:
            return []
        pos_rate = active[0].rate_hz if active else 0.2
        out = [_Emitter(AIS_POSITION, 1.0 / pos_rate)]
        if spec.ais is not None and spec.ais.include_type5:
            out.append(_Emitter(AIS_STATIC, spec.ais.type5_period_s))
        return out
    return [_Emitter(e.sentence, 1.0 / e.rate_hz) for e in spec.emit if e.enabled]


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


@dataclass(frozen=True)
class _ReplayLine:
    """Inbox marker for a replayed capture line (replay mode).

    Distinct from a passthrough tuple so the worker loop can tell the two apart: a passthrough tuple
    is arbitrated against the router (winner check), a ``_ReplayLine`` is injected unconditionally
    (the capture is the winner by construction) subject only to the channel OFF gate.
    """

    line: str


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


class _RouteDriver:
    """Stateful waypoint-playback cursor (simulate mode only), owned by the physics thread.

    Holds the mutable route progress that :meth:`PhysicsEngine.advance` must stay free of — the
    active waypoint index and the paused/finished flags — so ``advance`` remains a pure function of
    its inputs. Each physics tick calls :meth:`step`, which returns the ``(cog_deg, sog_kn)`` that
    steer own-ship toward the active waypoint; ``advance`` then does the actual dead-reckoning along
    that heading. The cursor is advanced to the next waypoint once own-ship comes within a single
    tick's travel of the active one, which stops dead-reckoning from overshooting and oscillating.
    A small lock guards the cursor so the control seam (start/pause/reset) and the progress readout
    can be served straight from a request handler without racing the physics thread.
    """

    def __init__(self, route: RouteSpec) -> None:
        self._waypoints = list(route.waypoints)
        self._speed_kn = route.speed_kn
        self._loop = route.loop
        self._lock = threading.Lock()
        self._index = 0
        self._paused = False
        self._finished = False

    def control(self, op: str) -> bool:
        """Apply a ``start``/``pause``/``reset`` op (a flag write); ``False`` on an unknown op."""
        with self._lock:
            if op == "start":
                self._paused = False
            elif op == "pause":
                self._paused = True
            elif op == "reset":
                self._index = 0
                self._paused = False
                self._finished = False
            else:
                return False
        return True

    def progress(self) -> dict[str, object]:
        """A thread-safe snapshot of route progress for the state/health surface."""
        with self._lock:
            n = len(self._waypoints)
            return {
                "active_waypoint": self._index,
                "waypoint_count": n,
                "fraction": (self._index / n) if n else 0.0,
                "paused": self._paused,
                "finished": self._finished,
            }

    def step(self, lat: float, lon: float, dt_s: float) -> tuple[float, float] | None:
        """Return the ``(cog_deg, sog_kn)`` to steer toward the active waypoint this tick.

        Returns ``None`` when the route is not driving own-ship — paused, finished, or fewer than
        two waypoints — so the caller holds position. Advances the cursor (then wraps when ``loop``,
        else finishes) once own-ship is within one tick's travel of the active waypoint.
        """
        with self._lock:
            if self._paused or self._finished or len(self._waypoints) < 2:
                return None
            if self._index >= len(self._waypoints):  # defensive: out-of-range cursor -> done
                self._finished = True
                return None
            target = self._waypoints[self._index]
            bearing, distance_m = _bearing_and_distance(lat, lon, target[0], target[1])
            step_m = knots_to_mps(self._speed_kn) * dt_s
            if distance_m <= step_m:
                # Arrived (within a tick's travel): advance the cursor, then steer to the next.
                self._index += 1
                if self._index >= len(self._waypoints):
                    if self._loop:
                        self._index = 0
                    else:
                        self._finished = True
                        return None
                target = self._waypoints[self._index]
                bearing, _distance = _bearing_and_distance(lat, lon, target[0], target[1])
            return bearing, self._speed_kn


class _PhysicsThread(threading.Thread):
    def __init__(
        self,
        shared: SharedState,
        physics: PhysicsEngine,
        hz: float,
        stop: threading.Event,
        route: _RouteDriver | None = None,
        replay_mode: bool = False,
    ) -> None:
        super().__init__(name="physics", daemon=True)
        self._shared = shared
        self._physics = physics
        self._period = 1.0 / hz
        self._stop_event = stop
        # Route driver (simulate route playback) and replay flag are both None/False in the common
        # case, in which case ``run`` is byte-identical to the pre-feature tick (see guards below).
        self._route = route
        self._replay_mode = replay_mode

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
            snap = self._shared.snapshot()
            state = snap
            route_changes: dict[str, object] = {}
            if self._route is not None:
                # Route playback steers cog/sog toward the active waypoint; advance dead-reckons
                # with those values. Kept OUT of advance so it stays pure (no cursor, no globals).
                steer = self._route.step(snap.lat, snap.lon, dt)
                if steer is not None:
                    cog, sog = steer
                    route_changes = {"cog_deg": cog, "sog_kn": sog}
                    state = replace(snap, cog_deg=cog, sog_kn=sog)
                else:  # paused/finished/too-few-waypoints -> hold position (sog 0, no dead-reckon)
                    route_changes = {"sog_kn": 0.0}
                    state = replace(snap, sog_kn=0.0)
            changes = self._physics.advance(state, dt)
            if self._replay_mode:
                # Replay owns own-ship position and the clock (from the capture); physics adds
                # only cosmetic sea-state motion, so a replayed track is never double-integrated.
                for owned in ("utc", "lat", "lon"):
                    changes.pop(owned, None)
            if route_changes:
                changes = {**route_changes, **changes}
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
        suppress_generation: bool = False,
    ) -> None:
        super().__init__(name=f"channel-{spec.id}", daemon=True)
        self._spec = spec
        self._source = source
        # Replay mode: the capture file is the source of truth, so no channel generates — it only
        # injects replayed lines handed to its inbox. False in simulate/auto, so those are intact.
        self._suppress_generation = suppress_generation
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
            elif isinstance(msg, _ReplayLine):  # replay mode: inject a captured line verbatim
                self._on_replay(msg.line)
            else:  # a classified passthrough tuple (input_id, cls, line)
                self._on_passthrough(cast("tuple[str, str, str]", msg))

    # -- emission -----------------------------------------------------------
    def _fire(self, em: _Emitter) -> None:
        # Checked before generation so a muted channel costs nothing and suppresses every
        # consumer at once — serial, TCP tap and the web monitor all hang off _fan_out.
        if not self._enabled.is_set():
            return
        # Replay mode suppresses ALL generation: the capture file is the source of truth, so this
        # channel only injects replayed lines (via _on_replay). Inert (False) in simulate/auto.
        if self._suppress_generation:
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

    def _on_replay(self, line: str) -> None:
        """Inject a replayed capture line VERBATIM (replay mode).

        The capture file is the winner by construction, so there is NO router/winner check — but the
        channel OFF gate still wins (R55): a disabled channel stays silent. Runs on the WORKER
        thread, the sole writer for this channel, so replay preserves the per-channel single-writer
        invariant exactly as passthrough does. Own-ship state and the clock are seeded by the replay
        reader (see :class:`_ReplayThread`); here the worker only emits.
        """
        if not self._enabled.is_set():
            return
        self._inject(line)

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


# --- replay source (mode == "replay") ---------------------------------------------


class _ReplayThread(threading.Thread):
    """Reads an NMEA capture and re-injects each line with its original inter-line timing.

    Replay mode only. It NEVER touches a sink: each line is classified and handed to the owning
    channel worker's inbox (the same single-writer path auto-mode passthrough uses), so the worker
    thread stays the ONLY writer per channel. Inter-line timing is derived from the capture's own
    time-bearing sentences (RMC/ZDA) and scaled by ``speed``; the bursts of non-time sentences
    between two timestamps are injected back-to-back, reproducing a receiver's per-second cadence.
    Own-ship position/heading (via ``rx.parse_line``) and the clock (via ``rx.parse_time``) are
    seeded from the replayed lines; replayed time is applied to ``state.utc`` directly and is EXEMPT
    from the monotonic clamp the live GNSS path uses. Every sleep is interruptible via the shared
    ``stop_event``; at EOF it restarts when ``loop`` else exits.
    """

    def __init__(
        self,
        file: str,
        loop: bool,
        speed: float,
        worker_by_class: dict[str, _ChannelWorker],
        shared: SharedState,
        stop: threading.Event,
    ) -> None:
        super().__init__(name="replay", daemon=True)
        self._file = file
        self._loop = loop
        # A non-positive speed would divide-by-zero / stall the pacing; clamp to real-time.
        self._speed = speed if speed > 0.0 else 1.0
        self._worker_by_class = worker_by_class
        self._shared = shared
        self._stop_event = stop

    def run(self) -> None:
        # A vanished/again-unreadable file just ends the thread quietly (validate proved it existed
        # at start; nothing else the reader can do mid-run). Emission on other paths is unaffected.
        with contextlib.suppress(OSError):
            while not self._stop_event.is_set():
                self._play_once()
                if not self._loop:
                    return

    def _play_once(self) -> None:
        prev_ts: datetime | None = None
        with open(self._file, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if self._stop_event.is_set():
                    return
                line = raw.strip()
                if not line:
                    continue
                prev_ts = self._pace(line, prev_ts)
                if self._stop_event.is_set():
                    return
                self._dispatch(line)

    def _pace(self, line: str, prev_ts: datetime | None) -> datetime | None:
        """Sleep the inter-line gap derived from time-bearing sentences; return the new ``prev_ts``.

        Only RMC/ZDA carry a full wall-clock. Between two such sentences we wait their timestamp
        delta (scaled by speed); a backwards or zero delta waits not at all. A line with no
        parseable time leaves ``prev_ts`` unchanged (its burst rides with the preceding timestamp).
        """
        ts: datetime | None = None
        with contextlib.suppress(pynmea2.ParseError):
            ts = rx.parse_time(line)
        if ts is None:
            return prev_ts
        if prev_ts is not None:
            delay = (ts - prev_ts).total_seconds() / self._speed
            if delay > 0.0:
                self._stop_event.wait(delay)  # interruptible: a stop wakes the reader at once
        return ts

    def _dispatch(self, line: str) -> None:
        """Route one replayed line to its class-matched worker + seed own-ship state.

        Lines that do not classify, or whose class has no configured channel, are DROPPED. A routed
        line seeds the shared snapshot (position/heading via ``rx.parse_line``; clock via
        ``rx.parse_time``, NO monotonic clamp) and is then enqueued to the owning worker, which
        injects it on its own thread — the reader never writes a sink, so single-writer holds.
        """
        cls = sentence_class(line)
        if cls is None:
            return
        worker = self._worker_by_class.get(cls)
        if worker is None:
            return
        with contextlib.suppress(pynmea2.ParseError):
            changes = rx.parse_line(line)
            if changes:
                self._shared.update(**changes)
        with contextlib.suppress(pynmea2.ParseError):
            utc = rx.parse_time(line)
            if utc is not None:
                self._shared.update(utc=utc)  # replayed clock: exempt from monotonic clamp (R51)
        worker.enqueue(_ReplayLine(line))


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

        # Scope C: replay "scope" decides who owns own-ship. "full" (the default, and every
        # non-replay mode) keeps the capture as the entire source of truth — own-ship AND AIS are
        # replayed and every generator is suppressed. "ais-only" replays just the AIS contacts while
        # own-ship is SIMULATED (route/physics own its position, gps/heading channels generate its
        # nav). Read once so the four seams below agree; both flags are False outside replay mode.
        replay_scope = config.replay.scope if config.replay is not None else "full"
        ais_only = config.mode == "replay" and replay_scope == "ais-only"
        full_replay = config.mode == "replay" and not ais_only

        # Route playback (F1): simulate mode, or replay in "ais-only" scope where own-ship is
        # simulated too. Opt-in; cross-field preconditions are enforced in ``validate``; we build
        # the driver only when actually enabled, so a disabled/absent route leaves the physics tick
        # byte-identical.
        self._route_driver: _RouteDriver | None = None
        if (
            (config.mode == "simulate" or ais_only)
            and config.route is not None
            and config.route.enabled
        ):
            self._route_driver = _RouteDriver(config.route)

        self._physics_engine = PhysicsEngine(config.movement.mode, clock)
        self._physics = _PhysicsThread(
            self._shared,
            self._physics_engine,
            config.movement.physics_hz,
            self._stop_event,
            route=self._route_driver,
            # Physics owns own-ship lat/lon/utc only under FULL replay; under ais-only own-ship is
            # simulated, so physics dead-reckons normally (replay_mode False).
            replay_mode=full_replay,
        )

        # Sinks that own I/O resources (serial ports, TCP taps) and must be started before
        # emission begins. Duck-typed on a ``start()`` method.
        self._startables: list[object] = []

        self._workers: list[_ChannelWorker] = []
        for spec in config.channels:
            if ais_only and spec.role == "ais" and spec.ais is not None:
                # Under ais-only the ONLY non-own-ship contacts must be the replayed ones, so the
                # synthetic-traffic spawner is forced off at construction. This is a build-time
                # override on a copy — the saved config is never mutated. Own-ship AIVDO is still
                # generated (the ais channel is NOT suppressed under ais-only).
                source: SentenceSource = _AisSource(replace(spec.ais, traffic=None))
            else:
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
                    # FULL replay suppresses generation on every channel — the capture is the source
                    # of truth and channels only inject replayed lines. Under ais-only NO channel is
                    # suppressed (own-ship nav is generated); False in simulate/auto.
                    suppress_generation=full_replay,
                )
            )
        self._worker_by_id = {w.channel_id: w for w in self._workers}

        # Replay source (F2): mode=='replay' only. One reader thread paces the capture and hands
        # each line to the worker whose role matches the line's class (gnss->gps, heading->heading,
        # ais->ais); classes with no configured channel are dropped. None outside replay mode.
        self._replay_thread: _ReplayThread | None = None
        if config.mode == "replay" and config.replay is not None and config.replay.enabled:
            channel_by_role = {ch.role: ch.id for ch in config.channels}
            worker_by_class: dict[str, _ChannelWorker] = {}
            # Under ais-only ONLY the 'ais' class is routed from the capture — replayed gnss/heading
            # lines are dropped, because own-ship nav is generated locally. Full replay routes all.
            routed_classes = {"ais": CLASS_TO_ROLE["ais"]} if ais_only else CLASS_TO_ROLE
            for cls, role in routed_classes.items():
                cid = channel_by_role.get(role)
                if cid is not None and cid in self._worker_by_id:
                    worker_by_class[cls] = self._worker_by_id[cid]
            self._replay_thread = _ReplayThread(
                config.replay.file,
                config.replay.loop,
                config.replay.speed,
                worker_by_class,
                self._shared,
                self._stop_event,
            )

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
        # Replay reader starts LAST, after every worker is draining, so no replayed line is enqueued
        # before its target worker can inject it (mirrors the input-reader ordering rule, R50).
        if self._replay_thread is not None:
            self._replay_thread.start()

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
        # The replay reader (if any) is joined first so it stops enqueuing before the workers wind
        # down; its sleeps are on ``stop_event.wait`` which the ``set`` above has already released.
        replay_threads = [self._replay_thread] if self._replay_thread is not None else []
        for thread in (*replay_threads, self._physics, *self._workers):
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

    def route_control(self, op: str) -> bool:
        """Runtime route-playback control (``start``/``pause``/``reset``).

        A cheap flag write on the route cursor — no worker or thread is touched, so it is served
        straight from a request handler. Returns ``False`` when no route is active (not replay/
        simulate route mode) or the op is unknown, so the web layer can 4xx a no-op cleanly.
        """
        if self._route_driver is None:
            return False
        return self._route_driver.control(op)

    def route_status(self) -> dict[str, object] | None:
        """Current route-playback progress (active waypoint / count / fraction / flags), or ``None``
        when no route is active — the read side of the F1 progress surface for the state dict."""
        return None if self._route_driver is None else self._route_driver.progress()

    def health(self) -> HealthReport:
        channels = [w.health() for w in self._workers]
        physics_alive = self._physics.is_alive()
        ok = (
            physics_alive
            and all(c.alive for c in channels)
            and all(not s.down for c in channels for s in c.sinks)
        )
        return HealthReport(ok=ok, physics_alive=physics_alive, channels=channels)
