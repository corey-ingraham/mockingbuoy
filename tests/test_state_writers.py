"""SharedState atomic updates and the P1 writers."""

from __future__ import annotations

import io

from nmea_sim.state import SharedState, VesselState
from nmea_sim.writers import LogWriter, NullWriter, Writer


def test_shared_state_snapshot_is_stable(sample_state: VesselState) -> None:
    shared = SharedState(sample_state)
    snap = shared.snapshot()
    shared.update(sog_kn=99.0)
    # The earlier snapshot is immutable and unaffected by the later update.
    assert snap.sog_kn == sample_state.sog_kn
    assert shared.snapshot().sog_kn == 99.0


def test_shared_state_update_returns_new_snapshot(sample_state: VesselState) -> None:
    shared = SharedState(sample_state)
    updated = shared.update(lat=1.0, lon=2.0)
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
