"""SharedState atomic updates and the P1 writers."""

from __future__ import annotations

import io
from dataclasses import fields

from nmea_sim.state import SharedState, VesselState
from nmea_sim.writers import LogWriter, NullWriter, Writer


def test_shared_state_snapshot_is_stable(sample_state: VesselState) -> None:
    shared = SharedState(sample_state)
    snap = shared.snapshot()
    shared.update(_sources="sim", sog_kn=99.0)
    # The earlier snapshot is immutable and unaffected by the later update.
    assert snap.sog_kn == sample_state.sog_kn
    assert shared.snapshot().sog_kn == 99.0


def test_shared_state_update_returns_new_snapshot(sample_state: VesselState) -> None:
    shared = SharedState(sample_state)
    updated = shared.update(_sources="sim", lat=1.0, lon=2.0)
    assert (updated.lat, updated.lon) == (1.0, 2.0)
    assert updated is shared.snapshot()


def test_log_writer_captures_lines() -> None:
    buffer = io.StringIO()
    writer = LogWriter(buffer)
    writer.write_line("$GPGGA,1")
    writer.write_line("$GPRMC,2")
    writer.close()
    assert buffer.getvalue() == "$GPGGA,1\n$GPRMC,2\n"


def test_null_writer_is_a_noop() -> None:
    writer = NullWriter()
    writer.write_line("anything")
    writer.close()  # must not raise


def test_writers_satisfy_protocol() -> None:
    assert isinstance(NullWriter(), Writer)
    assert isinstance(LogWriter(io.StringIO()), Writer)


# --- provenance side-map (RM-009) --------------------------------------------------


def test_provenance_seeds_every_field_from_config(sample_state: VesselState) -> None:
    """A value nobody ever rewrites still has to report where it came from."""
    shared = SharedState(sample_state)
    _, prov = shared.snapshot_with_provenance()
    assert set(prov) == {f.name for f in fields(sample_state)}
    assert all(p.source == "config" for p in prov.values())


def test_update_str_sources_tags_every_key(sample_state: VesselState) -> None:
    shared = SharedState(sample_state)
    shared.update(_sources="manual", lat=1.0, lon=2.0)
    _, prov = shared.snapshot_with_provenance()
    assert prov["lat"].source == "manual"
    assert prov["lon"].source == "manual"
    # Untouched fields keep their prior tag rather than inheriting this write's.
    assert prov["sog_kn"].source == "config"


def test_update_dict_sources_tags_per_key(sample_state: VesselState) -> None:
    """One atomic commit, mixed provenance — the case that forced the dict form: the physics tick
    writes a possibly-live-GNSS ``utc`` alongside genuinely simulated motion."""
    shared = SharedState(sample_state)
    shared.update(_sources={"utc": "clock:gps"}, utc=sample_state.utc, pitch_deg=1.0)
    _, prov = shared.snapshot_with_provenance()
    assert prov["utc"].source == "clock:gps"
    assert prov["pitch_deg"].source == "sim"  # unlisted keys fall back to sim, never to the tag


def test_update_tagged_carries_the_sentence_class(sample_state: VesselState) -> None:
    shared = SharedState(sample_state)
    shared.update_tagged({"lat": 5.0}, source="live:gps_in", cls="gnss")
    _, prov = shared.snapshot_with_provenance()
    assert (prov["lat"].source, prov["lat"].cls) == ("live:gps_in", "gnss")


def test_snapshot_with_provenance_pairs_value_and_tag(sample_state: VesselState) -> None:
    """Both come from ONE lock acquisition, so a frame can never carry write N's tag over write
    N+1's value (a real risk at 4 Hz against a 20 Hz physics writer)."""
    shared = SharedState(sample_state)
    shared.update_tagged({"lat": 7.5}, source="live:gps_in", cls="gnss")
    state, prov = shared.snapshot_with_provenance()
    assert state.lat == 7.5
    assert prov["lat"].source == "live:gps_in"
    # The returned map is a copy: mutating it cannot corrupt the shared side-map.
    prov.clear()
    assert shared.snapshot_with_provenance()[1]["lat"].source == "live:gps_in"
