"""Hardware-agnostic serial port: one class for any USB-serial device.

Behaviour is **capability config, not brand code**. A simplex adapter is just
``direction="tx"``; a bidirectional one is ``direction="both"``; nothing else changes, and
no adapter brand, model, or wiring assumption appears here. The port is defined only by
``path`` / ``baud`` / ``framing`` / ``direction``.

Design rules this class enforces:

* **Tolerant, self-healing open.** A missing device never crashes the process or blocks
  sibling ports — ``open()`` records ``present=False`` and schedules a backoff retry;
  ``write_line`` transparently reopens when the backoff elapses and silently drops output
  while the device is absent (it never raises for a missing device).
* **TX writes CRLF explicitly** (``\\r\\n``) with a non-zero ``write_timeout`` (a zero
  timeout busy-spins the CPU).
* **RX is first-class.** For ``rx``/``both`` a reader thread verifies each line's checksum,
  always forwards it to the monitor callback, and feeds ``VesselState`` **only** through the
  caller-supplied gate (``rx_feeds_state`` + ``rx_accept`` whitelist) — never a raw feed, so
  a loopback or rogue talker cannot rewrite state.
* ``exclusive=True`` (POSIX) so two processes cannot open the same device.

Threads use composition (``threading.Thread(target=...)``), never subclassing, to avoid
shadowing ``Thread`` internals.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import pynmea2
import serial

from . import checksum, rx

# Backoff bounds for reopening an absent/failed device (seconds).
_REOPEN_MIN = 0.5
_REOPEN_MAX = 5.0


@dataclass
class PortStats:
    """Cumulative counters surfaced to the health/monitor layers."""

    lines_tx: int = 0
    bytes_tx: int = 0
    tx_errors: int = 0
    reopens: int = 0
    rx_lines: int = 0
    rx_bad_checksum: int = 0
    rx_parse_errors: int = 0
    rx_state_updates: int = 0


def _serial_kwargs(framing: str) -> dict[str, object]:
    """Translate a framing string like ``8N1`` into pyserial bytesize/parity/stopbits."""
    f = framing.strip().upper()
    bytesizes = {
        "5": serial.FIVEBITS,
        "6": serial.SIXBITS,
        "7": serial.SEVENBITS,
        "8": serial.EIGHTBITS,
    }
    parities = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
    stopbits = {"1": serial.STOPBITS_ONE, "2": serial.STOPBITS_TWO}
    # One consistent, clear rejection for every malformed framing (bad length, data bits,
    # parity, or stop bits) so validate() can catch it instead of the engine tracebacking at
    # port-open time. Never emit a raw int()/lookup error out of here.
    if len(f) != 3 or f[0] not in bytesizes or f[1] not in parities or f[2] not in stopbits:
        raise ValueError(f"unsupported framing {framing!r} (expected e.g. 8N1)")
    return {
        "bytesize": bytesizes[f[0]],
        "parity": parities[f[1]],
        "stopbits": stopbits[f[2]],
    }


class SerialPort:
    """A tolerant, hotplug-aware serial writer (and, for rx/both, reader).

    Implements the ``Writer`` protocol (``write_line`` / ``close``) so the engine treats it
    as an ordinary sink, plus ``start()`` to open and begin reading.
    """

    def __init__(
        self,
        path: str,
        baud: int,
        *,
        framing: str = "8N1",
        direction: str = "tx",
        write_timeout: float = 1.0,
        read_timeout: float = 0.5,
        on_rx: Callable[[str], None] | None = None,
        on_raw: Callable[[bytes], None] | None = None,
        on_line: Callable[[str], None] | None = None,
        state_feed: Callable[[dict[str, float]], None] | None = None,
        rx_feeds_state: bool = False,
        rx_accept: list[str] | None = None,
    ) -> None:
        if direction not in ("tx", "rx", "both"):
            raise ValueError(f"direction must be tx|rx|both, got {direction!r}")
        self._path = path
        self._baud = baud
        self._kwargs = _serial_kwargs(framing)
        self._direction = direction
        self._write_timeout = write_timeout
        self._read_timeout = read_timeout
        self._on_rx = on_rx
        # Optional raw-bytes tap fed each chunk BEFORE line-splitting (the diagnostics seam). Left
        # None on every existing caller, so rx/both/tx behaviour is byte-identical; only the AUTO
        # engine attaches it to feed a per-input PortDiagnostics. It never influences framing,
        # checksum verification, state feed, or liveness — it is a pure best-effort observer.
        self._on_raw = on_raw
        # Optional complete-decoded-RX-line tap, fired for EVERY received line INCLUDING
        # malformed / bad-checksum lines, before checksum.verify. Left None on every existing
        # caller; only the AUTO engine attaches it to forward received lines to the web layer.
        # A pure best-effort observer — never influences framing, state, or liveness.
        self._on_line = on_line
        self._state_feed = state_feed
        self._rx_feeds_state = rx_feeds_state
        self._rx_accept = list(rx_accept or [])

        self.stats = PortStats()
        self.present = False
        # Monotonic timestamp of the last checksum-valid line, or None if none yet. This is the
        # liveness primitive the AUTO-mode router reads to decide whether a physical source still
        # owns an output channel; it is set only after checksum.verify passes so noise/garbage on
        # the wire cannot masquerade as a live source.
        self._last_valid_rx: float | None = None
        self._serial: serial.Serial | None = None
        self._backoff = _REOPEN_MIN
        self._next_retry = 0.0  # time.monotonic() after which reopen is allowed
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None

    # -- open / reopen ------------------------------------------------------
    def start(self) -> None:
        """Open the device (tolerant) and, for rx/both, launch the reader thread."""
        self._open()
        if self._direction in ("rx", "both") and self._reader is None:
            self._reader = threading.Thread(
                target=self._read_loop, name=f"rx-{self._path}", daemon=True
            )
            self._reader.start()

    def _open(self) -> bool:
        """Attempt to open the device. Never raises; records present/backoff on failure."""
        with self._lock:
            if self._serial is not None:
                return True
            try:
                self._serial = serial.Serial(
                    port=self._path,
                    baudrate=self._baud,
                    write_timeout=self._write_timeout,
                    timeout=self._read_timeout,
                    exclusive=(os.name == "posix"),
                    **self._kwargs,
                )
            except (serial.SerialException, OSError, ValueError):
                self.present = False
                self._schedule_retry()
                return False
            self.present = True
            self._backoff = _REOPEN_MIN
            self.stats.reopens += 1
            return True

    def _schedule_retry(self) -> None:
        self._next_retry = time.monotonic() + self._backoff
        self._backoff = min(self._backoff * 2, _REOPEN_MAX)

    def _mark_down(self) -> None:
        with self._lock:
            self.present = False
            if self._serial is not None:
                with contextlib.suppress(Exception):
                    self._serial.close()
                self._serial = None
            self._schedule_retry()

    def _reopen_if_due(self) -> None:
        if self.present or time.monotonic() < self._next_retry:
            return
        self._open()

    # -- TX (Writer protocol) ----------------------------------------------
    def write_line(self, line: str) -> None:
        """Write ``line`` + CRLF. Silently drops while the device is absent (self-heals)."""
        if self._direction == "rx":
            return  # a receive-only port never transmits
        if not self.present:
            self._reopen_if_due()
            if not self.present:
                return  # still gone; drop this line, retry later. Never blocks siblings.
        ser = self._serial
        if ser is None:
            return
        payload = (line + "\r\n").encode("ascii", "replace")
        try:
            ser.write(payload)
            self.stats.lines_tx += 1
            self.stats.bytes_tx += len(payload)
        except (serial.SerialException, OSError):
            self.stats.tx_errors += 1
            self._mark_down()

    # -- RX -----------------------------------------------------------------
    def _read_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            ser = self._serial
            if ser is None or not self.present:
                self._reopen_if_due()
                if self._stop.wait(0.2):
                    break
                continue
            try:
                # read_until returns the moment a newline lands (or a partial chunk at timeout),
                # so a passthrough line is forwarded with minimal latency instead of waiting for a
                # 256-byte fill or the full read timeout. The buffer/partition logic below still
                # handles partial reads and multiple lines per chunk identically.
                chunk = ser.read_until(b"\n")
            except (serial.SerialException, OSError):
                self._mark_down()
                continue
            if not chunk:
                continue
            # Raw tap (diagnostics): hand the untouched chunk to the observer BEFORE any line
            # splitting or checksum work, so the analyzer sees exactly what the wire delivered
            # (partial lines, garble, reversed-pair noise). Best-effort and fully isolated — a
            # raising or slow observer must never break or stall the reader (R28 boundedness is
            # the observer's own responsibility, never accumulated here).
            if self._on_raw is not None:
                with contextlib.suppress(Exception):
                    self._on_raw(chunk)
            buf += chunk
            while b"\n" in buf:
                raw, _, buf = buf.partition(b"\n")
                self._handle_rx_line(raw.decode("ascii", "replace").strip())

    def _handle_rx_line(self, line: str) -> None:
        if not line:
            return
        self.stats.rx_lines += 1
        if self._on_line is not None:
            with contextlib.suppress(Exception):
                # complete RX line (incl. malformed); observer must never break the reader
                self._on_line(line)
        if not checksum.verify(line):
            self.stats.rx_bad_checksum += 1
            return
        # Stamp liveness only after the checksum passes and before on_rx, so the router sees a
        # source as live exactly when a valid line arrives, regardless of downstream callbacks.
        self._last_valid_rx = time.monotonic()
        if self._on_rx is not None:
            with contextlib.suppress(Exception):
                self._on_rx(line)  # monitor forwarding must never break the reader
        if not (self._rx_feeds_state and self._state_feed):
            return
        try:
            accepted = rx.accepted_changes(line, self._rx_accept)
        except (pynmea2.ParseError, ValueError, TypeError, AttributeError):
            # rx.parse_line is total per-field, but a structurally-unparseable body still raises
            # ParseError; belt-and-suspenders on the value/type/attribute family keeps one bad
            # line from ever killing the reader thread.
            self.stats.rx_parse_errors += 1
            return
        if accepted:
            with contextlib.suppress(Exception):
                self._state_feed(accepted)
                self.stats.rx_state_updates += 1

    def is_live(self, timeout_s: float, now: float | None = None) -> bool:
        """Report whether a checksum-valid line arrived within ``timeout_s``.

        The router polls this to arbitrate LIVE-vs-SIM ownership of an output channel: a source is
        live only while fresh valid traffic keeps flowing, so a dead or unplugged input silently
        stops winning and generation resumes. ``now`` is injectable so the arbitration is
        deterministic under test without touching real serial hardware.
        """
        last = self._last_valid_rx
        if last is None:
            return False
        if now is None:
            now = time.monotonic()
        return (now - last) <= timeout_s

    # -- teardown -----------------------------------------------------------
    def close(self) -> None:
        self._stop.set()
        reader = self._reader
        if reader is not None and reader.is_alive():
            reader.join(2.0)
        with self._lock:
            if self._serial is not None:
                with contextlib.suppress(Exception):
                    self._serial.close()
                self._serial = None
            self.present = False
