"""AUTO-mode routing through a live Engine, driven purely through the in-memory ``_dispatch_rx``
seam — no serial, no pty, cross-platform.

``writer_backend="serial"`` never opens a real port on the dev box (the tolerant SerialPort just
records the device absent), so emitted/forwarded lines are captured through a fake-sink
``sink_hook``. Routing is exercised by handing fabricated, checksum-valid NMEA to
``engine._dispatch_rx(input_id, line)`` exactly as a real input reader would. Liveness turns on
real ``time.monotonic`` inside the worker threads, so the stale/failover cases give a short input
``liveness_timeout_s`` and poll the router through the bounded ``_wait_until`` helper rather than
sleeping blindly.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest

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
# A fixed-clock base state for fabricating distinctive input lines. Its baked-in UTC differs from
# the engine's live system clock, so a *generated* sentence (built off the live clock) never
# collides byte-for-byte with a *forwarded* one — that is what lets a test tell them apart.
_BASE = VesselState(**_INITIAL, utc=datetime(2024, 1, 1, tzinfo=UTC))


class CollectingWriter:
    """Thread-safe sink that records every line it receives (mirrors test_engine's)."""

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


def _wait_until(pred: Callable[[], bool], timeout: float = 3.0) -> bool:
    """Poll ``pred`` until true or ``timeout`` elapses. Bounded so a failure can't hang CI."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _seeded_generation_seen(collector: CollectingWriter, forwarded: str) -> Callable[[], bool]:
    """A predicate: some GENERATED gps line (not the forwarded ``live`` line) parses to lat 42.

    Proves generation resumed *from the seeded fix* rather than the 10.1 the engine started at.
    """

    def pred() -> bool:
        from nmea_sim import rx  # local: mirrors the engine's own seeding parse path

        for line in collector.snapshot():
            if line == forwarded:
                continue
            fields = rx.parse_line(line)
            if fields.get("lat") == pytest.approx(42.0, abs=1e-3):
                return True
        return False

    return pred


def _rmc(lat: float, lon: float = 3.4567) -> str:
    """A distinctive, checksum-valid RMC line (lat picks it out of the crowd)."""
    return GpsGenerator("GP").rmc(replace(_BASE, lat=lat, lon=lon))


def _hdt(heading_deg: float) -> str:
    """A distinctive, checksum-valid HDT line."""
    return HeadingGenerator("HE").hdt(replace(_BASE, heading_true_deg=heading_deg))


def _zda(*, hour: int = 6) -> str:
    """A checksum-valid ZDA line carrying a distinctive hour off the fixed base clock."""
    return GpsGenerator("GP").zda(replace(_BASE, utc=datetime(2024, 1, 1, hour, tzinfo=UTC)))


def _gps_channel(sources: list[str], *, rate: float = 20.0, enabled: bool = True) -> ChannelSpec:
    return ChannelSpec(
        id="gps",
        role="gps",
        path="none",  # placeholder: the tolerant serial backend never opens a real device
        baud=115200,
        talker="GP",
        emit=[EmitSpec("RMC", rate), EmitSpec("GGA", rate)],
        sources=sources,
        enabled=enabled,
    )


def _heading_channel(sources: list[str]) -> ChannelSpec:
    return ChannelSpec(
        id="heading",
        role="heading",
        path="none",
        baud=38400,
        talker="HE",
        emit=[EmitSpec("HDT", 10.0)],
        sources=sources,
    )


def _auto_engine(
    channels: list[ChannelSpec], inputs: list[InputSpec]
) -> tuple[Engine, dict[str, CollectingWriter]]:
    collectors: dict[str, CollectingWriter] = {}

    def hook(spec: ChannelSpec) -> list[CollectingWriter]:
        collector = CollectingWriter()
        collectors[spec.id] = collector
        return [collector]

    cfg = EngineConfig(
        writer_backend="serial",
        movement=MovementSpec(mode="static", physics_hz=20.0),
        time_source=TimeSourceSpec(mode="system_utc"),
        initial_state_raw=dict(_INITIAL),
        channels=channels,
        inputs=inputs,
        mode="auto",
    )
    engine = Engine(cfg, sink_hook=hook, strict_budget=False)
    return engine, collectors


# --- forward + seed ---------------------------------------------------------------


def test_live_gnss_is_forwarded_verbatim_and_seeds_state() -> None:
    engine, collectors = _auto_engine(
        [_gps_channel(["gps_in"])],
        [InputSpec(id="gps_in", path="none", liveness_timeout_s=30.0)],
    )
    engine.start()
    try:
        line = _rmc(lat=1.2345, lon=2.3456)
        engine._dispatch_rx("gps_in", line)
        # Forwarded byte-for-byte to the gps sink...
        assert _wait_until(lambda: line in collectors["gps"].snapshot())
        # ...and its parsed position seeded shared state (distinct from the 10.1 initial lat).
        assert _wait_until(lambda: engine.snapshot().lat == pytest.approx(1.2345, abs=1e-3))
        assert engine.snapshot().lon == pytest.approx(2.3456, abs=1e-3)
    finally:
        engine.stop()


def test_live_source_suppresses_the_generator() -> None:
    engine, collectors = _auto_engine(
        [_gps_channel(["gps_in"], rate=20.0)],
        [InputSpec(id="gps_in", path="none", liveness_timeout_s=30.0)],
    )
    # Dispatch BEFORE start so liveness is stamped ahead of the first schedule tick: the generator
    # is then suppressed from tick zero and cannot leak a single generated sentence.
    line = _rmc(lat=4.5678)
    engine._dispatch_rx("gps_in", line)
    engine.start()
    try:
        from nmea_sim import rx
        from nmea_sim.gps_generator import zda_from_datetime

        # The winning source sends RMC but no ZDA, so B3c's single-source carve-out synthesizes
        # exactly one ZDA carrying that RMC's EXACT time (not the live sim clock).
        parsed = rx.parse_time(line)
        assert parsed is not None
        synth_zda = zda_from_datetime("GP", parsed)
        assert _wait_until(lambda: synth_zda in collectors["gps"].snapshot())
        # Over a further window (many 20 Hz ticks) the generator stays suppressed: nothing but the
        # verbatim forwarded line and its single synthesized ZDA ever appears — a *generated*
        # RMC/GGA (built off the live clock) would be a third, distinct string.
        assert not _wait_until(
            lambda: bool(set(collectors["gps"].snapshot()) - {line, synth_zda}), timeout=0.4
        )
    finally:
        engine.stop()


# --- cross-routing: one satellite-compass input feeds two outputs -----------------


def test_sat_input_cross_routes_and_yields_to_higher_priority_gps() -> None:
    engine, collectors = _auto_engine(
        [_gps_channel(["gps_in", "sat_in"]), _heading_channel(["sat_in"])],
        [
            InputSpec(id="gps_in", path="none", liveness_timeout_s=0.15),
            InputSpec(id="sat_in", path="none", liveness_timeout_s=30.0),
        ],
    )
    engine.start()
    router = engine._router
    assert router is not None  # auto mode always builds an arbiter
    try:
        gps_line = _rmc(lat=10.0)
        sat_pos_early = _rmc(lat=20.0)
        sat_hdt = _hdt(heading_deg=123.4)

        engine._dispatch_rx("gps_in", gps_line)  # gps_in is highest priority -> wins gps output
        engine._dispatch_rx("sat_in", sat_pos_early)  # sat gnss loses to live gps_in -> dropped
        engine._dispatch_rx("sat_in", sat_hdt)  # sat heading has no rival -> forwarded

        assert _wait_until(lambda: gps_line in collectors["gps"].snapshot())
        # The sat's heading always routes to the heading output (distinctive so a generated HDT
        # off the initial 92.0 heading can't be mistaken for it).
        assert _wait_until(lambda: sat_hdt in collectors["heading"].snapshot())
        # While gps_in is the live winner, the sat's position line is dropped at the gps output.
        assert not _wait_until(lambda: sat_pos_early in collectors["gps"].snapshot(), timeout=0.3)

        # Let gps_in fall stale; the sat (still live) becomes the gps winner, and a NEW sat position
        # line now flows to the gps output.
        assert _wait_until(lambda: router.winner("gps", "gnss", time.monotonic()) == "sat_in")
        sat_pos_live = _rmc(lat=30.0)
        engine._dispatch_rx("sat_in", sat_pos_live)
        assert _wait_until(lambda: sat_pos_live in collectors["gps"].snapshot())
        # The earlier, dropped sat line was never retroactively forwarded.
        assert sat_pos_early not in collectors["gps"].snapshot()
    finally:
        engine.stop()


# --- failover + recovery ----------------------------------------------------------


def test_generation_resumes_seeded_after_source_dies_then_recovers() -> None:
    engine, collectors = _auto_engine(
        [_gps_channel(["gps_in"], rate=20.0)],
        [InputSpec(id="gps_in", path="none", liveness_timeout_s=0.15)],
    )
    engine.start()
    router = engine._router
    assert router is not None  # auto mode always builds an arbiter
    try:
        live = _rmc(lat=42.0)
        engine._dispatch_rx("gps_in", live)
        assert _wait_until(lambda: live in collectors["gps"].snapshot())
        # The live fix seeded shared state; in static movement it persists after the source dies.
        assert _wait_until(lambda: engine.snapshot().lat == pytest.approx(42.0, abs=1e-3))

        # Source goes dead -> the channel has no winner -> generation resumes.
        assert _wait_until(lambda: router.winner("gps", "gnss", time.monotonic()) is None)
        assert _wait_until(lambda: any(g != live for g in collectors["gps"].snapshot()))
        # Generation resumed FROM the seeded position: a generated sentence (not the forwarded
        # ``live`` line) now carries lat 42 — proof the generator resumed from the last real fix,
        # not the 10.1 it started at. (Early ticks before the seed landed carry 10.1; we look for
        # the seeded one to appear.)
        assert _wait_until(_seeded_generation_seen(collectors["gps"], live))

        # Recovery: the source returns and the channel flips straight back to LIVE passthrough.
        back = _rmc(lat=43.0)
        engine._dispatch_rx("gps_in", back)
        assert _wait_until(lambda: back in collectors["gps"].snapshot())
        assert _wait_until(lambda: router.winner("gps", "gnss", time.monotonic()) == "gps_in")
    finally:
        engine.stop()


# --- OFF beats live passthrough (R9/R55) ------------------------------------------


def test_disabled_channel_silences_live_passthrough() -> None:
    engine, collectors = _auto_engine(
        [_gps_channel(["gps_in"], enabled=False)],
        [InputSpec(id="gps_in", path="none", liveness_timeout_s=30.0)],
    )
    engine.start()
    try:
        line = _rmc(lat=7.7)
        engine._dispatch_rx("gps_in", line)
        # OFF silences live passthrough too: nothing reaches the sink while the channel is disabled.
        assert not _wait_until(lambda: bool(collectors["gps"].snapshot()), timeout=0.3)

        # Re-enable and prove the same line now flows — the silence was the OFF gate, not a drop.
        engine.set_channel_enabled("gps", True)
        engine._dispatch_rx("gps_in", line)
        assert _wait_until(lambda: line in collectors["gps"].snapshot())
    finally:
        engine.stop()


# --- single-source ZDA carve-out (B3c, R2/R55) ------------------------------------


def test_synthesized_zda_carries_the_rmcs_exact_time() -> None:
    """A winning GPS source sending RMC but NO ZDA gets exactly one synthesized ZDA whose time
    field EQUALS the RMC's — time and position stay single-source, never divergent on the wire."""
    engine, collectors = _auto_engine(
        [_gps_channel(["gps_in"], rate=20.0)],
        [InputSpec(id="gps_in", path="none", liveness_timeout_s=30.0)],
    )
    # Dispatch before start so generation is suppressed from tick zero (no generated ZDA can leak).
    rmc = _rmc(lat=8.9)
    engine._dispatch_rx("gps_in", rmc)
    engine.start()
    try:
        from nmea_sim import rx
        from nmea_sim.gps_generator import zda_from_datetime

        parsed = rx.parse_time(rmc)
        assert parsed is not None
        synth = zda_from_datetime("GP", parsed)  # what the carve-out must emit
        assert _wait_until(lambda: synth in collectors["gps"].snapshot())
        # The synthesized ZDA's time field equals the RMC's time field, byte-for-byte.
        import pynmea2

        assert pynmea2.parse(synth).timestamp == pynmea2.parse(rmc).timestamp
        # Nothing but the forwarded RMC and its single synthesized ZDA ever appears — no generated
        # or NTP-clock ZDA (which would carry a different, live-clock time) is emitted.
        assert not _wait_until(
            lambda: bool(set(collectors["gps"].snapshot()) - {rmc, synth}), timeout=0.4
        )
    finally:
        engine.stop()


def test_source_own_zda_forwards_and_none_is_synthesized() -> None:
    """When the source itself sends a ZDA, it forwards verbatim and the carve-out synthesizes
    nothing — even for a following RMC — so the wire never carries a duplicate ZDA."""
    engine, collectors = _auto_engine(
        [_gps_channel(["gps_in"], rate=20.0)],
        [InputSpec(id="gps_in", path="none", liveness_timeout_s=30.0)],
    )
    zda = _zda(hour=6)
    rmc = _rmc(lat=11.2)
    # ZDA first (so the carve-out records the source sends its own), then an RMC. Both before start.
    engine._dispatch_rx("gps_in", zda)
    engine._dispatch_rx("gps_in", rmc)
    engine.start()
    try:
        assert _wait_until(lambda: zda in collectors["gps"].snapshot())
        assert _wait_until(lambda: rmc in collectors["gps"].snapshot())
        # Only the two forwarded lines ever appear: the source's own ZDA suppressed synthesis.
        assert not _wait_until(
            lambda: bool(set(collectors["gps"].snapshot()) - {zda, rmc}), timeout=0.4
        )
    finally:
        engine.stop()


def test_no_synthesized_zda_when_gps_channel_is_off() -> None:
    """The carve-out is exempt from passthrough SUPPRESSION, never from the OFF gate (R55): a
    disabled GPS channel emits neither the forwarded RMC nor any synthesized ZDA."""
    engine, collectors = _auto_engine(
        [_gps_channel(["gps_in"], enabled=False)],
        [InputSpec(id="gps_in", path="none", liveness_timeout_s=30.0)],
    )
    engine.start()
    try:
        from nmea_sim import rx
        from nmea_sim.gps_generator import zda_from_datetime

        rmc = _rmc(lat=13.5)
        parsed = rx.parse_time(rmc)
        assert parsed is not None
        synth = zda_from_datetime("GP", parsed)
        engine._dispatch_rx("gps_in", rmc)
        # OFF silences everything on the channel — the forwarded RMC and its synthesized ZDA alike.
        assert not _wait_until(lambda: bool(collectors["gps"].snapshot()), timeout=0.3)
        assert synth not in collectors["gps"].snapshot()
    finally:
        engine.stop()


def test_zda_and_rmc_resume_from_one_clock_after_source_dies() -> None:
    """When the winning GPS source dies, the channel falls back to generation and BOTH ZDA and RMC
    resume — from the single engine clock, not a stale source — proving time and position stay
    unified across the LIVE->SIM handover."""
    gps = ChannelSpec(
        id="gps",
        role="gps",
        path="none",
        baud=115200,
        talker="GP",
        emit=[EmitSpec("RMC", 20.0), EmitSpec("ZDA", 20.0)],
        sources=["gps_in"],
    )
    engine, collectors = _auto_engine(
        [gps],
        [InputSpec(id="gps_in", path="none", liveness_timeout_s=0.15)],
    )
    engine.start()
    router = engine._router
    assert router is not None
    try:
        import pynmea2

        from nmea_sim import rx

        live = _rmc(lat=17.0)
        engine._dispatch_rx("gps_in", live)
        assert _wait_until(lambda: live in collectors["gps"].snapshot())

        # Source dies -> no winner -> generation resumes on the channel's own clock.
        assert _wait_until(lambda: router.winner("gps", "gnss", time.monotonic()) is None)

        def generated_pair_seen() -> bool:
            # The generated (non-forwarded) sentence types present since the source died.
            types = {
                getattr(pynmea2.parse(line), "sentence_type", "")
                for line in collectors["gps"].snapshot()
                if line != live
            }
            return {"ZDA", "RMC"} <= types

        # Both a generated ZDA and a generated RMC appear post-death — the pair resumed together.
        assert _wait_until(generated_pair_seen)
        # And parse_time agrees they carry a real clock instant (not a blank/failed synthesis).
        assert any(
            rx.parse_time(line) is not None for line in collectors["gps"].snapshot() if line != live
        )
    finally:
        engine.stop()
