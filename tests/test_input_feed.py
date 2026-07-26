"""Live INPUT line feed (Phase D): the ``on_line`` seam -> token bucket -> ``input_nmea`` frames.

Drives the REAL reader seam — fabricated lines through a wired ``SerialPort._handle_rx_line``,
exactly as an auto-mode input reader does — and asserts the web layer sees one ``input_nmea`` frame
per received line (INCLUDING malformed / bad-checksum), that blank lines are skipped, that the
per-port token bucket caps a burst, and that the tap is a silent no-op when no ``input_monitor`` is
attached (simulate/replay, or a reader with the tap unwired). No serial/pty; cross-platform.

The reader used is the engine's own ``_input_readers[0]`` — built in ``Engine.__init__`` under
``if config.mode == "auto"`` with ``on_line=self._make_input_line_feed(inp.id)`` — so the whole
chain (real ``_handle_rx_line`` -> real token-bucket feed -> real ``Broker.publish_input``) is
exercised end to end, not a stubbed shim.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import janus

from nmea_sim import checksum
from nmea_sim import engine as engine_mod
from nmea_sim.config import (
    ChannelSpec,
    EmitSpec,
    EngineConfig,
    InputSpec,
    MovementSpec,
    TimeSourceSpec,
)
from nmea_sim.engine import Engine
from nmea_sim.gps_generator import GpsGenerator
from nmea_sim.state import VesselState
from web.app import Broker, _render_frame

_INITIAL = {
    "lat": 10.1,
    "lon": -30.5,
    "sog_kn": 5.0,
    "cog_deg": 90.0,
    "heading_true_deg": 92.0,
    "heading_mag_deg": 105.0,
    "mag_variation_deg": -13.0,
    "altitude_m": 0.0,
    "fix_quality": 1,
    "satellites": 10,
    "hdop": 0.8,
}
# A fixed-clock base state so fabricated lines are distinctive and checksum-valid.
_BASE = VesselState(**_INITIAL, utc=datetime(2024, 1, 1, tzinfo=UTC))


class _Capture:
    """A stand-in for ``Broker.publish_input``: records every ``(input_id, line)`` it is handed."""

    def __init__(self) -> None:
        self.pairs: list[tuple[str, str]] = []

    def __call__(self, input_id: str, line: str) -> None:
        self.pairs.append((input_id, line))


def _rmc(lat: float, lon: float = 3.4567) -> str:
    """A distinctive, checksum-valid RMC line (lat picks it out of the crowd)."""
    return GpsGenerator("GP").rmc(replace(_BASE, lat=lat, lon=lon))


def _corrupt_checksum(line: str) -> str:
    """Clobber the two checksum hex digits so ``checksum.verify`` fails (definitely changed)."""
    return line[:-2] + ("11" if line[-2:] != "11" else "22")


def _auto_engine(
    input_monitor: Any = None,
    *,
    mode: str = "auto",
    inputs: list[InputSpec] | None = None,
) -> Engine:
    """An engine wired exactly like production: one gps channel sourced by a single input reader.

    Never started (no threads), never opens a real device (tolerant ``path='none'``), so
    ``_handle_rx_line`` can be driven directly, cross-platform.
    """
    channels = [
        ChannelSpec(
            id="gps",
            role="gps",
            path="none",  # placeholder: the tolerant serial backend never opens a real device
            baud=115200,
            talker="GP",
            emit=[EmitSpec("RMC", 20.0)],
            sources=["gps_in"],
        )
    ]
    if inputs is None:
        inputs = [InputSpec(id="gps_in", path="none", liveness_timeout_s=30.0)]
    cfg = EngineConfig(
        writer_backend="serial",
        movement=MovementSpec(mode="static", physics_hz=20.0),
        time_source=TimeSourceSpec(mode="system_utc"),
        initial_state_raw=dict(_INITIAL),
        channels=channels,
        inputs=inputs,
        mode=mode,
    )
    return Engine(cfg, input_monitor=input_monitor, strict_budget=False)


# --- one input_nmea frame per received line, real Broker frame shape -------------


def test_seam_emits_one_input_nmea_frame_per_received_line() -> None:
    """Each received line yields exactly one ``input_nmea`` frame carrying ``{input, line}``.

    Full chain: engine's real input reader ``_handle_rx_line`` -> token-bucket ``on_line`` feed ->
    real ``Broker.publish_input`` -> the single shared ``/api/stream`` bridge. The frames are
    drained straight off the janus sync side (the pump is never run) and asserted verbatim.
    """

    async def scenario() -> None:
        broker = Broker()
        # The janus.Queue must be constructed inside the running loop (as lifespan does).
        queue: janus.Queue[dict[str, Any]] = janus.Queue(maxsize=10_000)
        broker.bind(queue)
        try:
            engine = _auto_engine(input_monitor=broker.publish_input)
            reader = engine._input_readers[0]  # the real, fully-wired auto-mode input reader
            lines = [_rmc(1.0), _rmc(2.0), _rmc(3.0)]
            for ln in lines:
                reader._handle_rx_line(ln)

            frames: list[dict[str, Any]] = []
            while queue.sync_q.qsize():
                frames.append(queue.sync_q.get_nowait())

            # One frame per line, in order, each a distinct-event input_nmea with {input, line}.
            assert [f["event"] for f in frames] == ["input_nmea"] * len(lines)
            assert [f["data"] for f in frames] == [{"input": "gps_in", "line": ln} for ln in lines]

            # The event-agnostic renderer serialises input_nmea on the SAME wire path as nmea.
            wire = _render_frame(frames[0])
            assert wire.startswith("event: input_nmea\ndata: ")
            assert '"input": "gps_in"' in wire
            assert wire.endswith("\n\n")
        finally:
            broker.close()

    asyncio.run(scenario())


# --- malformed / bad-checksum still emits (fires BEFORE checksum.verify) ----------


def test_malformed_line_still_emits() -> None:
    """A bad-checksum line still reaches the input feed: the tap fires before ``checksum.verify``,
    so operators see the raw garbage on the wire while the checksum-gated dispatch path drops it."""
    cap = _Capture()
    engine = _auto_engine(input_monitor=cap)
    reader = engine._input_readers[0]

    bad = _corrupt_checksum(_rmc(4.5))
    assert not checksum.verify(bad)  # the line is genuinely malformed

    reader._handle_rx_line(bad)

    assert cap.pairs == [("gps_in", bad)]  # forwarded despite the bad checksum
    assert reader.stats.rx_bad_checksum == 1  # and still counted as a checksum failure downstream


# --- blank line is skipped by the empty-guard, never emits ------------------------


def test_blank_line_does_not_emit() -> None:
    cap = _Capture()
    engine = _auto_engine(input_monitor=cap)
    reader = engine._input_readers[0]

    reader._handle_rx_line("")  # empty-guard returns before the tap

    assert cap.pairs == []
    assert reader.stats.rx_lines == 0  # a blank line is not even counted


# --- token bucket caps a burst ----------------------------------------------------


def test_token_bucket_caps_a_burst(monkeypatch: Any) -> None:
    """With the clock frozen so the bucket never refills, a 25-line burst forwards exactly the
    capacity (20) and silently drops the rest — bursts are rate-capped, not the reader stalled."""
    # Freeze the engine's monotonic clock BEFORE construction so the bucket's captured ``last`` and
    # every ``now`` coincide -> zero refill across the whole burst.
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: 1000.0)

    cap = _Capture()
    engine = _auto_engine(input_monitor=cap)
    reader = engine._input_readers[0]

    line = _rmc(8.9)
    for _ in range(25):
        reader._handle_rx_line(line)

    assert len(cap.pairs) == 20  # capacity/burst == 20; the other 5 dropped on an empty bucket
    assert all(p == ("gps_in", line) for p in cap.pairs)


# --- no input_monitor / not auto mode = no input_nmea -----------------------------


def test_no_input_monitor_is_a_silent_noop() -> None:
    """The tap is wired on every auto reader, but with no ``input_monitor`` attached it is a pure
    no-op: it never raises and never forwards, so simulate/replay produce no ``input_nmea``."""
    engine = _auto_engine(input_monitor=None)
    reader = engine._input_readers[0]
    assert reader._on_line is not None  # the observer is still attached...

    reader._handle_rx_line(_rmc(6.7))  # ...and firing it is harmless with no monitor

    assert reader.stats.rx_lines == 1  # the line was received (guard passed)...
    # ...but nothing to forward to, so no input_nmea can exist.


def test_simulate_mode_builds_no_input_readers() -> None:
    """Input readers exist ONLY under ``if config.mode == 'auto'`` -> simulate has none at all,
    so there is no ``on_line`` seam to fire and no ``input_nmea`` is ever produced."""
    engine = _auto_engine(input_monitor=_Capture(), mode="simulate", inputs=[])
    assert engine._input_readers == []
