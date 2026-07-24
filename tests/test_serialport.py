"""SerialPort: framing math, tolerant open, RX checksum+whitelist gate, pty loopback."""

from __future__ import annotations

import os
import select
import time

import pytest

from nmea_sim import serialport
from nmea_sim.gps_generator import GpsGenerator
from nmea_sim.heading_generator import HeadingGenerator
from nmea_sim.serialport import SerialPort
from nmea_sim.state import VesselState

posix_only = pytest.mark.skipif(os.name != "posix", reason="requires POSIX pty")


# --- framing translation ---------------------------------------------------------


def test_serial_kwargs_valid() -> None:
    kw = serialport._serial_kwargs("8N1")
    assert kw["bytesize"] and kw["stopbits"]  # populated with pyserial constants


def test_serial_kwargs_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        serialport._serial_kwargs("banana")


# --- tolerant open ---------------------------------------------------------------


def test_missing_device_never_raises() -> None:
    port = SerialPort("/dev/does-not-exist-xyz", 4800, direction="tx")
    port.start()  # tolerant: records absence, schedules retry, does not raise
    assert port.present is False
    # A write to an absent device is silently dropped — it must never block siblings.
    port.write_line("$GPGGA,,,,,*00")
    assert port.stats.lines_tx == 0
    assert port._next_retry > 0  # a reopen is scheduled
    port.close()


def test_rx_only_port_never_transmits() -> None:
    port = SerialPort("/dev/does-not-exist-xyz", 4800, direction="rx")
    port.write_line("anything")  # rx-only: no-op, no open attempt
    assert port.stats.lines_tx == 0
    port.close()


def test_backoff_doubles_and_retry_time_advances() -> None:
    """Repeated failed reopens double the backoff (capped at ``_REOPEN_MAX``) and push the
    next-retry time forward each attempt. Platform-neutral: never opens a real device."""
    port = SerialPort("/dev/does-not-exist-xyz", 4800, direction="tx")
    port.start()  # first open attempt fails -> _schedule_retry already doubles once
    assert port._backoff == pytest.approx(serialport._REOPEN_MIN * 2)

    seen_backoffs = [port._backoff]
    for _ in range(5):
        before = time.monotonic()
        port._next_retry = 0.0  # force the retry to be due right now, no real sleep needed
        port._reopen_if_due()  # device still absent -> fails again, backoff doubles
        assert port._next_retry > before  # retry time pushed into the future
        seen_backoffs.append(port._backoff)

    for prev, nxt in zip(seen_backoffs, seen_backoffs[1:], strict=False):
        assert nxt == pytest.approx(min(prev * 2, serialport._REOPEN_MAX))
    assert seen_backoffs[-1] == pytest.approx(serialport._REOPEN_MAX)  # saturates at the cap
    port.close()


# --- RX gate (deterministic, no hardware) ----------------------------------------


def test_rx_feeds_only_whitelisted_fields(sample_state: VesselState) -> None:
    fed: list[dict] = []
    seen: list[str] = []
    port = SerialPort(
        "unused",
        4800,
        direction="both",
        rx_feeds_state=True,
        rx_accept=["heading_true_deg"],
        state_feed=fed.append,
        on_rx=seen.append,
    )
    hdt = HeadingGenerator("HE").hdt(sample_state)
    port._handle_rx_line(hdt)
    assert seen == [hdt]  # monitor always sees a verified line
    assert fed == [{"heading_true_deg": pytest.approx(sample_state.heading_true_deg, abs=0.05)}]
    assert port.stats.rx_state_updates == 1


def test_rx_non_whitelisted_field_is_not_fed(sample_state: VesselState) -> None:
    fed: list[dict] = []
    seen: list[str] = []
    port = SerialPort(
        "unused",
        4800,
        direction="both",
        rx_feeds_state=True,
        rx_accept=["heading_true_deg"],  # RMC carries no heading_true_deg
        state_feed=fed.append,
        on_rx=seen.append,
    )
    rmc = GpsGenerator("GP").rmc(sample_state)
    port._handle_rx_line(rmc)
    assert seen == [rmc]  # still forwarded to the monitor
    assert fed == []  # but nothing crosses into state
    assert port.stats.rx_state_updates == 0


def test_rx_feeds_state_disabled_never_feeds(sample_state: VesselState) -> None:
    fed: list[dict] = []
    port = SerialPort(
        "unused",
        4800,
        direction="both",
        rx_feeds_state=False,
        rx_accept=["heading_true_deg"],
        state_feed=fed.append,
    )
    port._handle_rx_line(HeadingGenerator("HE").hdt(sample_state))
    assert fed == []


def test_rx_parse_error_is_counted_and_never_feeds_state(sample_state: VesselState) -> None:
    """A valid checksum over a malformed NMEA body must count as a parse error, not a state
    update, and must never reach the monitor via a feed callback."""
    fed: list[dict] = []
    seen: list[str] = []
    port = SerialPort(
        "unused",
        4800,
        direction="both",
        rx_feeds_state=True,
        rx_accept=["heading_true_deg"],
        state_feed=fed.append,
        on_rx=seen.append,
    )
    # A recognised talker+type (HDT) with none of its required data fields: the checksum
    # is computed correctly over the body, but pynmea2 cannot parse the sentence itself.
    body = "HEHDT"
    line = "$" + body + "*" + serialport.checksum.compute(body)
    assert serialport.checksum.verify(line)  # checksum is valid; the body is what's malformed

    port._handle_rx_line(line)

    assert seen == [line]  # a verified line always reaches the monitor
    assert port.stats.rx_parse_errors == 1
    assert port.stats.rx_state_updates == 0
    assert fed == []  # a parse error must never feed state


def test_rx_rejects_bad_checksum(sample_state: VesselState) -> None:
    fed: list[dict] = []
    seen: list[str] = []
    port = SerialPort(
        "unused",
        4800,
        direction="both",
        rx_feeds_state=True,
        rx_accept=["heading_true_deg"],
        state_feed=fed.append,
        on_rx=seen.append,
    )
    good = HeadingGenerator("HE").hdt(sample_state)
    corrupt = good[:-2] + "00"  # clobber the checksum digits
    port._handle_rx_line(corrupt)
    assert port.stats.rx_bad_checksum == 1
    assert seen == []  # a failing checksum is dropped before the monitor
    assert fed == []


# --- liveness primitive (deterministic, no hardware) -----------------------------


def test_last_valid_rx_set_only_after_checksum_passes(sample_state: VesselState) -> None:
    """A checksum-valid line stamps ``_last_valid_rx``; a bad-checksum line must NOT refresh it,
    so noise on the wire can never make a dead source look live."""
    port = SerialPort("unused", 4800, direction="both")
    assert port._last_valid_rx is None  # nothing seen yet

    good = HeadingGenerator("HE").hdt(sample_state)
    port._handle_rx_line(good)
    stamped = port._last_valid_rx
    assert stamped is not None

    # A corrupt checksum is dropped before the liveness stamp -> the timestamp stays put.
    corrupt = good[:-2] + "00"
    port._handle_rx_line(corrupt)
    assert port._last_valid_rx == stamped
    port.close()


def test_is_live_true_within_window_false_once_expired(sample_state: VesselState) -> None:
    """``is_live`` compares against an injectable ``now`` so arbitration is deterministic without
    touching real hardware: fresh within the window, dead once the window elapses."""
    port = SerialPort("unused", 4800, direction="both")
    assert port.is_live(1.0, now=0.0) is False  # no valid line ever -> never live

    port._handle_rx_line(GpsGenerator("GP").rmc(sample_state))
    last = port._last_valid_rx
    assert last is not None
    assert port.is_live(1.0, now=last) is True  # exactly now
    assert port.is_live(1.0, now=last + 0.5) is True  # inside the window
    assert port.is_live(1.0, now=last + 1.0) is True  # boundary is inclusive
    assert port.is_live(1.0, now=last + 1.5) is False  # window elapsed -> not live
    port.close()


# --- pty loopback (POSIX) --------------------------------------------------------


@posix_only
def test_pty_tx_writes_crlf_terminated_lines() -> None:
    master, slave = os.openpty()
    slave_name = os.ttyname(slave)
    os.close(slave)  # hand the slave to pyserial exclusively
    port = SerialPort(slave_name, 4800, direction="tx")
    port.start()
    assert port.present is True
    try:
        port.write_line("$GPGGA,hello*00")
        ready, _, _ = select.select([master], [], [], 2.0)
        assert ready, "no bytes appeared on the pty master"
        data = os.read(master, 4096)
        assert data.endswith(b"\r\n")  # CRLF on the wire, explicitly
        assert b"$GPGGA,hello" in data
    finally:
        port.close()
        os.close(master)


@posix_only
def test_self_heals_when_device_reappears() -> None:
    """A port opened against a dead path, then repointed at a live pty and forced to retry,
    must reopen, resume transmitting, and record the reopen in its stats."""
    port = SerialPort("/dev/does-not-exist-xyz", 4800, direction="tx")
    port.start()
    assert port.present is False
    reopens_before = port.stats.reopens

    master, slave = os.openpty()
    slave_name = os.ttyname(slave)
    os.close(slave)  # hand the slave to pyserial exclusively
    try:
        port._path = slave_name  # repoint at a now-live device
        port._next_retry = 0.0  # force the next write to attempt a reopen immediately
        port.write_line("$GPGGA,heal*00")

        assert port.present is True
        assert port.stats.reopens == reopens_before + 1

        ready, _, _ = select.select([master], [], [], 2.0)
        assert ready, "no bytes appeared on the pty master after self-heal"
        data = os.read(master, 4096)
        assert b"$GPGGA,heal" in data
    finally:
        port.close()
        os.close(master)


@posix_only
def test_pty_rx_loopback_feeds_whitelisted_state(sample_state: VesselState) -> None:
    master, slave = os.openpty()
    slave_name = os.ttyname(slave)
    os.close(slave)
    fed: list[dict] = []
    port = SerialPort(
        slave_name,
        4800,
        direction="both",
        rx_feeds_state=True,
        rx_accept=["heading_true_deg"],
        state_feed=fed.append,
    )
    port.start()
    assert port.present is True
    try:
        line = HeadingGenerator("HE").hdt(sample_state) + "\r\n"
        os.write(master, line.encode("ascii"))
        deadline = time.monotonic() + 2.0
        while not fed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fed, "RX loopback never delivered a state update"
        assert "heading_true_deg" in fed[0]
    finally:
        port.close()
        os.close(master)
