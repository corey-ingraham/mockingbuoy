"""Config model: parse the example config, build initial state, reject bad values."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nmea_sim.config import (
    ChannelSpec,
    DepthSimSpec,
    DisplayOverridesSpec,
    EmitSpec,
    EngineConfig,
    HeadingSimSpec,
    InputSpec,
    MovementSpec,
    ReplaySpec,
    RouteSpec,
    RudderSimSpec,
    TimeSourceSpec,
    effective_depth_sim,
    effective_heading_sim,
    effective_rudder_sim,
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


def test_tap_only_channel_round_trips() -> None:
    # A tap-only (software) channel: no serial writer, published over its TCP tap only.
    raw = {
        "id": "tcp-tap",
        "role": "instrument",
        "path": "",
        "baud": 38400,
        "talker": "II",
        "emit": [{"sentence": "VHW", "rate_hz": 1.0}],
        "tap_only": True,
        "tcp_tap": {"enabled": True, "port": 10110},
    }
    ch = ChannelSpec.from_dict(raw)
    assert ch.tap_only is True
    restored = ChannelSpec.from_dict(ch.to_dict())
    assert restored.tap_only is True
    assert restored.tcp_tap is not None and restored.tcp_tap.port == 10110
    # Back-compat: absent/false key -> False, and to_dict omits it (no noise on normal channels).
    normal = ChannelSpec.from_dict({**raw, "tap_only": False})
    assert normal.tap_only is False
    assert "tap_only" not in normal.to_dict()


def test_aggregate_tap_round_trips() -> None:
    # The consolidated tap: one port every channel fans into (the multiplexer feed).
    raw: dict[str, object] = {
        "writer_backend": "serial",
        "initial_state": {"lat": 0.0, "lon": 0.0},
        "channels": [
            {
                "id": "gps",
                "role": "gps",
                "path": "/dev/x",
                "baud": 4800,
                "talker": "GP",
                "emit": [{"sentence": "RMC", "rate_hz": 1.0}],
            }
        ],
        "aggregate_tap": {"enabled": True, "port": 10110},
    }
    cfg = EngineConfig.from_dict(raw)
    assert cfg.aggregate_tap is not None and cfg.aggregate_tap.port == 10110
    restored = EngineConfig.from_dict(cfg.to_dict())
    assert restored.aggregate_tap is not None and restored.aggregate_tap.enabled is True
    # Absent -> None, and to_dict omits it (no noise for configs that never set it).
    without = {k: v for k, v in raw.items() if k != "aggregate_tap"}
    parsed = EngineConfig.from_dict(without)
    assert parsed.aggregate_tap is None
    assert "aggregate_tap" not in parsed.to_dict()


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
    assert state.depth_m == pytest.approx(125.0)
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


# --- unknown-key handling (M8) ----------------------------------------------------


def test_unknown_top_level_key_rejected() -> None:
    # A typo'd top-level key used to be silently dropped on load and deleted on save; it now
    # fails loud with a ValueError naming the key (caught by main._load's (ValueError, KeyError)).
    with pytest.raises(ValueError, match="unknown key"):
        EngineConfig.from_dict({"writer_backen": "log", "channels": []})


def test_comment_prefixed_top_level_key_tolerated() -> None:
    # The shipped config carries a "$schema_note"; comment-prefixed keys must be tolerated and
    # dropped, never rejected as unknown (and never round-tripped into the saved config).
    cfg = EngineConfig.from_dict({"$schema_note": "hi", "_note": "x", "writer_backend": "null"})
    assert cfg.writer_backend == "null"
    assert "$schema_note" not in cfg.to_dict()


def test_unknown_movement_key_raises_value_error_not_type_error() -> None:
    # The old MovementSpec(**data) splat raised a raw TypeError that escaped main._load's catch;
    # it must now surface as a ValueError naming the offending key.
    with pytest.raises(ValueError, match="unknown key"):
        EngineConfig.from_dict({"movement": {"physics_hz": 5.0, "bogus": 1}})


def test_unknown_time_source_key_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown key"):
        EngineConfig.from_dict({"time_source": {"mode": "system_utc", "typo": 2}})


def test_save_rejects_nan_field(tmp_path: Path) -> None:
    # A NaN own-ship value must not round-trip through a saved config (allow_nan=False), and the
    # atomic writer must leave no partial temp file behind on the rejection.
    cfg = EngineConfig(initial_state_raw={"lat": float("nan"), "lon": 0.0})
    out = tmp_path / "nan.json"
    with pytest.raises(ValueError):
        cfg.save(out)
    assert list(tmp_path.iterdir()) == []


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


# --- Phase A: display_overrides + depth_sim config blocks ---------------------------


def _baseline_raw() -> dict[str, object]:
    """The tracked baseline config as a mutable raw dict (a valid config to graft blocks onto)."""
    return json.loads(CONFIG_PATH.read_text())


def test_display_overrides_spec_emits_only_set_keys() -> None:
    """``to_dict`` emits ONLY the non-None keys, so a block that overrode nothing round-trips as
    ``{}`` and the manager can seed a clean ``dict[str, float]`` (no None values)."""
    spec = DisplayOverridesSpec.from_dict({"water_temp_c": 21.5, "fuel_total_l": 1234.0})
    assert spec.to_dict() == {"water_temp_c": 21.5, "fuel_total_l": 1234.0}
    assert DisplayOverridesSpec.from_dict({}).to_dict() == {}
    # Round-trips through from_dict/to_dict unchanged.
    assert DisplayOverridesSpec.from_dict(spec.to_dict()) == spec


def test_display_overrides_round_trips_through_engine_config() -> None:
    raw = _baseline_raw()
    raw["display_overrides"] = {"water_temp_c": 21.5, "fuel_total_l": 1234.0}
    cfg = EngineConfig.from_dict(raw)
    assert cfg.display_overrides is not None
    assert cfg.display_overrides.water_temp_c == pytest.approx(21.5)
    assert cfg.display_overrides.air_temp_c is None  # untouched key stays auto
    restored = EngineConfig.from_dict(cfg.to_dict())
    assert restored.to_dict()["display_overrides"] == {
        "water_temp_c": 21.5,
        "fuel_total_l": 1234.0,
    }


def test_depth_sim_round_trips_through_engine_config() -> None:
    raw = _baseline_raw()
    raw["depth_sim"] = {"enabled": True, "base_depth_m": 42.0}
    cfg = EngineConfig.from_dict(raw)
    assert cfg.depth_sim is not None
    assert cfg.depth_sim.enabled is True
    assert cfg.depth_sim.base_depth_m == pytest.approx(42.0)
    emitted = cfg.to_dict()["depth_sim"]
    # to_dict emits all 9 keys (defaults filled), and the round-trip is stable.
    assert set(emitted) == {
        "enabled",
        "base_depth_m",
        "drift_amp_m",
        "drift_period_s",
        "shoal_amp_m",
        "shoal_period_s",
        "ripple_amp_m",
        "ripple_period_s",
        "min_depth_m",
    }
    assert EngineConfig.from_dict(cfg.to_dict()).to_dict()["depth_sim"] == emitted


def test_config_without_new_blocks_round_trips_byte_identically() -> None:
    """A config that opted into neither block emits neither key and round-trips unchanged (no
    noise for configs that never set them)."""
    cfg = EngineConfig.from_dict(_baseline_raw())
    as_dict = cfg.to_dict()
    assert "display_overrides" not in as_dict
    assert "depth_sim" not in as_dict
    assert EngineConfig.from_dict(as_dict).to_dict() == as_dict


def test_unknown_top_level_key_still_raises() -> None:
    raw = _baseline_raw()
    raw["not_a_real_block"] = {"x": 1}
    with pytest.raises(ValueError):
        EngineConfig.from_dict(raw)


def test_new_blocks_survive_save_and_load(tmp_path: Path) -> None:
    raw = _baseline_raw()
    raw["display_overrides"] = {"air_temp_c": 18.0}
    raw["depth_sim"] = {"enabled": True, "base_depth_m": 30.0, "min_depth_m": 2.0}
    cfg = EngineConfig.from_dict(raw)
    dest = tmp_path / "config.json"
    cfg.save(dest)
    reloaded = EngineConfig.load(dest)
    assert reloaded.display_overrides is not None
    assert reloaded.display_overrides.air_temp_c == pytest.approx(18.0)
    assert reloaded.depth_sim is not None
    assert reloaded.depth_sim.enabled is True
    assert reloaded.depth_sim.min_depth_m == pytest.approx(2.0)
    assert reloaded.to_dict() == cfg.to_dict()


def test_altitude_m_is_unbounded_and_validates() -> None:
    """``altitude_m`` is in ``_STATE_RANGES`` as ``(None, None)`` -- a below-sea-level value is
    legal and validates cleanly (the baseline otherwise validates to no errors)."""
    raw = _baseline_raw()
    raw.setdefault("initial_state", {})
    assert isinstance(raw["initial_state"], dict)
    raw["initial_state"]["altitude_m"] = -1234.0
    assert validate(EngineConfig.from_dict(raw)) == []


def test_depth_sim_bad_period_fails_validation() -> None:
    raw = _baseline_raw()
    raw["depth_sim"] = {"enabled": True, "drift_period_s": 0.0}
    errors = validate(EngineConfig.from_dict(raw))
    assert any("depth_sim.drift_period_s" in e for e in errors)


def test_depth_sim_bad_amplitude_fails_validation() -> None:
    raw = _baseline_raw()
    raw["depth_sim"] = {"enabled": True, "shoal_amp_m": -5.0}
    errors = validate(EngineConfig.from_dict(raw))
    assert any("depth_sim.shoal_amp_m" in e for e in errors)


def test_depth_sim_default_block_validates_clean() -> None:
    raw = _baseline_raw()
    raw["depth_sim"] = {"enabled": True}
    assert [e for e in validate(EngineConfig.from_dict(raw)) if "depth_sim" in e] == []


def test_depth_sim_spec_defaults_match_contract() -> None:
    spec = DepthSimSpec()
    assert spec.enabled is False
    assert spec.base_depth_m == pytest.approx(50.0)
    assert spec.min_depth_m == pytest.approx(0.0)


# --- Phase E: steering-sim specs + default-ON effective helpers -------------------


def test_rudder_sim_spec_defaults_match_contract() -> None:
    """The dataclass default stays inert (``enabled False``) — default-ON is the helper's job, not
    the config layer's — with the contract's tuned amp/period."""
    spec = RudderSimSpec()
    assert spec.enabled is False
    assert spec.amp_deg == pytest.approx(1.5)
    assert spec.period_s == pytest.approx(10.0)


def test_heading_sim_spec_defaults_match_contract() -> None:
    spec = HeadingSimSpec()
    assert spec.enabled is False
    assert spec.amp_deg == pytest.approx(1.0)
    assert spec.period_s == pytest.approx(45.0)


def test_steering_sim_specs_round_trip_only_when_present() -> None:
    """A config that never mentions the steering-sim blocks emits neither key and round-trips
    byte-identically; an explicit block round-trips with all three fields (mirrors depth_sim)."""
    plain = EngineConfig.from_dict(_baseline_raw()).to_dict()
    assert "rudder_sim" not in plain
    assert "heading_sim" not in plain

    raw = _baseline_raw()
    raw["rudder_sim"] = {"enabled": True, "amp_deg": 2.0, "period_s": 8.0}
    raw["heading_sim"] = {"enabled": True}
    cfg = EngineConfig.from_dict(raw)
    as_dict = cfg.to_dict()
    assert as_dict["rudder_sim"] == {"enabled": True, "amp_deg": 2.0, "period_s": 8.0}
    assert set(as_dict["heading_sim"]) == {"enabled", "amp_deg", "period_s"}
    assert EngineConfig.from_dict(as_dict).to_dict() == as_dict


def test_effective_sims_default_on_in_simulate() -> None:
    """In simulate mode an ABSENT block resolves to an enabled default; depth seeds its base from
    the passed initial depth, rudder/heading take their spec defaults."""
    cfg = EngineConfig.from_dict(_baseline_raw())
    assert cfg.mode == "simulate"

    dep = effective_depth_sim(cfg, 123.0)
    assert dep is not None and dep.enabled is True
    assert dep.base_depth_m == pytest.approx(123.0)  # seeded from initial depth

    rud = effective_rudder_sim(cfg)
    assert rud is not None and rud.enabled is True

    hdg = effective_heading_sim(cfg)
    assert hdg is not None and hdg.enabled is True


def test_effective_sims_are_inert_outside_simulate() -> None:
    """Outside simulate mode every effective helper returns None, so auto RX / replay data is never
    overwritten by a background sim write."""
    for mode in ("auto", "replay"):
        raw = _baseline_raw()
        raw["mode"] = mode
        cfg = EngineConfig.from_dict(raw)
        assert effective_depth_sim(cfg, 10.0) is None
        assert effective_rudder_sim(cfg) is None
        assert effective_heading_sim(cfg) is None


def test_effective_sims_respect_explicit_disabled_block_in_simulate() -> None:
    """An explicit ``{enabled:false}`` block in simulate mode yields that disabled spec (inert), not
    the enabled default — the opt-out path."""
    raw = _baseline_raw()
    raw["rudder_sim"] = {"enabled": False}
    raw["heading_sim"] = {"enabled": False}
    raw["depth_sim"] = {"enabled": False}
    cfg = EngineConfig.from_dict(raw)
    assert effective_depth_sim(cfg, 10.0).enabled is False  # type: ignore[union-attr]
    assert effective_rudder_sim(cfg).enabled is False  # type: ignore[union-attr]
    assert effective_heading_sim(cfg).enabled is False  # type: ignore[union-attr]
