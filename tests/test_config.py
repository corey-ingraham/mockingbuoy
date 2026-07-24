"""Config model: parse the example config, build initial state, reject bad values."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nmea_sim.config import ChannelSpec, EngineConfig, MovementSpec, TimeSourceSpec
from nmea_sim.validate import validate

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def test_load_example_config() -> None:
    cfg = EngineConfig.load(CONFIG_PATH)
    assert cfg.writer_backend == "log"
    ids = [c.id for c in cfg.channels]
    assert ids == ["gps", "heading", "ais", "instrument"]
    ais = next(c for c in cfg.channels if c.role == "ais")
    assert ais.ais is not None
    assert ais.ais.include_type5 is True
    assert ais.ais.own_ship.mmsi == 366000001


def test_load_example_config_instrument_channel() -> None:
    cfg = EngineConfig.load(CONFIG_PATH)
    inst = next(c for c in cfg.channels if c.id == "instrument")
    assert inst.role == "instrument"
    assert inst.talker == "II"
    assert inst.baud == 38400  # 4800 is too tight for the instrument sentence set
    emitted = [e.sentence for e in inst.emit]
    assert emitted == ["VHW", "DPT", "MWV", "MWD", "ROT", "XDR", "RSA", "VDR", "PASHR"]
    assert "DBT" not in emitted  # redundant with DPT
    assert inst.tcp_tap is not None and inst.tcp_tap.enabled
    # The whole shipped config (instrument channel included) validates cleanly.
    assert validate(EngineConfig.load(CONFIG_PATH)) == []


def test_build_initial_state_fills_utc() -> None:
    cfg = EngineConfig.load(CONFIG_PATH)
    utc = datetime(2024, 1, 1, tzinfo=UTC)
    state = cfg.build_initial_state(utc)
    assert state.utc == utc
    assert state.lat == pytest.approx(0.0)
    assert isinstance(state.satellites, int)


def test_build_initial_state_populates_new_fields() -> None:
    cfg = EngineConfig.load(CONFIG_PATH)
    utc = datetime(2024, 1, 1, tzinfo=UTC)
    state = cfg.build_initial_state(utc)
    # Example config sets these explicitly.
    assert state.depth_m == pytest.approx(10.0)
    assert state.sea_state == 1
    assert isinstance(state.sea_state, int)
    assert state.wind_speed_kn == pytest.approx(8.0)
    assert state.wind_dir_deg == pytest.approx(45.0)


def test_build_initial_state_new_field_defaults() -> None:
    # A raw config carrying only the required lat/lon must fall back to documented defaults:
    # stw defaults to sog (0.0 here), depth to 10.0, sea_state to 1.
    cfg = EngineConfig(initial_state_raw={"lat": 1.0, "lon": 2.0, "sog_kn": 7.5})
    state = cfg.build_initial_state(datetime(2024, 1, 1, tzinfo=UTC))
    assert state.stw_kn == pytest.approx(7.5)  # defaults to SOG
    assert state.depth_m == pytest.approx(10.0)
    assert state.sea_state == 1
    assert state.rot_dpm == pytest.approx(0.0)


def _channel_raw(**overrides: object) -> dict[str, object]:
    """Minimal raw channel dict (only the keys ``from_dict`` requires), plus overrides."""
    raw: dict[str, object] = {"id": "gps", "role": "gps", "path": "none", "baud": 38400}
    raw.update(overrides)
    return raw


def test_channel_enabled_defaults_to_true_when_key_absent() -> None:
    """Configs written before per-channel toggling existed carry no ``enabled`` key; they
    must keep emitting, so the absent key has to read as on — never as off."""
    spec = ChannelSpec.from_dict(_channel_raw())
    assert spec.enabled is True
    assert ChannelSpec(id="gps", role="gps", path="none", baud=38400).enabled is True
    # Every channel in the shipped config likewise defaults to on.
    assert all(c.enabled is True for c in EngineConfig.load(CONFIG_PATH).channels)


@pytest.mark.parametrize("enabled", [True, False])
def test_channel_enabled_round_trips_through_from_dict_and_to_dict(enabled: bool) -> None:
    """Both serializers are hand-written, so the flag is only trustworthy if a value
    survives the full dict -> spec -> dict -> spec cycle in both states."""
    spec = ChannelSpec.from_dict(_channel_raw(enabled=enabled))
    assert spec.enabled is enabled

    as_dict = spec.to_dict()
    assert as_dict["enabled"] is enabled  # emitted unconditionally, not only when False

    assert ChannelSpec.from_dict(as_dict).enabled is enabled


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
