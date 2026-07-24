"""Cross-phase, hardware-free integration tests exercising the whole pipeline.

These sit above the per-module unit tests (checksum/navigation/generators/config/tcp_tap)
and prove the pieces work *together*: a real :class:`~nmea_sim.engine.Engine` running its
physics + per-channel sender threads, the headless ``main.py`` CLI, the TCP tap broadcaster,
profile-driven synthetic traffic, and the POSIX pty writer — all without any serial hardware.

Nothing here names a real locale: every :class:`~nmea_sim.realism.RealismProfile` is a small
synthetic box built inline, and all sockets bind to ``127.0.0.1``. Timing assertions never
pin an exact sentence count to a wall-clock duration (thread scheduling varies); they assert
structural correctness plus at-least-one within a generous, bounded poll so a hang cannot
stall CI.
"""

from __future__ import annotations

import json
import os
import select
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pynmea2
import pytest
from pyais import decode

import main as cli
from nmea_sim.config import (
    AisOwnShip,
    AisSpec,
    AisTrafficSpec,
    ChannelSpec,
    EmitSpec,
    EngineConfig,
    MovementSpec,
    TcpTapSpec,
    TimeSourceSpec,
)
from nmea_sim.engine import Engine
from nmea_sim.realism import Region
from nmea_sim.writers import PtyWriter

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# COG and heading are deliberately far apart so a test can prove RMC/VTG carry COG while the
# heading channel carries heading — the two must never be cross-wired.
_COG_DEG = 95.0
_HEADING_DEG = 280.0

_INITIAL_STATE = {
    "lat": 25.12345,
    "lon": -80.54321,
    "sog_kn": 12.3,
    "cog_deg": _COG_DEG,
    "heading_true_deg": _HEADING_DEG,
    "heading_mag_deg": 283.0,
    "mag_variation_deg": -3.0,
    "altitude_m": 15.4,
    "fix_quality": 1,
    "satellites": 9,
    "hdop": 0.8,
}

# A tight, area-neutral bounding box sitting in open water (no real locale).
_REGION = {"min_lat": 10.0, "max_lat": 10.2, "min_lon": -30.2, "max_lon": -30.0}


# --- helpers ----------------------------------------------------------------------


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

    def total(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._by_channel.values())


def _wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll ``pred`` until true or ``timeout`` elapses. Bounded so a failure can't hang CI."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _free_tcp_port() -> int:
    """Reserve an ephemeral 127.0.0.1 port, then release it for the tap to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _connect(port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    sock.settimeout(2.0)
    return sock


def _recv_lines(sock: socket.socket, count: int, timeout: float = 5.0) -> list[str]:
    """Read up to ``count`` CRLF-terminated lines from a socket within ``timeout``."""
    buf = b""
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    sock.settimeout(0.5)
    while len(lines) < count and time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            continue
        if not chunk:
            break
        buf += chunk
        while b"\r\n" in buf:
            raw, _, buf = buf.partition(b"\r\n")
            lines.append(raw.decode("ascii"))
    return lines


def _base_config(
    channels: list[ChannelSpec],
    *,
    backend: str = "null",
    initial: dict[str, object] | None = None,
    movement: MovementSpec | None = None,
) -> EngineConfig:
    return EngineConfig(
        writer_backend=backend,
        movement=movement or MovementSpec(mode="static", physics_hz=20.0),
        time_source=TimeSourceSpec(mode="system_utc"),
        initial_state_raw=dict(_INITIAL_STATE if initial is None else initial),
        channels=channels,
    )


# --- 1) headless engine smoke: gps + heading + ais through the monitor seam --------


def test_headless_engine_smoke_emits_valid_gps_heading_ais() -> None:
    gps = ChannelSpec(
        id="gps",
        role="gps",
        path="none",
        baud=38400,
        talker="GP",
        emit=[EmitSpec("GGA", 5.0), EmitSpec("RMC", 5.0), EmitSpec("VTG", 5.0)],
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
        ais=AisSpec(
            own_ship=AisOwnShip(mmsi=366000123, klass="A", name="MB", ship_type=37),
            include_type5=False,
        ),
    )
    monitor = _Monitor()
    engine = Engine(_base_config([gps, heading, ais]), monitor=monitor.record)
    engine.start()

    def _seen_all() -> bool:
        g = monitor.lines_for("gps")
        return (
            any(x.startswith("$GPGGA") for x in g)
            and any(x.startswith("$GPRMC") for x in g)
            and any(x.startswith("$GPVTG") for x in g)
            and bool(monitor.lines_for("heading"))
            and bool(monitor.lines_for("ais"))
        )

    try:
        assert _wait_until(_seen_all), "expected GGA+RMC+VTG, heading, and AIS within the window"
    finally:
        engine.stop()

    # GPS: valid pynmea2 sentences with the GP talker; RMC/VTG carry COG, never heading.
    gps_lines = monitor.lines_for("gps")
    by_type: dict[str, pynmea2.TalkerSentence] = {}
    for line in gps_lines:
        msg = pynmea2.parse(line)  # raises on a bad checksum / malformed sentence
        assert msg.talker == "GP"
        by_type[msg.sentence_type] = msg
    assert {"GGA", "RMC", "VTG"} <= set(by_type), f"missing GPS sentence types: {set(by_type)}"

    rmc = by_type["RMC"]
    assert float(rmc.true_course) == pytest.approx(_COG_DEG)
    assert float(rmc.true_course) != pytest.approx(_HEADING_DEG)
    vtg = by_type["VTG"]
    assert float(vtg.true_track) == pytest.approx(_COG_DEG)

    # Heading: HDT carries heading (bow), not COG.
    hdt = pynmea2.parse(monitor.lines_for("heading")[0])
    assert hdt.talker == "HE"
    assert hdt.sentence_type == "HDT"
    assert float(hdt.heading) == pytest.approx(_HEADING_DEG)
    assert float(hdt.heading) != pytest.approx(_COG_DEG)

    # AIS: own-ship position decodes via pyais and is a !AIVDO with the configured MMSI.
    ais_line = monitor.lines_for("ais")[0]
    assert ais_line.startswith("!AIVDO")
    assert decode(ais_line).mmsi == 366000123


# --- 2) main.py end-to-end via main(argv) -----------------------------------------


def test_main_validate_only_on_example_config_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["--config", str(CONFIG_PATH), "--validate-only"])
    assert rc == 0
    assert "is valid" in capsys.readouterr().err


def test_main_invalid_config_returns_one_with_problems_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "writer_backend": "null",
                "initial_state": {"lat": 200.0, "lon": 0.0},  # latitude out of range
                "channels": [
                    {
                        "id": "gps",
                        "role": "gps",
                        "path": "none",
                        "baud": 38400,
                        "talker": "GP",
                        "emit": [{"sentence": "GGA", "rate_hz": 1.0}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rc = cli.main(["--config", str(bad), "--validate-only"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "problem" in err
    assert "above maximum" in err


def test_main_bounded_null_run_leaves_no_engine_threads(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["--config", str(CONFIG_PATH), "--backend", "null", "--duration", "0.3"])
    assert rc == 0
    capsys.readouterr()  # drain the run banner

    # A clean shutdown must join every physics/channel thread the engine spawned.
    def _threads_gone() -> bool:
        names = {t.name for t in threading.enumerate() if t.is_alive()}
        return "physics" not in names and not any(n.startswith("channel-") for n in names)

    assert _wait_until(_threads_gone, timeout=2.0)


# --- 3) TCP tap integration over real 127.0.0.1 sockets ---------------------------


def test_tcp_tap_broadcasts_engine_lines_and_ignores_inbound() -> None:
    port = _free_tcp_port()
    gps = ChannelSpec(
        id="gps",
        role="gps",
        path="none",
        baud=38400,
        talker="GP",
        emit=[EmitSpec("GGA", 10.0), EmitSpec("RMC", 10.0)],
        tcp_tap=TcpTapSpec(enabled=True, port=port),
    )
    engine = Engine(_base_config([gps]))
    engine.start()
    fast: socket.socket | None = None
    slow: socket.socket | None = None
    try:
        fast = _connect(port)

        # The client receives exactly the CRLF-framed NMEA the engine emits.
        first = _recv_lines(fast, 1)
        assert first, "tap delivered no line within the poll window"
        assert first[0].startswith("$GP")
        assert pynmea2.parse(first[0]).talker == "GP"  # well-formed, valid checksum

        # Bytes the client SENDS are ignored: the tap is read-only, so state is untouched
        # and the engine keeps running and broadcasting.
        lat_before = engine.snapshot().lat
        fast.sendall(b"$INJECT,evil,command*00\r\n")
        time.sleep(0.1)
        assert engine.snapshot().lat == lat_before  # no inbound path mutated state
        assert engine.health().ok  # nothing crashed
        assert _recv_lines(fast, 1), "broadcast stopped after inbound bytes"

        # A second, never-reading client must not stall the first (drop-oldest per client).
        slow = _connect(port)
        assert _wait_until(lambda: len(_recv_lines(fast, 1)) == 1)
    finally:
        if fast is not None:
            fast.close()
        if slow is not None:
            slow.close()
        engine.stop()


# --- 4) profile-driven synthetic traffic end-to-end ------------------------------


def _write_profile(tmp_path: Path, *, target_count: int, seed_region: dict[str, float]) -> str:
    """Write a small, synthetic realism profile (tight box, no real locale) to disk."""
    profile = {
        "region": dict(seed_region),
        "target_count": target_count,
        "type_mix": {"cargo": 0.4, "fishing": 0.3, "pleasure": 0.2, "other": 0.1},
        "speed_profiles": {
            "cargo": {"mean_kn": 12, "std_kn": 2, "min_kn": 4, "max_kn": 20},
            "fishing": {"mean_kn": 4, "std_kn": 1, "min_kn": 0, "max_kn": 9},
        },
        "motion_model": "transiting",
        "class_a_fraction": 0.6,
    }
    path = tmp_path / "area.local.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    return str(path)


def test_profile_traffic_emits_ownship_and_in_region_targets(tmp_path: Path) -> None:
    target_count = 4
    profile_path = _write_profile(tmp_path, target_count=target_count, seed_region=_REGION)
    traffic = AisTrafficSpec(
        enabled=True, profile_path=profile_path, target_count=target_count, seed=7
    )
    ais = ChannelSpec(
        id="ais",
        role="ais",
        path="none",
        baud=38400,
        emit=[EmitSpec("AIVDM", 10.0)],  # fast so a position tick lands inside the window
        ais=AisSpec(
            own_ship=AisOwnShip(mmsi=366000123, klass="A", name="MB", ship_type=37),
            include_type5=False,
            traffic=traffic,
        ),
    )
    # Own-ship sits inside the same box; static movement keeps it put for the assertion.
    own_state = dict(_INITIAL_STATE)
    own_state.update({"lat": 10.1, "lon": -30.1, "sog_kn": 0.0})
    monitor = _Monitor()
    engine = Engine(_base_config([ais], initial=own_state), monitor=monitor.record)
    engine.start()
    try:
        got = _wait_until(
            lambda: any(x.startswith("!AIVDO") for x in monitor.lines_for("ais"))
            and any(x.startswith("!AIVDM") for x in monitor.lines_for("ais"))
        )
        assert got, "expected both own-ship VDO and target VDMs within the poll window"
    finally:
        engine.stop()

    lines = monitor.lines_for("ais")
    vdo = [x for x in lines if x.startswith("!AIVDO")]
    vdm = [x for x in lines if x.startswith("!AIVDM")]
    assert vdo, "own-ship position reports (VDO) should be emitted"
    assert vdm, "synthetic target position reports (VDM) should be emitted"

    region = Region(**_REGION)
    for line in vdm:
        d = decode(line)
        assert d.mmsi != 366000123  # a synthetic target, never own-ship
        assert region.contains(d.lat, d.lon), (d.lat, d.lon)


# --- 5) pty loopback (POSIX-only; SKIPS on Windows dev, RUNS on Linux CI) ----------


@pytest.mark.skipif(os.name != "posix", reason="pty is POSIX-only")
def test_pty_loopback_receives_crlf_terminated_checksummed_sentences() -> None:
    # Drive a real gps channel through a PtyWriter sink so we read exactly the bytes the
    # engine would put on a serial line — including the CRLF terminator.
    writer = PtyWriter()
    gps = ChannelSpec(
        id="gps",
        role="gps",
        path="none",
        baud=38400,
        talker="GP",
        emit=[EmitSpec("GGA", 10.0), EmitSpec("RMC", 10.0)],
    )
    engine = Engine(_base_config([gps]), sink_hook=lambda spec: [writer])
    slave_fd = os.open(writer.slave_name, os.O_RDONLY | os.O_NONBLOCK)
    engine.start()
    try:
        buf = b""
        deadline = time.monotonic() + 5.0
        while b"\r\n" not in buf and time.monotonic() < deadline:
            ready, _, _ = select.select([slave_fd], [], [], 0.5)
            if ready:
                buf += os.read(slave_fd, 4096)
        assert b"\r\n" in buf, "no CRLF-terminated sentence arrived on the pty"
        line = buf.split(b"\r\n", 1)[0].decode("ascii")
        assert line.startswith("$GP")
        assert pynmea2.parse(line).talker == "GP"  # valid framing + checksum survived the pty
    finally:
        os.close(slave_fd)
        engine.stop()
        writer.close()
