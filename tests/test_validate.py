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
    ReplaySpec,
    RouteSpec,
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


def test_tap_only_requires_a_tcp_tap() -> None:
    # A tap-only channel with no tcp_tap at all emits nowhere -> rejected.
    problems = validate(_config([_instrument(tap_only=True)]))
    assert any("tap_only" in p for p in problems)


def test_tap_only_with_disabled_tcp_tap_is_rejected() -> None:
    # A present-but-disabled tap is no output either.
    ch = _instrument(tap_only=True, tcp_tap=TcpTapSpec(enabled=False, port=10110))
    assert any("tap_only" in p for p in validate(_config([ch])))


def test_tap_only_with_enabled_tcp_tap_is_valid() -> None:
    ch = _instrument(tap_only=True, tcp_tap=TcpTapSpec(enabled=True, port=10110))
    assert validate(_config([ch])) == []


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
    object.__setattr__(cfg, "mode", "bogus")
    assert any("mode 'bogus' invalid" in p and "simulate|auto|replay" in p for p in validate(cfg))


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


def test_auto_mode_requires_static_movement() -> None:
    # Physics must not dead-reckon position while a live GNSS source may own it, or LIVE and DR'd
    # position fight; auto mode therefore requires movement.mode 'static'.
    cfg = _config(
        [_gps(sources=["gps_in"])],
        mode="auto",
        writer_backend="serial",
        inputs=[_input()],
        movement=MovementSpec(mode="underway"),
    )
    assert any(
        "auto mode requires movement.mode 'static' so simulated dead-reckoning cannot "
        "clobber live passthrough position" in p
        for p in validate(cfg)
    )


def test_auto_mode_with_static_movement_passes_the_movement_rule() -> None:
    # The same config with static movement raises no movement-mode complaint (control case).
    cfg = _config(
        [_gps(sources=["gps_in"])],
        mode="auto",
        writer_backend="serial",
        inputs=[_input()],
        movement=MovementSpec(mode="static"),
    )
    assert not any("auto mode requires movement.mode 'static'" in p for p in validate(cfg))


def test_shipped_config_still_validates_and_stays_simulate() -> None:
    cfg = EngineConfig.load(CONFIG_PATH)
    assert cfg.mode == "simulate"
    assert validate(cfg) == []


# --- route playback preconditions (F1, R53/R54) -----------------------------------


def _route_cfg(**over: object) -> EngineConfig:
    """A simulate+underway config carrying an enabled two-waypoint route (the valid baseline)."""
    base: dict[str, object] = {
        "mode": "simulate",
        "movement": MovementSpec(mode="underway"),
        "route": RouteSpec(enabled=True, waypoints=[(0.0, 0.0), (1.0, 1.0)], speed_kn=10.0),
    }
    base.update(over)
    return _config([_gps()], **base)


def test_route_enabled_valid_simulate_underway_two_waypoints_passes() -> None:
    assert validate(_route_cfg()) == []


def test_route_enabled_requires_underway() -> None:
    cfg = _route_cfg(movement=MovementSpec(mode="static"))
    assert any("route.enabled requires movement.mode 'underway'" in p for p in validate(cfg))


def test_route_enabled_requires_at_least_two_waypoints() -> None:
    cfg = _route_cfg(route=RouteSpec(enabled=True, waypoints=[(0.0, 0.0)], speed_kn=10.0))
    assert any("route.enabled requires at least 2 waypoints" in p for p in validate(cfg))


def test_route_enabled_requires_simulate_mode() -> None:
    # Auto mode carries its own extra errors (serial backend, static movement); we assert only
    # that the route-mode precondition message is among the reported problems.
    cfg = _route_cfg(mode="auto", writer_backend="serial", inputs=[_input()])
    assert any("route.enabled requires mode 'simulate'" in p for p in validate(cfg))


# --- replay mode preconditions (F2, R54) ------------------------------------------


def test_replay_mode_requires_enabled_block() -> None:
    cfg = _config([_gps()], mode="replay")
    assert any("requires a 'replay' block with enabled=true" in p for p in validate(cfg))


def test_replay_mode_requires_non_empty_file() -> None:
    cfg = _config([_gps()], mode="replay", replay=ReplaySpec(enabled=True, file=""))
    assert any("replay.file to name a capture" in p for p in validate(cfg))


def test_replay_mode_requires_existing_file() -> None:
    cfg = _config(
        [_gps()],
        mode="replay",
        replay=ReplaySpec(enabled=True, file="/no/such/capture-file.nmea"),
    )
    assert any("does not exist" in p for p in validate(cfg))


def test_replay_mode_with_existing_file_passes(tmp_path: Path) -> None:
    cap = tmp_path / "cap.nmea"
    cap.write_text("$GPRMC,123519,A,4807.038,N,01131.000,E,,,230394,,*4B\n", encoding="utf-8")
    cfg = _config([_gps()], mode="replay", replay=ReplaySpec(enabled=True, file=str(cap)))
    assert validate(cfg) == []


def test_route_and_replay_are_mutually_exclusive(tmp_path: Path) -> None:
    cap = tmp_path / "cap.nmea"
    cap.write_text("$GPRMC\n", encoding="utf-8")
    cfg = _config(
        [_gps()],
        mode="simulate",
        movement=MovementSpec(mode="underway"),
        route=RouteSpec(enabled=True, waypoints=[(0.0, 0.0), (1.0, 1.0)], speed_kn=1.0),
        replay=ReplaySpec(enabled=True, file=str(cap)),
    )
    assert any("incompatible with replay" in p for p in validate(cfg))


# --- replay scope selector (Scope C) ----------------------------------------------


def _ais(**over: object) -> ChannelSpec:
    from nmea_sim.config import AisOwnShip, AisSpec

    base: dict[str, object] = {
        "id": "ais",
        "role": "ais",
        "path": "/dev/serial/by-id/unit-ais",
        "baud": 38400,
        "talker": "AI",
        "emit": [EmitSpec("AIVDM", 1.0)],
        "ais": AisSpec(own_ship=AisOwnShip(mmsi=366000123, klass="A")),
    }
    base.update(over)
    return ChannelSpec(**base)  # type: ignore[arg-type]


def test_replay_scope_full_default_passes(tmp_path: Path) -> None:
    cap = tmp_path / "cap.nmea"
    cap.write_text("$GPRMC\n", encoding="utf-8")
    cfg = _config([_gps()], mode="replay", replay=ReplaySpec(enabled=True, file=str(cap)))
    assert validate(cfg) == []  # scope defaults to "full"


def test_replay_scope_bad_enum_rejected(tmp_path: Path) -> None:
    cap = tmp_path / "cap.nmea"
    cap.write_text("$GPRMC\n", encoding="utf-8")
    cfg = _config(
        [_gps()],
        mode="replay",
        replay=ReplaySpec(enabled=True, file=str(cap), scope="bogus"),
    )
    assert any("replay.scope 'bogus' invalid" in p and "full|ais-only" in p for p in validate(cfg))


def test_replay_scope_ais_only_requires_ais_channel(tmp_path: Path) -> None:
    cap = tmp_path / "cap.nmea"
    cap.write_text("$GPRMC\n", encoding="utf-8")
    # gps channel only, no ais channel -> ais-only has nowhere to land the replayed contacts.
    cfg = _config(
        [_gps()],
        mode="replay",
        replay=ReplaySpec(enabled=True, file=str(cap), scope="ais-only"),
    )
    assert any(
        "replay.scope 'ais-only' requires a channel with role 'ais'" in p for p in validate(cfg)
    )


def test_replay_scope_ais_only_with_ais_channel_passes(tmp_path: Path) -> None:
    cap = tmp_path / "cap.nmea"
    cap.write_text("$GPRMC\n", encoding="utf-8")
    cfg = _config(
        [_gps(), _ais()],
        mode="replay",
        replay=ReplaySpec(enabled=True, file=str(cap), scope="ais-only"),
    )
    assert validate(cfg) == []


# --- per-sentence enable/rate x baud budget (F4) ----------------------------------


def _heading_hdt_hdg(*, hdg_enabled: bool) -> ChannelSpec:
    # HDT + HDG both at 10 Hz on 4800 8N1 is over the 80% budget; disabling HDG frees its slot.
    return ChannelSpec(
        id="heading",
        role="heading",
        path="/dev/serial/by-id/hd",
        baud=4800,
        talker="HE",
        emit=[EmitSpec("HDT", 10.0), EmitSpec("HDG", 10.0, enabled=hdg_enabled)],
    )


def test_disabled_emit_frees_baud_budget() -> None:
    # Both sentences enabled busts the wire...
    assert any(
        "over baud budget" in p for p in validate(_config([_heading_hdt_hdg(hdg_enabled=True)]))
    )
    # ...disabling one frees its budget, so the same channel now validates cleanly.
    assert validate(_config([_heading_hdt_hdg(hdg_enabled=False)])) == []


def test_per_sentence_rate_change_busting_budget_fails_validate() -> None:
    # A GPS channel that fits at 1 Hz busts budget once one sentence is re-rated far higher.
    ok = _gps(baud=4800, emit=[EmitSpec("GGA", 1.0), EmitSpec("RMC", 1.0)])
    assert validate(_config([ok])) == []
    busted = _gps(baud=4800, emit=[EmitSpec("GGA", 1.0), EmitSpec("RMC", 50.0)])
    assert any("over baud budget" in p for p in validate(_config([busted])))


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


# --- R22/R52 range consistency: web edit bounds must agree with the state validator -----


# --- AIS own-ship identity validation (H8) ----------------------------------------


def _ais_own(**own_over: object) -> ChannelSpec:
    """An AIS channel whose own-ship identity fields can be overridden (valid by default)."""
    from nmea_sim.config import AisOwnShip, AisSpec

    base: dict[str, object] = {
        "mmsi": 366000123,
        "klass": "A",
        "name": "TESTBUOY",
        "call_sign": "TB1",
        "ship_type": 37,
    }
    base.update(own_over)
    return _ais(ais=AisSpec(own_ship=AisOwnShip(**base)))  # type: ignore[arg-type]


def test_ais_valid_identity_passes() -> None:
    assert validate(_config([_ais_own()])) == []


@pytest.mark.parametrize("bad_mmsi", [0, -5, 9999999999])
def test_ais_mmsi_out_of_range_rejected(bad_mmsi: int) -> None:
    problems = validate(_config([_ais_own(mmsi=bad_mmsi)]))
    assert any("ais.own_ship.mmsi" in p and "out of range" in p for p in problems)


@pytest.mark.parametrize("bad_type", [-1, 100, 700])
def test_ais_ship_type_out_of_range_rejected(bad_type: int) -> None:
    problems = validate(_config([_ais_own(ship_type=bad_type)]))
    assert any("ais.own_ship.ship_type" in p and "out of range" in p for p in problems)


def test_ais_name_too_long_rejected() -> None:
    problems = validate(_config([_ais_own(name="A" * 21)]))
    assert any("ais.own_ship.name" in p and "too long" in p for p in problems)


def test_ais_call_sign_too_long_rejected() -> None:
    problems = validate(_config([_ais_own(call_sign="ABCDEFGH")]))  # 8 chars, max 7
    assert any("ais.own_ship.call_sign" in p and "too long" in p for p in problems)


def test_ais_name_bad_charset_rejected() -> None:
    # Lower-case letters are not in the AIS 6-bit ASCII set and would be mangled on the wire.
    problems = validate(_config([_ais_own(name="test buoy")]))
    assert any("ais.own_ship.name" in p and "outside the" in p and "6-bit" in p for p in problems)


def test_ais_type5_period_non_positive_rejected() -> None:
    from nmea_sim.config import AisOwnShip, AisSpec

    ch = _ais(ais=AisSpec(own_ship=AisOwnShip(mmsi=366000123), type5_period_s=0.0))
    problems = validate(_config([ch]))
    assert any("ais.type5_period_s must be > 0" in p for p in problems)


# --- framing validation (M5) ------------------------------------------------------


@pytest.mark.parametrize("bad_framing", ["8X1", "9N1", "8N3", "8N", "abc"])
def test_bad_framing_rejected_without_raising(bad_framing: str) -> None:
    # validate() must never raise on a bad framing (its docstring promises a problem list), and it
    # must report the framing as unbuildable rather than letting budget.evaluate raise.
    problems = validate(_config([_gps(framing=bad_framing)]))
    assert any("is not buildable" in p and "framing" in p for p in problems)


def test_valid_framings_pass_framing_check() -> None:
    for good in ("8N1", "7E1", "8O2", "5N1"):
        assert not any("is not buildable" in p for p in validate(_config([_gps(framing=good)])))


def test_bad_input_framing_rejected() -> None:
    problems = validate(_config([_gps()], inputs=[_input(framing="8X1")]))
    assert any("input 'gps_in'" in p and "is not buildable" in p for p in problems)


# --- duplicate arbitrated role (M1) -----------------------------------------------


def test_duplicate_gps_role_rejected() -> None:
    a = _gps(id="gps_a", path="/dev/serial/by-id/unit-a")
    b = _gps(id="gps_b", path="/dev/serial/by-id/unit-b")
    problems = validate(_config([a, b]))
    assert any("duplicate role 'gps'" in p for p in problems)


def test_two_instrument_channels_allowed() -> None:
    # instrument is not arbitrated, so two of them raise no duplicate-role complaint.
    a = _instrument(id="inst_a", path="/dev/serial/by-id/unit-ia")
    b = _instrument(id="inst_b", path="/dev/serial/by-id/unit-ib")
    assert not any("duplicate role" in p for p in validate(_config([a, b])))


# --- writer_backend enum + time_source.rate (M7) ----------------------------------


def test_bad_writer_backend_rejected() -> None:
    problems = validate(_config([_gps()], writer_backend="bogus"))
    assert any("writer_backend 'bogus' invalid" in p for p in problems)


@pytest.mark.parametrize("backend", ["log", "null", "pty", "serial"])
def test_known_writer_backends_pass(backend: str) -> None:
    # A known backend raises no writer_backend complaint (serial carries other auto-mode rules,
    # so we assert only the backend enum message is absent).
    assert not any(
        "writer_backend" in p and "invalid" in p
        for p in validate(_config([_gps()], writer_backend=backend))
    )


def test_non_positive_rate_rejected() -> None:
    cfg = _config([_gps()], time_source=TimeSourceSpec(mode="system_utc", rate=0.0))
    assert any("time_source.rate must be a finite number > 0" in p for p in validate(cfg))


def test_non_numeric_rate_rejected_without_raising() -> None:
    # An uncoerced string rate must be reported, not raise a TypeError out of validate().
    ts = TimeSourceSpec(mode="system_utc")
    object.__setattr__(ts, "rate", "fast")
    cfg = _config([_gps()], time_source=ts)
    assert any("time_source.rate must be a finite number > 0" in p for p in validate(cfg))


# --- H2: transmitting channel needs an enabled emit entry -------------------------


def test_all_disabled_emit_on_tx_channel_rejected() -> None:
    ch = _gps(emit=[EmitSpec("GGA", 1.0, enabled=False), EmitSpec("RMC", 1.0, enabled=False)])
    problems = validate(_config([ch]))
    assert any("every 'emit' entry is disabled" in p for p in problems)


# --- H9: route speed + waypoint finiteness/range ----------------------------------


def test_route_enabled_requires_positive_speed() -> None:
    cfg = _route_cfg(
        route=RouteSpec(enabled=True, waypoints=[(0.0, 0.0), (1.0, 1.0)], speed_kn=0.0)
    )
    assert any("requires speed_kn > 0" in p for p in validate(cfg))


def test_route_out_of_range_waypoint_rejected() -> None:
    cfg = _route_cfg(
        route=RouteSpec(enabled=True, waypoints=[(95.0, 200.0), (96.0, 201.0)], speed_kn=8.0)
    )
    problems = validate(cfg)
    assert any("out of range" in p and "route.waypoints" in p for p in problems)


def test_route_nan_waypoint_rejected() -> None:
    nan = float("nan")
    cfg = _route_cfg(
        route=RouteSpec(enabled=True, waypoints=[(nan, 0.0), (1.0, 1.0)], speed_kn=8.0)
    )
    assert any("route.waypoints" in p and "is not finite" in p for p in validate(cfg))


def test_nan_initial_state_field_rejected() -> None:
    bad = dict(_STATE, sog_kn=float("nan"))
    problems = validate(_config([_gps()], initial_state_raw=bad))
    assert any("initial_state.sog_kn" in p and "is not a finite number" in p for p in problems)


def test_update_ranges_agree_with_state_ranges_for_manual_fields() -> None:
    """The web edit/persist bounds (``_UPDATE_RANGES``) and the state validator bounds
    (``_STATE_RANGES``) must AGREE for every manual own-ship field both define, so a value the
    UI accepts can never be one the config validator would later reject (or vice versa).

    This is a SUBSET check on the manual allow-list: derived ``pitch_deg``/``roll_deg`` (state-only)
    and web-only ``altitude_m`` legitimately live in just one table and are out of scope here — we
    assert equality only across the operator-editable manual fields, which both tables define."""
    from nmea_sim.validate import _STATE_RANGES
    from web.app import _INITIAL_STATE_MANUAL_FIELDS, _UPDATE_RANGES

    mismatches: dict[str, tuple[object, object]] = {}
    for field_name in _INITIAL_STATE_MANUAL_FIELDS:
        assert field_name in _UPDATE_RANGES, f"{field_name} missing from _UPDATE_RANGES"
        assert field_name in _STATE_RANGES, f"{field_name} missing from _STATE_RANGES"
        if _UPDATE_RANGES[field_name] != _STATE_RANGES[field_name]:
            mismatches[field_name] = (_UPDATE_RANGES[field_name], _STATE_RANGES[field_name])

    assert not mismatches, (
        "web _UPDATE_RANGES and validate._STATE_RANGES disagree on manual-field bounds "
        f"(field -> (update, state)): {mismatches}"
    )
