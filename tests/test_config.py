"""Config model: parse the example config, build initial state, reject bad values."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nmea_sim.config import EngineConfig, MovementSpec, TimeSourceSpec

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def test_load_example_config() -> None:
    cfg = EngineConfig.load(CONFIG_PATH)
    assert cfg.writer_backend == "log"
    ids = [c.id for c in cfg.channels]
    assert ids == ["gps", "heading", "ais"]
    ais = next(c for c in cfg.channels if c.role == "ais")
    assert ais.ais is not None
    assert ais.ais.include_type5 is True
    assert ais.ais.own_ship.mmsi == 366000001


def test_build_initial_state_fills_utc() -> None:
    cfg = EngineConfig.load(CONFIG_PATH)
    utc = datetime(2024, 1, 1, tzinfo=UTC)
    state = cfg.build_initial_state(utc)
    assert state.utc == utc
    assert state.lat == pytest.approx(0.0)
    assert isinstance(state.satellites, int)


def test_movement_rejects_bad_hz() -> None:
    with pytest.raises(ValueError):
        MovementSpec(physics_hz=0)


def test_movement_rejects_bad_mode() -> None:
    with pytest.raises(ValueError):
        MovementSpec(mode="teleport")


def test_time_source_rejects_bad_mode() -> None:
    with pytest.raises(ValueError):
        TimeSourceSpec(mode="sundial")


def test_epoch_datetime_parses_and_defaults() -> None:
    assert EngineConfig().epoch_datetime() is None
    cfg = EngineConfig(time_source=TimeSourceSpec(mode="simulated", epoch="2024-06-21T12:00:00"))
    dt = cfg.epoch_datetime()
    assert dt is not None and dt.tzinfo is not None
