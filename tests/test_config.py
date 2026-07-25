"""Config model: parse the example config, build initial state, reject bad values."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nmea_sim.config import (
    ChannelSpec,
    EmitSpec,
    EngineConfig,
    InputSpec,
    MovementSpec,
    ReplaySpec,
    RouteSpec,
    TimeSourceSpec,
)
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


# --- operating mode (B3a config seam) ---------------------------------------------


def test_mode_defaults_to_simulate() -> None:
    """Every config written before the auto-mode seam existed carries no ``mode`` key; the
    absent key must read as today's behaviour, never silently arm passthrough."""
    assert EngineConfig().mode == "simulate"
    assert EngineConfig.from_dict({}).mode == "simulate"
    assert EngineConfig.load(CONFIG_PATH).mode == "simulate"


def test_mode_round_trips_auto_through_from_dict_and_to_dict() -> None:
    cfg = EngineConfig(mode="auto")
    assert cfg.to_dict()["mode"] == "auto"
    assert EngineConfig.from_dict(cfg.to_dict()).mode == "auto"


def test_unknown_mode_rejected_by_dataclass_guard() -> None:
    # Only simulate|auto|replay are honoured; the guard must refuse anything else rather than let
    # a silently-unhonoured mode through.
    with pytest.raises(ValueError):
        EngineConfig(mode="bogus")


def test_replay_mode_accepted_by_dataclass_guard() -> None:
    # "replay" is now a first-class operating mode, so construction must accept it.
    assert EngineConfig(mode="replay").mode == "replay"


# --- InputSpec (B3a config seam) --------------------------------------------------


def test_input_spec_defaults_when_optional_keys_absent() -> None:
    """Only ``id`` and ``path`` are required; the rest take the documented defaults."""
    spec = InputSpec.from_dict({"id": "gps_in", "path": "none"})
    assert spec.function == "unused"
    assert spec.baud == 4800
    assert spec.framing == "8N1"
    assert spec.liveness_timeout_s == pytest.approx(3.0)
    assert spec.read_timeout_s == pytest.approx(0.03)


def test_input_spec_all_fields_round_trip() -> None:
    """Both serializers are hand-written, so every field is only trustworthy once a fully
    populated value survives the dict -> spec -> dict -> spec cycle unchanged."""
    raw = {
        "id": "satcompass_in",
        "path": "/dev/serial/by-id/unit-sat",
        "function": "sat",
        "baud": 38400,
        "framing": "8N1",
        "liveness_timeout_s": 5.0,
        "read_timeout_s": 0.05,
    }
    spec = InputSpec.from_dict(raw)
    assert spec.to_dict() == raw
    assert InputSpec.from_dict(spec.to_dict()) == spec


def test_input_spec_rejects_unknown_function() -> None:
    with pytest.raises(ValueError):
        InputSpec(id="x", path="none", function="radar")


# --- ChannelSpec.sources (B3a config seam) ----------------------------------------


def test_channel_sources_defaults_to_empty_when_key_absent() -> None:
    """Absent ``sources`` means "always simulate" — the behaviour every pre-auto config keeps."""
    assert ChannelSpec.from_dict(_channel_raw()).sources == []
    assert ChannelSpec(id="gps", role="gps", path="none", baud=38400).sources == []


def test_channel_sources_round_trips_populated_list() -> None:
    spec = ChannelSpec.from_dict(_channel_raw(sources=["gps_in", "satcompass_in"]))
    assert spec.sources == ["gps_in", "satcompass_in"]
    assert spec.to_dict()["sources"] == ["gps_in", "satcompass_in"]
    assert ChannelSpec.from_dict(spec.to_dict()).sources == ["gps_in", "satcompass_in"]


def test_full_config_with_inputs_and_sources_round_trips() -> None:
    """Regression guard for the hand-written serializers: a config exercising BOTH new fields
    must survive to_dict -> from_dict byte-for-byte. A field added to one serializer but not the
    other (or a shape mismatch) drops out here, comparing dicts rather than object identity."""
    cfg = EngineConfig(
        mode="auto",
        writer_backend="serial",
        inputs=[
            InputSpec(id="gps_in", path="/dev/serial/by-id/unit-g", function="gps"),
            InputSpec(id="sat_in", path="/dev/serial/by-id/unit-s", function="sat", baud=38400),
        ],
        channels=[
            ChannelSpec(
                id="gps",
                role="gps",
                path="/dev/serial/by-id/unit-out-g",
                baud=4800,
                talker="GP",
                sources=["gps_in", "sat_in"],
            ),
            ChannelSpec(
                id="heading",
                role="heading",
                path="/dev/serial/by-id/unit-out-h",
                baud=4800,
                talker="HE",
                sources=["sat_in"],
            ),
        ],
    )
    reloaded = EngineConfig.from_dict(cfg.to_dict())
    assert reloaded.to_dict() == cfg.to_dict()


# --- EmitSpec.enabled (F4 per-sentence on/off) ------------------------------------


def test_emit_enabled_defaults_true_when_key_absent() -> None:
    """Configs written before the per-sentence switch carry no ``enabled`` on an emit entry;
    the absent key must read as on so every listed sentence keeps emitting."""
    spec = ChannelSpec.from_dict(_channel_raw(emit=[{"sentence": "GGA", "rate_hz": 1.0}]))
    assert spec.emit[0].enabled is True
    assert EmitSpec("GGA", 1.0).enabled is True


@pytest.mark.parametrize("enabled", [True, False])
def test_emit_enabled_round_trips_through_channel_serializers(enabled: bool) -> None:
    """Both channel serializers are hand-written, so the per-sentence flag is only trustworthy
    once a value survives the full dict -> spec -> dict -> spec cycle in either state."""
    raw = _channel_raw(emit=[{"sentence": "GGA", "rate_hz": 1.0, "enabled": enabled}])
    spec = ChannelSpec.from_dict(raw)
    assert spec.emit[0].enabled is enabled

    as_dict = spec.to_dict()
    assert as_dict["emit"][0]["enabled"] is enabled  # emitted unconditionally, not only when off

    assert ChannelSpec.from_dict(as_dict).emit[0].enabled is enabled


# --- RouteSpec (F1 config seam) ---------------------------------------------------


def test_route_absent_is_none_and_omitted_from_to_dict() -> None:
    """A config that never named a route reads back as None and omits the key entirely, so it
    round-trips byte-identically to a pre-route config."""
    assert EngineConfig.from_dict({}).route is None
    assert "route" not in EngineConfig().to_dict()


def test_route_spec_round_trips_through_from_dict_and_to_dict() -> None:
    cfg = EngineConfig(
        route=RouteSpec(
            enabled=True,
            waypoints=[(10.0, -30.0), (10.5, -29.5)],
            speed_kn=8.0,
            loop=True,
        )
    )
    d = cfg.to_dict()
    # JSON has no tuples: waypoints are emitted as [lat, lon] lists.
    assert d["route"] == {
        "enabled": True,
        "waypoints": [[10.0, -30.0], [10.5, -29.5]],
        "speed_kn": 8.0,
        "loop": True,
    }
    reloaded = EngineConfig.from_dict(d)
    assert reloaded.route is not None
    # ...and normalised straight back to tuples on load.
    assert reloaded.route.waypoints == [(10.0, -30.0), (10.5, -29.5)]
    assert all(isinstance(wp, tuple) for wp in reloaded.route.waypoints)
    assert reloaded.to_dict() == d


def test_route_from_dict_defaults_when_keys_absent() -> None:
    spec = RouteSpec.from_dict({})
    assert spec.enabled is False
    assert spec.waypoints == []
    assert spec.speed_kn == pytest.approx(0.0)
    assert spec.loop is False


# --- ReplaySpec + "replay" mode (F2 config seam) ----------------------------------


def test_replay_absent_is_none_and_omitted_from_to_dict() -> None:
    assert EngineConfig.from_dict({}).replay is None
    assert "replay" not in EngineConfig().to_dict()


def test_replay_spec_and_mode_round_trip() -> None:
    cfg = EngineConfig(
        mode="replay",
        replay=ReplaySpec(enabled=True, file="cap.nmea", loop=True, speed=2.0),
    )
    d = cfg.to_dict()
    assert d["mode"] == "replay"
    assert d["replay"] == {
        "enabled": True,
        "file": "cap.nmea",
        "loop": True,
        "speed": 2.0,
        "scope": "full",
    }
    reloaded = EngineConfig.from_dict(d)
    assert reloaded.mode == "replay"
    assert reloaded.replay == ReplaySpec(enabled=True, file="cap.nmea", loop=True, speed=2.0)
    assert reloaded.to_dict() == d


def test_replay_from_dict_defaults_when_keys_absent() -> None:
    spec = ReplaySpec.from_dict({})
    assert spec.enabled is False
    assert spec.file == ""
    assert spec.loop is False
    assert spec.speed == pytest.approx(1.0)
    assert spec.scope == "full"


def test_replay_scope_defaults_to_full() -> None:
    # A replay block with no scope key defaults to "full" (today's whole-capture behaviour).
    assert ReplaySpec().scope == "full"
    assert ReplaySpec.from_dict({"enabled": True, "file": "cap.nmea"}).scope == "full"
    assert EngineConfig().to_dict().get("replay") is None  # still omitted when absent


def test_replay_scope_ais_only_round_trips() -> None:
    cfg = EngineConfig(
        mode="replay",
        replay=ReplaySpec(enabled=True, file="cap.nmea", scope="ais-only"),
    )
    d = cfg.to_dict()
    assert d["replay"]["scope"] == "ais-only"
    reloaded = EngineConfig.from_dict(d)
    assert reloaded.replay is not None
    assert reloaded.replay.scope == "ais-only"
    assert reloaded.to_dict() == d


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
