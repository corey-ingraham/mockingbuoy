"""Engine: emission rate, clean shutdown, physics motion, fan-out isolation, budget guard."""

from __future__ import annotations

import queue
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

import pytest
from geographiclib.geodesic import Geodesic
from pyais import decode

from nmea_sim.config import (
    AisOwnShip,
    AisSpec,
    ChannelSpec,
    EmitSpec,
    EngineConfig,
    HeadingSimSpec,
    MovementSpec,
    RouteSpec,
    RudderSimSpec,
    TimeSourceSpec,
)
from nmea_sim.engine import (
    _STOP,
    BudgetExceeded,
    Engine,
    PhysicsEngine,
    StatusMsg,
    TimeSource,
    ZdaCarveout,
    _ChannelWorker,
    _InstrumentSource,
    _PhysicsThread,
    _ReplayLine,
    _ReplayThread,
    _RouteDriver,
    _sanitize_state_changes,
    _Sink,
    advance_next_fire,
    build_source,
    emission_offsets,
    emitters_for,
)
from nmea_sim.gps_generator import GpsGenerator
from nmea_sim.ntpsync import NtpSync
from nmea_sim.router import Router
from nmea_sim.state import SharedState, VesselState
from nmea_sim.tcp_tap import TcpTap
from nmea_sim.timeauthority import TimeAuthority


def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 5.0, interval: float = 0.005
) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses; return its final value.

    A bounded wait on a definite condition — the project's approved alternative to a fixed
    wall-clock sleep followed by a fragile count-band assertion (the anti-flake rule): it returns
    the instant the condition holds and only fails (returns False) on genuine breakage.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


_INITIAL = {
    "lat": 10.1,
    "lon": -30.5,
    "sog_kn": 10.0,
    "cog_deg": 90.0,
    "heading_true_deg": 92.0,
    "heading_mag_deg": 105.0,
    "mag_variation_deg": -13.0,
    "altitude_m": 0.0,
    "fix_quality": 1,
    "satellites": 10,
    "hdop": 0.8,
}


class CollectingWriter:
    """Thread-safe sink that records every line it receives."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._lock = threading.Lock()

    def write_line(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.lines)

    def close(self) -> None:
        return None


class BoomWriter:
    """Sink that always raises — exercises per-sink failure isolation."""

    def write_line(self, line: str) -> None:
        raise RuntimeError("boom")

    def close(self) -> None:
        return None


def _config(
    channels: list[ChannelSpec],
    *,
    movement: MovementSpec | None = None,
    time_source: TimeSourceSpec | None = None,
) -> EngineConfig:
    return EngineConfig(
        writer_backend="null",
        movement=movement or MovementSpec(mode="static", physics_hz=20.0),
        time_source=time_source or TimeSourceSpec(mode="system_utc"),
        initial_state_raw=dict(_INITIAL),
        channels=channels,
    )


def _gps_channel(rate: float = 5.0) -> ChannelSpec:
    # 38400 baud so these fast functional emissions sit within budget; the budget
    # guard itself is covered separately by the heading over-budget test.
    return ChannelSpec(
        id="gps",
        role="gps",
        path="none",
        baud=38400,
        talker="GP",
        emit=[EmitSpec("GGA", rate), EmitSpec("RMC", rate)],
    )


# --- pure physics ----------------------------------------------------------------


def test_physics_advance_moves_along_cog() -> None:
    ts = TimeSource(TimeSourceSpec(mode="hold"), None)
    physics = PhysicsEngine("underway", ts)
    state = VesselState(**_INITIAL, utc=ts.initial())
    changes = physics.advance(state, dt_s=3600.0)  # one hour at 10 kn ≈ 10 nm

    inv = Geodesic.WGS84.Inverse(state.lat, state.lon, changes["lat"], changes["lon"])
    assert inv["s12"] == pytest.approx(10 * 1852, rel=0.01)  # 10 nm in metres
    assert inv["azi1"] == pytest.approx(90.0, abs=0.5)  # heading due east (COG)


def test_physics_static_holds_position() -> None:
    ts = TimeSource(TimeSourceSpec(mode="hold"), None)
    physics = PhysicsEngine("static", ts)
    state = VesselState(**_INITIAL, utc=ts.initial())
    changes = physics.advance(state, dt_s=3600.0)
    assert "lat" not in changes and "lon" not in changes


def test_physics_advance_emits_pitch_and_roll() -> None:
    ts = TimeSource(TimeSourceSpec(mode="hold"), None)
    physics = PhysicsEngine("static", ts)
    state = VesselState(**_INITIAL, sea_state=4, utc=datetime(2024, 1, 1, tzinfo=UTC))
    changes = physics.advance(state, dt_s=0.1)
    assert "pitch_deg" in changes
    assert "roll_deg" in changes


def test_physics_pitch_roll_vary_over_time_and_stay_pure() -> None:
    ts = TimeSource(TimeSourceSpec(mode="hold"), None)
    physics = PhysicsEngine("static", ts)

    # Purity: with a held clock, advancing the same state twice yields identical output.
    base = VesselState(**_INITIAL, sea_state=5, utc=datetime(2024, 1, 1, tzinfo=UTC))
    first = physics.advance(base, dt_s=0.1)
    second = physics.advance(base, dt_s=0.1)
    assert first["pitch_deg"] == second["pitch_deg"]
    assert first["roll_deg"] == second["roll_deg"]

    # Non-constant: at sea_state > 0 the motion model varies across distinct clock values.
    pitches = set()
    rolls = set()
    for sec in range(12):
        s = VesselState(**_INITIAL, sea_state=5, utc=datetime(2024, 1, 1, 0, 0, sec, tzinfo=UTC))
        ch = physics.advance(s, dt_s=0.1)
        pitches.add(ch["pitch_deg"])
        rolls.add(ch["roll_deg"])
    assert len(pitches) > 1
    assert len(rolls) > 1


def test_advance_writes_sim_heading_and_rudder_when_enabled() -> None:
    """With the steering sims enabled, ``advance`` emits ``heading_true_deg``/``heading_mag_deg``
    and ``rudder_angle_deg`` every tick (the writes the route pop later strips). Key presence is
    deterministic regardless of the sinusoid phase."""
    ts = TimeSource(TimeSourceSpec(mode="hold"), None)
    physics = PhysicsEngine(
        "static",
        ts,
        rudder_sim=RudderSimSpec(enabled=True),
        heading_sim=HeadingSimSpec(enabled=True),
        initial_heading_deg=92.0,
    )
    state = VesselState(**_INITIAL, utc=ts.initial())
    changes = physics.advance(state, dt_s=0.1)
    assert "heading_true_deg" in changes
    assert "heading_mag_deg" in changes
    assert "rudder_angle_deg" in changes


def test_route_driver_suppresses_sim_heading_and_rudder_writes() -> None:
    """When a route driver EXISTS the physics tick drops the sim-authored heading/rudder so they
    never fight the route (gated on the driver existing, not on ``route_changes`` truthiness). The
    route still owns cog/sog; depth is intentionally NOT popped."""
    ts = TimeSource(TimeSourceSpec(mode="hold"), None)
    physics = PhysicsEngine(
        "static",
        ts,
        rudder_sim=RudderSimSpec(enabled=True),
        heading_sim=HeadingSimSpec(enabled=True),
        initial_heading_deg=92.0,  # matches _INITIAL["heading_true_deg"]
    )
    shared = SharedState(VesselState(**_INITIAL, utc=ts.initial()))
    before = shared.snapshot()
    # A route far to the north so the driver returns a live steer (cog/sog) this tick.
    route = _RouteDriver(
        RouteSpec(
            enabled=True,
            waypoints=[(11.0, -30.5), (12.0, -30.5)],
            speed_kn=6.0,
            loop=False,
        )
    )
    thread = _PhysicsThread(shared, physics, hz=10.0, stop=threading.Event(), route=route)
    thread._tick(0.1)
    after = shared.snapshot()

    # Sim-authored helm/heading were popped -> unchanged from the pre-tick state.
    assert after.heading_true_deg == pytest.approx(before.heading_true_deg)
    assert after.heading_mag_deg == pytest.approx(before.heading_mag_deg)
    assert after.rudder_angle_deg == pytest.approx(before.rudder_angle_deg)
    # The route driver did run and own the course (proving the pop was meaningful, not a no-op).
    assert after.sog_kn == pytest.approx(6.0)


def test_advance_next_fire_accumulates_period_with_no_drift() -> None:
    """Steady on-time ticks must accumulate exactly ``period`` each step, with zero drift
    over many iterations (no floating point creep, no resync triggered)."""
    period = 0.25
    start = 100.0
    next_fire = start
    for i in range(1, 5000):
        # `now` sits exactly at the previous next_fire, i.e. the scheduler is never behind.
        now = next_fire
        next_fire = advance_next_fire(next_fire, period, now)
        assert next_fire == pytest.approx(start + i * period, abs=1e-9)


def test_advance_next_fire_resyncs_once_when_far_behind() -> None:
    """A ``now`` far past ``next_fire`` (the scheduler fell behind by many periods) resyncs
    to exactly ``now + period`` in a single step — never a catch-up burst of missed fires."""
    period = 1.0
    next_fire = 10.0
    now = 500.0  # ~490 periods behind
    resynced = advance_next_fire(next_fire, period, now)
    assert resynced == pytest.approx(now + period)

    # A second call starting from the resynced value, with `now` unchanged, does not
    # resync again (it advances normally by one more period).
    again = advance_next_fire(resynced, period, now)
    assert again == pytest.approx(resynced + period)


def test_advance_next_fire_returns_naive_advance_when_on_time() -> None:
    assert advance_next_fire(next_fire=10.0, period=2.0, now=9.0) == pytest.approx(12.0)


def test_emission_offsets_spread_equal_periods() -> None:
    offsets = emission_offsets([1.0, 1.0, 1.0, 1.0])
    assert offsets == [0.0, 0.25, 0.5, 0.75]
    assert len(set(offsets)) == 4  # no two emissions fire at the same instant


# --- per-sentence enable (F4) ----------------------------------------------------


def test_emitters_for_skips_disabled_emit() -> None:
    spec = ChannelSpec(
        id="gps",
        role="gps",
        path="none",
        baud=38400,
        talker="GP",
        emit=[EmitSpec("GGA", 1.0), EmitSpec("RMC", 1.0, enabled=False)],
    )
    assert [e.sentence for e in emitters_for(spec)] == ["GGA"]  # the disabled RMC is not scheduled


def test_engine_does_not_emit_a_disabled_sentence() -> None:
    """A per-sentence disable stops that sentence at the source: the channel keeps emitting its
    enabled sentence while the disabled one never reaches a sink."""
    collector = CollectingWriter()
    gps = ChannelSpec(
        id="gps",
        role="gps",
        path="none",
        baud=38400,
        talker="GP",
        emit=[EmitSpec("GGA", 20.0), EmitSpec("RMC", 20.0, enabled=False)],
    )
    engine = Engine(_config([gps]), sink_hook=lambda spec: [collector])
    engine.start()
    time.sleep(0.4)
    engine.stop()

    lines = collector.snapshot()
    assert any(line.startswith("$GPGGA") for line in lines)  # the enabled sentence flows
    assert not any(line.startswith("$GPRMC") for line in lines)  # the disabled one never emits


# --- route driver (F1, pure/deterministic — no threads) --------------------------


def _driver(
    waypoints: list[tuple[float, float]], *, speed_kn: float = 600.0, loop: bool = False
) -> _RouteDriver:
    return _RouteDriver(RouteSpec(enabled=True, waypoints=waypoints, speed_kn=speed_kn, loop=loop))


def test_route_driver_steers_toward_active_waypoint() -> None:
    driver = _driver([(0.0, 0.0), (0.0, 1.0)])
    # Well short of waypoint 0 (at the origin), which lies due east of the current position.
    steer = driver.step(0.0, -0.5, 1.0)
    assert steer is not None
    cog, sog = steer
    assert cog == pytest.approx(90.0, abs=1.0)  # steered due east toward the active waypoint
    assert sog == pytest.approx(600.0)  # driven at the configured speed
    assert driver.progress()["active_waypoint"] == 0  # still short of it, cursor unmoved


def test_route_driver_progress_is_surfaced() -> None:
    driver = _driver([(0.0, 0.0), (0.0, 1.0), (0.0, 2.0)])
    progress = driver.progress()
    assert progress["active_waypoint"] == 0
    assert progress["waypoint_count"] == 3
    assert progress["fraction"] == pytest.approx(0.0)
    assert progress["paused"] is False
    assert progress["finished"] is False


def test_route_driver_pause_holds_position_and_reset_rewinds() -> None:
    driver = _driver([(0.0, 0.0), (0.0, 0.001)])
    # Stepping through waypoint 0 (we start on it) advances the cursor to waypoint 1.
    driver.step(0.0, 0.0, 1.0)
    assert driver.progress()["active_waypoint"] == 1

    assert driver.control("pause") is True
    assert driver.step(0.0, 0.0005, 1.0) is None  # paused -> no steering, caller holds position
    assert driver.progress()["paused"] is True

    assert driver.control("reset") is True
    reset = driver.progress()
    assert reset["active_waypoint"] == 0  # cursor rewound to the first waypoint
    assert reset["paused"] is False
    assert reset["finished"] is False


def test_route_driver_finishes_at_last_waypoint_without_loop() -> None:
    driver = _driver([(0.0, 0.0), (0.0, 0.001)], loop=False)
    driver.step(0.0, 0.0, 1.0)  # consume waypoint 0, steer to waypoint 1
    assert driver.step(0.0, 0.001, 1.0) is None  # arriving at the last waypoint finishes the route
    assert driver.progress()["finished"] is True


def test_route_driver_loops_back_to_first_waypoint() -> None:
    driver = _driver([(0.0, 0.0), (0.0, 0.001)], loop=True)
    driver.step(0.0, 0.0, 1.0)  # -> cursor at waypoint 1
    steer = driver.step(0.0, 0.001, 1.0)  # arrive at last -> wrap to waypoint 0
    assert steer is not None
    cog, _sog = steer
    assert cog == pytest.approx(270.0, abs=1.0)  # now steering back west toward waypoint 0
    assert driver.progress()["active_waypoint"] == 0


def test_route_driver_rejects_unknown_op() -> None:
    driver = _driver([(0.0, 0.0), (0.0, 1.0)])
    assert driver.control("frobnicate") is False


# --- running engine --------------------------------------------------------------


def test_engine_emits_at_configured_rate() -> None:
    collector = CollectingWriter()
    cfg = _config([_gps_channel(rate=5.0)])
    engine = Engine(cfg, sink_hook=lambda spec: [collector])
    engine.start()
    try:
        # Two sentences at 5 Hz => ~10 lines/s. Wait (bounded) until a full second's worth has
        # flowed — a definite condition that holds the moment emission works — instead of sleeping a
        # fixed wall-clock span and asserting a fragile count band (the project's anti-flake rule).
        assert _wait_until(lambda: len(collector.snapshot()) >= 10)
    finally:
        engine.stop()

    # Validate WHAT was emitted deterministically: every line is a well-formed GPS sentence at the
    # configured talker and both configured sentences flow — independent of scheduler jitter.
    lines = collector.snapshot()
    assert all(line.startswith("$GP") for line in lines)
    assert any(line.startswith("$GPGGA") for line in lines)
    assert any(line.startswith("$GPRMC") for line in lines)


def test_engine_clean_stop_leaves_no_threads() -> None:
    cfg = _config([_gps_channel()])
    engine = Engine(cfg, sink_hook=lambda spec: [CollectingWriter()])
    engine.start()
    time.sleep(0.2)
    engine.stop()

    names = {t.name for t in threading.enumerate() if t.is_alive()}
    assert "physics" not in names
    assert not any(name.startswith("channel-") for name in names)
    assert engine.health().physics_alive is False


def test_engine_underway_updates_position() -> None:
    cfg = _config([_gps_channel()], movement=MovementSpec(mode="underway", physics_hz=20.0))
    engine = Engine(cfg, sink_hook=lambda spec: [CollectingWriter()])
    start_pos = (engine.snapshot().lat, engine.snapshot().lon)
    engine.start()
    time.sleep(0.4)
    engine.stop()

    end = engine.snapshot()
    # Heading due east: longitude moves, latitude barely changes.
    assert end.lon != start_pos[1]
    assert end.lat == pytest.approx(start_pos[0], abs=1e-3)


def test_sink_failure_is_isolated() -> None:
    good = CollectingWriter()
    boom = BoomWriter()
    cfg = _config([_gps_channel()])
    engine = Engine(cfg, sink_hook=lambda spec: [boom, good])
    engine.start()
    time.sleep(0.4)
    engine.stop()

    assert good.snapshot()  # the healthy sink kept receiving despite the failing one
    report = engine.health()
    gps = next(c for c in report.channels if c.channel_id == "gps")
    assert gps.emitted > 0  # the worker survived the sink exception
    boom_health = next(s for s in gps.sinks if "BoomWriter" in s.name)
    assert boom_health.down is True
    assert report.ok is False  # a down sink flips overall health


# --- budget guard ----------------------------------------------------------------


def _heading_hdg_hdt() -> ChannelSpec:
    return ChannelSpec(
        id="heading",
        role="heading",
        path="none",
        baud=4800,
        talker="HE",
        emit=[EmitSpec("HDT", 10.0), EmitSpec("HDG", 10.0)],
    )


def test_budget_guard_raises_in_strict_mode() -> None:
    cfg = _config([_heading_hdg_hdt()])
    with pytest.raises(BudgetExceeded):
        Engine(cfg, sink_hook=lambda spec: [CollectingWriter()], strict_budget=True)


def test_budget_guard_warns_when_not_strict() -> None:
    cfg = _config([_heading_hdg_hdt()])
    engine = Engine(cfg, sink_hook=lambda spec: [CollectingWriter()], strict_budget=False)
    kinds = []
    while not engine.status_queue.empty():
        kinds.append(engine.status_queue.get_nowait().kind)
    assert "budget_warning" in kinds


# --- AIS through the engine ------------------------------------------------------


def test_ais_channel_emits_decodable_ownship() -> None:
    collector = CollectingWriter()
    ais_channel = ChannelSpec(
        id="ais",
        role="ais",
        path="none",
        baud=38400,
        emit=[EmitSpec("AIVDM", 20.0)],  # fast so a position emits within the test window
        ais=AisSpec(
            own_ship=AisOwnShip(mmsi=366000123, klass="A", name="MB", ship_type=37),
            include_type5=False,
        ),
    )
    engine = Engine(_config([ais_channel]), sink_hook=lambda spec: [collector])
    engine.start()
    time.sleep(0.4)
    engine.stop()

    lines = collector.snapshot()
    assert lines
    assert all(line.startswith("!AIVDO") for line in lines)  # own-ship uses VDO
    decoded = decode(lines[0])
    assert decoded.mmsi == 366000123


# --- instrument source -----------------------------------------------------------


def _checksum_ok(line: str) -> bool:
    """Verify a `$`/`!`-framed NMEA line's XOR checksum matches its `*HH` suffix."""
    if line[0] not in "$!" or "*" not in line:
        return False
    body, _, suffix = line[1:].partition("*")
    got = 0
    for ch in body:
        got ^= ord(ch)
    return f"{got:02X}" == suffix[:2].upper()


def test_build_source_returns_instrument_source() -> None:
    spec = ChannelSpec(
        id="instrument",
        role="instrument",
        path="none",
        baud=38400,
        talker="II",
        emit=[EmitSpec("VHW", 1.0)],
    )
    source = build_source(spec)
    assert isinstance(source, _InstrumentSource)


def test_instrument_source_builds_checksummed_lines() -> None:
    state = VesselState(**_INITIAL, utc=datetime(2024, 1, 1, tzinfo=UTC))
    source = _InstrumentSource("II")
    for sentence in ("VHW", "ROT", "XDR"):
        lines = source.build(sentence, state)
        assert len(lines) == 1
        line = lines[0]
        assert line[0] in "$!"
        assert not line.endswith("\r\n")  # serial layer appends the terminator
        assert _checksum_ok(line), line


# --- TCP tap fan-out through the engine ------------------------------------------


def test_engine_fans_out_to_tcp_tap() -> None:
    """A TCP-tap sink is started by the engine and mirrors emitted lines to subscribers."""
    tap = TcpTap("127.0.0.1", 0)
    cfg = _config([_gps_channel(rate=10.0)])
    engine = Engine(cfg, sink_hook=lambda spec: [tap])
    engine.start()  # engine must start() I/O sinks before emission
    try:
        client = socket.create_connection(("127.0.0.1", tap.bound_port), timeout=2.0)
        client.settimeout(0.5)
        buf = b""
        deadline = time.monotonic() + 2.0
        while b"\r\n" not in buf and time.monotonic() < deadline:
            try:
                chunk = client.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                break
            buf += chunk
        client.close()
        assert b"\r\n" in buf  # the tap delivered a CRLF-terminated sentence
        assert buf.lstrip().startswith(b"$GP")
    finally:
        engine.stop()


# --- empty-emitter / rx-only channels (H2) ---------------------------------------


def _make_shared() -> SharedState:
    return SharedState(VesselState(**_INITIAL, utc=datetime(2024, 1, 1, tzinfo=UTC)))


def test_worker_with_no_emitters_stays_alive_and_injects_replay() -> None:
    """H2: a channel whose emit list yields no emitters (rx-only / all-disabled) must not crash on
    ``min()`` of an empty sequence — the worker stays alive and still injects replayed lines."""
    collector = CollectingWriter()
    spec = ChannelSpec(id="rx0", role="gps", path="none", baud=38400, talker="GP", emit=[])
    assert emitters_for(spec) == []  # rx-only: nothing scheduled
    stop = threading.Event()
    status_q: queue.Queue[StatusMsg] = queue.Queue()
    worker = _ChannelWorker(
        spec, build_source(spec), [_Sink("c", collector)], _make_shared(), status_q, stop, None
    )
    worker.start()
    try:
        assert _wait_until(worker.is_alive)  # came up instead of dying on empty min()
        worker.enqueue(_ReplayLine("$GPRMC,injected"))
        assert _wait_until(lambda: bool(collector.snapshot()))  # replayed line injected
        assert collector.snapshot() == ["$GPRMC,injected"]
    finally:
        stop.set()
        worker.enqueue(_STOP)
        worker.join(2.0)
    assert not worker.is_alive()  # clean stop


def test_engine_all_disabled_channel_worker_stays_alive() -> None:
    """H2: a validate-clean config where every emit entry is disabled must not crash the worker."""
    gps = ChannelSpec(
        id="gps",
        role="gps",
        path="none",
        baud=38400,
        talker="GP",
        emit=[EmitSpec("GGA", 5.0, enabled=False), EmitSpec("RMC", 5.0, enabled=False)],
    )
    assert emitters_for(gps) == []
    engine = Engine(_config([gps]), sink_hook=lambda spec: [CollectingWriter()])
    engine.start()
    try:
        assert _wait_until(lambda: engine.health().channels[0].alive)
        report = engine.health()
        assert report.physics_alive
        assert report.channels[0].alive  # did not die on empty min() (H2)
        assert report.ok  # nothing down
    finally:
        engine.stop()
    assert engine.health().channels[0].alive is False


# --- H1: engine call sites survive a garbage checksum-valid field -----------------


def test_feed_passthrough_state_survives_garbage_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """H1 belt-and-suspenders: a ``ValueError``/``TypeError`` from a checksum-valid garbage field is
    swallowed at the engine call site, so the channel's only writer never dies mid-forward."""
    import nmea_sim.engine as engine_mod

    def boom(line: str) -> dict[str, float]:
        raise ValueError("garbage speed field 1.2.3")

    monkeypatch.setattr(engine_mod.rx, "parse_line", boom)
    spec = ChannelSpec(
        id="gps", role="gps", path="none", baud=38400, talker="GP", emit=[EmitSpec("RMC", 1.0)]
    )
    stop = threading.Event()
    status_q: queue.Queue[StatusMsg] = queue.Queue()
    worker = _ChannelWorker(
        spec,
        build_source(spec),
        [_Sink("c", CollectingWriter())],
        _make_shared(),
        status_q,
        stop,
        None,
    )
    # An RMC formatter passes the state-formatter gate; the raising parser must not propagate.
    worker._feed_passthrough_state("$GPRMC,garbage")


# --- H9: finite / range gate before SharedState.update ---------------------------


def test_sanitize_state_changes_drops_nan_and_out_of_range() -> None:
    """H9: non-finite / out-of-range fields are dropped before they can reach SharedState."""
    assert _sanitize_state_changes({"sog_kn": float("nan"), "cog_deg": 90.0}) == {"cog_deg": 90.0}
    assert _sanitize_state_changes({"sog_kn": -3.0}) == {}  # negative SOG rejected
    # A bad half of a lat/lon pair invalidates the whole fix (both dropped).
    assert _sanitize_state_changes({"lat": 95.0, "lon": 10.0}) == {}
    assert _sanitize_state_changes({"lat": float("inf"), "lon": 10.0}) == {}
    # A clean set passes through untouched.
    clean = {"lat": 10.0, "lon": -30.0, "sog_kn": 5.0}
    assert _sanitize_state_changes(clean) == clean


# --- DOM10: ZDA carve-out latch expiry -------------------------------------------


def test_zda_carveout_latch_expires() -> None:
    """DOM10: once a source stops sending its own ZDA, synthesis resumes after the latch window
    instead of being suppressed forever."""
    gen = GpsGenerator("GP")
    state = VesselState(**_INITIAL, utc=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC))
    rmc = gen.build(state, ("RMC",))[0]
    zda = gen.build(state, ("ZDA",))[0]
    carve = ZdaCarveout("GP")
    assert carve.on_forward("in0", zda, now=0.0) == []  # source owns ZDA -> latched
    assert carve.on_forward("in0", rmc, now=1.0) == []  # recent ZDA -> no synthesis
    synth = carve.on_forward("in0", rmc, now=31.0)  # well past the latch window -> resume
    assert len(synth) == 1
    assert synth[0].startswith("$GPZDA")


def test_zda_carveout_synthesizes_when_source_never_sends_zda() -> None:
    """Baseline: a source that only ever sends RMC gets a synthesized ZDA immediately."""
    gen = GpsGenerator("GP")
    state = VesselState(**_INITIAL, utc=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC))
    rmc = gen.build(state, ("RMC",))[0]
    synth = ZdaCarveout("GP").on_forward("in0", rmc, now=0.0)
    assert len(synth) == 1
    assert synth[0].startswith("$GPZDA")


# --- H6: replay / reader thread aliveness folds into health ----------------------


def test_replay_thread_missing_file_is_unhealthy() -> None:
    """H6: a replay thread that dies on a vanished/unreadable file reports unhealthy (not green)
    and surfaces a ``replay_error`` status, instead of silently dying under a whole-loop suppress.
    """
    stop = threading.Event()
    status_q: queue.Queue[StatusMsg] = queue.Queue()
    thread = _ReplayThread(
        "this-capture-does-not-exist.nmea", False, 1.0, {}, _make_shared(), stop, status_q
    )
    thread.start()
    thread.join(2.0)
    assert not thread.is_alive()
    assert thread.healthy() is False  # dead-but-green is disqualified
    kinds = []
    while not status_q.empty():
        kinds.append(status_q.get_nowait().kind)
    assert "replay_error" in kinds


# --- M4: TimeAuthority monotonic clamp must not cross tiers -----------------------


class _StubRouter:
    """Minimal Router stand-in that always names the same GNSS winner."""

    def __init__(self, winner: str | None) -> None:
        self._winner = winner

    def winner(self, channel_id: str, cls: str, now: float) -> str | None:
        return self._winner


def test_time_authority_future_sim_base_does_not_freeze_live_gnss() -> None:
    """M4: a sim-epoch-ahead base clock must not freeze the live GNSS time via the monotonic clamp.

    Before the fix, clamping the projected GNSS time up to ``current`` (seeded from a future sim
    epoch) pinned the clock at the future value forever, tagged ``gps``.
    """
    base = TimeSource(TimeSourceSpec(mode="simulated"), datetime(2030, 1, 1, tzinfo=UTC))
    router = cast(Router, _StubRouter("in0"))
    authority = TimeAuthority(base, router, "gps", {"in0": "gps"}, NtpSync())
    now = time.monotonic()
    authority.note_time("in0", datetime(2026, 1, 1, tzinfo=UTC), now)

    current = datetime(2030, 1, 1, tzinfo=UTC)  # state seeded from the future sim epoch
    resolved = authority.advance(current, dt_s=0.05)
    assert resolved.year == 2026  # live GNSS time wins; NOT frozen at the 2030 sim epoch
    assert authority.source_tag() == "gps"

    # And it keeps advancing on the next tick (clamped only against the last real-time value).
    resolved2 = authority.advance(resolved, dt_s=0.05)
    assert resolved2 >= resolved
    assert resolved2.year == 2026


# --- physics-tick provenance (RM-009) ---------------------------------------------


def _prov_thread(clock_tag: object = None) -> tuple[SharedState, _PhysicsThread]:
    """A minimal physics thread over a static-movement engine, for provenance assertions."""
    ts = TimeSource(TimeSourceSpec(mode="system_utc"), None)
    physics = PhysicsEngine(MovementSpec(mode="static", physics_hz=10.0), ts)
    shared = SharedState(VesselState(**_INITIAL, utc=ts.initial()))
    thread = _PhysicsThread(
        shared,
        physics,
        hz=10.0,
        stop=threading.Event(),
        **({} if clock_tag is None else {"clock_tag": clock_tag}),
    )
    return shared, thread


def test_physics_tick_tags_derived_fields_sim() -> None:
    """pitch/roll come from the sea-state model and can never be live — this is the regression
    guard for the reported bug, where the Attitude panel read LIVE over wholly simulated values."""
    shared, thread = _prov_thread()
    thread._tick(0.1)
    _, prov = shared.snapshot_with_provenance()
    assert prov["pitch_deg"].source == "sim"
    assert prov["roll_deg"].source == "sim"


def test_physics_tick_tags_utc_from_the_clock_callable() -> None:
    """``utc`` rides the SAME atomic commit as the simulated motion but may be a live GNSS instant,
    so it must carry its own tag rather than inheriting the write's blanket 'sim'."""
    shared, thread = _prov_thread(clock_tag=lambda: "gps")
    thread._tick(0.1)
    _, prov = shared.snapshot_with_provenance()
    assert prov["utc"].source == "clock:gps"
    assert prov["pitch_deg"].source == "sim"  # same commit, different provenance


def test_physics_tick_survives_a_clock_without_source_tag() -> None:
    """In simulate/replay the clock is a bare ``TimeSource`` with no ``source_tag``. Reaching for it
    would raise AttributeError, which the run-loop's blanket except converts into a DEAD physics
    thread and every channel frozen — so the default tag must never touch the clock object."""
    shared, thread = _prov_thread()  # no clock_tag -> default
    thread._tick(0.1)  # must not raise
    _, prov = shared.snapshot_with_provenance()
    assert prov["utc"].source == "clock:simulated"


def test_physics_thread_stays_alive_in_simulate_mode() -> None:
    """End-to-end form of the trap above: a real engine over a bare TimeSource must report
    physics_alive, not a silently dead thread."""
    engine = Engine(_config([_gps_channel()]), strict_budget=False)
    engine.start()
    try:
        assert _wait_until(lambda: engine.health().physics_alive, timeout=2.0)
        assert engine.health().physics_alive is True
    finally:
        engine.stop()
