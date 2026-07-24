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


# --- RX gate (deterministic, no hardware) ----------------------------------------


def _collector() -> tuple[list, list]:
    return [], []


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
