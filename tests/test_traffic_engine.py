"""Profile-driven AIS traffic wired through the engine: own-ship-only regression, target
interleave (position + static), region/bounds containment, budget accounting, config round-trip.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from pyais import decode

from nmea_sim.config import (
    AisOwnShip,
    AisSpec,
    AisTrafficSpec,
    ChannelSpec,
    EmitSpec,
    EngineConfig,
    MovementSpec,
    TimeSourceSpec,
)
from nmea_sim.engine import AIS_POSITION, AIS_STATIC, Engine, _AisSource
from nmea_sim.realism import RealismProfile, Region, TargetSpawner
from nmea_sim.state import VesselState

# A tight, area-neutral bounding box sitting in open water (no real locale).
_REGION = {"min_lat": 10.0, "max_lat": 10.2, "min_lon": -30.2, "max_lon": -30.0}


def _profile_dict(target_count: int = 3) -> dict[str, object]:
    return {
        "region": dict(_REGION),
        "target_count": target_count,
        "type_mix": {"cargo": 0.4, "fishing": 0.3, "pleasure": 0.2, "other": 0.1},
        "speed_profiles": {
            "cargo": {"mean_kn": 12, "std_kn": 2, "min_kn": 4, "max_kn": 20},
            "fishing": {"mean_kn": 4, "std_kn": 1, "min_kn": 0, "max_kn": 9},
        },
        "motion_model": "transiting",
        "class_a_fraction": 0.6,
    }


def _write_profile(tmp_path: Path, target_count: int = 3) -> str:
    path = tmp_path / "area.local.json"
    path.write_text(json.dumps(_profile_dict(target_count)), encoding="utf-8")
    return str(path)


def _own_state() -> VesselState:
    return VesselState(
        lat=10.1,
        lon=-30.1,
        sog_kn=8.0,
        cog_deg=110.0,
        heading_true_deg=115.0,
        heading_mag_deg=118.0,
        mag_variation_deg=-3.0,
        altitude_m=0.0,
        fix_quality=1,
        satellites=10,
        hdop=0.7,
        utc=datetime(2024, 6, 21, 12, 0, 0, tzinfo=UTC),
    )


def _ais_spec(traffic: AisTrafficSpec | None) -> AisSpec:
    return AisSpec(
        own_ship=AisOwnShip(mmsi=366000123, klass="A", name="MB", ship_type=37),
        include_type5=True,
        traffic=traffic,
    )


class _Collector:
    """Thread-safe sink recording every line."""

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


# --- regression: traffic disabled => own-ship only --------------------------------


def test_traffic_disabled_emits_own_ship_only() -> None:
    src = _AisSource(_ais_spec(None))
    lines = src.build(AIS_POSITION, _own_state())
    assert len(lines) == 1
    assert lines[0].startswith("!AIVDO")
    assert decode(lines[0]).mmsi == 366000123


def test_traffic_present_but_disabled_matches_none() -> None:
    disabled = _AisSource(_ais_spec(AisTrafficSpec(enabled=False)))
    lines = disabled.build(AIS_POSITION, _own_state())
    assert len(lines) == 1
    assert lines[0].startswith("!AIVDO")


# --- traffic enabled: position interleave -----------------------------------------


def test_position_yields_ownship_then_targets_in_region(tmp_path: Path) -> None:
    count = 5
    profile_path = _write_profile(tmp_path, target_count=3)  # overridden below
    traffic = AisTrafficSpec(enabled=True, profile_path=profile_path, target_count=count, seed=7)
    src = _AisSource(_ais_spec(traffic))

    lines = src.build(AIS_POSITION, _own_state())
    assert len(lines) == 1 + count  # own-ship + one position report per target
    assert lines[0].startswith("!AIVDO")  # own-ship first
    targets = lines[1:]
    assert all(line.startswith("!AIVDM") for line in targets)

    region = Region(**_REGION)
    for line in targets:
        d = decode(line)
        assert d.mmsi != 366000123  # a synthetic target, not own-ship
        assert region.contains(d.lat, d.lon), (d.lat, d.lon)
        assert 0.0 <= d.speed <= 25.0  # within the profile's clamped speed envelope


def test_target_count_overrides_profile(tmp_path: Path) -> None:
    profile_path = _write_profile(tmp_path, target_count=3)
    traffic = AisTrafficSpec(enabled=True, profile_path=profile_path, target_count=6, seed=1)
    src = _AisSource(_ais_spec(traffic))
    lines = src.build(AIS_POSITION, _own_state())
    assert len(lines) == 1 + 6  # the override wins over the profile's target_count


def test_default_profile_used_when_no_path() -> None:
    traffic = AisTrafficSpec(enabled=True, profile_path=None, target_count=2, seed=3)
    src = _AisSource(_ais_spec(traffic))
    lines = src.build(AIS_POSITION, _own_state())
    assert len(lines) == 1 + 2
    assert lines[0].startswith("!AIVDO")


# --- traffic enabled: static interleave -------------------------------------------


def test_static_yields_ownship_static_then_target_statics(tmp_path: Path) -> None:
    count = 4
    # Force class_a_fraction=1.0 so every target static is a clean 2-fragment Type 5 (Class B
    # would instead emit two independent single-fragment Type 24 sentences). The class-mix
    # itself is exercised in test_realism; here we only assert engine ordering + interleave.
    data = _profile_dict()
    data["class_a_fraction"] = 1.0
    path = tmp_path / "class_a.local.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    traffic = AisTrafficSpec(enabled=True, profile_path=str(path), target_count=count, seed=9)
    src = _AisSource(_ais_spec(traffic))

    lines = src.build(AIS_STATIC, _own_state())
    # Own-ship (class A) static is a 2-fragment Type 5, and every all-class-A target static is
    # likewise a 2-fragment Type 5 — so the sentence count is 2 per vessel.
    assert len(lines) == 2 + 2 * count
    own = decode(lines[0], lines[1])
    assert own.msg_type == 5
    assert own.mmsi == 366000123  # own-ship static comes first

    # Reconstruct the exact per-target ship types the spawner produces for this seed/profile.
    # For AIS_STATIC the engine does not advance targets, so this matches the emitted set 1:1.
    expected_types = [
        t.ship_type for t in TargetSpawner(RealismProfile.from_dict(data), 9).spawn(count)
    ]
    assert any(st != 0 for st in expected_types)  # fixture actually exercises non-'other' types
    decoded_types = []
    target_mmsis = set()
    for i in range(2, len(lines), 2):
        d = decode(lines[i], lines[i + 1])
        assert d.msg_type == 5
        decoded_types.append(d.ship_type)
        target_mmsis.add(d.mmsi)
    # H3: the realism type_mix must reach the wire per target — NOT decode to a uniform 0
    # (which the old `d.ship_type in ship_types` check accepted because 0 was in the set).
    assert decoded_types == expected_types
    assert 366000123 not in target_mmsis  # targets are synthetic, never own-ship
    assert len(target_mmsis) == count


# --- budget guard counts the extra traffic ----------------------------------------


def _engine_config(channel: ChannelSpec) -> EngineConfig:
    return EngineConfig(
        writer_backend="null",
        movement=MovementSpec(mode="static", physics_hz=20.0),
        time_source=TimeSourceSpec(mode="system_utc"),
        initial_state_raw={"lat": 10.1, "lon": -30.1},
        channels=[channel],
    )


def test_engine_emits_ownship_and_targets_within_budget(tmp_path: Path) -> None:
    profile_path = _write_profile(tmp_path)
    traffic = AisTrafficSpec(enabled=True, profile_path=profile_path, target_count=4, seed=5)
    channel = ChannelSpec(
        id="ais",
        role="ais",
        path="none",
        baud=38400,
        emit=[EmitSpec("AIVDM", 10.0)],  # fast so a tick lands inside the window
        ais=AisSpec(
            own_ship=AisOwnShip(mmsi=366000123, klass="A", name="MB", ship_type=37),
            include_type5=False,
            traffic=traffic,
        ),
    )
    collector = _Collector()
    # strict_budget=True: construction must NOT raise even though targets add sentences.
    engine = Engine(_engine_config(channel), sink_hook=lambda spec: [collector])
    engine.start()
    time.sleep(0.4)
    engine.stop()

    lines = collector.snapshot()
    vdo = [line for line in lines if line.startswith("!AIVDO")]
    vdm = [line for line in lines if line.startswith("!AIVDM")]
    assert vdo, "own-ship position reports should still be emitted"
    assert vdm, "target position reports should now be emitted too"
    # Each own-ship position tick brings four targets with it, so VDMs dominate.
    assert len(vdm) >= 4 * len(vdo) - 4


# --- config round-trip ------------------------------------------------------------


def test_config_with_traffic_survives_roundtrip() -> None:
    traffic = AisTrafficSpec(
        enabled=True,
        profile_path="profiles/area.local.json",
        target_count=8,
        seed=42,
        max_advance_s=15.0,
    )
    channel = ChannelSpec(
        id="ais",
        role="ais",
        path="none",
        baud=38400,
        emit=[EmitSpec("AIVDM", 0.2)],
        ais=AisSpec(own_ship=AisOwnShip(mmsi=366000123), traffic=traffic),
    )
    cfg = _engine_config(channel)

    restored = EngineConfig.from_dict(cfg.to_dict())
    assert restored.to_dict() == cfg.to_dict()
    got = restored.channels[0].ais
    assert got is not None and got.traffic == traffic


def test_ais_to_dict_omits_traffic_when_absent() -> None:
    ais = AisSpec(own_ship=AisOwnShip(mmsi=366000123), traffic=None)
    assert "traffic" not in ais.to_dict()  # existing configs stay byte-stable
