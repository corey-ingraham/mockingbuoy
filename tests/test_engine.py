"""Engine: emission rate, clean shutdown, physics motion, fan-out isolation, budget guard."""

from __future__ import annotations

import socket
import threading
import time

import pytest
from geographiclib.geodesic import Geodesic
from pyais import decode

from nmea_sim.config import (
    AisOwnShip,
    AisSpec,
    ChannelSpec,
    EmitSpec,
    EngineConfig,
    MovementSpec,
    TimeSourceSpec,
)
from nmea_sim.engine import (
    BudgetExceeded,
    Engine,
    PhysicsEngine,
    TimeSource,
    advance_next_fire,
    emission_offsets,
)
from nmea_sim.state import VesselState
from nmea_sim.tcp_tap import TcpTap

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


# --- running engine --------------------------------------------------------------


def test_engine_emits_at_configured_rate() -> None:
    collector = CollectingWriter()
    cfg = _config([_gps_channel(rate=5.0)])
    engine = Engine(cfg, sink_hook=lambda spec: [collector])
    t0 = time.monotonic()
    engine.start()
    time.sleep(0.8)
    engine.stop()
    elapsed = time.monotonic() - t0

    n = len(collector.snapshot())
    # Two sentences at 5 Hz => ~10 lines/s. Generous band absorbs scheduler jitter.
    assert n >= 6
    assert n <= 10 * elapsed * 2


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
