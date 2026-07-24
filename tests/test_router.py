"""Router: the pure in-memory arbiter — classify+record+route, ordered winner, cross-routing.

Every case injects an explicit ``now`` so liveness is deterministic without a clock or a sleep:
the router holds no threads and touches no sink, so it is tested entirely by method calls.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nmea_sim.config import ChannelSpec, EmitSpec, EngineConfig, InputSpec
from nmea_sim.gps_generator import GpsGenerator
from nmea_sim.heading_generator import HeadingGenerator
from nmea_sim.router import Router
from nmea_sim.state import VesselState

_STATE = VesselState(
    lat=25.12345,
    lon=-80.54321,
    sog_kn=12.3,
    cog_deg=95.0,
    heading_true_deg=280.0,
    heading_mag_deg=283.0,
    mag_variation_deg=-3.0,
    altitude_m=15.4,
    fix_quality=1,
    satellites=9,
    hdop=0.8,
    utc=datetime(2024, 6, 21, 12, 0, 0, tzinfo=UTC),
)

_RMC = GpsGenerator("GP").rmc(_STATE)
_GGA = GpsGenerator("GP").gga(_STATE)
_HDT = HeadingGenerator("HE").hdt(_STATE)


def _gps_channel(sources: list[str]) -> ChannelSpec:
    return ChannelSpec(
        id="gps",
        role="gps",
        path="none",
        baud=38400,
        talker="GP",
        emit=[EmitSpec("RMC", 1.0)],
        sources=sources,
    )


def _heading_channel(sources: list[str]) -> ChannelSpec:
    return ChannelSpec(
        id="heading",
        role="heading",
        path="none",
        baud=4800,
        talker="HE",
        emit=[EmitSpec("HDT", 1.0)],
        sources=sources,
    )


def _router(channels: list[ChannelSpec], inputs: list[InputSpec]) -> Router:
    # mode stays the default "simulate": the Router only reads channels + inputs, so it needs no
    # serial backend and no auto-mode preconditions to be exercised in memory.
    return Router(EngineConfig(channels=channels, inputs=inputs))


# --- note_rx: classify + record + route -------------------------------------------


def test_note_rx_routes_gnss_to_gps_channel() -> None:
    router = _router([_gps_channel(["gps_in"])], [InputSpec(id="gps_in", path="none")])
    routed = router.note_rx("gps_in", _RMC, now=100.0)
    assert routed == ("gps", "gnss", _RMC)


def test_note_rx_routes_heading_to_heading_channel() -> None:
    router = _router([_heading_channel(["sat_in"])], [InputSpec(id="sat_in", path="none")])
    routed = router.note_rx("sat_in", _HDT, now=100.0)
    assert routed == ("heading", "heading", _HDT)


def test_note_rx_returns_none_for_unclassifiable_line() -> None:
    router = _router([_gps_channel(["gps_in"])], [InputSpec(id="gps_in", path="none")])
    assert router.note_rx("gps_in", "not a sentence", now=100.0) is None


def test_note_rx_drops_class_whose_input_is_not_a_source() -> None:
    """A heading line arriving on an input that only the gps channel lists must be dropped —
    the input is not a source for the heading channel, so it can't make it look live."""
    router = _router(
        [_gps_channel(["gps_in"]), _heading_channel(["sat_in"])],
        [InputSpec(id="gps_in", path="none"), InputSpec(id="sat_in", path="none")],
    )
    # gps_in is a source for the gps (gnss) channel only; a heading line on it has no home.
    assert router.note_rx("gps_in", _HDT, now=100.0) is None
    # And it never stamped liveness, so the heading channel still has no live source.
    assert router.winner("heading", "heading", now=100.0) is None


def test_note_rx_drops_gnss_when_no_channel_consumes_it() -> None:
    # Only a heading channel exists; a gnss line has no target channel at all.
    router = _router([_heading_channel(["sat_in"])], [InputSpec(id="sat_in", path="none")])
    assert router.note_rx("sat_in", _RMC, now=100.0) is None


# --- winner: source ORDER + per-input liveness_timeout ----------------------------


def test_winner_prefers_higher_priority_source_and_expires_in_order() -> None:
    """gps_in outranks sat_in; while both are live gps_in wins. gps_in expires after its own
    (short) timeout and sat_in takes over; once sat_in expires too, the channel has no winner."""
    router = _router(
        [_gps_channel(["gps_in", "sat_in"])],
        [
            InputSpec(id="gps_in", path="none", liveness_timeout_s=1.0),
            InputSpec(id="sat_in", path="none", liveness_timeout_s=5.0),
        ],
    )
    router.note_rx("gps_in", _RMC, now=100.0)
    router.note_rx("sat_in", _GGA, now=100.0)

    assert router.winner("gps", "gnss", now=100.0) == "gps_in"  # both live -> priority wins
    assert router.any_live("gps", "gnss", now=100.0) is True
    # gps_in expired (>1.0s), sat_in still within its 5.0s window -> fallback source wins.
    assert router.winner("gps", "gnss", now=101.5) == "sat_in"
    # Both expired -> no winner, the channel falls back to generating.
    assert router.winner("gps", "gnss", now=106.5) is None
    assert router.any_live("gps", "gnss", now=106.5) is False


def test_winner_boundary_is_inclusive_of_the_timeout() -> None:
    router = _router(
        [_gps_channel(["gps_in"])],
        [InputSpec(id="gps_in", path="none", liveness_timeout_s=2.0)],
    )
    router.note_rx("gps_in", _RMC, now=50.0)
    assert router.winner("gps", "gnss", now=52.0) == "gps_in"  # exactly at the timeout: still live
    assert router.winner("gps", "gnss", now=52.001) is None  # a hair past: dead


# --- cross-routing: one input feeds two channels ----------------------------------


def test_sat_input_feeds_both_heading_and_gps_channels() -> None:
    """A satellite compass wired to ``sat_in`` carries heading (for the heading channel) AND GNSS
    position/time (for the gps channel), so one input legitimately routes to both outputs."""
    router = _router(
        [_gps_channel(["gps_in", "sat_in"]), _heading_channel(["sat_in"])],
        [
            InputSpec(id="gps_in", path="none", liveness_timeout_s=3.0),
            InputSpec(id="sat_in", path="none", liveness_timeout_s=3.0),
        ],
    )
    assert router.note_rx("sat_in", _HDT, now=10.0) == ("heading", "heading", _HDT)
    assert router.note_rx("sat_in", _RMC, now=10.0) == ("gps", "gnss", _RMC)

    # The sat is the sole live source for heading, but only the fallback for gps.
    assert router.winner("heading", "heading", now=10.0) == "sat_in"
    assert router.winner("gps", "gnss", now=10.0) == "sat_in"  # gps_in never spoke -> sat wins

    # Once the higher-priority gps_in speaks, it takes the gps channel but leaves heading untouched.
    router.note_rx("gps_in", _GGA, now=11.0)
    assert router.winner("gps", "gnss", now=11.0) == "gps_in"
    assert router.winner("heading", "heading", now=11.0) == "sat_in"


# --- source_label + channel_class -------------------------------------------------


def test_source_label_reports_live_input_then_sim_on_expiry() -> None:
    router = _router(
        [_gps_channel(["gps_in"])],
        [InputSpec(id="gps_in", path="none", liveness_timeout_s=2.0)],
    )
    assert router.source_label("gps", "gnss", now=0.0) == "SIM"  # nothing seen yet
    router.note_rx("gps_in", _RMC, now=0.0)
    assert router.source_label("gps", "gnss", now=1.0) == "LIVE:gps_in"
    assert router.source_label("gps", "gnss", now=9.0) == "SIM"  # source went dead


def test_channel_class_maps_role_to_consumed_class() -> None:
    instrument = ChannelSpec(
        id="instrument",
        role="instrument",
        path="none",
        baud=38400,
        talker="II",
        emit=[EmitSpec("VHW", 1.0)],
    )
    router = _router(
        [_gps_channel(["gps_in"]), _heading_channel(["sat_in"]), instrument],
        [InputSpec(id="gps_in", path="none"), InputSpec(id="sat_in", path="none")],
    )
    assert router.channel_class("gps") == "gnss"
    assert router.channel_class("heading") == "heading"
    # An instrument channel consumes no live class, so it is never suppressed by a source.
    assert router.channel_class("instrument") is None
    assert router.channel_class("no-such-channel") is None
