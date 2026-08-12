#!/usr/bin/env python3
"""AIS bench acceptance check — run this at the bench instead of eyeballing panes.

Observes the RUNNING service through its own instrumentation rather than opening the serial ports:
the engine holds every configured port exclusively, so a second reader would fail. Outputs are read
from the aggregate TCP tap and inputs from the ``input_nmea`` SSE frames, which is exactly what the
web UI sees — so a pass here means the UI would have shown a pass too.

Checks, in order (later ones are skipped when an earlier precondition fails):

  1. PORTS      every configured channel/input path is a real node AND actually open by the service.
                This is the ISSUE-020 guard: ``sinks[].down`` can never be true for serial, so the
                health report claims a channel is fine whether or not its port ever opened. Only
                ``lsof`` distinguishes them.
  2. RELAY WIRED  the live config really has rx_transparent_relay on the AIS channel, and a status
                sentence routes as `transparent` without stamping liveness.
  3. TRAFFIC    something is arriving on the AIS input at all, with a checksum/TAG-block breakdown.
                A wrong RS-422/RS-485 switch position or a baud mismatch shows up here as bytes with
                no valid sentences -- the two failures that look identical from the UI.
  4. LIVE       with traffic flowing the AIS channel reports LIVE:<input>, not SIM.
  5. TRANSPARENCY  every sentence body seen on the input appears on the output. Compares BODIES, not
                raw bytes: the RX path decodes with errors="replace" and strips, the TX path
                re-frames with a hard-coded CRLF, so byte equality is impossible by construction.
  6. MUTE       muting the input flips the channel to SIM within a second -- NOT after
                liveness_timeout_s -- and the relayed status chatter stops. Restores the mute state
                afterwards even on failure.

Usage (on the appliance):
    sudo /opt/mockingbuoy/.venv/bin/python ops/ais-bench-check.py
    sudo /opt/mockingbuoy/.venv/bin/python ops/ais-bench-check.py --seconds 60 --input ais_in

Exit code is 0 only when nothing FAILED; SKIP does not fail the run (no traffic yet is a legitimate
state, e.g. testing the wiring before the transponder is powered).
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

SOCK = "/run/mockingbuoy/app.sock"
APP_ROOT = Path("/opt/mockingbuoy")

_PASS, _FAIL, _SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    mark = {"PASS": "  ok  ", "FAIL": " FAIL ", "SKIP": " skip "}[status]
    print(f"[{mark}] {name}" + (f"\n         {detail}" if detail else ""), flush=True)


# --- talking to the running service ------------------------------------------------


def _http(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, str]:
    """One request over the app's unix socket. No dependency on Caddy, DNS, or TLS trust."""
    payload = json.dumps(body).encode() if body is not None else b""
    head = (
        f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
        + ("Content-Type: application/json\r\n" if body is not None else "")
        + (f"Content-Length: {len(payload)}\r\n" if body is not None else "")
        + "\r\n"
    ).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10.0)
        s.connect(SOCK)
        s.sendall(head + payload)
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    raw = b"".join(chunks).decode("utf-8", "replace")
    head_txt, _, body_txt = raw.partition("\r\n\r\n")
    code = int(head_txt.split(" ", 2)[1]) if " " in head_txt else 0
    return code, body_txt


def health() -> dict[str, Any]:
    code, body = _http("GET", "/healthz")
    if code != 200:
        raise RuntimeError(f"/healthz returned HTTP {code}")
    return json.loads(body)


def ais_source() -> str:
    for ch in health().get("channels", []):
        if ch.get("channel_id") == _ais_channel_id:
            return str(ch.get("source", "?"))
    return "?"


def set_input(input_id: str, enabled: bool) -> bool:
    code, _ = _http(
        "POST", "/api/control", {"action": "input", "input_id": input_id, "enabled": enabled}
    )
    return code == 200


# --- collectors ---------------------------------------------------------------------


def collect_tap(port: int, seconds: float, out: list[str]) -> None:
    """Every line the engine emits on any channel, via the aggregate tap."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as s:
            s.settimeout(1.0)
            end, buf = time.time() + seconds, b""
            while time.time() < end:
                try:
                    b = s.recv(65536)
                except TimeoutError:
                    continue
                if not b:
                    break
                buf += b
                while b"\n" in buf:
                    ln, _, buf = buf.partition(b"\n")
                    text = ln.decode("ascii", "replace").strip()
                    if text:
                        out.append(text)
    except OSError as exc:
        out.append(f"__ERROR__ {exc}")


def collect_inputs(seconds: float, out: list[tuple[str, str]]) -> None:
    """``input_nmea`` SSE frames — the received lines, including malformed ones."""
    req = (
        b"GET /api/stream HTTP/1.1\r\nHost: localhost\r\nAccept: text/event-stream\r\n"
        b"Connection: close\r\n\r\n"
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(SOCK)
            s.sendall(req)
            end, buf, event = time.time() + seconds, "", ""
            while time.time() < end:
                try:
                    chunk = s.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                while "\n" in buf:
                    line, _, buf = buf.partition("\n")
                    line = line.rstrip("\r")
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: ") and event == "input_nmea":
                        try:
                            d = json.loads(line[6:])
                            out.append((str(d.get("input", "")), str(d.get("line", ""))))
                        except json.JSONDecodeError:
                            pass
    except OSError as exc:
        out.append(("__ERROR__", str(exc)))


def body_of(sentence: str) -> str:
    """The comparable part of a sentence: delimiter..'*', no checksum, no framing, no whitespace."""
    return sentence.strip().lstrip("\\").partition("*")[0]


# --- checks -------------------------------------------------------------------------


def check_ports(cfg: dict[str, Any]) -> bool:
    want: list[tuple[str, str]] = []
    for ch in cfg.get("channels", []):
        if ch.get("path") and not ch.get("tap_only"):
            want.append((f"channel {ch['id']}", ch["path"]))
    for inp in cfg.get("inputs", []):
        if inp.get("path"):
            want.append((f"input {inp['id']}", inp["path"]))

    try:
        held = subprocess.run(
            ["lsof", "-Fn", "-p", str(_pid)], capture_output=True, text=True, check=False
        ).stdout
    except FileNotFoundError:
        record(_SKIP, "PORTS", "lsof not installed; cannot prove a port actually opened")
        return True

    open_targets = {ln[1:] for ln in held.splitlines() if ln.startswith("n/dev/")}
    ok = True
    for label, path in want:
        p = Path(path)
        if "CHANGE-ME" in path:
            record(_FAIL, f"PORTS {label}", f"still a placeholder path: {path}")
            ok = False
            continue
        if not p.exists():
            record(_FAIL, f"PORTS {label}", f"{path} does not exist (adapter unplugged?)")
            ok = False
            continue
        resolved = str(p.resolve())
        if resolved not in open_targets and path not in open_targets:
            record(
                _FAIL,
                f"PORTS {label}",
                f"{path} -> {resolved} exists but the service has NOT opened it. "
                "Per ISSUE-020 an open failure reports as 'device absent', so check group "
                "membership (must be dialout) and that nothing else holds the port.",
            )
            ok = False
        else:
            record(_PASS, f"PORTS {label}", f"{path} -> {resolved} open")
    return ok


def check_relay_wired() -> bool:
    sys.path.insert(0, str(APP_ROOT))
    from nmea_sim.config import EngineConfig  # noqa: PLC0415
    from nmea_sim.router import Router  # noqa: PLC0415

    cfg = EngineConfig.load(str(APP_ROOT / "data/config.local.json"))
    ais = next((c for c in cfg.channels if c.role == "ais"), None)
    if ais is None:
        record(_FAIL, "RELAY WIRED", "no channel with role 'ais'")
        return False
    if not ais.rx_transparent_relay:
        record(
            _SKIP,
            "RELAY WIRED",
            f"channel {ais.id!r} has rx_transparent_relay=false — status/alarm sentences "
            "(ALR/ALF/ABK/TXT/VER) will be DROPPED, not forwarded",
        )
        return True

    router = Router(cfg)
    alr = "$AIALR,000000.00,001,A,V,AIS: general failure*04"
    decision = router.note_rx(_input_id, alr, 100.0)
    if decision is None or decision.kind != "transparent":
        record(_FAIL, "RELAY WIRED", f"status sentence did not route as transparent: {decision}")
        return False
    if router.any_live(ais.id, "ais", 100.0):
        record(
            _FAIL,
            "RELAY WIRED",
            "a status sentence STAMPED liveness — the channel would sit LIVE on alarm chatter "
            "alone and never fall back to simulating",
        )
        return False
    record(_PASS, "RELAY WIRED", "status sentence forwards as transparent and stamps no liveness")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=30.0, help="observation window (default 30)")
    ap.add_argument("--input", default="ais_in", help="input slot id (default ais_in)")
    ap.add_argument("--tap-port", type=int, default=10110, help="aggregate tap port")
    args = ap.parse_args()

    global _pid, _input_id, _ais_channel_id
    _input_id = args.input

    print("=" * 78)
    print("AIS bench acceptance check")
    print("=" * 78)

    try:
        h = health()
    except Exception as exc:  # noqa: BLE001 - any failure here means the service is unusable
        print(f"cannot reach the service on {SOCK}: {exc}")
        return 2

    _pid = int(
        subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", "mockingbuoy"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        or 0
    )
    cfg = json.loads(_http("GET", "/api/config")[1])
    ais_ch = next((c for c in cfg.get("channels", []) if c.get("role") == "ais"), None)
    _ais_channel_id = str(ais_ch["id"]) if ais_ch else "ais"

    print(f"mode={h.get('mode')}  time_source={h.get('time_source')}  pid={_pid}")
    print(f"ais channel={_ais_channel_id}  input slot={_input_id}\n")

    # /api/config strips device paths (R19), so read them from the file for the port check.
    raw_cfg = json.loads((APP_ROOT / "data/config.local.json").read_text())
    check_ports(raw_cfg)
    check_relay_wired()

    print(f"\n-- observing {args.seconds:.0f}s --")
    tap_lines: list[str] = []
    in_lines: list[tuple[str, str]] = []
    threads = [
        threading.Thread(target=collect_tap, args=(args.tap_port, args.seconds, tap_lines)),
        threading.Thread(target=collect_inputs, args=(args.seconds, in_lines)),
    ]
    for t in threads:
        t.start()
    source_seen = set()
    end = time.time() + args.seconds
    while time.time() < end:
        source_seen.add(ais_source())
        time.sleep(1.0)
    for t in threads:
        t.join(timeout=5.0)

    mine = [ln for slot, ln in in_lines if slot == _input_id]
    if not mine:
        record(
            _SKIP,
            "TRAFFIC",
            f"nothing received on {_input_id} in {args.seconds:.0f}s. If the talker IS connected: "
            "check the adapter mode switch is RS-422 (not RS-485), the baud matches, and A/B are "
            "not swapped.",
        )
    else:
        sys.path.insert(0, str(APP_ROOT))
        from nmea_sim import checksum  # noqa: PLC0415

        bad = [ln for ln in mine if not checksum.verify(ln)]
        tagged = [ln for ln in mine if ln.startswith("\\")]
        detail = f"{len(mine)} lines, {len(bad)} bad checksum, {len(tagged)} TAG-block"
        if tagged:
            record(
                _FAIL,
                "TRAFFIC",
                detail + " — TAG-block lines are DROPPED before routing (ISSUE-033); no config "
                "setting forwards them",
            )
        elif len(bad) == len(mine):
            record(
                _FAIL,
                "TRAFFIC",
                detail + " — bytes are arriving but NOTHING verifies. This is what a wrong baud or "
                "a wrong RS-422/485 switch position looks like.",
            )
        else:
            record(_PASS, "TRAFFIC", detail)

        if "SIM" in source_seen and not any(s.startswith("LIVE") for s in source_seen):
            record(_FAIL, "LIVE", f"traffic arrived but the channel never left SIM: {source_seen}")
        else:
            record(_PASS, "LIVE", f"channel source observed: {sorted(source_seen)}")

        out_bodies = {body_of(x) for x in tap_lines}
        missing = [ln for ln in mine if checksum.verify(ln) and body_of(ln) not in out_bodies]
        if missing:
            sample = "; ".join(m[:48] for m in missing[:3])
            record(
                _FAIL,
                "TRANSPARENCY",
                f"{len(missing)}/{len(mine)} received sentences never reached the output. "
                f"First: {sample}",
            )
        else:
            record(_PASS, "TRANSPARENCY", f"all {len(mine)} received sentences appeared on output")

    # --- MUTE: must be immediate, and must close the relay window -------------------
    before = ais_source()
    if not before.startswith("LIVE"):
        record(_SKIP, "MUTE", f"channel is {before}, not LIVE — nothing to fall back from")
    else:
        if not set_input(_input_id, False):
            record(_FAIL, "MUTE", "the input toggle request failed")
        else:
            try:
                deadline = time.time() + 2.0
                flipped = None
                while time.time() < deadline:
                    if ais_source() == "SIM":
                        flipped = time.time()
                        break
                    time.sleep(0.1)
                if flipped is None:
                    record(
                        _FAIL,
                        "MUTE",
                        "still not SIM 2s after muting — clear_liveness is not being called, so "
                        "fallback is waiting out liveness_timeout_s",
                    )
                else:
                    record(
                        _PASS, "MUTE", "flipped to SIM within 2s of the mute (not after timeout)"
                    )
                    post: list[str] = []
                    collect_tap(args.tap_port, 4.0, post)
                    leaked = [x for x in post if body_of(x) in {body_of(m) for m in mine}]
                    if leaked:
                        record(
                            _FAIL,
                            "RELAY SUPPRESSED",
                            f"{len(leaked)} relayed sentences still reaching the output while SIM",
                        )
                    else:
                        record(_PASS, "RELAY SUPPRESSED", "no relayed traffic on the output in SIM")
            finally:
                set_input(_input_id, True)  # always restore, even on failure

    failed = [r for r in _results if r[0] == _FAIL]
    skipped = [r for r in _results if r[0] == _SKIP]
    print("\n" + "=" * 78)
    print(
        f"{len(_results) - len(failed) - len(skipped)} passed, {len(failed)} failed, "
        f"{len(skipped)} skipped"
    )
    for _, name, detail in failed:
        print(f"  FAILED: {name} — {detail}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    _pid = 0
    _input_id = "ais_in"
    _ais_channel_id = "ais"
    sys.exit(main())
