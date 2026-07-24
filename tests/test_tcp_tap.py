"""TcpTap: broadcast, LAN-bind guard, read-only (inbound ignored), slow-client isolation."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable

import pytest

from nmea_sim.tcp_tap import TcpTap


def _wait(pred: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _connect(tap: TcpTap) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", tap.bound_port), timeout=2.0)
    sock.settimeout(2.0)
    return sock


def _recv_lines(sock: socket.socket, count: int, timeout: float = 2.0) -> list[str]:
    """Read until ``count`` CRLF-terminated lines have arrived (or timeout)."""
    sock.settimeout(timeout)
    buf = b""
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while len(lines) < count and time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\r\n" in buf:
            raw, _, buf = buf.partition(b"\r\n")
            lines.append(raw.decode("ascii"))
    return lines


def test_rejects_wildcard_bind() -> None:
    with pytest.raises(ValueError):
        TcpTap("0.0.0.0", 0)  # noqa: S104 - asserting the wildcard is refused
    with pytest.raises(ValueError):
        TcpTap("", 0)


def test_bound_port_assigned_on_start() -> None:
    tap = TcpTap("127.0.0.1", 0)
    tap.start()
    try:
        assert tap.bound_port > 0
    finally:
        tap.close()


def test_client_receives_broadcast_lines() -> None:
    tap = TcpTap("127.0.0.1", 0)
    tap.start()
    client = _connect(tap)
    try:
        assert _wait(lambda: tap.client_count() == 1)
        tap.write_line("$GPGGA,one*00")
        tap.write_line("$GPRMC,two*00")
        lines = _recv_lines(client, 2)
        assert lines == ["$GPGGA,one*00", "$GPRMC,two*00"]
    finally:
        client.close()
        tap.close()


def test_inbound_bytes_are_ignored() -> None:
    """A client sending data must not affect the sim — the tap is read-only."""
    tap = TcpTap("127.0.0.1", 0)
    tap.start()
    client = _connect(tap)
    try:
        assert _wait(lambda: tap.client_count() == 1)
        client.sendall(b"$INJECT,evil,command*00\r\n")  # tap must never read this
        time.sleep(0.1)
        tap.write_line("$GPGGA,after*00")
        assert _recv_lines(client, 1) == ["$GPGGA,after*00"]  # broadcast still works
    finally:
        client.close()
        tap.close()


def test_slow_client_does_not_stall_others() -> None:
    # Small buffer so the never-reading slow client overflows and exercises drop-oldest.
    tap = TcpTap("127.0.0.1", 0, max_queue=8)
    tap.start()
    fast = _connect(tap)
    slow = _connect(tap)  # connected but never reads — its buffer overflows and drops
    try:
        assert _wait(lambda: tap.client_count() == 2)
        expected = [f"$GPGGA,{i}*00" for i in range(40)]
        got: list[str] = []
        # Read each line right after it is sent so the fast client stays drained. The slow
        # client never drains, yet neither the broadcaster nor the fast client is stalled.
        for line in expected:
            tap.write_line(line)
            got += _recv_lines(fast, 1)
        assert got == expected
    finally:
        fast.close()
        slow.close()
        tap.close()


def test_disconnected_client_is_pruned() -> None:
    tap = TcpTap("127.0.0.1", 0)
    tap.start()
    client = _connect(tap)
    try:
        assert _wait(lambda: tap.client_count() == 1)
        client.close()

        def gone() -> bool:
            tap.write_line("$GPGGA,ping*00")  # provokes the dead-socket send error
            return tap.client_count() == 0

        assert _wait(gone)
    finally:
        tap.close()
