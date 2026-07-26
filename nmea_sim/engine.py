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
import math
import queue
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, SupportsFloat, cast

import pynmea2
from geographiclib.geodesic import Geodesic

from . import budget, rx
from .ais_generator import AisGenerator
from .classify import CLASS_TO_ROLE, sentence_class
from .config import (
    AisSpec,
    ChannelSpec,
    DepthSimSpec,
    EngineConfig,
    HeadingSimSpec,
    RouteSpec,
    RudderSimSpec,
    TimeSourceSpec,
    WindSimSpec,
    effective_depth_sim,
    effective_heading_sim,
    effective_rudder_sim,
    effective_wind_sim,
)
from .depthsim import depth_sim
from .depthsim import depth_sim as depth_sim_fn  # alias for use where a param shadows ``depth_sim``
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
from .steeringsim import heading_sim, rudder_sim
from .tcp_tap import TcpTap
from .timeauthority import TimeAuthority
from .windsim import wind_sim
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

# A worker with no scheduled emitters (an all-disabled emit list, or a receive-only channel) has
# nothing to fire, so it blocks on its inbox with this fixed timeout instead of computing ``min()``
# over an empty sequence (H2) — the thread stays alive to inject replay/passthrough lines and to
# observe ``_STOP``, and the timeout just bounds how often it re-checks ``stop_event``.
_IDLE_POLL_S = 1.0

# pynmea2 converts fields lazily, so a *checksum-valid* line carrying a garbage field raises a plain
# ``ValueError``/``TypeError``/``AttributeError`` right past a ``ParseError``-only guard
# (``pynmea2.ParseError`` subclasses ``ValueError``, but ``suppress(ParseError)`` never catches the
# parent). Every ``rx.parse_*`` call site widens to this tuple (belt-and-suspenders alongside the
# total ``rx`` contract) so one bad wire line degrades to skip-and-continue, never killing the
# channel worker, input reader, or replay thread (H1).
_RX_PARSE_ERRORS = (pynmea2.ParseError, ValueError, TypeError, AttributeError)

# Formatters that carry a full wall-clock (``parse_time``) and, respectively, own-ship state fields
# (``parse_line``). Gating the parse on the cheap ``_formatter_of`` slice skips a full pynmea2 parse
# on every line that could not contribute — most importantly every high-rate ``!AIVDM``/heading line
# that ``parse_time`` would otherwise parse-and-discard (EFF1). Behaviour is unchanged: the parse
# functions already return ``None``/``{}`` for these sentences.
_TIME_FORMATTERS = frozenset({"RMC", "ZDA"})
_STATE_FORMATTERS = frozenset({"RMC", "GGA", "VTG", "HDT", "HDG"})

# A device may emit ZDA only transiently. If the winning GNSS source falls silent on ZDA for longer
# than this, the "source sends its own ZDA" latch expires so synthesis resumes (DOM10) — otherwise a
# transiently-ZDA device loses its ZDA on the wire forever.
_ZDA_LATCH_EXPIRY_S = 10.0


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
            lines = self._gen.static(own, own_ship=True)
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
        # A non-positive ``type5_period_s`` is rejected by validate; defensively skip it here so a
        # hand-edited config cannot flood the wire (period<=0 fires every tick) or divide by zero in
        # the budget guard (M6). No static reports is a safe degradation from a bad period.
        if spec.ais is not None and spec.ais.include_type5 and spec.ais.type5_period_s > 0:
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
    # Aliveness of the background threads that can go silently dark (H6). ``True`` when the mode has
    # no such thread (no replay thread; no input readers), so they only ever subtract from ``ok``.
    # A replay/reader thread that died while it should still be running flips these — and ``ok`` —
    # to False, so a dead-but-green report is impossible.
    replay_alive: bool = True
    inputs_alive: bool = True


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
    """Pure position/clock integrator — no threading, so it is deterministically testable.

    ``_heading_setpoint`` (hook-updated by :meth:`set_heading_setpoint`) and ``_depth_offset``
    (construction-time) are scalars held CONSTANT across any single :meth:`advance` call, so advance
    stays pure — identical state + clock give identical output. A manual heading update between two
    calls re-centres the wander, which is intended and not part of the purity test.
    """

    def __init__(
        self,
        movement_mode: str,
        time_source: _Clock,
        *,
        depth_sim: DepthSimSpec | None = None,
        initial_depth_m: float = 0.0,
        initial_utc_ts: float = 0.0,
        rudder_sim: RudderSimSpec | None = None,
        heading_sim: HeadingSimSpec | None = None,
        initial_heading_deg: float = 0.0,
        wind_sim: WindSimSpec | None = None,
    ) -> None:
        self._mode = movement_mode
        self._time = time_source
        # The ALREADY-effective depth spec (or None). None keeps every existing construction
        # byte-identical: no depth_m write, so depth_m tracks whatever the initial state / RX seam
        # set it to, exactly as before.
        self._depth_sim = depth_sim
        # Construction-time depth offset so depth STARTS at the configured depth and drifts smoothly
        # from there (subtract the sim's own t0 value so the first tick lands on initial_depth_m).
        self._depth_offset = 0.0
        if depth_sim is not None and depth_sim.enabled:
            self._depth_offset = (
                depth_sim_fn(depth_sim.base_depth_m, initial_utc_ts, depth_sim) - initial_depth_m
            )
        self._rudder_sim = rudder_sim
        self._heading_sim = heading_sim
        # Mutable single float (GIL-atomic); the heading wander is centred here, refreshed by the
        # update_state hook when a manual edit sets heading_true_deg.
        self._heading_setpoint = initial_heading_deg
        # ALREADY-effective wind spec (or None). None keeps wind_speed_kn/wind_dir_deg untouched
        # exactly as before (initial state / RX seam owns them).
        self._wind_sim = wind_sim

    def set_heading_setpoint(self, deg: float) -> None:
        """Re-centre the heading wander (called from the manual-edit hook)."""
        self._heading_setpoint = deg

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
        # Optional deterministic depth-under-keel: when enabled, drive the wire-backed depth_m off
        # the same absolute clock (new_utc.timestamp()) the pitch/roll write uses, so advance stays
        # pure and deterministic. depth_m is wire-backed, so DPT/DBT and the depth chart track it.
        if self._depth_sim is not None and self._depth_sim.enabled:
            # Re-apply the floor AFTER the offset subtraction: depth_sim() floors its own sum at
            # min_depth_m, but subtracting the construction-time offset can push the result back
            # below the floor (deeply negative for a shallow seeded base), which would emit invalid
            # DPT/DBT on the wire. Clamp again so depth_m can never go below min_depth_m.
            changes["depth_m"] = max(
                self._depth_sim.min_depth_m,
                depth_sim(self._depth_sim.base_depth_m, new_utc.timestamp(), self._depth_sim)
                - self._depth_offset,
            )
        # Optional helm-hold oscillation and heading wander, off the same absolute clock so advance
        # stays pure. Route gating is NOT here — it lives in _PhysicsThread._tick (a route owns the
        # helm and pops these). Depth runs regardless of route.
        if self._rudder_sim is not None and self._rudder_sim.enabled:
            changes["rudder_angle_deg"] = rudder_sim(new_utc.timestamp(), self._rudder_sim)
        if self._heading_sim is not None and self._heading_sim.enabled:
            ht = heading_sim(self._heading_setpoint, new_utc.timestamp(), self._heading_sim)
            changes["heading_true_deg"] = ht
            changes["heading_mag_deg"] = (ht - state.mag_variation_deg) % 360.0
        # Optional true-wind drift off the same absolute clock (advance stays pure). Wind is not
        # helm, so it is NOT route-gated; the apparent wind / MWV / MWD recompute from these.
        # Deliberately NO construction-time offset (unlike depth): wind has no canonical instant
        # value, so the gust/veer stay symmetric around the configured mean instead of being shifted
        # to land on it at t0 (which would skew the window to one side). The one-frame transient at
        # Start is bounded by the (modest) amplitudes.
        if self._wind_sim is not None and self._wind_sim.enabled:
            ws, wd = wind_sim(new_utc.timestamp(), self._wind_sim)
            changes["wind_speed_kn"] = ws
            changes["wind_dir_deg"] = wd
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


def _through_water_speed(sog_kn: float, cog_deg: float, set_deg: float, drift_kn: float) -> float:
    """Speed through water = |ground velocity - current(set/drift)|.

    Equals SOG when there is no current (the common case), so a route driving SOG
    also gives a matching STW instead of leaving the log reading zero.
    """
    if not drift_kn:
        return sog_kn
    cog_r = math.radians(cog_deg)
    set_r = math.radians(set_deg)
    ve = sog_kn * math.sin(cog_r) - drift_kn * math.sin(set_r)
    vn = sog_kn * math.cos(cog_r) - drift_kn * math.cos(set_r)
    return math.hypot(ve, vn)


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
        # Set if a tick raises (e.g. a non-numeric ``time_source.rate``); the thread then ends
        # cleanly so ``is_alive()`` — and therefore ``HealthReport.physics_alive`` and ``ok`` — goes
        # False, surfacing the stall instead of freezing every channel behind a bare traceback (M7).
        self._error: str | None = None

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
            try:
                self._tick(dt)
            except Exception as exc:
                # A bad clock model must not kill physics silently and freeze every channel (M7).
                # Record it for health and stop advancing; the thread ends cleanly (see _error).
                self._error = f"physics_error: {exc!r}"
                return
            next_tick = advance_next_fire(next_tick, self._period, now)

    def _tick(self, dt: float) -> None:
        snap = self._shared.snapshot()
        state = snap
        route_changes: dict[str, object] = {}
        if self._route is not None:
            # Route playback steers cog/sog toward the active waypoint; advance dead-reckons
            # with those values. Kept OUT of advance so it stays pure (no cursor, no globals).
            steer = self._route.step(snap.lat, snap.lon, dt)
            if steer is not None:
                cog, sog = steer
                # keep speed-through-water in step with the route's ground speed (SOG w/o current)
                stw = _through_water_speed(sog, cog, snap.set_deg, snap.drift_kn)
                route_changes = {"cog_deg": cog, "sog_kn": sog, "stw_kn": stw}
                state = replace(snap, cog_deg=cog, sog_kn=sog, stw_kn=stw)
            else:  # paused/finished/too-few-waypoints -> hold position (sog 0, no dead-reckon)
                stw = _through_water_speed(0.0, snap.cog_deg, snap.set_deg, snap.drift_kn)
                route_changes = {"sog_kn": 0.0, "stw_kn": stw}
                state = replace(snap, sog_kn=0.0, stw_kn=stw)
        changes = self._physics.advance(state, dt)
        if self._replay_mode:
            # Replay owns own-ship position and the clock (from the capture); physics adds
            # only cosmetic sea-state motion, so a replayed track is never double-integrated.
            for owned in ("utc", "lat", "lon"):
                changes.pop(owned, None)
        if self._route is not None:
            # A route owns the helm; drop sim-authored heading/rudder so they never fight the route
            # (same carve-out shape as replay above, keyed on the route DRIVER existing — NOT on
            # route_changes truthiness, which stays {"sog_kn":0.0} when paused/finished). Depth is
            # NOT popped — it runs under a route too.
            for owned in ("heading_true_deg", "heading_mag_deg", "rudder_angle_deg"):
                changes.pop(owned, None)
        if route_changes:
            changes = {**route_changes, **changes}
        self._shared.update(**changes)


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


def _finite_in_range(value: object, lo: float, hi: float | None) -> bool:
    """True when ``value`` is a finite real number in ``[lo, hi]`` (``hi=None`` = unbounded)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if not math.isfinite(value):
        return False
    if value < lo:
        return False
    return hi is None or value <= hi


def _sanitize_state_changes(changes: dict[str, float]) -> dict[str, float]:
    """Drop state changes that would poison own-ship position/motion before they are applied (H9).

    A checksum-valid line can still carry a non-finite or out-of-range value: a ``NaN`` sog survives
    failover (``state.sog_kn != 0.0`` is True for ``NaN``) and a bad lat/lon propagates into
    dead-reckoning and then crashes every GGA/RMC build. This gate runs right before
    ``SharedState.update`` on the RX and replay seams and skips only the offending field(s) — a good
    field on the same line still applies (degrade by count+skip, never crash). Hemisphere-present
    handling lives in ``rx`` (the parser omits a blank-hemisphere fix); here we range/finite-gate.
    """
    clean = dict(changes)
    if "lat" in clean or "lon" in clean:
        lat_ok = _finite_in_range(clean.get("lat"), -90.0, 90.0)
        lon_ok = _finite_in_range(clean.get("lon"), -180.0, 180.0)
        if not (lat_ok and lon_ok):
            # lat/lon are a pair; a bad half invalidates the fix, so drop both.
            clean.pop("lat", None)
            clean.pop("lon", None)
    if "sog_kn" in clean and not _finite_in_range(clean.get("sog_kn"), 0.0, None):
        clean.pop("sog_kn", None)
    # Defensive sweep: any remaining non-finite numeric value is dropped so it can never
    # reach state.
    for key in list(clean):
        value = clean[key]
        if isinstance(value, float) and not math.isfinite(value):
            clean.pop(key, None)
    return clean


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
        # time.monotonic() of the last ZDA the winning source sent, so the latch below can expire.
        self._last_zda_at = 0.0

    def on_forward(self, input_id: str, line: str, now: float | None = None) -> list[str]:
        """Return synthesized ZDA line(s) to inject after ``line`` (a winning gnss line)."""
        if now is None:
            now = time.monotonic()
        # A change of winning source resets the "has this source sent a ZDA?" memory, so a new
        # source that does send its own ZDA is never shadowed by the previous source's history.
        if input_id != self._winner:
            self._winner = input_id
            self._seen_zda = False
        # Expire the latch (DOM10): a device that once sent a ZDA but has since gone quiet on
        # ZDA for longer than the window resumes synthesis, instead of losing ZDA on the wire
        # permanently.
        if self._seen_zda and now - self._last_zda_at > _ZDA_LATCH_EXPIRY_S:
            self._seen_zda = False
        formatter = _formatter_of(line)
        if formatter == "ZDA":
            # The source sends its own ZDA -> the caller already forwarded it; add nothing so we
            # never double up on the wire, and remember not to synthesize for this source.
            self._seen_zda = True
            self._last_zda_at = now
            return []
        if formatter == "RMC" and not self._seen_zda:
            # A checksum-valid RMC with a garbage time field must not kill the worker (H1).
            utc: datetime | None = None
            with contextlib.suppress(*_RX_PARSE_ERRORS):
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
            if self._emitters:
                now = time.monotonic()
                soonest = min(em.next_fire for em in self._emitters)
                wait = max(0.0, soonest - now)
            else:
                # No scheduled emitters (all-disabled emit list, or an rx-only channel): there is
                # nothing to fire, so block on the inbox with a fixed timeout instead of computing
                # min() over an empty sequence (H2). The worker stays alive to inject replay/
                # passthrough lines and to observe _STOP; the due-emitter loop below is a no-op.
                wait = _IDLE_POLL_S
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
                for synth in self._zda_carveout.on_forward(input_id, line, now):
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
        if _formatter_of(line) not in _STATE_FORMATTERS:
            return  # non-state sentence (e.g. AIVDM) never seeds state; skip the parse (EFF1)
        # Widen past ParseError (H1): a checksum-valid line with a garbage field raises plain
        # ValueError/TypeError/AttributeError and would otherwise kill this channel's only writer.
        with contextlib.suppress(*_RX_PARSE_ERRORS):
            changes = _sanitize_state_changes(rx.parse_line(line))  # H9 finite/range gate
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
        status_q: queue.Queue[StatusMsg] | None = None,
    ) -> None:
        super().__init__(name="replay", daemon=True)
        self._file = file
        self._loop = loop
        # A non-positive speed would divide-by-zero / stall the pacing; clamp to real-time.
        self._speed = speed if speed > 0.0 else 1.0
        self._worker_by_class = worker_by_class
        self._shared = shared
        self._stop_event = stop
        self._status = status_q
        # Health surface (H6): distinguish a thread that finished a non-loop capture cleanly from
        # one that died on an I/O error. ``_error`` set == the wire went silent and health
        # must fail.
        self._error: str | None = None
        self._finished = False

    def run(self) -> None:
        # Per-iteration OSError handling (H6): a vanished/again-unreadable file mid-run records a
        # replay_error and stops reporting healthy, instead of silently dying under a whole-loop
        # suppress while the health tile stays green (the replay thread is the sole producer in full
        # replay). Emission on other paths is unaffected.
        try:
            while not self._stop_event.is_set():
                try:
                    self._play_once()
                except OSError as exc:
                    self._fail(f"replay_error: {exc!r}")
                    return
                if not self._loop:
                    self._finished = True
                    return
        except Exception as exc:  # never a bare traceback out of the replay thread; surface it
            self._fail(f"replay_error: {exc!r}")

    def _fail(self, detail: str) -> None:
        self._error = detail
        if self._status is not None:
            with contextlib.suppress(queue.Full):
                self._status.put_nowait(
                    StatusMsg("replay", "replay_error", detail, time.monotonic())
                )

    def healthy(self) -> bool:
        """True while replay is working: running, or a non-loop capture finished cleanly.

        A thread that died on an I/O error (``_error`` set) is NOT healthy — that is the dead-but-
        green case H6 makes disqualifying. A clean EOF on a non-loop capture is healthy.
        """
        return self._error is None and (self.is_alive() or self._finished)

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
        if _formatter_of(line) in _TIME_FORMATTERS:  # skip a full parse on non-time lines (EFF1)
            with contextlib.suppress(*_RX_PARSE_ERRORS):  # one bad field must not kill replay (H1)
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
        # Gate each parse on the cheap formatter slice (EFF1) and widen past ParseError so one bad
        # field in a real capture can't kill replay mid-file (H1).
        fmt = _formatter_of(line)
        if fmt in _STATE_FORMATTERS:
            with contextlib.suppress(*_RX_PARSE_ERRORS):
                changes = _sanitize_state_changes(rx.parse_line(line))  # H9 finite/range gate
                if changes:
                    self._shared.update(**changes)
        if fmt in _TIME_FORMATTERS:
            with contextlib.suppress(*_RX_PARSE_ERRORS):
                utc = rx.parse_time(line)
                if utc is not None:
                    self._shared.update(
                        utc=utc
                    )  # replayed clock: exempt from monotonic clamp (R51)
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


def _reader_alive(reader: SerialPort) -> bool:
    """Whether an input reader's RX thread is still running (H6).

    A receive-only :class:`SerialPort` runs its checksum/dispatch loop on an internal daemon thread;
    if that thread dies the slot goes silent while the port object lingers, and health must see it.
    The port exposes no thread-liveness flag, so we read its reader thread directly; a reader that
    has not been started yet (``None``) counts as alive-enough (it cannot be dead).
    """
    thread = reader._reader  # noqa: SLF001 - no public aliveness accessor on SerialPort
    return thread is None or thread.is_alive()


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
        input_monitor: Callable[[str, str], None] | None = None,
        strict_budget: bool = True,
    ) -> None:
        self._config = config
        self._monitor = monitor
        self._input_monitor = input_monitor
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

        # Resolve the effective sim specs and seed values from the initial state. The helpers return
        # None outside simulate mode, so auto/replay get no sim writes (live RX / replayed data is
        # never overwritten). Route driver is still built exactly as above (route section).
        _init = self._shared.snapshot()
        self._physics_engine = PhysicsEngine(
            config.movement.mode,
            clock,
            depth_sim=effective_depth_sim(config, _init.depth_m),
            initial_depth_m=_init.depth_m,
            initial_utc_ts=_init.utc.timestamp(),
            rudder_sim=effective_rudder_sim(config),
            heading_sim=effective_heading_sim(config),
            initial_heading_deg=_init.heading_true_deg,
            wind_sim=effective_wind_sim(config, _init.wind_speed_kn, _init.wind_dir_deg),
        )
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

        # Optional CONSOLIDATED tap: one shared TcpTap that EVERY channel fans out to, so a single
        # client sees the full merged NMEA stream (the multiplexer feed). Built and registered ONCE;
        # ``_build_sinks`` appends a per-channel _Sink wrapping this shared writer. TcpTap's
        # write_line is lock-guarded, so concurrent fan-in from all worker threads is safe.
        self._aggregate_tap: TcpTap | None = None
        self._aggregate_tap_name = ""
        if config.aggregate_tap is not None and config.aggregate_tap.enabled:
            self._aggregate_tap = TcpTap(config.tcp_tap_host, config.aggregate_tap.port)
            self._aggregate_tap_name = f"aggregate_tap:{config.aggregate_tap.port}"
            self._register_startable(self._aggregate_tap)

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
                self._status,
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
                        on_line=self._make_input_line_feed(inp.id),
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
        clean = _sanitize_state_changes(changes)  # H9: never let a non-finite/out-of-range field in
        if clean:
            self._shared.update(**clean)

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
        # time-bearing GNSS sentence (RMC/ZDA) and stamp a fix. Gate on the cheap formatter
        # slice so a full pynmea2 parse is skipped on every non-time line (every high-rate
        # AIVDM/heading line) — the single biggest CPU win (EFF1); behaviour is unchanged since
        # parse_time returns None for those anyway. The guard is widened past ParseError so a
        # garbage field can't kill the reader thread (H1).
        if self._time_authority is not None and _formatter_of(line) in _TIME_FORMATTERS:
            utc: datetime | None = None
            with contextlib.suppress(*_RX_PARSE_ERRORS):
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

    def _make_input_line_feed(self, input_id: str) -> Callable[[str], None]:
        """Rate-limited on_line tap: forward complete received lines (incl. malformed) to the web
        layer via input_monitor, capped by a per-port token bucket so a flood can't swamp the SSE
        bus. Bucket state is a closure touched only by this port's single reader thread (no lock).
        Uses a token bucket (capacity/burst 20, refill ~20/s) NOT a min-interval, so per-second NMEA
        bursts are preserved rather than collapsed to one line."""
        capacity = 20.0
        refill_per_s = 20.0
        tokens = capacity
        last = time.monotonic()

        def feed(line: str) -> None:
            nonlocal tokens, last
            monitor = self._input_monitor
            if monitor is None:
                return
            now = time.monotonic()
            tokens = min(capacity, tokens + (now - last) * refill_per_s)
            last = now
            if tokens < 1.0:
                return  # empty bucket -> drop silently
            tokens -= 1.0
            with contextlib.suppress(Exception):
                monitor(input_id, line)  # a slow/broken web sink must never break the reader

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
        sinks: list[_Sink] = []
        # A tap-only channel carries NO backend writer: it publishes solely over its TCP tap (a
        # software feed with no serial adapter). Skipping the backend sink keeps the global serial
        # backend from trying to open a port it doesn't have — which would mark the channel down
        # and flip /healthz to 503. Validation guarantees an enabled tcp_tap exists in this case.
        if not spec.tap_only:
            backend_writer = self._make_backend_writer(spec)
            self._register_startable(backend_writer)
            sinks.append(_Sink(self._config.writer_backend, backend_writer))
        if spec.tcp_tap is not None and spec.tcp_tap.enabled:
            tap = TcpTap(self._config.tcp_tap_host, spec.tcp_tap.port)
            self._register_startable(tap)
            sinks.append(_Sink(f"tcp_tap:{spec.tcp_tap.port}", tap))
        # The consolidated aggregate tap (if enabled) is a SHARED writer: every channel gets its
        # own _Sink wrapper (per-channel down/error state) around the one TcpTap, already
        # registered once at construction — do NOT re-register (it would start/close N times).
        if self._aggregate_tap is not None:
            sinks.append(_Sink(self._aggregate_tap_name, self._aggregate_tap))
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
            if em.period <= 0.0:
                continue  # a non-positive period can't be rate-budgeted (validate rejects it) — M6
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
        """Apply an external state edit (the web control seam).

        A manual heading edit re-centres the heading-sim wander (via the setpoint hook) instead of
        being fought by it next tick; rudder/depth manual edits are simply overwritten by their sim
        while it is on — the intended "driven" behaviour, surfaced to the UI by grey-out.
        """
        if "heading_true_deg" in changes:
            # The manual-edit payload carries a validated number here; cast narrows the ``object``
            # value so the float coercion type-checks (mypy) without changing runtime behaviour.
            self._physics_engine.set_heading_setpoint(
                float(cast("SupportsFloat", changes["heading_true_deg"]))
            )
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
        # Fold the background threads that can go silently dark into ``ok`` (H6). ``replay_alive``
        # is True when there is no replay thread or it is doing its job; ``inputs_alive`` is True
        # when every configured input reader's RX thread is still running (or not yet started).
        replay_alive = self._replay_thread is None or self._replay_thread.healthy()
        inputs_alive = all(_reader_alive(r) for r in self._input_readers)
        ok = (
            physics_alive
            and replay_alive
            and inputs_alive
            and all(c.alive for c in channels)
            and all(not s.down for c in channels for s in c.sinks)
        )
        return HealthReport(
            ok=ok,
            physics_alive=physics_alive,
            channels=channels,
            replay_alive=replay_alive,
            inputs_alive=inputs_alive,
        )
