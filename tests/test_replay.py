"""Record-and-replay mode (F2): re-inject a captured NMEA file through the worker path.

Replay mode reads an NMEA capture line by line and re-injects each line to the channel whose role
matches the line's class (gnss->gps, heading->heading, ais->ais) through the SAME single-writer
worker path auto-mode passthrough uses; a line whose class has no configured channel is dropped.
The generators are suppressed on every channel — the file is the source of truth.

All deterministic and cross-platform: replay is driven from a tmp NMEA file (no pty/serial), every
emitted line is captured via the engine's ``monitor`` seam, and each wait is a bounded poll so a
failure can never hang CI. Inter-line pacing keys off the capture's own RMC timestamp; with a single
time-bearing sentence per pass there is no real sleeping, so ``speed`` is set high for good measure.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from nmea_sim.config import (
    AisOwnShip,
    AisSpec,
    ChannelSpec,
    EmitSpec,
    EngineConfig,
    MovementSpec,
    ReplaySpec,
    RouteSpec,
    TimeSourceSpec,
)
from nmea_sim.engine import Engine

# Capture lines (order is significant — the worker injects in enqueue = file order). Only the RMC
# carries a full wall-clock, so it is the sole pacing anchor. The last line's address ($GPXYZ) is an
# unknown formatter -> classifies to None -> dropped by the replay reader.
_RMC = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
_GGA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
_HDT = "$HEHDT,280.0,T*25"
_AIVDM = "!AIVDM,1,1,,A,15M67FC000G?ufbE`FepT@3n00Sa,0*5C"
_JUNK = "$GPXYZ,nonsense*00"

# The RMC decodes to this position; replay seeds own-ship state from it (exempt from the clamp).
_RMC_LAT = 48.1173
_RMC_LON = 11.516666666666667


class _Monitor:
    """Thread-safe capture of the engine's ``monitor(channel_id, line)`` callback."""

    def __init__(self) -> None:
        self._by_channel: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def record(self, channel_id: str, line: str) -> None:
        with self._lock:
            self._by_channel.setdefault(channel_id, []).append(line)

    def lines_for(self, channel_id: str) -> list[str]:
        with self._lock:
            return list(self._by_channel.get(channel_id, []))


def _wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _write_capture(tmp_path: Path, lines: list[str]) -> str:
    cap = tmp_path / "capture.nmea"
    cap.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(cap)


def _replay_config(capture: str, *, loop: bool = False, speed: float = 100.0) -> EngineConfig:
    """A replay-mode config with gps/heading/ais channels for the three routable classes."""
    gps = ChannelSpec(
        id="gps",
        role="gps",
        path="none",
        baud=38400,
        talker="GP",
        emit=[EmitSpec("RMC", 1.0)],
    )
    heading = ChannelSpec(
        id="heading",
        role="heading",
        path="none",
        baud=38400,
        talker="HE",
        emit=[EmitSpec("HDT", 1.0)],
    )
    ais = ChannelSpec(
        id="ais",
        role="ais",
        path="none",
        baud=38400,
        emit=[EmitSpec("AIVDM", 1.0)],
        ais=AisSpec(own_ship=AisOwnShip(mmsi=366000123, klass="A"), include_type5=False),
    )
    return EngineConfig(
        writer_backend="null",
        movement=MovementSpec(mode="static", physics_hz=20.0),
        time_source=TimeSourceSpec(mode="system_utc"),
        initial_state_raw={"lat": 0.0, "lon": 0.0},
        channels=[gps, heading, ais],
        mode="replay",
        replay=ReplaySpec(enabled=True, file=capture, loop=loop, speed=speed),
    )


def test_replay_reinjects_lines_to_matching_channels_in_order(tmp_path: Path) -> None:
    """Each replayed line reaches the channel whose role matches its class, verbatim and in order;
    a line whose class has no channel is dropped."""
    capture = _write_capture(tmp_path, [_RMC, _GGA, _HDT, _AIVDM, _JUNK])
    monitor = _Monitor()
    engine = Engine(_replay_config(capture), monitor=monitor.record)
    engine.start()
    try:
        assert _wait_until(
            lambda: len(monitor.lines_for("gps")) >= 2
            and bool(monitor.lines_for("heading"))
            and bool(monitor.lines_for("ais"))
        ), "expected the gnss/heading/ais lines to be re-injected to their channels"
    finally:
        engine.stop()

    # gnss lines (RMC, GGA) route to the gps channel, in file order; heading -> heading; ais -> ais.
    assert monitor.lines_for("gps") == [_RMC, _GGA]
    assert monitor.lines_for("heading") == [_HDT]
    assert monitor.lines_for("ais") == [_AIVDM]

    # The unclassifiable line was dropped, never surfacing on any channel.
    all_lines = monitor.lines_for("gps") + monitor.lines_for("heading") + monitor.lines_for("ais")
    assert _JUNK not in all_lines


def test_replay_suppresses_generators_on_fed_channels(tmp_path: Path) -> None:
    """In replay mode the capture is the source of truth: a replay-fed channel emits ONLY the
    replayed lines and never a generated sentence of its own."""
    capture = _write_capture(tmp_path, [_RMC, _GGA, _HDT, _AIVDM])
    monitor = _Monitor()
    engine = Engine(_replay_config(capture), monitor=monitor.record)
    engine.start()
    try:
        assert _wait_until(lambda: len(monitor.lines_for("gps")) >= 2)
        # Give a generator ample time to (wrongly) fire before asserting suppression.
        time.sleep(0.4)
    finally:
        engine.stop()

    # Every gps line is one of the two replayed gnss lines — a generated GGA/RMC would break this.
    assert set(monitor.lines_for("gps")) <= {_RMC, _GGA}
    assert set(monitor.lines_for("heading")) <= {_HDT}
    assert set(monitor.lines_for("ais")) <= {_AIVDM}


def test_replay_seeds_ownship_state_from_replayed_rmc(tmp_path: Path) -> None:
    """Own-ship position is updated from the replayed RMC (via ``rx.parse_line``)."""
    capture = _write_capture(tmp_path, [_RMC, _GGA])
    monitor = _Monitor()
    engine = Engine(_replay_config(capture), monitor=monitor.record)
    engine.start()
    try:
        assert _wait_until(
            lambda: abs(engine.snapshot().lat - _RMC_LAT) < 1e-3
        ), "expected replayed RMC to seed own-ship latitude"
    finally:
        engine.stop()
    snap = engine.snapshot()
    assert snap.lat == _RMC_LAT
    assert snap.lon == _RMC_LON


def test_replay_loop_wraps_at_eof(tmp_path: Path) -> None:
    """With ``loop`` set, the reader restarts at EOF, so the capture's lines replay again."""
    capture = _write_capture(tmp_path, [_RMC, _GGA])
    monitor = _Monitor()
    engine = Engine(_replay_config(capture, loop=True), monitor=monitor.record)
    engine.start()
    try:
        # Two full passes => at least four gps lines, and the pattern repeats.
        assert _wait_until(
            lambda: len(monitor.lines_for("gps")) >= 4
        ), "expected the capture to replay a second time when looping"
    finally:
        engine.stop()
    assert monitor.lines_for("gps")[:4] == [_RMC, _GGA, _RMC, _GGA]


def test_replay_stops_cleanly_without_leaked_threads(tmp_path: Path) -> None:
    """Stopping a replay engine joins the reader, physics, and worker threads — none leak."""
    capture = _write_capture(tmp_path, [_RMC, _GGA, _HDT, _AIVDM])
    monitor = _Monitor()
    engine = Engine(_replay_config(capture, loop=True), monitor=monitor.record)
    engine.start()
    try:
        assert _wait_until(lambda: bool(monitor.lines_for("gps")))
    finally:
        engine.stop()

    names = {t.name for t in threading.enumerate() if t.is_alive()}
    assert "replay" not in names
    assert "physics" not in names
    assert not any(name.startswith("channel-") for name in names)


# --- Scope C: full (default) regression + ais-only (own-ship simulated) -----------


def test_replay_scope_defaults_to_full_capture_is_source_of_truth(tmp_path: Path) -> None:
    """Scope defaults to 'full': the capture owns own-ship AND AIS, every generator is suppressed,
    and own-ship state is seeded from the capture — byte-identical to pre-scope replay."""
    cfg = _replay_config(_write_capture(tmp_path, [_RMC, _GGA, _HDT, _AIVDM]))
    assert cfg.replay is not None
    assert cfg.replay.scope == "full"  # default, unset by _replay_config

    monitor = _Monitor()
    engine = Engine(cfg, monitor=monitor.record)
    engine.start()
    try:
        assert _wait_until(
            lambda: len(monitor.lines_for("gps")) >= 2 and bool(monitor.lines_for("ais"))
        )
        time.sleep(0.3)  # give a (wrongly) un-suppressed generator time to fire
    finally:
        engine.stop()

    # Only replayed lines appear — no generated sentence of any channel's own.
    assert set(monitor.lines_for("gps")) <= {_RMC, _GGA}
    assert set(monitor.lines_for("heading")) <= {_HDT}
    assert set(monitor.lines_for("ais")) <= {_AIVDM}
    # Own-ship position was seeded from the replayed capture RMC (full replay owns own-ship).
    assert engine.snapshot().lat == _RMC_LAT


def _ais_only_config(capture: str, *, with_route: bool = True) -> EngineConfig:
    """A replay + scope='ais-only' config: AIS comes from the capture, own-ship is SIMULATED.

    gps/heading channels generate own-ship nav; the ais channel injects the replayed contacts AND
    generates own-ship AIVDO; movement is underway so physics owns own-ship position, optionally
    steered by a waypoint route (which drives own-ship north, far from the capture's RMC fix)."""
    gps = ChannelSpec(
        id="gps", role="gps", path="none", baud=38400, talker="GP", emit=[EmitSpec("RMC", 5.0)]
    )
    heading = ChannelSpec(
        id="heading",
        role="heading",
        path="none",
        baud=38400,
        talker="HE",
        emit=[EmitSpec("HDT", 5.0)],
    )
    ais = ChannelSpec(
        id="ais",
        role="ais",
        path="none",
        baud=38400,
        emit=[EmitSpec("AIVDM", 5.0)],
        ais=AisSpec(own_ship=AisOwnShip(mmsi=366000123, klass="A"), include_type5=False),
    )
    route = (
        RouteSpec(enabled=True, waypoints=[(0.5, 0.0), (1.0, 0.0)], speed_kn=3000.0, loop=False)
        if with_route
        else None
    )
    return EngineConfig(
        writer_backend="null",
        movement=MovementSpec(mode="underway", physics_hz=20.0),
        time_source=TimeSourceSpec(mode="system_utc"),
        initial_state_raw={"lat": 0.0, "lon": 0.0, "sog_kn": 0.0, "cog_deg": 0.0},
        channels=[gps, heading, ais],
        mode="replay",
        replay=ReplaySpec(enabled=True, file=capture, loop=True, speed=100.0, scope="ais-only"),
        route=route,
    )


def test_replay_ais_only_replays_contacts_and_simulates_ownship(tmp_path: Path) -> None:
    """Scope 'ais-only': the captured !AIVDM is injected verbatim to the ais channel, the gps and
    heading channels GENERATE own-ship nav, own-ship !AIVDO rides alongside the replayed contact,
    and the replayed gnss/heading lines are dropped (own-ship is simulated, not the capture's)."""
    capture = _write_capture(tmp_path, [_RMC, _GGA, _HDT, _AIVDM])
    monitor = _Monitor()
    engine = Engine(_ais_only_config(capture), monitor=monitor.record)
    start_lat = engine.snapshot().lat
    engine.start()
    try:
        assert _wait_until(
            lambda: bool(monitor.lines_for("gps"))
            and bool(monitor.lines_for("heading"))
            and _AIVDM in monitor.lines_for("ais")
            and any(line.startswith("!AIVDO") for line in monitor.lines_for("ais"))
        ), "expected generated own-ship nav + replayed AIS contact + own-ship AIVDO"
        # Let physics keep integrating so own-ship demonstrably advances past its start.
        assert _wait_until(lambda: engine.snapshot().lat > start_lat + 1e-4)
    finally:
        engine.stop()

    ais_lines = monitor.lines_for("ais")
    # The replayed contact is present verbatim, alongside at least one generated own-ship AIVDO.
    assert _AIVDM in ais_lines
    assert any(line.startswith("!AIVDO") for line in ais_lines)

    # gps/heading GENERATE own-ship nav — the captured gnss/heading lines never reach any channel.
    assert _RMC not in monitor.lines_for("gps")
    assert _GGA not in monitor.lines_for("gps")
    assert _HDT not in monitor.lines_for("heading")
    # The replayed contact only ever lands on the ais channel.
    assert _AIVDM not in monitor.lines_for("gps")
    assert _AIVDM not in monitor.lines_for("heading")

    # Own-ship is SIMULATED: its position advanced from the start and never took the capture's fix.
    moved = engine.snapshot()
    assert moved.lat > start_lat
    assert moved.lat != _RMC_LAT
    assert moved.lon != _RMC_LON


def test_replay_ais_only_route_drives_ownship_north(tmp_path: Path) -> None:
    """Under ais-only own-ship is simulated, so a configured waypoint route drives it: latitude
    climbs toward the northern waypoints and COG steers due north on the live schedule."""
    capture = _write_capture(tmp_path, [_RMC, _AIVDM])
    engine = Engine(_ais_only_config(capture, with_route=True))
    start_lat = engine.snapshot().lat
    engine.start()
    try:
        assert _wait_until(
            lambda: engine.snapshot().lat > start_lat + 1e-4
        ), "expected the route to walk own-ship latitude north"
        moved = engine.snapshot()
        assert moved.cog_deg < 5.0 or moved.cog_deg > 355.0  # steered due north
        status = engine.route_status()
        assert status is not None
        assert status["waypoint_count"] == 2
    finally:
        engine.stop()
