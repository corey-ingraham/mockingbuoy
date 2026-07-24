"""Deep config validation: sentence x role legality, collisions, ranges, budget, save round-trip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nmea_sim.config import (
    ChannelSpec,
    EmitSpec,
    EngineConfig,
    InputSpec,
    MovementSpec,
    TcpTapSpec,
    TimeSourceSpec,
)
from nmea_sim.validate import ConfigError, validate, validate_or_raise

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

_STATE = {
    "lat": 10.1,
    "lon": -30.5,
    "sog_kn": 0.0,
    "cog_deg": 90.0,
    "heading_true_deg": 90.0,
    "heading_mag_deg": 90.0,
    "mag_variation_deg": 0.0,
    "altitude_m": 0.0,
    "fix_quality": 1,
    "satellites": 10,
    "hdop": 0.8,
}


def _gps(**over: object) -> ChannelSpec:
    base: dict[str, object] = {
        "id": "gps",
        "role": "gps",
        "path": "/dev/serial/by-id/unit-a",
        "baud": 4800,
        "talker": "GP",
        "emit": [EmitSpec("GGA", 1.0), EmitSpec("RMC", 1.0)],
    }
    base.update(over)
    return ChannelSpec(**base)  # type: ignore[arg-type]


def _config(channels: list[ChannelSpec], **over: object) -> EngineConfig:
    base: dict[str, object] = {"initial_state_raw": dict(_STATE), "channels": channels}
    base.update(over)
    return EngineConfig(**base)  # type: ignore[arg-type]


def _input(**over: object) -> InputSpec:
    base: dict[str, object] = {"id": "gps_in", "path": "/dev/serial/by-id/in-a", "function": "gps"}
    base.update(over)
    return InputSpec(**base)  # type: ignore[arg-type]


# --- the shipped example config must be valid -------------------------------------


def test_example_config_is_valid() -> None:
    assert validate(EngineConfig.load(CONFIG_PATH)) == []


def test_valid_minimal_config_has_no_problems() -> None:
    assert validate(_config([_gps()])) == []


# --- sentence x role legality -----------------------------------------------------


def test_role_cannot_emit_foreign_sentence() -> None:
    # HDT is a heading sentence; a GPS channel cannot build it.
    problems = validate(_config([_gps(emit=[EmitSpec("HDT", 1.0)])]))
    assert any("cannot emit 'HDT'" in p for p in problems)


def test_heading_channel_accepts_hdt() -> None:
    heading = ChannelSpec(
        id="heading",
        role="heading",
        path="/dev/serial/by-id/unit-b",
        baud=4800,
        talker="HE",
        emit=[EmitSpec("HDT", 5.0)],
    )
    assert validate(_config([heading])) == []


def test_unknown_role_is_rejected() -> None:
    problems = validate(_config([_gps(role="sonar")]))
    assert any("unknown role" in p for p in problems)


def _instrument(**over: object) -> ChannelSpec:
    base: dict[str, object] = {
        "id": "instrument",
        "role": "instrument",
        "path": "/dev/serial/by-id/unit-i",
        "baud": 38400,
        "talker": "II",
        "emit": [EmitSpec("VHW", 1.0), EmitSpec("ROT", 1.0)],
    }
    base.update(over)
    return ChannelSpec(**base)  # type: ignore[arg-type]


def test_instrument_is_a_valid_role() -> None:
    # A well-formed instrument channel with legal sentences has no problems.
    assert validate(_config([_instrument()])) == []


def test_instrument_cannot_emit_gps_sentence() -> None:
    # GGA is a GPS sentence; an instrument channel cannot build it.
    problems = validate(_config([_instrument(emit=[EmitSpec("GGA", 1.0)])]))
    assert any("cannot emit 'GGA'" in p for p in problems)


def test_example_instrument_channel_fits_budget_at_38400() -> None:
    # The shipped instrument channel emits its full sentence set at 1 Hz on 38400 8N1 —
    # comfortably within budget (no overflow problem).
    cfg = EngineConfig.load(CONFIG_PATH)
    inst = next(c for c in cfg.channels if c.id == "instrument")
    assert validate(_config([inst])) == []


def test_gps_requires_talker() -> None:
    problems = validate(_config([_gps(talker="")]))
    assert any("requires a 'talker'" in p for p in problems)


# --- direction / emit coupling ----------------------------------------------------


def test_tx_channel_needs_emit() -> None:
    problems = validate(_config([_gps(direction="tx", emit=[])]))
    assert any("no 'emit'" in p for p in problems)


def test_rx_channel_must_not_emit() -> None:
    problems = validate(_config([_gps(direction="rx", emit=[EmitSpec("GGA", 1.0)])]))
    assert any("direction 'rx' cannot have 'emit'" in p for p in problems)


def test_bad_direction_rejected() -> None:
    problems = validate(_config([_gps(direction="sideways")]))
    assert any("direction 'sideways' invalid" in p for p in problems)


# --- RX whitelist -----------------------------------------------------------------


def test_rx_accept_must_name_real_state_fields() -> None:
    problems = validate(_config([_gps(rx_feeds_state=True, rx_accept=["not_a_field"])]))
    assert any("not a VesselState field" in p for p in problems)


def test_rx_feeds_state_with_empty_whitelist_warns() -> None:
    problems = validate(_config([_gps(rx_feeds_state=True, rx_accept=[])]))
    assert any("accepts nothing" in p for p in problems)


# --- cross-channel collisions -----------------------------------------------------


def test_duplicate_device_path_rejected() -> None:
    a = _gps(id="a", path="/dev/serial/by-id/same")
    b = _gps(id="b", path="/dev/serial/by-id/same")
    problems = validate(_config([a, b]))
    assert any("already used by" in p for p in problems)


def test_placeholder_paths_do_not_collide() -> None:
    a = _gps(id="a", path="/dev/serial/by-id/CHANGE-ME-gps")
    b = _gps(id="b", path="/dev/serial/by-id/CHANGE-ME-heading")
    # Distinct placeholders (and even identical ones) never count as a real collision.
    assert not any("already used by" in p for p in validate(_config([a, b])))


def test_duplicate_tap_port_rejected() -> None:
    a = _gps(id="a", path="/dev/serial/by-id/a", tcp_tap=TcpTapSpec(enabled=True, port=10110))
    b = _gps(id="b", path="/dev/serial/by-id/b", tcp_tap=TcpTapSpec(enabled=True, port=10110))
    problems = validate(_config([a, b]))
    assert any("collides with" in p for p in problems)


def test_disabled_taps_never_collide() -> None:
    a = _gps(id="a", path="/dev/serial/by-id/a", tcp_tap=TcpTapSpec(enabled=False, port=10110))
    b = _gps(id="b", path="/dev/serial/by-id/b", tcp_tap=TcpTapSpec(enabled=False, port=10110))
    assert not any("collides" in p for p in validate(_config([a, b])))


def test_tap_port_out_of_range_rejected() -> None:
    a = _gps(tcp_tap=TcpTapSpec(enabled=True, port=70000))
    assert any("out of range" in p for p in validate(_config([a])))


def test_duplicate_channel_id_rejected() -> None:
    a = _gps(id="dup", path="/dev/serial/by-id/a")
    b = _gps(id="dup", path="/dev/serial/by-id/b")
    problems = validate(_config([a, b]))
    assert any("duplicate channel id" in p for p in problems)


# --- initial-state ranges ---------------------------------------------------------


def test_out_of_range_latitude_rejected() -> None:
    bad = dict(_STATE, lat=200.0)
    problems = validate(_config([_gps()], initial_state_raw=bad))
    assert any("initial_state.lat" in p and "above maximum" in p for p in problems)


def test_missing_lat_rejected() -> None:
    bad = {k: v for k, v in _STATE.items() if k != "lat"}
    problems = validate(_config([_gps()], initial_state_raw=bad))
    assert any("missing required field 'lat'" in p for p in problems)


def test_negative_speed_rejected() -> None:
    bad = dict(_STATE, sog_kn=-3.0)
    problems = validate(_config([_gps()], initial_state_raw=bad))
    assert any("initial_state.sog_kn" in p for p in problems)


def test_out_of_range_sea_state_rejected() -> None:
    bad = dict(_STATE, sea_state=12)
    problems = validate(_config([_gps()], initial_state_raw=bad))
    assert any("initial_state.sea_state" in p and "above maximum 9" in p for p in problems)


def test_negative_depth_rejected() -> None:
    bad = dict(_STATE, depth_m=-1.0)
    problems = validate(_config([_gps()], initial_state_raw=bad))
    assert any("initial_state.depth_m" in p and "below minimum" in p for p in problems)


def test_out_of_range_wind_dir_rejected() -> None:
    bad = dict(_STATE, wind_dir_deg=400.0)
    problems = validate(_config([_gps()], initial_state_raw=bad))
    assert any("initial_state.wind_dir_deg" in p and "above maximum 360" in p for p in problems)


def test_in_range_new_fields_pass() -> None:
    good = dict(
        _STATE,
        sea_state=4,
        depth_m=120.0,
        stw_kn=9.5,
        wind_speed_kn=22.0,
        wind_dir_deg=200.0,
        rot_dpm=-15.0,
    )
    assert validate(_config([_gps()], initial_state_raw=good)) == []


# --- global checks ----------------------------------------------------------------


def test_wildcard_tap_host_rejected() -> None:
    problems = validate(_config([_gps()], tcp_tap_host="0.0.0.0"))  # noqa: S104
    assert any("0.0.0.0 wildcard" in p for p in problems)


def test_simulated_time_requires_epoch() -> None:
    cfg = _config([_gps()], time_source=TimeSourceSpec(mode="simulated", epoch=None))
    assert any("requires an 'epoch'" in p for p in validate(cfg))


def test_empty_channels_rejected() -> None:
    assert any("no channels" in p for p in validate(_config([])))


# --- baud budget ------------------------------------------------------------------


def test_over_budget_channel_rejected() -> None:
    # HDG + HDT both at 10 Hz on 4800 8N1 is ~100% of the wire — over the 80% budget.
    heading = ChannelSpec(
        id="heading",
        role="heading",
        path="/dev/serial/by-id/hd",
        baud=4800,
        talker="HE",
        emit=[EmitSpec("HDT", 10.0), EmitSpec("HDG", 10.0)],
    )
    assert any("over baud budget" in p for p in validate(_config([heading])))


# --- operating mode + inputs seam (B3a) -------------------------------------------


def test_bad_mode_rejected_by_validator() -> None:
    # The dataclass guard already refuses a bad mode at construction, so reach the validator's
    # belt-and-braces check by writing the frozen field directly (what a bypassing caller does).
    cfg = _config([_gps()])
    object.__setattr__(cfg, "mode", "replay")
    assert any("mode 'replay' invalid" in p and "simulate|auto" in p for p in validate(cfg))


def test_auto_mode_requires_serial_backend() -> None:
    cfg = _config([_gps()], mode="auto", writer_backend="log", inputs=[_input()])
    problems = validate(cfg)
    assert any("mode 'auto' requires writer_backend 'serial'" in p for p in problems)


def test_auto_mode_requires_at_least_one_input() -> None:
    cfg = _config([_gps()], mode="auto", writer_backend="serial", inputs=[])
    assert any("mode 'auto' requires at least one entry in 'inputs'" in p for p in validate(cfg))


def test_channel_source_must_name_a_defined_input() -> None:
    cfg = _config([_gps(sources=["ghost_in"])], inputs=[])
    assert any("source 'ghost_in' does not match any inputs[].id" in p for p in validate(cfg))


def test_duplicate_input_id_rejected() -> None:
    # Placeholder paths keep this focused on the id clash, not a path collision.
    inputs = [_input(id="dup", path="none"), _input(id="dup", path="none")]
    assert any("duplicate input id 'dup'" in p for p in validate(_config([_gps()], inputs=inputs)))


def test_input_path_colliding_with_channel_path_rejected() -> None:
    shared = "/dev/serial/by-id/shared-tty"
    cfg = _config([_gps(path=shared)], inputs=[_input(path=shared)])
    problems = validate(cfg)
    assert any("already used by" in p and "input 'gps_in'" in p for p in problems)


def test_input_path_colliding_with_another_input_path_rejected() -> None:
    shared = "/dev/serial/by-id/shared-in"
    inputs = [_input(id="a", path=shared), _input(id="b", path=shared)]
    assert any("already used by" in p for p in validate(_config([_gps()], inputs=inputs)))


def test_input_bad_function_rejected_by_validator() -> None:
    # InputSpec's own guard refuses a bad function at construction, so reach the validator's
    # belt-and-braces check by writing the frozen field directly.
    inp = _input()
    object.__setattr__(inp, "function", "radar")
    problems = validate(_config([_gps()], inputs=[inp]))
    assert any("unknown function 'radar'" in p and "gps|sat|ais|unused" in p for p in problems)


def test_input_non_positive_timeouts_rejected() -> None:
    liveness = validate(_config([_gps()], inputs=[_input(liveness_timeout_s=0.0)]))
    assert any("liveness_timeout_s must be > 0" in p for p in liveness)
    read = validate(_config([_gps()], inputs=[_input(read_timeout_s=-0.01)]))
    assert any("read_timeout_s must be > 0" in p for p in read)


def test_input_non_positive_baud_rejected() -> None:
    problems = validate(_config([_gps()], inputs=[_input(baud=0)]))
    assert any("input 'gps_in': baud must be > 0" in p for p in problems)


def test_auto_sources_and_rx_feeds_state_conflict_rejected() -> None:
    # Both the top-level input and the per-channel rx_feeds_state path would write shared state
    # from the same wire in auto mode — a hard error.
    ch = _gps(
        direction="both",
        sources=["gps_in"],
        rx_feeds_state=True,
        rx_accept=["lat"],
    )
    cfg = _config([ch], mode="auto", writer_backend="serial", inputs=[_input()])
    assert any(
        "cannot set both 'sources' and rx_feeds_state=true in auto mode" in p for p in validate(cfg)
    )


def test_shipped_config_still_validates_and_stays_simulate() -> None:
    cfg = EngineConfig.load(CONFIG_PATH)
    assert cfg.mode == "simulate"
    assert validate(cfg) == []


# --- validate_or_raise ------------------------------------------------------------


def test_validate_or_raise_raises_on_invalid() -> None:
    with pytest.raises(ConfigError):
        validate_or_raise(_config([_gps(role="sonar")]))


def test_validate_or_raise_passes_on_valid() -> None:
    validate_or_raise(_config([_gps()]))  # must not raise


# --- save / to_dict round-trip ----------------------------------------------------


def test_save_round_trips_through_load(tmp_path: Path) -> None:
    original = EngineConfig.load(CONFIG_PATH)
    out = tmp_path / "round.json"
    original.save(out)
    reloaded = EngineConfig.load(out)
    assert reloaded.to_dict() == original.to_dict()


def test_save_is_atomic_and_valid_json(tmp_path: Path) -> None:
    out = tmp_path / "cfg.json"
    _config([_gps()]).save(out)
    # File is complete, parseable JSON with no leftover temp files beside it.
    assert json.loads(out.read_text(encoding="utf-8"))["channels"][0]["id"] == "gps"
    assert list(tmp_path.iterdir()) == [out]


def test_edit_then_save_persists(tmp_path: Path) -> None:
    cfg = _config([_gps()], movement=MovementSpec(mode="underway", physics_hz=5.0))
    out = tmp_path / "edited.json"
    cfg.save(out)
    reloaded = EngineConfig.load(out)
    assert reloaded.movement.mode == "underway"
    assert reloaded.movement.physics_hz == 5.0
