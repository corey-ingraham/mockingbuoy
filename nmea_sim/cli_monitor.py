"""Headless terminal frontend to the same diagnostics core the web layer drives.

This module exists to prove one thing: the analysis *core* (``PortDiagnostics`` +
``classify_fault`` + ``score_baud`` + ``decode_line``) is usable with no web server, no ASGI
runtime, and no reverse proxy in front of it — the case that matters over SSH, on a headless
field box, or during a disaster-recovery rebuild where the web stack may not be up. It is a
read-only monitor by default; the only active operation it can perform is an operator-chosen
baud sweep, and even that is refused the moment a config reveals the target maps to an
operational port (the same R17 rule the web layer enforces).

The file is deliberately split into two layers. The PURE layer — argument parsing, the
``render_*`` helpers, ``format_baud_sweep``, and ``parse_sse_frames`` — is plain functions of
plain data so it unit-tests identically on any platform, including Windows where curses does
not exist. The IO layer — the standalone serial source, the attach HTTP client, and the curses
TUI — is thin, isolated, and imports curses lazily so merely importing this module never drags
in a POSIX-only dependency. Everything the TUI paints comes back out of the pure ``render_*``
helpers, so its content is covered even though the curses paint itself is manual-tested only.

No new third-party dependency is introduced: the attach client is stdlib ``http.client`` over
loopback or the production unix socket, attaching to the app DIRECTLY (bypassing the proxy, so
no credentials are needed) and adding zero new server surface.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import socket
import sys
import threading
import time
from typing import Any

from .config import EngineConfig
from .diagnostics import STANDARD_BAUDS, PortDiagnostics, decode_line, score_baud
from .engine import port_is_operational
from .serialport import SerialPort

# --- tunables ---------------------------------------------------------------------

#: Rolling diagnostics window for a standalone source, in seconds. Matches the engine's default
#: so a CLI verdict lines up with what the web surface would show for the same wire.
_WINDOW_S = 10.0
#: Read timeout for the standalone reader thread. Short so a dead device is noticed promptly and
#: the render loop is never starved waiting on a single blocking read.
_STANDALONE_READ_TIMEOUT = 0.2
#: A standalone line assembler must stay bounded (R28); past this a newline-less runaway is
#: dropped rather than accumulated for the life of the process.
_MAX_LINE_BYTES = 4096
#: Hard ceiling on a full baud sweep so a no-hardware box can never hang the sweep-and-exit path.
_SWEEP_TOTAL_CAP_S = 20.0

# ANSI colour is always a SECONDARY cue: every verdict is printed as legible text with counts, so
# the block reads correctly when piped, redirected, or viewed by a colourblind operator.
_RESET = "\033[0m"
_VERDICT_COLORS = {
    "valid": "\033[32m",  # green — nothing wrong
    "no-data": "\033[90m",  # grey — silent wire
    "noise": "\033[33m",  # yellow — minority checksum failures
    "device-fault": "\033[33m",
    "wrong-baud": "\033[31m",  # red — actionable physical fault
    "reversed-ab": "\033[31m",
    "collision": "\033[31m",
}

#: The stable subset of a snapshot that ``render_json`` emits, so machine consumers get a fixed
#: shape regardless of what the analysis core adds internally later.
_JSON_KEYS: tuple[str, ...] = (
    "port_id",
    "baud",
    "bytes",
    "printable_ratio",
    "lines",
    "valid",
    "bad_checksum",
    "malformed",
    "sentences_per_s",
    "bytes_per_s",
    "bus_load_pct",
    "talkers",
    "inventory",
    "verdict",
    "advice",
)

_TUI_UNAVAILABLE = "the TUI needs a POSIX terminal — use --plain / --json"


# --- PURE: argument parsing -------------------------------------------------------


def parse_host_port(value: str) -> tuple[str, int]:
    """Split a ``HOST:PORT`` attach target into its parts, validating the port range.

    Uses ``rpartition`` so the last colon wins; a missing colon, an empty host, or a
    non-numeric/out-of-range port raises ``ValueError`` with a clean message the arg layer turns
    into an argparse error rather than a traceback.
    """
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(f"expected HOST:PORT, got {value!r}")
    number = int(port)
    if not 1 <= number <= 65535:
        raise ValueError(f"port out of range in {value!r}")
    return host, number


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser: exactly one source plus optional renderer flags.

    The four source flags live in a required mutually-exclusive group so argparse itself enforces
    "exactly one of --port/--slot/--attach/--attach-uds" with a clean error, and the two whole-UI
    renderers (--plain/--json) are their own exclusive group. Cross-flag rules that argparse can
    not express (e.g. --slot needs a config, --baud-sweep needs a standalone source) are checked
    in :func:`_validate` so every rejection is an argparse error, never a traceback.
    """
    parser = argparse.ArgumentParser(
        prog="mockingbuoy-mon",
        description="Read-only terminal NMEA diagnostics monitor (no web layer).",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--port", metavar="PATH", help="open a serial device directly")
    source.add_argument("--slot", metavar="ID", help="open a config input slot (needs --config)")
    source.add_argument("--attach", metavar="HOST:PORT", help="attach to a running service (TCP)")
    source.add_argument("--attach-uds", metavar="PATH", help="attach over the service unix socket")

    parser.add_argument(
        "--config", metavar="PATH", help="engine config, to resolve --slot and enforce R17"
    )
    parser.add_argument("--baud", type=int, default=4800, help="baud for a standalone --port")

    renderer = parser.add_mutually_exclusive_group()
    renderer.add_argument("--plain", action="store_true", help="line renderer (no curses)")
    renderer.add_argument("--json", action="store_true", help="one JSON object per interval")

    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("--decode", action="store_true", help="inline per-line field decode")
    parser.add_argument(
        "--baud-sweep", action="store_true", help="run a standalone auto-baud sweep and exit"
    )
    parser.add_argument(
        "--interval", type=float, default=1.0, metavar="SECONDS", help="render cadence"
    )
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.Namespace:
    """Enforce cross-flag rules and stash a parsed ``(host, port)`` on the namespace.

    Any violation is reported via ``parser.error`` (exit code 2, no traceback). The parsed attach
    target and a ``standalone`` convenience flag are attached to ``args`` so ``main`` never
    re-parses. ``AF_UNIX`` absence (Windows) turns ``--attach-uds`` into a clean error rather than
    a later ``AttributeError``.
    """
    args.standalone = bool(args.port or args.slot)
    args.host = None
    args.port_num = None

    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.baud_sweep and not args.standalone:
        parser.error("--baud-sweep requires a standalone --port or --slot source")
    if args.slot and not args.config:
        parser.error("--slot requires --config to resolve the slot to a device path")
    if args.attach:
        try:
            args.host, args.port_num = parse_host_port(args.attach)
        except ValueError as exc:
            parser.error(str(exc))
    if args.attach_uds and getattr(socket, "AF_UNIX", None) is None:
        parser.error("--attach-uds needs AF_UNIX sockets (POSIX only)")
    return args


# --- PURE: renderers --------------------------------------------------------------


def _colorize(text: str, color: str | None, enabled: bool) -> str:
    """Wrap ``text`` in an ANSI colour when enabled; otherwise return it untouched."""
    if not enabled or not color:
        return text
    return f"{color}{text}{_RESET}"


def render_plain(snapshot: dict[str, Any], *, color: bool) -> str:
    """Render one snapshot as a compact multi-line block; colour is a legibility bonus only.

    The verdict is always printed as uppercase TEXT with the full valid/bad/malformed counts, so
    the block diagnoses correctly with colour stripped (piped, redirected, colourblind). When
    ``color`` is set the verdict token and a non-zero bad-checksum count are tinted as a fast
    secondary cue, never as the sole carrier of meaning.
    """
    port = snapshot.get("port_id", "?")
    baud = snapshot.get("baud", "?")
    verdict = str(snapshot.get("verdict", "unknown"))
    advice = str(snapshot.get("advice", "")).strip()
    lines = int(snapshot.get("lines", 0) or 0)
    valid = int(snapshot.get("valid", 0) or 0)
    bad = int(snapshot.get("bad_checksum", 0) or 0)
    malformed = int(snapshot.get("malformed", 0) or 0)
    sentences = snapshot.get("sentences_per_s", 0)
    bytes_ps = snapshot.get("bytes_per_s", 0)
    bus = snapshot.get("bus_load_pct", 0)
    printable = snapshot.get("printable_ratio", 0)
    talkers = snapshot.get("talkers", []) or []

    verdict_token = _colorize(verdict.upper(), _VERDICT_COLORS.get(verdict), color)
    bad_token = _colorize(str(bad), _VERDICT_COLORS["noise"], color and bad > 0)

    out = [
        f"[{port}] baud={baud}  verdict={verdict_token}",
        f"  lines={lines} valid={valid} bad_checksum={bad_token} malformed={malformed}",
        f"  sentences/s={sentences} bytes/s={bytes_ps} bus_load={bus}% printable={printable}",
        f"  talkers: {', '.join(talkers) if talkers else '(none)'}",
    ]
    if advice:
        out.append(f"  advice: {advice}")
    return "\n".join(out)


def render_json(snapshot: dict[str, Any]) -> str:
    """Serialize the stable :data:`_JSON_KEYS` subset of a snapshot as one compact JSON line.

    ``sort_keys`` keeps the field order deterministic so downstream diffing/parsing is stable
    across runs and across the standalone-vs-attach source of the snapshot.
    """
    subset = {key: snapshot[key] for key in _JSON_KEYS if key in snapshot}
    return json.dumps(subset, sort_keys=True, separators=(",", ":"))


def render_decode(line: str) -> str:
    """Format one line's full decode as a small field table. Never raises (``decode_line`` can't).

    A malformed/half-fragment line comes back as an ``{error, checksum_ok}`` block; a proprietary
    ``$P`` sentence reflects its raw comma fields; everything else lists ``field: value`` rows
    under a type/talker/checksum header.
    """
    info = decode_line(line)
    checksum = "ok" if info.get("checksum_ok") else "BAD"
    if "error" in info:
        return f"{line}\n  error: {info['error']}  (checksum {checksum})"

    stype = info.get("sentence_type") or ("proprietary" if info.get("proprietary") else "?")
    talker = info.get("talker")
    header = f"  type={stype}"
    if talker:
        header += f" talker={talker}"
    header += f" checksum={checksum}"
    out = [line, header]

    fields = info.get("fields")
    if isinstance(fields, dict):
        out.extend(f"    {key}: {value}" for key, value in fields.items())
    elif info.get("proprietary"):
        out.append(f"    raw_fields: {info.get('raw_fields')}")
    return "\n".join(out)


def format_baud_sweep(result: dict[str, Any]) -> str:
    """Render a :func:`score_baud` result as per-rate ratios plus a winner line.

    A ``None`` winner is reported honestly as "no printable structure at any rate" — per R29 that
    implicates polarity/wiring, not baud, so the sweep must not pick a bogus "best" rate. Rate
    keys are coerced to int because the same result may arrive from JSON (string keys) or straight
    from the scorer (int keys).
    """
    ratios_raw = result.get("ratios", {}) or {}
    ratios = {int(baud): float(ratio) for baud, ratio in ratios_raw.items()}
    winner = result.get("winner")
    winner = int(winner) if winner is not None else None

    out = ["baud sweep:"]
    for baud in sorted(ratios):
        mark = "  <- winner" if winner is not None and baud == winner else ""
        out.append(f"  {baud:>7}: {ratios[baud] * 100:5.1f}% valid{mark}")
    if winner is None:
        out.append(
            "winner: none — no printable structure at any rate "
            "(suspect polarity/wiring, not baud)"
        )
    else:
        out.append(f"winner: {winner} ({ratios[winner] * 100:.1f}% checksum-valid)")
    return "\n".join(out)


def parse_sse_frames(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse SSE ``event:``/``data:`` blocks out of a text buffer. Never raises on a partial frame.

    Blocks are separated by a blank line; comment/keepalive lines (leading ``:``) are ignored, and
    a frame whose ``data`` payload does not parse as a JSON object is skipped rather than raising —
    so feeding a half-received tail returns only the frames that fully parsed. Multi-line ``data``
    fields are concatenated with newlines per the SSE grammar.
    """
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        event = "message"
        data_lines: list[str] = []
        for raw in block.split("\n"):
            field_line = raw.rstrip("\r")
            if not field_line or field_line.startswith(":"):
                continue
            field, _, value = field_line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event = value
            elif field == "data":
                data_lines.append(value)
        if not data_lines:
            continue
        try:
            data = json.loads("\n".join(data_lines))
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            frames.append((event, data))
    return frames


# --- IO: sources ------------------------------------------------------------------


class _StandaloneSource:
    """A directly-opened serial device feeding one rolling :class:`PortDiagnostics`.

    Owns a receive-only :class:`SerialPort` whose raw tap feeds ``feed_bytes`` (exactly the seam
    the engine uses) and, for ``--decode``, assembles complete lines into a thread-safe pending
    list the render loop drains. On a dev box with no hardware the port simply opens ``present =
    False`` and yields nothing, so ``poll`` returns a clean "no-data" verdict and never blocks.
    """

    def __init__(self, path: str, baud: int, port_id: str, window_s: float = _WINDOW_S) -> None:
        self._diag = PortDiagnostics(port_id, baud, window_s=window_s)
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._linebuf = bytearray()
        self._port = SerialPort(
            path,
            baud,
            direction="rx",
            read_timeout=_STANDALONE_READ_TIMEOUT,
            on_raw=self._on_raw,
        )

    def _on_raw(self, chunk: bytes) -> None:
        """Feed diagnostics and, when decoding, split completed lines. Runs in the reader thread."""
        self._diag.feed_bytes(chunk, time.monotonic())
        with self._lock:
            self._linebuf += chunk
            while b"\n" in self._linebuf:
                raw, _, rest = self._linebuf.partition(b"\n")
                self._linebuf[:] = rest
                text = raw.rstrip(b"\r").decode("latin-1")
                if text:
                    self._pending.append(text)
            if len(self._linebuf) > _MAX_LINE_BYTES:
                self._linebuf.clear()  # newline-less runaway — stay bounded

    def start(self) -> None:
        self._port.start()

    def poll(self) -> list[dict[str, Any]]:
        return [self._diag.snapshot(time.monotonic())]

    def lines(self) -> list[str]:
        with self._lock:
            out = self._pending[:]
            self._pending.clear()
        return out

    def close(self) -> None:
        self._port.close()


class _UnixHTTPConnection(http.client.HTTPConnection):
    """An ``HTTPConnection`` that dials a unix socket — the production bind — instead of TCP.

    Lets the attach client reach the app over its unix socket with the ordinary ``http.client``
    request/response machinery, no new dependency. Only referenced on POSIX (``--attach-uds`` is
    rejected earlier where ``AF_UNIX`` is missing).
    """

    def __init__(self, uds_path: str, timeout: float | None = None) -> None:
        super().__init__("localhost", timeout=timeout)
        self._uds_path = uds_path

    def connect(self) -> None:
        # AF_UNIX is POSIX-only; this class is never reached on Windows (``--attach-uds`` is
        # rejected up front where AF_UNIX is missing), so the type checker's win32 stub gap is safe.
        family = socket.AF_UNIX  # type: ignore[attr-defined]
        sock = socket.socket(family, socket.SOCK_STREAM)
        if self.timeout is not None:
            sock.settimeout(self.timeout)
        sock.connect(self._uds_path)
        self.sock = sock


class _AttachSource:
    """A stdlib HTTP client attached to a running service's read-only diagnostics endpoints.

    Polls ``GET /api/diag`` for the per-port rolling snapshots the service already computes for the
    ports it is ACTIVELY using — zero contention, no new server surface, no credentials (it dials
    the app directly, behind the proxy). When ``stream`` is set it also opens ``GET /api/stream``
    on a background thread and harvests live ``nmea`` lines for ``--decode``, parsing frames with
    the pure :func:`parse_sse_frames` helper. All network errors degrade: a poll failure returns an
    empty list so a service restart is tolerated rather than crashing the monitor.
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        uds_path: str | None = None,
        timeout: float = 5.0,
        stream: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._uds_path = uds_path
        self._timeout = timeout
        self._stream = stream
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        if self._uds_path is not None:
            return _UnixHTTPConnection(self._uds_path, timeout=timeout)
        return http.client.HTTPConnection(self._host or "", self._port or 0, timeout=timeout)

    def start(self) -> None:
        if self._stream:
            self._thread = threading.Thread(target=self._run_stream, daemon=True)
            self._thread.start()

    def poll(self) -> list[dict[str, Any]]:
        """Fetch one ``/api/diag`` snapshot list. Returns ``[]`` on any network/parse failure."""
        conn = self._connect(self._timeout)
        try:
            conn.request("GET", "/api/diag")
            body = conn.getresponse().read()
        except OSError:
            return []
        finally:
            conn.close()
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, TypeError):
            return []
        ports = data.get("ports", []) if isinstance(data, dict) else []
        return [snap for snap in ports if isinstance(snap, dict)]

    def lines(self) -> list[str]:
        with self._lock:
            out = self._pending[:]
            self._pending.clear()
        return out

    def _run_stream(self) -> None:
        """Background reader for ``/api/stream``; extracts ``nmea`` lines until stopped or dropped.

        A short socket timeout lets the blocking read return periodically so the stop flag is
        honoured; any network hiccup ends the thread quietly (the poll path keeps the verdicts
        flowing regardless). Bounded: only completed ``\\n\\n`` frames are parsed and the buffer is
        never allowed to grow without a frame boundary.
        """
        conn = self._connect(max(self._timeout, 1.0))
        try:
            conn.request("GET", "/api/stream")
            resp = conn.getresponse()
        except OSError:
            conn.close()
            return
        buffer = ""
        try:
            while not self._stop.is_set():
                try:
                    chunk = resp.read(512)
                except (TimeoutError, OSError):
                    continue
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", "replace")
                while "\n\n" in buffer:
                    frame, _, buffer = buffer.partition("\n\n")
                    for event, data in parse_sse_frames(frame + "\n\n"):
                        if event == "nmea" and "line" in data:
                            with self._lock:
                                self._pending.append(str(data["line"]))
                if len(buffer) > _MAX_LINE_BYTES:
                    buffer = ""
        finally:
            conn.close()

    def close(self) -> None:
        self._stop.set()


# --- IO: config + safety ----------------------------------------------------------


def _load_config(path: str) -> EngineConfig:
    """Load an engine config for slot resolution / R17 enforcement. Raises on a bad path."""
    return EngineConfig.load(path)


def _resolve_standalone(
    args: argparse.Namespace, config: EngineConfig | None
) -> tuple[str, int, str]:
    """Resolve a standalone source to ``(path, baud, label)``.

    ``--slot`` draws its device path and baud from the named config input (KeyError-style clean
    failure if the slot is unknown); ``--port`` uses the operator-supplied path and ``--baud``.
    """
    if args.slot:
        assert config is not None  # guaranteed by _validate (--slot requires --config)
        for inp in config.inputs:
            if inp.id == args.slot:
                return inp.path, inp.baud, args.slot
        raise ValueError(f"unknown slot {args.slot!r} in config")
    return args.port, args.baud, args.port


def _sweep_refusal(args: argparse.Namespace, config: EngineConfig | None, path: str) -> str | None:
    """Return a refusal message if a baud sweep would hit an operational port, else ``None``.

    A baud sweep is the one active operation this monitor performs, so it obeys R17: an operator
    who names a raw ``--port`` chose that wire and is allowed, but if a config is present and the
    target maps to a slot that is operational (an assigned input, a channel source, or an output),
    the sweep is refused — a bench action must never drive a wire the running config depends on.
    """
    if config is None:
        return None
    if args.slot:
        if port_is_operational(config, args.slot):
            return f"refusing baud-sweep: slot {args.slot!r} is an operational port (read-only)"
        return None
    for inp in config.inputs:
        if inp.path == path and port_is_operational(config, inp.id):
            return f"refusing baud-sweep: {path} maps to operational slot {inp.id!r} (read-only)"
    return None


# --- IO: run loops ----------------------------------------------------------------


def _capture_at_baud(path: str, baud: int, window_s: float) -> bytes:
    """Open ``path`` at ``baud`` receive-only, harvest raw bytes for ``window_s``, and return them.

    A missing device opens ``present = False`` and yields nothing, so the capture is simply empty
    (no crash, no block) and ``score_baud`` scores a 0.0 ratio for that rate.
    """
    captured = bytearray()
    port = SerialPort(
        path,
        baud,
        direction="rx",
        read_timeout=_STANDALONE_READ_TIMEOUT,
        on_raw=captured.extend,
    )
    port.start()
    try:
        time.sleep(window_s)
    finally:
        port.close()
    return bytes(captured)


def _run_baud_sweep(path: str, args: argparse.Namespace) -> int:
    """Cycle the standard rates on a standalone port, score valid yield, print the result, exit."""
    per_baud = min(args.interval, _SWEEP_TOTAL_CAP_S / len(STANDARD_BAUDS))
    samples: dict[int, bytes] = {}
    deadline = time.monotonic() + _SWEEP_TOTAL_CAP_S
    try:
        for baud in STANDARD_BAUDS:
            samples[baud] = _capture_at_baud(path, baud, per_baud)
            if time.monotonic() >= deadline:
                break
    except KeyboardInterrupt:
        pass
    print(format_baud_sweep(score_baud(samples)))
    return 0


def _run_stream(source: Any, args: argparse.Namespace, *, color: bool) -> int:
    """Poll a source every interval and render each snapshot (and, with ``--decode``, live lines).

    First attach failure surfaces as a clean message + non-zero exit so a mistyped target is not a
    silent blank screen; later failures degrade quietly so a mid-run service restart is tolerated.
    """
    source.start()
    first = True
    try:
        while True:
            snapshots = source.poll()
            if first and not snapshots and isinstance(source, _AttachSource):
                print("attach failed: no diagnostics from the service", file=sys.stderr)
                return 4
            first = False
            for snapshot in snapshots:
                print(render_json(snapshot) if args.json else render_plain(snapshot, color=color))
            if args.decode:
                for line in source.lines():
                    print(render_decode(line))
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        source.close()


def _run_curses(source: Any, args: argparse.Namespace) -> int:
    """Run the curses TUI (POSIX only); on Windows or any ImportError, degrade with a clean note.

    curses is imported lazily HERE so importing this module never pulls in a POSIX-only extension;
    on failure the operator is pointed at ``--plain``/``--json`` and a non-zero-but-clean code is
    returned. Every cell the TUI paints comes from the pure ``render_*`` helpers, so its content is
    exercised by their unit tests even though the paint itself is manual-tested.
    """
    if os.name != "posix":
        print(_TUI_UNAVAILABLE, file=sys.stderr)
        return 3
    try:
        import curses
    except ImportError:
        print(_TUI_UNAVAILABLE, file=sys.stderr)
        return 3

    source.start()
    try:
        curses.wrapper(lambda screen: _curses_loop(screen, curses, source, args))
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
    return 0


def _curses_loop(screen: Any, curses: Any, source: Any, args: argparse.Namespace) -> None:
    """The curses paint loop: repaint each interval from the pure renderers; ``q`` quits."""
    curses.curs_set(0)
    screen.nodelay(True)
    screen.timeout(int(args.interval * 1000))
    while True:
        screen.erase()
        row = 0
        for snapshot in source.poll():
            for text in render_plain(snapshot, color=False).splitlines():
                with contextlib.suppress(curses.error):
                    screen.addstr(row, 0, text[: max(0, curses.COLS - 1)])
                row += 1
            row += 1
        if args.decode:
            for line in source.lines():
                flat = render_decode(line).replace("\n", " ")
                with contextlib.suppress(curses.error):
                    screen.addstr(row, 0, flat[: max(0, curses.COLS - 1)])
                row += 1
        screen.refresh()
        if screen.getch() in (ord("q"), ord("Q")):
            break


# --- entry point ------------------------------------------------------------------


def _build_source(args: argparse.Namespace, config: EngineConfig | None) -> Any:
    """Construct the standalone or attach source for the render loops from validated args."""
    if args.standalone:
        path, baud, label = _resolve_standalone(args, config)
        return _StandaloneSource(path, baud, label)
    if args.attach_uds:
        return _AttachSource(uds_path=args.attach_uds, stream=args.decode)
    return _AttachSource(host=args.host, port=args.port_num, stream=args.decode)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse, resolve safety, and dispatch to the requested renderer.

    Returns a process exit code; bad argument combinations exit 2 via argparse (no traceback), a
    refused baud sweep exits 5, an unreachable attach target exits 4, and an unavailable TUI exits
    3. Ctrl-C anywhere unwinds cleanly to 0.
    """
    parser = build_parser()
    args = _validate(parser, parser.parse_args(argv))

    config: EngineConfig | None = None
    if args.config:
        try:
            config = _load_config(args.config)
        except (OSError, ValueError, KeyError) as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2

    if args.baud_sweep:
        try:
            path, _, _ = _resolve_standalone(args, config)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        refusal = _sweep_refusal(args, config, path)
        if refusal:
            print(refusal, file=sys.stderr)
            return 5
        return _run_baud_sweep(path, args)

    try:
        source = _build_source(args, config)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    color = (not args.no_color) and sys.stdout.isatty()
    if args.json or args.plain:
        return _run_stream(source, args, color=color)
    return _run_curses(source, args)


if __name__ == "__main__":
    sys.exit(main())
