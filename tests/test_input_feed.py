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
import time
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
from nmea_sim.heading_generator import HeadingGenerator
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


# --- per-input RX mute (the signal-loss rehearsal) --------------------------------
#
# Muting an input is a flag write: the port stays open and the reader thread keeps running, but
# lines stop reaching the monitor and the router. Liveness then simply ages out, so the channel
# falls back to SIM on its own. These drive the REAL reader seam (``_handle_rx_line``), so the
# gate is exercised exactly where a live wire would hit it.


def _muted_engine(monitor: Any = None, *, enabled: bool = True) -> Engine:
    """An auto engine whose single input slot starts in the given enable state."""
    return _auto_engine(
        monitor,
        inputs=[InputSpec(id="gps_in", path="none", liveness_timeout_s=30.0, enabled=enabled)],
    )


def test_muted_input_publishes_no_input_nmea() -> None:
    """The pane must go fully silent: no ``input_nmea`` escapes a disabled slot."""
    cap = _Capture()
    engine = _muted_engine(cap)
    reader = engine._input_readers[0]

    reader._handle_rx_line(_rmc(1.1))
    assert len(cap.pairs) == 1  # baseline: enabled slots publish

    assert engine.set_input_enabled("gps_in", False) is True
    reader._handle_rx_line(_rmc(2.2))
    reader._handle_rx_line(_rmc(3.3))
    assert len(cap.pairs) == 1  # muted: nothing further published

    assert engine.set_input_enabled("gps_in", True) is True
    reader._handle_rx_line(_rmc(4.4))
    assert len(cap.pairs) == 2  # unmuting restores the feed immediately


def test_muted_input_falls_back_to_sim_then_recovers() -> None:
    """The heart of the feature: mute -> liveness ages out -> the channel reports SIM; unmute ->
    LIVE returns. ``now`` is passed forward explicitly rather than sleeping, so the ageing window
    is exercised deterministically and the test stays fast."""
    engine = _muted_engine(_Capture())
    reader = engine._input_readers[0]
    router = engine._router
    assert router is not None

    reader._handle_rx_line(_rmc(1.1))
    now = time.monotonic()
    assert router.source_label("gps", "gnss", now) == "LIVE:gps_in"

    # Mute, then keep feeding the wire: no new line may stamp liveness, so once the existing
    # stamp ages past liveness_timeout_s the channel is on its own and reports SIM.
    engine.set_input_enabled("gps_in", False)
    reader._handle_rx_line(_rmc(2.2))
    aged = now + 31.0  # > liveness_timeout_s (30.0)
    assert router.source_label("gps", "gnss", aged) == "SIM"
    # ...and generation is no longer suppressed, so the channel resumes emitting its own sentences.
    assert router.any_live("gps", "gnss", aged) is False

    # Unmute and feed again: the source wins straight back, no restart involved.
    engine.set_input_enabled("gps_in", True)
    reader._handle_rx_line(_rmc(3.3))
    assert router.source_label("gps", "gnss", time.monotonic()) == "LIVE:gps_in"


def test_muted_input_feeds_neither_router_nor_clock() -> None:
    """The gate sits at the TOP of ``_dispatch_rx``, so a muted slot supplies no time fix either —
    the Time Authority demotes itself to the base clock rather than coasting on a dead source."""
    engine = _muted_engine(_Capture())
    engine.set_input_enabled("gps_in", False)
    router = engine._router
    assert router is not None

    engine._dispatch_rx("gps_in", _rmc(5.5))

    now = time.monotonic()
    assert router.live_class_for_input("gps_in", now) is None
    assert router.winner("gps", "gnss", now) is None


def test_muted_input_still_feeds_diagnostics() -> None:
    """Deliberate asymmetry: the raw-bytes tap is NOT gated, because it observes the wire rather
    than the sim. A muted slot still scores bytes, so Maintenance can prove data is arriving."""
    engine = _muted_engine(_Capture())
    engine.set_input_enabled("gps_in", False)
    reader = engine._input_readers[0]
    assert reader._on_raw is not None

    reader._on_raw((_rmc(6.6) + "\r\n").encode("ascii"))

    snap = engine._diagnostics["gps_in"].snapshot(time.monotonic())
    assert int(snap["bytes"]) > 0


def test_input_enable_defaults_from_config() -> None:
    """``InputSpec.enabled`` is a STARTUP default only; the engine owns the live value after."""
    engine = _muted_engine(_Capture(), enabled=False)
    assert engine.input_status()[0]["enabled"] is False
    engine.set_input_enabled("gps_in", True)
    assert engine.input_status()[0]["enabled"] is True


def test_set_input_enabled_reports_unknown_slot() -> None:
    engine = _muted_engine(_Capture())
    assert engine.set_input_enabled("nope", False) is False


def test_input_status_works_in_simulate_mode_with_inputs() -> None:
    """Regression guard: the enable map is built for EVERY mode, not only auto. ``input_status``
    walks ``config.inputs`` unconditionally, so a simulate config that still declares slots (the
    shipped config.json shape) must not KeyError the way an auto-only dict would."""
    engine = _auto_engine(_Capture(), mode="simulate", inputs=[InputSpec(id="gps_in", path="none")])
    assert engine._input_readers == []  # simulate builds no readers...
    entries = engine.input_status()  # ...but the slot is still reportable
    assert entries == [
        {
            "id": "gps_in",
            "function": "unused",
            "detected_class": None,
            "live": False,
            "enabled": True,
        }
    ]
    assert engine.set_input_enabled("gps_in", False) is True
    assert engine.input_status()[0]["enabled"] is False


# --- per-field provenance + expiry (RM-009) ---------------------------------------
#
# The point of these is the EXPIRY half. Capturing "who wrote this" is easy; the safety property
# is that a live tag cannot outlive its source. Auto mode requires movement.mode static, so once a
# source dies nothing rewrites lat/lon and the value freezes at its last real reading — a tag that
# did not expire would keep calling that frozen number LIVE forever.


def _fed_engine() -> tuple[Engine, Any, str]:
    """An auto engine with one live-fed gps input; returns (engine, reader, line)."""
    engine = _auto_engine(
        _Capture(), inputs=[InputSpec(id="gps_in", path="none", liveness_timeout_s=30.0)]
    )
    return engine, engine._input_readers[0], _rmc(12.3)


def _forward(engine: Engine, line: str) -> None:
    """Drive one line all the way through routing AND the owning worker's passthrough.

    The worker thread is not running (the engine is never started), so the inbox it would drain is
    pumped by hand — this is what actually seeds state, and therefore provenance.
    """
    engine._dispatch_rx("gps_in", line)
    engine._worker_by_id["gps"]._on_passthrough(("gps_in", "gnss", line))


def test_passthrough_fields_resolve_live() -> None:
    engine, _, line = _fed_engine()
    assert engine.provenance() == {}  # nothing fed yet -> everything SIM (sparse: omitted)

    _forward(engine, line)

    prov = engine.provenance()
    assert prov["lat"] == "LIVE"
    assert prov["lon"] == "LIVE"
    assert prov["cog_deg"] == "LIVE"
    # Never-live fields stay omitted even while a source is winning.
    assert "pitch_deg" not in prov
    assert "depth_m" not in prov


def test_live_tag_expires_when_the_source_dies_but_the_value_does_not() -> None:
    """The core safety property, driven through the per-input mute so it needs no hardware."""
    engine, _, line = _fed_engine()
    _forward(engine, line)
    assert engine.provenance()["lat"] == "LIVE"
    frozen = engine._shared.snapshot().lat

    # Mute the input: no further line stamps liveness, so the router's winner ages out.
    engine.set_input_enabled("gps_in", False)
    engine._router._liveness.clear()  # equivalent to the timeout elapsing, without sleeping

    assert engine.provenance() == {}  # degraded to SIM...
    assert engine._shared.snapshot().lat == frozen  # ...while the value is unchanged, not re-simmed


def test_live_tag_returns_when_the_source_comes_back() -> None:
    engine, _, line = _fed_engine()
    _forward(engine, line)
    engine.set_input_enabled("gps_in", False)
    engine._router._liveness.clear()
    assert engine.provenance() == {}

    engine.set_input_enabled("gps_in", True)
    _forward(engine, line)
    assert engine.provenance()["lat"] == "LIVE"


def test_manual_and_replay_writes_never_resolve_live() -> None:
    engine, _, _ = _fed_engine()
    engine.update_state(lat=1.0)
    assert "lat" not in engine.provenance()
    engine._shared.update(_sources="replay", lon=2.0)
    assert "lon" not in engine.provenance()


def test_simulate_mode_reports_no_live_fields() -> None:
    """No router, so nothing can be arbitrated live — and provenance must not crash without one."""
    engine = _auto_engine(_Capture(), mode="simulate", inputs=[InputSpec(id="gps_in", path="none")])
    assert engine.provenance() == {}


def test_conning_panel_field_groups_can_all_reach_live() -> None:
    """The regression guard for the aggregation rule, automated because the browser check needs
    hardware.

    Each conning pill is LIVE only when EVERY live-capable field its panel shows is LIVE, so a
    field that can never be live-seeded would pin its panel to SIM forever — a silent regression
    that every simulate-mode test would happily pass. This drives a realistic two-input auto config
    (GNSS + satellite compass) and asserts each pill's field group actually resolves LIVE.
    """
    channels = [
        ChannelSpec(
            id="gps",
            role="gps",
            path="none",
            baud=115200,
            talker="GP",
            emit=[EmitSpec("RMC", 5.0)],
            sources=["gps_in", "sat_in"],
        ),
        ChannelSpec(
            id="heading",
            role="heading",
            path="none",
            baud=115200,
            talker="HE",
            emit=[EmitSpec("HDT", 5.0)],
            sources=["sat_in"],
        ),
    ]
    inputs = [
        InputSpec(id="gps_in", path="none", function="gps", liveness_timeout_s=30.0),
        InputSpec(id="sat_in", path="none", function="sat", liveness_timeout_s=30.0),
    ]
    cfg = EngineConfig(
        writer_backend="serial",
        movement=MovementSpec(mode="static", physics_hz=20.0),
        time_source=TimeSourceSpec(mode="system_utc"),
        initial_state_raw=dict(_INITIAL),
        channels=channels,
        inputs=inputs,
        mode="auto",
    )
    engine = Engine(cfg, strict_budget=False)

    rmc = _rmc(12.3)
    hdt = HeadingGenerator("HE").hdt(replace(_BASE, heading_true_deg=77.0))
    engine._dispatch_rx("gps_in", rmc)
    engine._worker_by_id["gps"]._on_passthrough(("gps_in", "gnss", rmc))
    engine._dispatch_rx("sat_in", hdt)
    engine._worker_by_id["heading"]._on_passthrough(("sat_in", "heading", hdt))

    prov = engine.provenance()
    # Mirrors PILL_FIELDS in app.js — if these lists drift apart, a pill silently pins to SIM.
    assert all(prov.get(f) == "LIVE" for f in ("lat", "lon"))  # pill-coords
    assert all(prov.get(f) == "LIVE" for f in ("heading_true_deg", "sog_kn", "cog_deg"))  # heading
    assert all(
        prov.get(f) == "LIVE" for f in ("lat", "lon", "cog_deg", "heading_true_deg")
    )  # pill-ship
    # Panels with no live-capable field must NOT appear, however live the sources are.
    assert "pitch_deg" not in prov and "roll_deg" not in prov  # pill-attitude
    assert "depth_m" not in prov  # pill-depth
    assert "wind_speed_kn" not in prov and "wind_dir_deg" not in prov  # pill-env


def test_utc_resolves_live_only_for_gnss_clock_tiers() -> None:
    """The Time panel must answer the same question as every other panel: did this value come from
    a sensor on the wire? An NTP-disciplined clock is real time but LOCAL time — not an NMEA source
    — so it resolves SIM. Guards against the pill and the engine disagreeing about one value.
    """
    engine = _auto_engine(None)
    utc = _BASE.utc

    for tier in ("gps", "sat"):
        engine._shared.update(_sources={"utc": f"clock:{tier}"}, utc=utc)
        assert engine.provenance().get("utc") == "LIVE", tier

    for tier in ("ntp", "system", "simulated", "hold"):
        engine._shared.update(_sources={"utc": f"clock:{tier}"}, utc=utc)
        assert "utc" not in engine.provenance(), tier
