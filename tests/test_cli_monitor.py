"""Cross-platform tests for the ``mockingbuoy-mon`` CLI's pure surface.

Only the platform-independent parts are exercised here — argument parsing/validation, the pure
renderers, the SSE frame parser, and the curses-unavailable fallback. The curses TUI paint itself
is POSIX-only and manually verified; every cell it draws comes from the pure ``render_*`` helpers
tested below, so its content is covered. Nothing here opens a serial port or a socket.
"""

from __future__ import annotations

import argparse
import json

import pytest

from nmea_sim import cli_monitor
from nmea_sim.cli_monitor import (
    build_parser,
    format_baud_sweep,
    main,
    parse_host_port,
    parse_sse_frames,
    render_decode,
    render_json,
    render_plain,
)
from nmea_sim.diagnostics import PortDiagnostics


def _parse(argv: list[str]) -> argparse.Namespace:
    """Parse + validate an argv, matching how ``main`` prepares its namespace."""
    parser = build_parser()
    return cli_monitor._validate(parser, parser.parse_args(argv))


def _sample_snapshot() -> dict:
    """A real snapshot from feeding a couple of valid sentences to the analyzer."""
    diag = PortDiagnostics("in-1", 4800)
    diag.feed_bytes(b"$GPRMC,120000.00,A,0000.00,N,00000.00,E,0.0,0.0,010120,,,A*5F\r\n", 0.0)
    diag.feed_bytes(b"$GPGGA,120000.00,0000.00,N,00000.00,E,1,08,0.9,0.0,M,0.0,M,,*5E\r\n", 0.1)
    return diag.snapshot(0.2)


# --- argument parsing / validation ------------------------------------------------


def test_standalone_port_parses() -> None:
    args = _parse(["--port", "/dev/nmea-in-1", "--plain"])
    assert args.standalone is True
    assert args.port == "/dev/nmea-in-1"


def test_attach_parses_host_port() -> None:
    args = _parse(["--attach", "127.0.0.1:8000", "--json"])
    assert args.standalone is False
    assert args.host == "127.0.0.1"
    assert args.port_num == 8000


@pytest.mark.parametrize(
    "argv",
    [
        ["--port", "/dev/x", "--attach", "127.0.0.1:8000"],  # two mutually-exclusive sources
        [],  # no source at all
        ["--slot", "in-1"],  # --slot without --config
        ["--attach", "127.0.0.1:8000", "--baud-sweep"],  # sweep needs a standalone source
        ["--port", "/dev/x", "--interval", "0"],  # non-positive interval
        ["--attach", "not-a-host-port"],  # unparseable attach target
    ],
)
def test_bad_arg_combos_exit_cleanly(argv: list[str]) -> None:
    # argparse reports the error and raises SystemExit(2) — never a traceback.
    with pytest.raises(SystemExit) as exc:
        _parse(argv)
    assert exc.value.code == 2


def test_parse_host_port_rejects_garbage() -> None:
    assert parse_host_port("127.0.0.1:8000") == ("127.0.0.1", 8000)
    with pytest.raises(ValueError):
        parse_host_port("no-colon")
    with pytest.raises(ValueError):
        parse_host_port("host:notaport")


# --- pure renderers ---------------------------------------------------------------


def test_render_plain_is_legible_without_colour() -> None:
    snap = _sample_snapshot()
    out = render_plain(snap, color=False)
    # Verdict text + full counts are present, so the block diagnoses with colour stripped.
    assert snap["verdict"].upper() in out
    assert "valid=" in out and "bad_checksum=" in out and "malformed=" in out
    # No ANSI escape when colour is off.
    assert "\x1b[" not in out


def test_render_plain_colour_adds_ansi_but_same_text() -> None:
    snap = _sample_snapshot()
    plain = render_plain(snap, color=False)
    coloured = render_plain(snap, color=True)
    assert "\x1b[" in coloured
    # Stripping ANSI from the coloured render yields the same information.
    assert snap["verdict"].upper() in coloured
    assert plain.splitlines()[1] == render_plain(snap, color=False).splitlines()[1]


def test_render_json_round_trips() -> None:
    snap = _sample_snapshot()
    parsed = json.loads(render_json(snap))
    assert parsed["port_id"] == "in-1"
    assert parsed["valid"] == snap["valid"]
    assert parsed["verdict"] == snap["verdict"]


def test_render_decode_reflects_fields() -> None:
    out = render_decode("$GPRMC,120000.00,A,0000.00,N,00000.00,E,0.0,0.0,010120,,,A*5F")
    assert "type=RMC" in out
    assert "checksum=ok" in out


def test_render_decode_malformed_never_raises() -> None:
    out = render_decode("not a sentence at all")
    # Degrades to an error/checksum note rather than raising.
    assert "checksum" in out.lower()


# --- baud sweep formatting --------------------------------------------------------


def test_format_baud_sweep_marks_winner() -> None:
    result = {"ratios": {4800: 0.02, 9600: 0.98, 38400: 0.0}, "winner": 9600}
    out = format_baud_sweep(result)
    assert "9600" in out and "winner: 9600" in out
    assert "98.0% valid  <- winner" in out


def test_format_baud_sweep_none_winner_is_honest() -> None:
    result = {"ratios": {4800: 0.0, 9600: 0.0}, "winner": None}
    out = format_baud_sweep(result)
    assert "none" in out.lower()
    assert "polarity" in out.lower()  # implicates wiring, not baud (R29)


# --- SSE frame parsing ------------------------------------------------------------


def test_parse_sse_frames_multiple_and_keepalive() -> None:
    text = (
        ": keepalive\n\n"
        'event: nmea\ndata: {"channel": "gps", "line": "$GPRMC,,,"}\n\n'
        'event: state\ndata: {"mode": "auto"}\n\n'
    )
    frames = parse_sse_frames(text)
    assert ("nmea", {"channel": "gps", "line": "$GPRMC,,,"}) in frames
    assert ("state", {"mode": "auto"}) in frames


def test_parse_sse_frames_partial_tail_no_raise() -> None:
    # A trailing, not-yet-complete frame must be dropped, not crash the parser.
    text = 'event: nmea\ndata: {"channel": "gps"}\n\nevent: state\ndata: {"mode":'
    frames = parse_sse_frames(text)
    assert frames == [("nmea", {"channel": "gps"})]


def test_parse_sse_frames_empty() -> None:
    assert parse_sse_frames("") == []


# --- curses fallback --------------------------------------------------------------


def test_curses_unavailable_returns_clean_code(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the non-POSIX branch so the TUI degrades with a message and a non-zero-but-clean code,
    # regardless of the host OS (so it also exercises on Linux CI).
    monkeypatch.setattr(cli_monitor.os, "name", "nt")

    class _DummySource:
        started = False

        def start(self) -> None:  # must NOT be reached on the non-POSIX path
            self.started = True

        def close(self) -> None:
            pass

    args = build_parser().parse_args(["--port", "/dev/x"])
    src = _DummySource()
    assert cli_monitor._run_curses(src, args) == 3
    assert src.started is False


def test_main_bad_args_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


# --- standalone source: present surfacing + pending bound (M15) --------------------


def test_absent_standalone_device_surfaces_error_and_exits_four(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """M15: a mistyped/permission-denied --port opens present=False (SerialPort never raises); the
    monitor must surface that as a clear source error + non-zero exit, not a perpetual silent
    'no-data' that keeps advising a TX/RX swap."""
    args = _parse(["--port", "/dev/mockingbuoy-nonexistent-xyz", "--plain"])
    source = cli_monitor._build_source(args, None)
    code = cli_monitor._run_stream(source, args, color=False)
    assert code == 4
    err = capsys.readouterr().err.lower()
    assert "not present" in err
    assert "mockingbuoy-nonexistent-xyz" in err


def test_standalone_pending_buffer_is_bounded_when_undrained() -> None:
    """M15: with --decode off nothing drains the pending list, so the reader-thread assembler must
    write into a bounded ring — far more lines than the cap must not grow it without bound."""
    source = cli_monitor._StandaloneSource("/dev/mockingbuoy-nonexistent-xyz", 4800, "in-1")
    for i in range(cli_monitor._PENDING_MAX * 3):
        source._on_raw(f"$GPTXT,{i:04d}*00\n".encode("latin-1"))
    assert len(source._pending) <= cli_monitor._PENDING_MAX
    # Draining still returns the (bounded) retained lines and clears the ring.
    drained = source.lines()
    assert len(drained) <= cli_monitor._PENDING_MAX
    assert source.lines() == []
