"""Output sinks for generated sentences.

A ``Writer`` receives one already-formatted sentence at a time (no line ending) and is
responsible for however it emits — a real serial port (P4), stdout/a log stream, or
nothing. Keeping the interface this narrow lets the engine fan one sentence out to many
sinks (serial + web + TCP tap) without any sink knowing about the others.

P1 provides the two hardware-free sinks: ``LogWriter`` and ``NullWriter``.
The serial- and pty-backed writers arrive in P4.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Protocol, TextIO, runtime_checkable


@runtime_checkable
class Writer(Protocol):
    """Something that can emit a formatted NMEA sentence and be closed."""

    def write_line(self, line: str) -> None: ...

    def close(self) -> None: ...


class NullWriter:
    """Discards everything. Useful as a placeholder or to disable a channel's output."""

    def write_line(self, line: str) -> None:
        return None

    def close(self) -> None:
        return None


class LogWriter:
    """Writes each sentence to a text stream (default ``sys.stdout``), one per line.

    This is a human-readable view, so it uses ``\\n`` — the CRLF-on-the-wire rule
    applies only to the serial writer.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def write_line(self, line: str) -> None:
        self._stream.write(line + "\n")

    def close(self) -> None:
        # Never close a borrowed stream (e.g. stdout); flush best-effort.
        with contextlib.suppress(ValueError, OSError):
            self._stream.flush()
