"""PtyWriter: the hardware-free serial stand-in emits exactly what a real port would."""

from __future__ import annotations

import os
import select

import pytest

posix_only = pytest.mark.skipif(os.name != "posix", reason="requires POSIX pty")


@posix_only
def test_pty_writer_emits_crlf_lines() -> None:
    from nmea_sim.writers import PtyWriter

    writer = PtyWriter()
    reader = os.open(writer.slave_name, os.O_RDWR | os.O_NOCTTY)
    try:
        writer.write_line("$GPGGA,pty-test*00")
        ready, _, _ = select.select([reader], [], [], 2.0)
        assert ready, "PtyWriter produced no output on the slave"
        data = os.read(reader, 4096)
        assert data.endswith(b"\r\n")
        assert b"$GPGGA,pty-test" in data
    finally:
        os.close(reader)
        writer.close()
