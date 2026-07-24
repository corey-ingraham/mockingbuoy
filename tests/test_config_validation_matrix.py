"""Data-driven matrix over the deep cross-field validator (``config.validate()``).

Each row builds a config that violates exactly one rule and asserts the validator emits a
problem containing the rule's real, human-readable substring (taken verbatim from
``nmea_sim/validate.py`` — not invented here), so a wrong-rule regression is caught rather
than merely "some error happened". A final row asserts a fully valid config yields ``[]``.

All configs are built from small synthetic dataclasses inline; nothing here references any
real host, device path, or location profile.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from nmea_sim.config import (
    AisOwnShip,
    AisSpec,
    AisTrafficSpec,
    ChannelSpec,
    EmitSpec,
    EngineConfig,
    TcpTapSpec,
    TimeSourceSpec,
)

# --- synthetic building blocks (no real paths/locations) --------------------------


def _initial_state() -> dict[str, Any]:
    """A known-good, fully in-range initial fix."""
    return {
        "lat": 0.0,
        "lon": 0.0,
        "sog_kn": 0.0,
        "cog_deg": 90.0,
        "heading_true_deg": 90.0,
        "heading_mag_deg": 90.0,
        "mag_variation_deg": 0.0,
        "altitude_m": 5.0,
        "fix_quality": 1,
        "satellites": 10,
        "hdop": 0.8,
    }


def _gps_channel() -> ChannelSpec:
    return ChannelSpec(
        id="gps",
        role="gps",
        path="/dev/serial/by-id/synthetic-gps-A",
        baud=4800,
        talker="GP",
        emit=[EmitSpec("GGA", 1.0), EmitSpec("RMC", 1.0)],
    )


def _heading_channel() -> ChannelSpec:
    return ChannelSpec(
        id="heading",
        role="heading",
        path="/dev/serial/by-id/synthetic-hdg-A",
        baud=4800,
        talker="HE",
        emit=[EmitSpec("HDT", 10.0)],
    )


def _ais_channel() -> ChannelSpec:
    return ChannelSpec(
        id="ais",
        role="ais",
        path="/dev/serial/by-id/synthetic-ais-A",
        baud=38400,
        emit=[EmitSpec("AIVDM", 0.2)],
        ais=AisSpec(own_ship=AisOwnShip(mmsi=366000001)),
    )


def _config(
    *,
    channels: list[ChannelSpec] | None = None,
    initial_state: dict[str, Any] | None = None,
    time_source: TimeSourceSpec | None = None,
) -> EngineConfig:
    """A valid-by-default config; each row overrides just the piece it wants to break."""
    return EngineConfig(
        channels=channels if channels is not None else [_gps_channel(), _heading_channel()],
        initial_state_raw=initial_state if initial_state is not None else _initial_state(),
        time_source=time_source if time_source is not None else TimeSourceSpec(),
    )


# --- bad-config builders (one broken rule each) -----------------------------------


def _bad_role_sentence() -> EngineConfig:
    # gps role cannot build a heading sentence
    return _config(channels=[replace(_gps_channel(), emit=[EmitSpec("HDT", 1.0)])])


def _missing_talker() -> EngineConfig:
    return _config(channels=[replace(_gps_channel(), talker="")])


def _duplicate_channel_id() -> EngineConfig:
    return _config(channels=[_gps_channel(), replace(_heading_channel(), id="gps")])


def _duplicate_device_path() -> EngineConfig:
    first = _gps_channel()
    return _config(channels=[first, replace(_heading_channel(), path=first.path)])


def _tcp_tap_port_out_of_range() -> EngineConfig:
    return _config(channels=[replace(_gps_channel(), tcp_tap=TcpTapSpec(enabled=True, port=70000))])


def _duplicate_tcp_tap_port() -> EngineConfig:
    tap = TcpTapSpec(enabled=True, port=10110)
    return _config(
        channels=[
            replace(_gps_channel(), tcp_tap=tap),
            replace(_heading_channel(), tcp_tap=tap),
        ]
    )


def _baud_budget_exceeded() -> EngineConfig:
    # HDT + HDG both at 10 Hz on 4800 8N1 is ~96% of the wire — over the 80% guard.
    over = replace(_heading_channel(), emit=[EmitSpec("HDT", 10.0), EmitSpec("HDG", 10.0)])
    return _config(channels=[over])


def _rx_feeds_state_empty_accept() -> EngineConfig:
    ch = replace(_gps_channel(), direction="both", rx_feeds_state=True, rx_accept=[])
    return _config(channels=[ch])


def _rx_accept_unknown_field() -> EngineConfig:
    ch = replace(
        _gps_channel(),
        direction="both",
        rx_feeds_state=True,
        rx_accept=["not_a_state_field"],
    )
    return _config(channels=[ch])


def _rx_direction_with_emit() -> EngineConfig:
    # direction rx but keeps its emit list
    return _config(channels=[replace(_gps_channel(), direction="rx")])


def _tx_without_emit() -> EngineConfig:
    return _config(channels=[replace(_gps_channel(), emit=[])])


def _ais_traffic_missing_profile() -> EngineConfig:
    traffic = AisTrafficSpec(
        enabled=True, profile_path="/nonexistent/synthetic-profile-does-not-exist.json"
    )
    ais = AisSpec(own_ship=AisOwnShip(mmsi=366000001), traffic=traffic)
    return _config(channels=[replace(_ais_channel(), ais=ais)])


def _simulated_without_epoch() -> EngineConfig:
    return _config(time_source=TimeSourceSpec(mode="simulated", epoch=None))


def _empty_channels() -> EngineConfig:
    return _config(channels=[])


def _initial_state_out_of_range() -> EngineConfig:
    state = _initial_state()
    state["lat"] = 200.0
    return _config(initial_state=state)


def _initial_state_missing_field() -> EngineConfig:
    state = _initial_state()
    del state["lat"]
    return _config(initial_state=state)


# --- the matrix -------------------------------------------------------------------
# (builder, expected human-readable substring). Substrings are distinctive enough that a
# rule firing under the wrong message would fail the assertion.

_REJECTIONS: list[tuple[str, Callable[[], EngineConfig], str]] = [
    ("bad_role_sentence", _bad_role_sentence, "role 'gps' cannot emit 'HDT'"),
    ("missing_talker", _missing_talker, "role 'gps' requires a 'talker'"),
    ("duplicate_channel_id", _duplicate_channel_id, "duplicate channel id 'gps'"),
    ("duplicate_device_path", _duplicate_device_path, "already used by"),
    ("tcp_tap_port_out_of_range", _tcp_tap_port_out_of_range, "out of range 1-65535"),
    ("duplicate_tcp_tap_port", _duplicate_tcp_tap_port, "collides with"),
    ("baud_budget_exceeded", _baud_budget_exceeded, "over baud budget"),
    (
        "rx_feeds_state_empty_accept",
        _rx_feeds_state_empty_accept,
        "rx_feeds_state is true but rx_accept is empty",
    ),
    (
        "rx_accept_unknown_field",
        _rx_accept_unknown_field,
        "rx_accept field 'not_a_state_field' is not a VesselState field",
    ),
    (
        "rx_direction_with_emit",
        _rx_direction_with_emit,
        "direction 'rx' cannot have 'emit' entries",
    ),
    ("tx_without_emit", _tx_without_emit, "direction 'tx' but no 'emit' entries"),
    (
        "ais_traffic_missing_profile",
        _ais_traffic_missing_profile,
        "ais.traffic.profile_path",
    ),
    (
        "simulated_without_epoch",
        _simulated_without_epoch,
        "time_source.mode 'simulated' requires an 'epoch'",
    ),
    ("empty_channels", _empty_channels, "config has no channels"),
    ("initial_state_out_of_range", _initial_state_out_of_range, "initial_state.lat"),
    (
        "initial_state_missing_field",
        _initial_state_missing_field,
        "initial_state: missing required field 'lat'",
    ),
]


@pytest.mark.parametrize(
    ("builder", "expected"),
    [pytest.param(builder, expected, id=name) for name, builder, expected in _REJECTIONS],
)
def test_validate_rejects(builder: Callable[[], EngineConfig], expected: str) -> None:
    problems = builder().validate()
    matches = [p for p in problems if expected in p]
    assert matches, f"expected a problem containing {expected!r}, got {problems!r}"


def test_ais_traffic_missing_profile_message_is_specific() -> None:
    # Guard the exact rule: a set-but-missing profile path is a hard "does not exist" error,
    # not merely a mention of the field name.
    problems = _ais_traffic_missing_profile().validate()
    assert any("does not exist" in p for p in problems), problems


def test_initial_state_out_of_range_reports_the_bound() -> None:
    problems = _initial_state_out_of_range().validate()
    assert any("above maximum 90" in p for p in problems), problems


def test_validate_accepts_known_good_config() -> None:
    channels = [_gps_channel(), _heading_channel(), _ais_channel()]
    cfg = _config(channels=channels)
    assert cfg.validate() == []
