"""FastAPI front end for the NMEA simulator engine.

This is the only web-aware module in the project. It imports :mod:`nmea_sim` (the pure,
hardware-free engine) but the engine never imports this layer — the dependency arrow points
one way (``web`` -> ``nmea_sim``), and ``tkinter`` is never imported anywhere.

The app wraps a single :class:`~nmea_sim.engine.Engine` behind an
:class:`EngineManager`, fans every emitted sentence out to browser clients over
Server-Sent Events via a thread-safe :class:`Broker` (a ``janus.Queue`` bridging the
engine's worker threads to the asyncio event loop), and exposes a small control API.

Key seams:

* ``Engine(config, monitor=broker.publish)`` — the engine calls ``monitor(channel_id, line)``
  from its worker threads for every emitted line; that is the SSE source.
* The Broker's ``janus.Queue`` is constructed **inside the running event loop** (lifespan
  startup), never at import time.
* Engine threads are one-shot: :meth:`EngineManager.start` always builds a **fresh** Engine.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import math
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import janus
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field

from nmea_sim.config import (
    EngineConfig,
    InputSpec,
    effective_depth_sim,
    effective_heading_sim,
    effective_rudder_sim,
    effective_wind_sim,
)
from nmea_sim.diagnostics import decode_line, score_baud
from nmea_sim.engine import Engine, HealthReport, port_is_operational, targetable_slots
from nmea_sim.serialport import SerialPort
from nmea_sim.state import VesselState
from nmea_sim.wind import apparent_wind
from web.display_sim import simulate_display_instruments

# --- constants --------------------------------------------------------------------

#: Module logger. The SSE background tasks log here so an iteration error is surfaced
#: (fail loud) rather than silently killing the loop.
logger = logging.getLogger(__name__)

#: Directory holding the static single-page UI, resolved relative to this file so it
#: works regardless of the process working directory.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"
#: The UI's externalized stylesheet + script (kept out of the HTML so a strict CSP with
#: no ``'unsafe-inline'`` for scripts still loads them — they are same-origin ``'self'``).
_APP_CSS = _STATIC_DIR / "app.css"
_APP_JS = _STATIC_DIR / "app.js"
#: Throwaway conning-redesign proof-of-concept page (static, self-contained). Served by its
#: own explicit route like the other static files; remove with the POC once the redesign lands.
_CONNING_POC = _STATIC_DIR / "conning-poc.html"

#: Per-subscriber SSE queue depth. On overflow the oldest frame is dropped so a slow
#: browser can never apply back-pressure to the engine threads.
_SUBSCRIBER_MAXSIZE = 1000

#: Hard cap on concurrent SSE subscribers. Bounds total memory (each subscriber holds up
#: to ``_SUBSCRIBER_MAXSIZE`` frames) and the per-frame fan-out cost. This in-app cap is the
#: ONLY subscriber bound: the shipped reverse proxy enforces no per-IP connection limit, so a
#: single authenticated client can legitimately occupy all of these slots (over-limit -> 503).
_MAX_SUBSCRIBERS = 64

#: Bridge queue depth between engine threads and the event loop. On overflow
#: :meth:`Broker.publish` drops the line (never blocks an engine thread).
_BRIDGE_MAXSIZE = 10_000

#: How often the health-broadcast task emits a ``health`` SSE frame, in seconds.
_HEALTH_INTERVAL_S = 1.0

#: How often the state-broadcast task emits a ``state`` SSE frame, in seconds (~4 Hz). Fast enough
#: to drive a smooth conning display; the drop-oldest subscriber queue keeps the NEWEST frames, so a
#: state frame is never starved by the nmea flood under normal load. A fuller interest-filtered /
#: latest-wins broker redesign (a separate diagnostics stream) is deferred to the diagnostics phase.
_STATE_INTERVAL_S = 0.25

#: Minimum seconds between two active diagnostics actions on the SAME slot. A deliberate friction
#: (single-flight is enforced separately): a second action inside the cooldown is refused (429) so a
#: bench port can't be hammered with back-to-back send/loopback/sweep drives.
_DIAG_COOLDOWN_S = 5.0

#: Where diagnostics captures are written — the sole writable path (``data/``, git-ignored). Every
#: capture filename is SERVER-generated under here (R18); a caller can never supply a path.
_DIAG_DATA_DIR = "data"

#: In-app web-password rotation files, all under the app's one writable dir (``data/``). The app
#: (sandboxed, cannot write ``secrets/`` or restart caddy) drops a hash+nonce REQUEST here; a ROOT
#: systemd path-unit performs the privileged rewrite + caddy restart and writes back a RESULT the
#: app polls. Only a bcrypt HASH ever touches disk — never plaintext (Global CLAUDE.md §9).
_WEBPASS_REQUEST = "webpass-request.json"  # app -> root, in data/, mode 0600, hash-only
_WEBPASS_RESULT = "webpass-result.json"  # root -> app, in data/, written by the root unit
_WEBPASS_MARKER = ".webpass_changed"  # app-written marker; existence => password changed
_WEBPASS_MIN_LEN = 12  # length policy for a new web password
_WEBPASS_POLL_TIMEOUT_S = 15.0  # app bound waiting for the root unit's result
_WEBPASS_POLL_INTERVAL_S = 0.5  # result-file poll cadence

#: At most this many raw captures may run at once (web-layer cap; per-file byte + wall-clock caps
#: live in ``CaptureSession``, the total-``data/`` quota is checked below).
_CAPTURE_MAX_CONCURRENT = 2

#: Per-capture-file hard byte cap (handed to ``CaptureSession``; it auto-stops on reaching it).
_CAPTURE_MAX_BYTES = 1 << 20  # 1 MiB

#: Per-capture wall-clock ceiling in seconds (``CaptureSession`` auto-stops on reaching it).
_CAPTURE_MAX_SECONDS = 300.0

#: Total-``data/`` byte quota. A new capture is refused once the directory already holds this much,
#: so runaway captures can never fill the only writable path.
_CAPTURE_DATA_QUOTA_BYTES = 64 << 20  # 64 MiB

#: Accepted numeric state-edit fields and their inclusive ``(min, max)`` ranges.
#: ``None`` means unbounded on that side. Mirrors ``nmea_sim.validate`` where sensible.
_UPDATE_RANGES: dict[str, tuple[float | None, float | None]] = {
    "lat": (-90.0, 90.0),
    "lon": (-180.0, 180.0),
    "sog_kn": (0.0, None),
    "cog_deg": (0.0, 360.0),
    "heading_true_deg": (0.0, 360.0),
    "heading_mag_deg": (0.0, 360.0),
    "mag_variation_deg": (-180.0, 180.0),
    "altitude_m": (None, None),
    "fix_quality": (0.0, None),
    "satellites": (0.0, None),
    "hdop": (0.0, None),
    # Manual (operator-editable) instrument/nav fields. Nothing DERIVED lives here: pitch_deg and
    # roll_deg are owned by the physics tick (sea-state motion model) and must never be set by hand.
    "stw_kn": (0.0, 100.0),
    "depth_m": (0.0, 12000.0),
    "rot_dpm": (-720.0, 720.0),
    "wind_speed_kn": (0.0, 200.0),
    "wind_dir_deg": (0.0, 360.0),
    "sea_state": (0.0, 9.0),
    "rudder_angle_deg": (-45.0, 45.0),
    "set_deg": (0.0, 360.0),
    "drift_kn": (0.0, 100.0),
}

#: The subset of :data:`_UPDATE_RANGES` that the Save-as-defaults persist endpoint (R15 allow-list)
#: accepts as own-ship initial-state overrides. The original manual fields (``sea_state`` included)
#: PLUS the 11 nav/GNSS position fields (A3) — the whole persistable own-ship set. Every DERIVED
#: field (pitch_deg/roll_deg, apparent wind) is still deliberately excluded. Each name here is a key
#: of :data:`_UPDATE_RANGES` so the shared range-check-then-merge loop covers all of them; tuple
#: order is irrelevant.
_INITIAL_STATE_MANUAL_FIELDS: tuple[str, ...] = (
    "stw_kn",
    "depth_m",
    "rot_dpm",
    "wind_speed_kn",
    "wind_dir_deg",
    "sea_state",
    "rudder_angle_deg",
    "set_deg",
    "drift_kn",
    # A3: the 11 nav/GNSS position fields (fix_quality/satellites are int on the model).
    "lat",
    "lon",
    "sog_kn",
    "cog_deg",
    "heading_true_deg",
    "heading_mag_deg",
    "mag_variation_deg",
    "altitude_m",
    "fix_quality",
    "satellites",
    "hdop",
)

#: Directories searched for AIS realism-profile files, in priority order: the shipped ``profiles/``
#: dir first, then the writable ``data/profiles/`` dir. The web only ever names a profile by
#: BASENAME (a dropdown value from ``GET /api/profiles``); that basename is resolved against these
#: dirs before it reaches config, so a persist request can never point ``profile_path`` at an
#: arbitrary filesystem location (R15/R18), and only basenames are ever returned (R19). A missing
#: ``data/profiles/`` is treated as empty, never an error — a fresh install has no writable dir yet.
_PROFILE_SEARCH_DIRS: tuple[str, ...] = ("profiles", "data/profiles")

#: Inclusive ``(min, max)`` bounds for the value-bearing GPS-fault actions (F3). ``hdop_spike`` and
#: ``drop_sats`` each carry a numeric magnitude that is range-checked here before it reaches
#: ``update_state`` — a fault can never poke a field outside these bounds. The valueless faults
#: (``no_fix``/``restore_fix``/``gps_kill``/``gps_restore``) set a fixed value and take no value.
_FAULT_BOUNDS: dict[str, tuple[float, float]] = {
    "hdop_spike": (0.0, 99.0),
    "drop_sats": (0.0, 64.0),
}

#: The ONLY display-only keys an override may touch (A4). These MUST equal keys
#: ``simulate_display_instruments`` produces under the SSE ``sim`` block — every one is already
#: present in ``sim``, so applying an override via ``sim.update(...)`` overwrites and never adds a
#: key, keeping that dict's key set intact. None are wire-backed (they emit no NMEA) and none
#: live in ``_UPDATE_RANGES``, so a display override can never leak into a vessel-state
#: ``update``. Six are purely cosmetic; ``engine_order_pct`` is the ENGINE ORDER telegraph,
#: which now DRIVES the rpm/load model inside ``simulate_display_instruments`` (still
#: display-only — never on the wire).
_DISPLAY_OVERRIDE_KEYS: frozenset[str] = frozenset(
    {
        "water_temp_c",
        "air_temp_c",
        "humidity_pct",
        "pressure_hpa",
        "fuel_total_l",
        "fuel_rate_lph",
        "engine_order_pct",
    }
)

#: Inclusive ``(lo, hi)`` range for the display-override keys that are NOT free-floating. Most keys
#: are finiteness-only (any finite float rides through), but ``engine_order_pct`` is the telegraph
#: order that must stay in ``[-100, 100]`` % (negative = astern). Enforced at BOTH the live
#: ``display_override`` handler and the persist-merge, so neither a live set nor a saved default can
#: park an out-of-range order. A key absent from this map is finiteness-only, as before.
_DISPLAY_OVERRIDE_BOUNDS: dict[str, tuple[float, float]] = {
    "engine_order_pct": (-100.0, 100.0),
}


# --- (de)serialization helpers ----------------------------------------------------


def _state_to_dict(state: VesselState) -> dict[str, Any]:
    """Serialize a vessel snapshot to JSON-safe primitives (``utc`` as ISO 8601).

    Includes the derived ``pitch_deg``/``roll_deg`` (physics-owned) for display, plus the apparent
    (relative) wind computed ON READ from the true wind and the vessel's motion over ground — the
    conning display wants bow-relative wind, which the stored state never holds, so it is resolved
    here rather than carried as a persisted field that could drift out of sync with the true wind.
    """
    app_speed, app_angle = apparent_wind(
        state.wind_speed_kn,
        state.wind_dir_deg,
        state.heading_true_deg,
        state.cog_deg,
        state.sog_kn,
    )
    return {
        "lat": state.lat,
        "lon": state.lon,
        "sog_kn": state.sog_kn,
        "cog_deg": state.cog_deg,
        "heading_true_deg": state.heading_true_deg,
        "heading_mag_deg": state.heading_mag_deg,
        "mag_variation_deg": state.mag_variation_deg,
        "altitude_m": state.altitude_m,
        "fix_quality": state.fix_quality,
        "satellites": state.satellites,
        "hdop": state.hdop,
        "stw_kn": state.stw_kn,
        "depth_m": state.depth_m,
        "rot_dpm": state.rot_dpm,
        "wind_speed_kn": state.wind_speed_kn,
        "wind_dir_deg": state.wind_dir_deg,
        "sea_state": state.sea_state,
        "pitch_deg": state.pitch_deg,
        "roll_deg": state.roll_deg,
        "rudder_angle_deg": state.rudder_angle_deg,
        "set_deg": state.set_deg,
        "drift_kn": state.drift_kn,
        "app_wind_speed_kn": app_speed,
        "app_wind_angle_deg": app_angle,
        "utc": state.utc.isoformat(),
    }


def _driven_fields(manager: EngineManager) -> list[str]:
    """State fields the engine overwrites every tick, so a manual ``update`` won't stick (A3b).

    Advisory only — the UI greys these ``cfg-<field>`` inputs out; the ``update`` action still
    applies them (no hard refuse). Computed from the CONFIG (not the live tick) so it is stable and
    cheap. Frozen rules (tests + UI depend on exactly this):

    * ``depth_m`` — present iff the EFFECTIVE depth-sim is enabled (default-ON in simulate mode via
      ``effective_depth_sim``; an explicit ``{enabled:false}`` block or a non-simulate mode disables
      it). Depth runs regardless of an active route.
    * ``rudder_angle_deg`` — present iff the EFFECTIVE rudder-sim is enabled AND no route driver
      exists (a route owns the helm; the sim yields to it).
    * ``heading_true_deg``/``heading_mag_deg`` — present iff the EFFECTIVE heading-sim is enabled
      AND no route driver exists (same yield-to-route rule as rudder).
    * ``wind_speed_kn``/``wind_dir_deg`` — present iff the EFFECTIVE wind-sim is enabled (default-ON
      in simulate mode via ``effective_wind_sim``). Wind is not helm, so it runs regardless of a
      route.
    * ``cog_deg``/``sog_kn`` — present iff a route driver exists (``route_status()`` is not None; it
      returns None with no driver), i.e. simulate route mode.
    * In ``auto`` mode, every ``rx_accept`` field of each channel whose ``rx_feeds_state`` is set
      (that channel's parsed RX feed owns those fields).

    The route gate is the SINGLE predicate ``route_status() is not None`` — the same one the
    engine's physics tick uses to drop sim-authored heading/rudder — so grey-out and engine writes
    agree through the paused/finished window (``route_status()`` stays non-None until the driver is
    gone).
    """
    driven: set[str] = set()
    cfg = manager.config
    # ``effective_*`` resolve the default-ON mechanism (absent block => enabled in simulate mode)
    # and return None outside simulate, so auto RX / replay report no sim-driven fields. The depth
    # seed only affects a DEFAULT block's base, never the enabled flag, so a cheap fallback is fine.
    initial_depth = float(cfg.initial_state_raw.get("depth_m", 10.0))
    route_active = manager.route_status() is not None
    dep = effective_depth_sim(cfg, initial_depth)
    if dep is not None and dep.enabled:
        driven.add("depth_m")
    rud = effective_rudder_sim(cfg)
    if rud is not None and rud.enabled and not route_active:
        driven.add("rudder_angle_deg")
    hdg = effective_heading_sim(cfg)
    if hdg is not None and hdg.enabled and not route_active:
        driven.update(("heading_true_deg", "heading_mag_deg"))
    initial_ws = float(cfg.initial_state_raw.get("wind_speed_kn", 0.0))
    initial_wd = float(cfg.initial_state_raw.get("wind_dir_deg", 0.0))
    wnd = effective_wind_sim(cfg, initial_ws, initial_wd)
    if wnd is not None and wnd.enabled:
        driven.update(("wind_speed_kn", "wind_dir_deg"))
    if manager.route_status() is not None:
        driven.update(("cog_deg", "sog_kn"))
    if cfg.mode == "auto":
        for ch in cfg.channels:
            if getattr(ch, "rx_feeds_state", False):
                driven.update(ch.rx_accept)
    return sorted(driven)


def _health_to_dict(
    report: HealthReport,
    *,
    mode: str | None = None,
    time_source: str | None = None,
) -> dict[str, Any]:
    """Serialize a :class:`HealthReport` to a JSON-safe dict (``status`` = ``running``).

    Each channel carries its ``source`` badge (``OFF``/``LIVE:<id>``/``SIM``). ``mode`` and
    ``time_source`` are top-level UI badges supplied by the caller (which alone knows the running
    config's mode and the engine's resolved time tag); each is included only when provided, so a
    stopped-engine health dict can carry ``mode`` from config while omitting ``time_source``.
    """
    out: dict[str, Any] = {
        "status": "running",
        "ok": report.ok,
        "physics_alive": report.physics_alive,
        "channels": [
            {
                "channel_id": ch.channel_id,
                "alive": ch.alive,
                "enabled": ch.enabled,
                "emitted": ch.emitted,
                "build_errors": ch.build_errors,
                "last_emit_age_s": ch.last_emit_age_s,
                "source": ch.source,
                "sinks": [{"name": s.name, "down": s.down, "errors": s.errors} for s in ch.sinks],
            }
            for ch in report.channels
        ],
        # Per-input RX mute state, so the input toggle reconciles off the same 1 Hz frame as the
        # output toggle instead of a tab-gated poll. Id + flag only — never the device path (R19).
        "inputs": [{"input_id": i.input_id, "enabled": i.enabled} for i in report.inputs],
    }
    if mode is not None:
        out["mode"] = mode
    if time_source is not None:
        out["time_source"] = time_source
    return out


def _resolve_port(path: str | None) -> str | None:
    """Resolve a configured device path to its kernel port name (e.g. ``ttyUSB0``) for display.

    A ``/dev/serial/by-id/...`` symlink resolves to its ``/dev/ttyX`` target, so the operator sees
    WHICH port a channel uses without the adapter brand/serial the by-id link itself carries (R19).
    We reveal ONLY a real resolved device node (basename) — if the path does not resolve (a Simulate
    placeholder, or hardware absent) we return None rather than leak the by-id tail (adapter serial)
    or a placeholder. Only a basename is ever returned — never a full path."""
    if not path:
        return None
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return None
    # Unresolved (still under by-id, or the raw path came back unchanged) => reveal nothing.
    if resolved == path or "by-id" in resolved:
        return None
    base = os.path.basename(resolved)
    if not base or base.startswith(("usb-", "pci-")):  # never surface an adapter-serial basename
        return None
    return base


#: Where USB-serial adapters advertise themselves under stable, per-unit names. Module-level so
#: tests can point it at a tmpdir — CI has no ``/dev/serial/by-id``, and the enumeration below is
#: the only place the app touches it.
_BY_ID_DIR = "/dev/serial/by-id"


def _adapter_handle(by_id_path: str) -> str:
    """Opaque, stable handle for an adapter, derived from its by-id link.

    Deliberately a digest, not the link name: the by-id tail IS the adapter brand + per-unit serial
    (R19). The client picks an adapter by handle and the SERVER maps handle -> path, so a device
    path is never a client-supplied value — accepting one would be an arbitrary-device-open
    primitive. Stable across reboots and replugs because the by-id name is.
    """
    return hashlib.sha256(by_id_path.encode("utf-8")).hexdigest()[:16]


def _enumerate_adapters() -> list[tuple[str, str]]:
    """Attached USB-serial adapters as ``(handle, by_id_path)``, sorted for a stable UI order.

    Returns ``[]`` when the directory is absent (no adapters, or not Linux) rather than raising —
    an empty picker is a correct answer on a hardware-less dev box.
    """
    try:
        names = sorted(os.listdir(_BY_ID_DIR))
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for name in names:
        path = os.path.join(_BY_ID_DIR, name)
        out.append((_adapter_handle(path), path))
    return out


def _redacted_config_dict(config: EngineConfig) -> dict[str, Any]:
    """``config.to_dict()`` with every device ``path`` dropped (R19 / M9).

    ``GET /api/config`` returns this instead of the raw dict: the channel + input ``path`` fields
    are ``/dev/serial/by-id/...`` links that carry the adapter brand + serial (and full fs paths),
    exactly the info ``/api/inputs`` and ``/api/profiles`` deliberately withhold. The UI never reads
    a device path (it uses ``/api/inputs`` for slot state and the tap host/port for taps), so the
    key is removed outright. ``to_dict`` builds fresh nested dicts, so this never mutates the live
    config. The AIS ``profile_path`` (a repo-relative profile filename the dropdown pre-selects on)
    is left intact — it is not a device path and the UI basenames it for display."""
    data = config.to_dict()
    for channel in data.get("channels", []):
        # Expose only the resolved kernel port name (ttyUSB0), not the by-id link (brand/serial).
        channel["port"] = _resolve_port(channel.get("path"))
        channel.pop("path", None)
    for inp in data.get("inputs", []):
        inp["port"] = _resolve_port(inp.get("path"))
        inp.pop("path", None)
    return data


def _input_mismatch(function: str, detected_class: str | None) -> bool:
    """Whether a slot's detected sentence class contradicts its declared ``function``.

    A read-only sanity flag for the operator: ``gps`` expects ``gnss``; ``sat`` (a satellite
    compass) legitimately carries ``gnss`` *or* ``heading``, so it only conflicts when the detected
    class is ``ais``; ``ais`` expects ``ais``; ``unused`` never conflicts. No detection (``None``)
    is never a mismatch — an idle slot is not a wiring error.
    """
    if detected_class is None or function == "unused":
        return False
    if function == "gps":
        return detected_class != "gnss"
    if function == "sat":
        return detected_class == "ais"
    if function == "ais":
        return detected_class != "ais"
    return False


# --- diagnostics active-action IO (bounded, best-effort) --------------------------


def _nmea_checksum(body: str) -> str:
    """XOR the sentence body (between ``$`` and ``*``) into the two-hex-digit ``*HH`` suffix."""
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"{cs:02X}"


def _loopback_sentence() -> str:
    """A well-formed, vendor-neutral self-test sentence written during a loopback probe."""
    body = "PMBLOOPBACK,1"
    return f"${body}*{_nmea_checksum(body)}"


def _tx_probe(path: str, baud: int, framing: str, line: str) -> dict[str, Any]:
    """Best-effort single-line TX on a bench port, then close. Bounded and tolerant.

    Uses the ordinary tolerant :class:`SerialPort`: a missing/absent device never raises — it
    reports ``present=False`` and the write is silently dropped. On a dev box with no hardware the
    port never opens, so ``sent`` is ``False``. Runs off the event loop (the caller offloads it).
    The device ``path`` is consumed here and never returned (R19 info-leak hygiene).
    """
    port = SerialPort(path, baud, framing=framing, direction="tx")
    port.start()
    try:
        port.write_line(line)
        return {"sent": port.present, "present": port.present}
    finally:
        port.close()


def _data_dir_bytes(data_dir: str) -> int:
    """Total size in bytes of regular files directly under ``data_dir`` (0 if it does not exist)."""
    root = Path(data_dir)
    if not root.is_dir():
        return 0
    total = 0
    for child in root.iterdir():
        if child.is_file():
            with contextlib.suppress(OSError):
                total += child.stat().st_size
    return total


# --- AIS realism-profile discovery / basename resolution --------------------------


def _list_profile_names() -> list[str]:
    """Sorted, de-duplicated ``*.json`` basenames across the profile search dirs.

    A search dir that does not exist contributes nothing (never an error — ``data/profiles/`` may be
    absent on a fresh install). Only basenames are returned; the search-dir prefix / absolute path
    is never exposed (R19). A name present in both dirs appears once.
    """
    names: set[str] = set()
    for search_dir in _PROFILE_SEARCH_DIRS:
        root = Path(search_dir)
        if not root.is_dir():
            continue
        for child in root.glob("*.json"):
            if child.is_file():
                names.add(child.name)
    return sorted(names)


def _resolve_profile_basename(name: str) -> str:
    """Resolve a web-supplied profile BASENAME to a stored path under a search dir, or raise 400.

    The web only ever sends a bare basename (the value of a ``GET /api/profiles`` dropdown option).
    Any value carrying a path separator (``/`` or ``\\``) or a ``..`` parent segment is rejected
    outright — it can never be a legitimate basename and is the shape a directory-traversal takes
    (R18). The basename is then located in the search dirs (shipped first, then writable); a name
    that matches no profile is a hard error (fail loudly), never a silent write. The returned path
    is stored in config; ``validate_or_raise`` re-checks it exists and loads before the save lands.
    """
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(
            status_code=400,
            detail="profile_path must be a bare filename (no path separators or '..')",
        )
    for search_dir in _PROFILE_SEARCH_DIRS:
        candidate = Path(search_dir) / name
        if candidate.is_file():
            return str(candidate)
    raise HTTPException(status_code=400, detail=f"unknown profile: {name!r}")


# --- in-app web-password rotation (hash-only; plaintext never persisted) -----------


def _caddy_hash(plaintext: str) -> str:
    """Produce a bcrypt hash for ``plaintext`` by shelling to caddy, feeding the plaintext on STDIN
    (never argv). No new Python dependency. The plaintext is used only here and never logged; caddy
    stderr is NOT surfaced (it could, in principle, echo input) — errors are raised generically."""
    caddy = shutil.which("caddy") or "/usr/bin/caddy"
    proc = subprocess.run(
        [caddy, "hash-password", "--algorithm", "bcrypt"],
        input=(plaintext + "\n").encode(),  # caddy needs a newline-TERMINATED line (else EOF abort)
        capture_output=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("caddy hash-password failed")  # no plaintext, no stderr in the message
    out = proc.stdout.decode().strip()
    if not out.startswith(("$2a$", "$2b$", "$2y$")):
        raise RuntimeError("unexpected hash format")
    return out


def _write_webpass_request(record: dict[str, Any]) -> None:
    """Atomically write ``data/webpass-request.json`` (hash+nonce+ts; NO plaintext) at 0600."""
    path = Path(_DIAG_DATA_DIR) / _WEBPASS_REQUEST
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(record).encode())
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))  # atomic; preserves the 0600 mode of tmp


def _read_webpass_result(nonce: str) -> dict[str, Any] | None:
    """Return the result record iff ``data/webpass-result.json`` exists and its nonce matches; else
    None. A torn/partial read (JSONDecodeError) returns None so the caller keeps polling."""
    path = Path(_DIAG_DATA_DIR) / _WEBPASS_RESULT
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if isinstance(data, dict) and data.get("nonce") == nonce:
        return data
    return None


def _write_webpass_marker() -> None:
    """Write the app-owned ``data/.webpass_changed`` marker (existence => not default)."""
    path = Path(_DIAG_DATA_DIR) / _WEBPASS_MARKER
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)


def _delete_webpass_marker() -> None:
    """Undo an OPTIMISTIC marker write when a rotation is later observed to fail."""
    (Path(_DIAG_DATA_DIR) / _WEBPASS_MARKER).unlink(missing_ok=True)


def _request_basic_password(request: Request) -> str | None:
    """The password half of the request's HTTP Basic ``Authorization`` header, or None.

    Caddy has ALREADY bcrypt-verified this header against the live password before proxying, so it
    is the authoritative "current password". We cannot re-verify bcrypt in-process (no bcrypt lib;
    3.13 dropped ``crypt``), so requiring the user to re-type the current password and comparing it
    (constant-time) to this value proves they know it — without any new dependency. Basic userids
    cannot contain ':' so the password is everything after the first ':'."""
    header = request.headers.get("authorization", "")
    if header[:6].lower() != "basic ":
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None
    if ":" not in decoded:
        return None
    return decoded.split(":", 1)[1]


# --- control request model --------------------------------------------------------


class ControlRequest(BaseModel):
    """Body of ``POST /api/control``. ``action`` is required; the numeric fields apply
    only to ``action == "update"`` and are each optional, while ``channel_id``/``enabled``
    apply only to ``action == "channel"`` and ``input_id``/``enabled`` only to
    ``action == "input"``. The groups are kept in one model because
    :meth:`state_changes` reads *only* the keys in :data:`_UPDATE_RANGES`, so a channel
    toggle can never leak into a vessel-state update."""

    model_config = ConfigDict(extra="forbid")

    action: str
    channel_id: str | None = None
    # Input-slot mute (action == "input"): the RX mirror of channel_id. Like channel_id it is
    # absent from _UPDATE_RANGES, so it can never leak into a vessel-state update either.
    input_id: str | None = None
    enabled: bool | None = None
    # Route-playback control (action == "route"): op in {start, pause, reset}. Fault injection
    # (action == "fault"): ``fault`` names the allow-listed fault; ``value`` is the magnitude for
    # the value-bearing faults (hdop_spike/drop_sats). None of these three are in _UPDATE_RANGES, so
    # ``state_changes`` never picks them up — a route/fault control can't leak into a state update.
    op: str | None = None
    fault: str | None = None
    value: float | None = None
    lat: float | None = None
    lon: float | None = None
    sog_kn: float | None = None
    cog_deg: float | None = None
    heading_true_deg: float | None = None
    heading_mag_deg: float | None = None
    mag_variation_deg: float | None = None
    altitude_m: float | None = None
    fix_quality: float | None = None
    satellites: float | None = None
    hdop: float | None = None
    # Manual instrument/nav fields (mirroring the new ``_UPDATE_RANGES`` keys so ``state_changes``
    # picks them up). ``sea_state`` is edited as a float and applied as-is — the engine clamps and
    # rounds it to the integer WMO scale — kept ``float | None`` for uniformity with the rest.
    stw_kn: float | None = None
    depth_m: float | None = None
    rot_dpm: float | None = None
    wind_speed_kn: float | None = None
    wind_dir_deg: float | None = None
    sea_state: float | None = None
    rudder_angle_deg: float | None = None
    set_deg: float | None = None
    drift_kn: float | None = None
    # A4 display-override control (action == "display_override"). Neither is a key of
    # ``_UPDATE_RANGES``, so ``state_changes`` can never pick them up — a cosmetic override can't
    # leak into a wire-backed vessel-state update. ``overrides`` is a subset of
    # ``_DISPLAY_OVERRIDE_KEYS`` (validated in the handler); ``clear`` truthy reverts ALL to auto.
    overrides: dict[str, float] | None = None
    clear: bool | None = None

    def state_changes(self) -> dict[str, float]:
        """Return only the supplied numeric state fields (drop ``action`` and ``None``s)."""
        changes: dict[str, float] = {}
        for field in _UPDATE_RANGES:
            value = getattr(self, field)
            if value is not None:
                changes[field] = float(value)
        return changes


# --- persist (Save-as-defaults) request models ------------------------------------


class EmitDefault(BaseModel):
    """One per-sentence enable/rate override in a persist request (F4, R47). Names an EXISTING emit
    sentence on the channel and flips its ``enabled`` and/or ``rate_hz``; ``sentence`` is required,
    the two knobs optional so a caller can toggle one without disturbing the other. Extras forbidden
    so no other EmitSpec field can be smuggled in. The merged config is deep-validated before save,
    so a re-enable/re-rate that blows the channel's baud budget is rejected, never written."""

    model_config = ConfigDict(extra="forbid")

    sentence: str
    enabled: bool | None = None
    rate_hz: float | None = None


class ChannelDefault(BaseModel):
    """One per-channel enable default in a persist request. Extras are forbidden so a request can
    never smuggle an unrelated channel field (path, baud, framing, ...) through this narrow seam.
    ``emit`` optionally carries per-sentence enable/rate overrides (F4) applied to this channel."""

    model_config = ConfigDict(extra="forbid")

    id: str
    enabled: bool
    emit: list[EmitDefault] | None = None


class RouteDefault(BaseModel):
    """The route-playback block in a persist request (F1). ``waypoints`` are ``(lat, lon)`` pairs;
    the fixed-length tuple type makes a malformed waypoint a 422. Extras forbidden. The merged
    config is deep-validated (validate_or_raise) before save, which enforces the R53/R54
    preconditions (simulate mode, movement underway, >= 2 waypoints, not both route and replay)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    waypoints: list[tuple[float, float]] = Field(default_factory=list)
    speed_kn: float = 0.0
    loop: bool = False


class ReplayDefault(BaseModel):
    """The record-and-replay block in a persist request (F2). ``file`` names a capture to re-inject.
    Extras forbidden. The merged config is deep-validated before save, which enforces that
    ``mode == 'replay'`` has an enabled block whose file exists (R54)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    file: str = ""
    loop: bool = False
    speed: float = 1.0
    # Replay scope: "full" (default) replays own-ship + AIS from the capture; "ais-only" replays
    # only the AIS contacts while own-ship is simulated. Enum-validated by ``validate`` before save.
    scope: str = "full"


class AisTrafficDefault(BaseModel):
    """The synthetic-traffic block in a persist request (Scope B). Opts the ``role=='ais'`` channel
    into synthetic contacts: ``enabled`` flips the seam on/off, ``profile_path`` is a BASENAME ONLY
    (a value chosen from ``GET /api/profiles``) resolved against the shipped ``profiles/`` and
    writable ``data/profiles/`` search dirs, and ``target_count`` optionally overrides the profile's
    own count. ``seed`` and ``max_advance_s`` are deliberately NOT reachable here — the two knobs an
    operator never sets from the web stay out of the allow-list. Extras forbidden so no other
    ``AisTrafficSpec`` field can be smuggled in; the merged config is deep-validated (a set profile
    must exist and load) before save, so a bad/missing profile is a 400, never a silent write."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    profile_path: str | None = None
    target_count: int | None = None


class InputDefault(BaseModel):
    """One per-slot input assignment in a persist request.

    Two fields are reachable, and no others (extras forbidden, R15/R19):

    * ``function`` — what the operator declares is wired to this slot.
    * ``handle`` — WHICH physical adapter the slot binds to, as the opaque handle from
      ``GET /api/ports``. The client never sends, and never sees, a device path: the handler maps
      handle -> by-id path against the LIVE enumeration and rejects anything unknown. Accepting a
      path here would be an arbitrary-device-open primitive (bounded by the unit's char-major
      ``DeviceAllow=`` to serial ttys, but still wider than any UI needs).

    ``None`` means "leave the current binding alone", so a function-only save is unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    function: str
    handle: str | None = None


class DepthSimDefault(BaseModel):
    """The depth-sim toggle in a persist request (A6). Exposes ONLY ``enabled`` — the tuned
    sinusoid params (base/drift/shoal/ripple amplitudes and periods, floor) are deliberately NOT
    reachable from the web, so a UI save can flip the block on/off without ever clobbering a
    hand-tuned bathymetry. Extras forbidden. The merged config is deep-validated
    (``_validate_depth_sim`` via ``validate_or_raise``) before save, so a saved block with a bad
    period/amplitude is a 400."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class RudderSimDefault(BaseModel):
    """The rudder-hold-sim toggle in a persist request (mirrors :class:`DepthSimDefault`). Exposes
    ONLY ``enabled`` — the tuned oscillation params (amp_deg, period_s) are deliberately NOT
    reachable from the web, so a UI save flips the block on/off without ever clobbering a hand-tuned
    helm. Extras forbidden. Deep-validated (``_validate_rudder_sim`` via ``validate_or_raise``)
    before save. Default-ON in simulate mode lives in ``effective_rudder_sim``, not this seam."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class HeadingSimDefault(BaseModel):
    """The heading-hold-sim toggle in a persist request (mirrors :class:`DepthSimDefault`). Exposes
    ONLY ``enabled`` — the tuned wander params (amp_deg, period_s) are NOT reachable from the web.
    Extras forbidden. Deep-validated (``_validate_heading_sim`` via ``validate_or_raise``) before
    save. Default-ON in simulate mode lives in ``effective_heading_sim``, not this persist seam."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class WindSimDefault(BaseModel):
    """The wind-sim toggle in a persist request (mirrors :class:`DepthSimDefault`). Exposes ONLY
    ``enabled`` — the tuned gust/veer params and base wind are NOT reachable from the web (the base
    is seeded from the initial wind state). Extras forbidden. Deep-validated (``_validate_wind_sim``
    via ``validate_or_raise``) before save. Default-ON in simulate mode lives in
    ``effective_wind_sim``, not this persist seam."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class InitialStateRequest(BaseModel):
    """Body of ``POST /api/config/initial-state`` — the Save-as-defaults persist allow-list (R15).

    This is a DEDICATED model, not :class:`ControlRequest`: it exposes ONLY the operator-editable
    manual own-ship fields, an optional operating ``mode`` (``simulate``/``auto``/``replay``),
    per-channel enable defaults (each optionally carrying per-sentence emit overrides, F4), per-slot
    input-function assignments, and the opt-in ``route`` (F1) / ``replay`` (F2) / ``ais_traffic``
    (Scope B synthetic-traffic) blocks. Every field
    an attacker might use to redirect I/O or leak paths — ``path``, ``tcp_tap*``, ``baud``,
    ``writer_backend``, ``direction``, ``framing``, ``voltage_sense``, ``rx_feeds_state`` — is
    simply absent, and ``extra="forbid"`` turns any unknown key into a 422 rather than a silent
    write. ``mode`` is validated in the handler (not a pydantic ``Literal``) so a bad value is a 400
    with a clear message; ``"replay"`` is now accepted, but the merged config is deep-validated
    before save, so a bare ``mode: "replay"`` with no valid replay block is still a 400.
    ``voltage_sense`` deliberately stays OUT of the allow-list.
    """

    model_config = ConfigDict(extra="forbid")

    stw_kn: float | None = None
    depth_m: float | None = None
    rot_dpm: float | None = None
    wind_speed_kn: float | None = None
    wind_dir_deg: float | None = None
    sea_state: float | None = None
    rudder_angle_deg: float | None = None
    set_deg: float | None = None
    drift_kn: float | None = None
    # A3: the 11 nav/GNSS position fields. ``fix_quality``/``satellites`` are ``int | None`` — they
    # are ``int`` on ``VesselState`` and a ``float`` model would silently truncate on round-trip;
    # handler range-check against ``_UPDATE_RANGES`` (both ``(0.0, None)``) is int-vs-float safe.
    lat: float | None = None
    lon: float | None = None
    sog_kn: float | None = None
    cog_deg: float | None = None
    heading_true_deg: float | None = None
    heading_mag_deg: float | None = None
    mag_variation_deg: float | None = None
    altitude_m: float | None = None
    fix_quality: int | None = None
    satellites: int | None = None
    hdop: float | None = None
    mode: str | None = None
    # A5 time-source defaults (apply on the next Start; they drive ZDA/RMC/GGA/PASHR timestamps, not
    # a display field). ``time_source_mode`` is validated in the handler (system_utc|simulated|hold)
    # deep validation (epoch required when simulated, rate > 0) is done by the config round-trip.
    time_source_mode: str | None = None
    time_source_epoch: str | None = None
    time_source_rate: float | None = None
    # A4: persist a subset of the 7 cosmetic display-override keys as defaults. Keys are validated
    # against ``_DISPLAY_OVERRIDE_KEYS`` in the handler; skip-on-None so a partial save is safe.
    display_overrides: dict[str, float | None] | None = None
    # A6: toggle the wire-backed depth-sim block. Only ``enabled`` is reachable; server defaults
    # fill the sinusoid params, and any prior tuned params are preserved on re-save (skip-on-None).
    depth_sim: DepthSimDefault | None = None
    # Steering-sim toggles (mirror ``depth_sim``): only ``enabled`` is reachable; server defaults
    # fill the oscillation params and prior tuned params are preserved on re-save (skip-on-None).
    rudder_sim: RudderSimDefault | None = None
    heading_sim: HeadingSimDefault | None = None
    wind_sim: WindSimDefault | None = None
    channels: list[ChannelDefault] | None = None
    inputs: list[InputDefault] | None = None
    route: RouteDefault | None = None
    replay: ReplayDefault | None = None
    ais_traffic: AisTrafficDefault | None = None


# --- diagnostics request models ---------------------------------------------------


class DecodeRequest(BaseModel):
    """Body of ``POST /api/diag/decode``. Read-only single-line inspector — no port is touched."""

    model_config = ConfigDict(extra="forbid")

    line: str


class BaudSweepRequest(BaseModel):
    """Body of ``POST /api/diag/baud-sweep``. ``confirm`` must echo the slot id (R17 friction)."""

    model_config = ConfigDict(extra="forbid")

    slot: str
    confirm: str


class SendRequest(BaseModel):
    """Body of ``POST /api/diag/send``. ``confirm`` must echo the slot id (R17 friction)."""

    model_config = ConfigDict(extra="forbid")

    slot: str
    line: str
    confirm: str


class LoopbackRequest(BaseModel):
    """Body of ``POST /api/diag/loopback``. ``confirm`` must echo the slot id (R17 friction)."""

    model_config = ConfigDict(extra="forbid")

    slot: str
    confirm: str


class CaptureRequest(BaseModel):
    """Body of ``POST /api/diag/capture``. ``action`` is ``start`` or ``stop``; the filename is
    always server-generated under ``data/`` (R18) — a caller can never supply a path."""

    model_config = ConfigDict(extra="forbid")

    slot: str
    action: str


class RotatePasswordRequest(BaseModel):
    """Body of ``POST /api/security/rotate-password``. Plaintext is used ONCE (hashed via caddy on
    stdin) then discarded; NEVER logged, persisted, or in argv (Global CLAUDE.md §9)."""

    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str


# --- broker: engine threads <-> event loop <-> SSE clients -------------------------


class SubscriberLimitError(RuntimeError):
    """Raised by :meth:`Broker.subscribe` when the concurrent-subscriber cap is reached."""


class Broker:
    """Fans engine-thread output out to any number of SSE subscribers.

    A single ``janus.Queue`` bridges the (synchronous, multi-threaded) engine side to the
    (async) event-loop side. Engine threads call :meth:`publish`; the :meth:`pump`
    coroutine drains the async end and copies each frame into every subscriber's bounded
    :class:`asyncio.Queue`, dropping the oldest frame on overflow.
    """

    def __init__(self) -> None:
        self._queue: janus.Queue[dict[str, Any]] | None = None
        # Subscribers hold PRE-RENDERED SSE text: :meth:`pump` serializes each frame ONCE and
        # fans the finished string out, so N viewers cost one ``json.dumps``, not N (EFF2).
        self._subscribers: set[asyncio.Queue[str]] = set()

    def bind(self, queue: janus.Queue[dict[str, Any]]) -> None:
        """Attach the janus queue (built inside the running loop during lifespan startup)."""
        self._queue = queue

    # -- producer side (called from engine worker threads) --------------------

    def publish(self, channel_id: str, line: str) -> None:
        """Thread-safe: enqueue one emitted NMEA line. Drops on a full bridge (never blocks).

        This exact signature is the engine's ``monitor(channel_id, line)`` seam.
        """
        self._emit({"event": "nmea", "data": {"channel": channel_id, "line": line}})

    def publish_input(self, input_id: str, line: str) -> None:
        """Thread-safe: enqueue one RECEIVED input line (incl. malformed) for the streams tab.
        Rides the SAME bus as ``nmea`` under a distinct event so the existing pump/subscribe path
        fans it out unchanged. This exact signature is the engine's
        ``input_monitor(input_id, line)`` seam. Drops on a full bridge (never blocks)."""
        self._emit({"event": "input_nmea", "data": {"input": input_id, "line": line}})

    def publish_health(self, health: dict[str, Any]) -> None:
        """Enqueue a health frame for fan-out. Drops on a full bridge."""
        self._emit({"event": "health", "data": health})

    def publish_state(self, state: dict[str, Any]) -> None:
        """Enqueue a vessel-state frame for fan-out (the smooth conning stream). Drops on a full
        bridge — a missed state frame is harmless, the next one carries the whole snapshot."""
        self._emit({"event": "state", "data": state})

    def _emit(self, frame: dict[str, Any]) -> None:
        queue = self._queue
        if queue is None:
            return
        with contextlib.suppress(janus.SyncQueueFull):
            queue.sync_q.put_nowait(frame)

    # -- consumer side (event loop) ------------------------------------------

    async def pump(self) -> None:
        """Drain the bridge, RENDER each frame once, and fan the text out (drop-oldest on full).

        The loop body is wrapped so a single bad frame degrades gracefully (count + skip via the
        log) instead of killing the task forever; the ``await get()`` stays outside so shutdown's
        ``CancelledError`` still tears the task down cleanly.
        """
        queue = self._queue
        if queue is None:
            return
        while True:
            frame = await queue.async_q.get()
            try:
                rendered = _render_frame(frame)
                for sub in list(self._subscribers):
                    _put_drop_oldest(sub, rendered)
            except Exception:
                logger.exception("SSE pump: dropping a frame after a fan-out error")

    def subscribe(self) -> asyncio.Queue[str]:
        """Register a new SSE client and return its bounded (pre-rendered) frame queue.

        Raises :class:`SubscriberLimitError` when :data:`_MAX_SUBSCRIBERS` is reached so the
        caller can reject the connection (503) rather than grow unbounded.
        """
        if len(self._subscribers) >= _MAX_SUBSCRIBERS:
            raise SubscriberLimitError(f"subscriber limit reached ({_MAX_SUBSCRIBERS})")
        sub: asyncio.Queue[str] = asyncio.Queue(maxsize=_SUBSCRIBER_MAXSIZE)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: asyncio.Queue[str]) -> None:
        """Deregister an SSE client (idempotent)."""
        self._subscribers.discard(sub)

    @property
    def subscriber_count(self) -> int:
        """Current number of registered SSE subscribers (read-only, for the posture summary)."""
        return len(self._subscribers)

    def close(self) -> None:
        """Close the underlying janus queue, if bound."""
        if self._queue is not None:
            self._queue.close()


def _render_frame(frame: dict[str, Any]) -> str:
    """Serialize one broker frame to its final SSE wire text ONCE (shared across subscribers)."""
    return f"event: {frame['event']}\ndata: {json.dumps(frame['data'])}\n\n"


def _put_drop_oldest(sub: asyncio.Queue[str], frame: str) -> None:
    """Enqueue ``frame`` into ``sub``; if full, drop the oldest frame to make room."""
    try:
        sub.put_nowait(frame)
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            sub.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            sub.put_nowait(frame)


# --- engine manager ---------------------------------------------------------------


class EngineManager:
    """Owns the config, the (optional) running Engine, and the Broker.

    Every :meth:`start` builds a **fresh** Engine because engine threads are one-shot and
    cannot be restarted after :meth:`Engine.stop`.
    """

    def __init__(self, config: EngineConfig, broker: Broker) -> None:
        self._config = config
        self._broker = broker
        self._engine: Engine | None = None
        # Monotonic instant of the last successful start (None while stopped), for uptime_s.
        self._started_at: float | None = None
        # A4: live display-override values (subset of _DISPLAY_OVERRIDE_KEYS -> finite float),
        # from the config block so a persisted override is live from boot. Applied ONLY at the SSE
        # assembly site (``sim.update(...)``) so ``simulate_display_instruments`` stays pure. The
        # engine never reads this — cosmetics ride SSE only and emit no NMEA.
        self._display_overrides: dict[str, float] = (
            config.display_overrides.to_dict() if config.display_overrides is not None else {}
        )

    @property
    def config(self) -> EngineConfig:
        return self._config

    def set_config(self, config: EngineConfig) -> None:
        """Replace the config the NEXT :meth:`start` builds its Engine from.

        Does not touch a running engine (structural changes take effect on the next (re)start,
        consistent with how ``writer_backend`` and other structural fields are treated). Called
        under ``control_lock`` after a successful Save-as-defaults so a subsequent in-app Start
        picks up the saved values, and so the next save merges onto the just-saved config rather
        than the stale boot config (H4)."""
        self._config = config

    @property
    def running(self) -> bool:
        return self._engine is not None

    def start(self) -> None:
        """Start the engine if stopped; no-op if already running."""
        if self._engine is not None:
            return
        engine = Engine(
            self._config,
            monitor=self._broker.publish,
            input_monitor=self._broker.publish_input,
        )
        engine.start()
        self._engine = engine
        self._started_at = time.monotonic()

    def stop(self) -> None:
        """Stop and drop the engine if running; idempotent."""
        engine = self._engine
        if engine is None:
            return
        self._engine = None
        self._started_at = None
        engine.stop()

    def uptime_s(self) -> float:
        """Seconds since the running engine last started, or ``0.0`` when stopped."""
        return time.monotonic() - self._started_at if self._started_at is not None else 0.0

    def snapshot(self) -> VesselState | None:
        # Capture the engine into a local ONCE: a concurrent stop() (from a to_thread worker)
        # nulls self._engine, so re-reading it mid-method would race to AttributeError (H10).
        engine = self._engine
        return engine.snapshot() if engine is not None else None

    def update_state(self, **changes: float) -> VesselState:
        if self._engine is None:
            raise RuntimeError("engine is not running")
        return self._engine.update_state(**changes)

    # -- A4 display overrides (cosmetic SSE-only, applied at the state-broadcast assembly site) --
    def get_display_overrides(self) -> dict[str, float]:
        """A copy of the current display-override values (subset of the 7 cosmetic keys)."""
        return dict(self._display_overrides)

    def set_display_overrides(self, ov: dict[str, float]) -> None:
        """Merge ``ov`` into the live overrides (keys pre-validated vs ``_DISPLAY_OVERRIDE_KEYS``
        by the caller). Merge, not replace — an unset key keeps its prior override / auto value."""
        self._display_overrides.update(ov)

    def clear_display_overrides(self) -> None:
        """Drop every display override so all 7 cosmetic keys revert to their auto (simulated)
        values on the next SSE frame."""
        self._display_overrides.clear()

    def set_channel_enabled(self, channel_id: str, enabled: bool) -> bool:
        """Toggle one output channel on/off; ``False`` when the id is unknown.

        A flag write only — the worker thread and its drift-free schedule are untouched, so
        this needs no engine restart and cannot block the caller.
        """
        if self._engine is None:
            raise RuntimeError("engine is not running")
        return self._engine.set_channel_enabled(channel_id, enabled)

    def set_input_enabled(self, input_id: str, enabled: bool) -> bool:
        """Toggle one input slot's RX feed on/off; ``False`` when the id is unknown.

        A flag write only — the reader thread and its serial port are untouched, so this needs no
        engine restart and cannot fail the way a close/reopen cycle can.
        """
        if self._engine is None:
            raise RuntimeError("engine is not running")
        return self._engine.set_input_enabled(input_id, enabled)

    def route_control(self, op: str) -> bool:
        """Runtime route-playback control (``start``/``pause``/``reset``) — a cheap cursor-flag
        write like the channel toggle, no restart. ``False`` when no route is active (not simulate
        route mode) or the op is unknown, so the web layer can 4xx a no-op cleanly."""
        if self._engine is None:
            raise RuntimeError("engine is not running")
        return self._engine.route_control(op)

    def route_status(self) -> dict[str, Any] | None:
        """Current route-playback progress (active waypoint / count / fraction / flags), or ``None``
        when no route is active or the engine is stopped — the read side of the F1 progress."""
        # Capture-once for the same reason as snapshot(): a concurrent stop() must not race
        # this into an AttributeError on a re-read of self._engine (H10).
        engine = self._engine
        return engine.route_status() if engine is not None else None

    def gps_channel_id(self) -> str | None:
        """Config id of the channel whose role is ``gps`` (or ``None``) — the target the
        ``gps_kill``/``gps_restore`` faults toggle. Resolved by ROLE, not a hard-coded id."""
        return next((c.id for c in self._config.channels if c.role == "gps"), None)

    def input_status(self) -> list[dict[str, object]]:
        """Per-slot detection view. When running, delegates to :meth:`Engine.input_status` (which
        never exposes the device path — R19); when stopped, reports every configured slot idle
        (``detected_class=None``, ``live=False``) so the UI can still render the slot list.

        The stopped branch must carry ``enabled`` too, sourced from the config default: the UI seeds
        each input toggle from this payload at boot, so omitting it here would paint every slot ON
        regardless of what the config says."""
        if self._engine is not None:
            return self._engine.input_status()
        return [
            {
                "id": inp.id,
                "function": inp.function,
                "detected_class": None,
                "live": False,
                "enabled": inp.enabled,
            }
            for inp in self._config.inputs
        ]

    # -- diagnostics (read-only + gated active actions) --------------------
    def _input_spec(self, slot: str) -> InputSpec | None:
        """The :class:`InputSpec` for ``slot`` (or None). Its ``path`` stays internal — R19."""
        return next((inp for inp in self._config.inputs if inp.id == slot), None)

    def diagnostics_snapshot(self) -> list[dict[str, Any]]:
        """Per-input PortDiagnostics snapshots (empty when stopped or in simulate mode)."""
        return self._engine.diagnostics_snapshot() if self._engine is not None else []

    def is_operational_port(self, slot: str) -> bool:
        """R17 gate half: whether ``slot`` is an operational port an active action must refuse."""
        return port_is_operational(self._config, slot)

    def targetable_slots(self) -> set[str]:
        """R17 gate half: input-slot ids eligible as an active-diagnostics target."""
        return targetable_slots(self._config)

    def baud_sweep(self, slot: str) -> dict[str, Any]:
        """Score a baud sweep on a bench slot. No hardware retune/capture is wired on this box, so
        the scorer runs over an empty sample set and returns ``winner=None`` (R29: no printable
        structure at any rate implicates polarity/wiring, not baud). Real per-baud capture IO is
        the hardware extension point."""
        if self._input_spec(slot) is None:
            raise KeyError(slot)
        return score_baud({})

    def send_test(self, slot: str, line: str) -> dict[str, Any]:
        """Best-effort single-line TX on a bench slot (bounded, tolerant). Blocking — offload it."""
        spec = self._input_spec(slot)
        if spec is None:
            raise KeyError(slot)
        return _tx_probe(spec.path, spec.baud, spec.framing, line)

    def loopback_test(self, slot: str) -> dict[str, Any]:
        """TX a canned self-test sentence on a bench slot (bounded, tolerant). Blocking; offload."""
        spec = self._input_spec(slot)
        if spec is None:
            raise KeyError(slot)
        line = _loopback_sentence()
        result = _tx_probe(spec.path, spec.baud, spec.framing, line)
        return {"probe": "loopback", "sent_line": line, **result}

    def start_capture(self, slot: str) -> dict[str, Any]:
        """Arm a bounded raw capture (server-named file under ``data/``). Requires a running engine
        and a slot the engine reads; raises ``RuntimeError``/``KeyError`` otherwise."""
        if self._engine is None:
            raise RuntimeError("engine is not running")
        return self._engine.start_capture(
            slot,
            data_dir=_DIAG_DATA_DIR,
            max_bytes=_CAPTURE_MAX_BYTES,
            max_seconds=_CAPTURE_MAX_SECONDS,
        )

    def stop_capture(self, slot: str) -> dict[str, Any] | None:
        """Stop + deregister the capture on ``slot``; None if none was active."""
        if self._engine is None:
            raise RuntimeError("engine is not running")
        return self._engine.stop_capture(slot)

    def capture_status(self) -> list[dict[str, Any]]:
        """Status of every registered capture (empty when stopped)."""
        return self._engine.capture_status() if self._engine is not None else []

    def health(self) -> dict[str, Any]:
        """Serialized health dict; ``{"status": "stopped", ...}`` when not running.

        When running the dict carries the engine's resolved ``time_source`` tag and the running
        ``mode``; when stopped only ``mode`` (from config) is reported and ``time_source`` is
        omitted — there is no live clock to name.
        """
        # Capture the engine ONCE. The 1 Hz health broadcast loop calls this while a control
        # request's stop() may null self._engine from a to_thread worker; re-reading it three
        # times (the old None-check + two field reads) raced to AttributeError and killed the
        # broadcast loop forever while the app kept serving 200s (H10).
        engine = self._engine
        if engine is None:
            return {
                "status": "stopped",
                "ok": False,
                "physics_alive": False,
                "channels": [],
                "mode": self._config.mode,
            }
        return _health_to_dict(
            engine.health(),
            mode=self._config.mode,
            time_source=engine.time_source(),
        )


# --- optional in-app HTTP Basic (defense in depth, default OFF) --------------------


def _make_auth_dependency() -> Callable[..., Awaitable[None]]:
    """Return an auth dependency.

    This is an OPTIONAL defense-in-depth layer, off by default. Caddy is the primary auth
    layer. These env vars are deliberately distinct from Caddy's ``MOCKINGBUOY_BASIC_*``
    (which carry a *bcrypt* hash from ``caddy hash-password``): if the app reused those, the
    service would load Caddy's credential and try to verify a bcrypt hash with the argon2
    scheme below, rejecting every already-Caddy-authenticated request. To turn this layer on,
    set both ``MOCKINGBUOY_APP_BASIC_USER`` and ``MOCKINGBUOY_APP_BASIC_HASH`` — a passlib
    **argon2** hash (``from passlib.hash import argon2; argon2.hash("...")``). No credential
    is ever embedded here.
    """
    user = os.environ.get("MOCKINGBUOY_APP_BASIC_USER")
    password_hash = os.environ.get("MOCKINGBUOY_APP_BASIC_HASH")

    # Fail closed on a half-configured auth: setting exactly one env var must NOT silently
    # serve every endpoint unauthenticated while the operator believes Basic is enabled.
    if bool(user) != bool(password_hash):
        raise RuntimeError(
            "in-app Basic auth is half-configured: set BOTH MOCKINGBUOY_APP_BASIC_USER and "
            "MOCKINGBUOY_APP_BASIC_HASH, or neither"
        )

    if not user or not password_hash:

        async def _noop() -> None:
            return None

        return _noop

    from passlib.context import CryptContext

    security = HTTPBasic()
    pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

    # `security` is captured as a default-argument value (evaluated eagerly at def time),
    # not inside the annotation itself — with `from __future__ import annotations` every
    # annotation is a lazily-evaluated string, and a closure-local name like `security`
    # is not visible to that later `eval()` (which only sees module globals). Putting the
    # `Depends(...)` call in the annotation (e.g. `Annotated[T, Depends(security)]`) would
    # silently fail to resolve as a security dependency and mis-parse the request instead.
    async def _verify(credentials: HTTPBasicCredentials = Depends(security)) -> None:  # noqa: B008
        user_ok = secrets.compare_digest(credentials.username, user)
        pass_ok = pwd_context.verify(credentials.password, password_hash)
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=401,
                detail="unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )

    return _verify


# --- app factory ------------------------------------------------------------------


def create_app(config_path: str | None = None) -> FastAPI:
    """Build the FastAPI app.

    Load order (R49 first-boot fallback): the explicit ``config_path`` arg, else the
    ``MOCKINGBUOY_CONFIG`` env var, else the operator-written ``data/config.local.json`` if it
    already exists, else the tracked ``config.json`` baseline. So a fresh install with no local file
    boots from the read-only baseline, and once a Save-as-defaults write has landed the local file
    is preferred. Because :meth:`EngineConfig.save` is atomic (temp file + ``os.replace``), a
    concurrent read here can only ever see the whole-old or whole-new file, never a torn one.

    The persist target is resolved separately (it must never default to the read-only baseline):
    the ``config_path`` arg, else ``MOCKINGBUOY_CONFIG``, else ``data/config.local.json`` — never
    ``config.json``.
    """
    resolved_path = (
        config_path
        or os.environ.get("MOCKINGBUOY_CONFIG")
        or ("data/config.local.json" if Path("data/config.local.json").exists() else "config.json")
    )
    config = EngineConfig.load(resolved_path)
    # H5: deep-validate on the production entrypoint too. ``main.py`` validates before building
    # the engine, but the web path (``uvicorn web.app:app``) went straight to ``Engine()``, which
    # runs only the baud-budget check — a hand-edited local config could skip every deep rule
    # (duplicate paths, tap collisions, auto-mode preconditions, replay-file existence, NaN /
    # out-of-range initial state). Raise loudly here so systemd surfaces the validator's message
    # instead of booting a subtly-broken engine.
    config.validate_or_raise()

    #: Where Save-as-defaults writes. Deliberately does NOT fall back to ``config.json``: that file
    #: is the tracked baseline and, under a strict sandbox, its directory is read-only.
    persist_path = config_path or os.environ.get("MOCKINGBUOY_CONFIG") or "data/config.local.json"

    broker = Broker()
    manager = EngineManager(config, broker)
    auth = _make_auth_dependency()
    # Serializes engine start/stop so concurrent control requests can't double-open ports;
    # the transitions themselves run off the event loop (they join threads / open serial).
    control_lock = asyncio.Lock()
    # Serializes Save-as-defaults writes so two concurrent persists can't interleave their
    # validate -> save; the atomic writer handles crash safety, this handles request concurrency.
    persist_lock = asyncio.Lock()
    # Active-diagnostics single-flight + per-slot cooldown state. ``diag_lock`` guards both maps so
    # the check-and-reserve is atomic across concurrent requests; ``diag_inflight`` holds slots with
    # an action running now (reject a second with 429), ``diag_last_action`` the monotonic finish
    # time per slot (reject a follow-up inside the cooldown with 429).
    diag_lock = asyncio.Lock()
    diag_inflight: set[str] = set()
    diag_last_action: dict[str, float] = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Build the janus queue inside the running loop, then wire the broker.
        queue: janus.Queue[dict[str, Any]] = janus.Queue(maxsize=_BRIDGE_MAXSIZE)
        broker.bind(queue)

        # Auto-start the engine so the UI shows live data immediately. Run the (blocking)
        # start off the loop so opening serial ports / TCP taps can't stall startup.
        async with control_lock:
            await asyncio.to_thread(manager.start)

        pump_task = asyncio.create_task(broker.pump(), name="sse-pump")
        health_task = asyncio.create_task(
            _health_broadcast_loop(manager, broker), name="sse-health"
        )
        state_task = asyncio.create_task(_state_broadcast_loop(manager, broker), name="sse-state")
        # Fail loud if any SSE task ends unexpectedly (i.e. not via shutdown cancellation): a
        # bare ``while True`` that dies would otherwise leave the app serving 200s with a dead
        # stream and no signal (H10).
        for task in (pump_task, health_task, state_task):
            task.add_done_callback(_log_task_done)
        try:
            yield
        finally:
            for task in (pump_task, health_task, state_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # Engine.stop() joins worker threads (up to its timeout); keep it off the loop
            # so a stalled sink can't freeze pending SSE flushes during shutdown.
            async with control_lock:
                await asyncio.to_thread(manager.stop)  # joins threads -> no leaked threads
            broker.close()

    app = FastAPI(title="mockingbuoy", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index(_: None = Depends(auth)) -> HTMLResponse:
        # no-store on the HTML document so a redeploy always lands: the browser must re-fetch
        # index.html (and thus its ?v-busted css/js) instead of serving a stale cached copy.
        return HTMLResponse(
            _INDEX_HTML.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    # C2: the UI's CSS + JS are served as their own same-origin ``'self'`` files (not inlined),
    # so a strict CSP with no ``script-src 'unsafe-inline'`` still loads them behind the proxy.
    # Served through explicit routes (not a StaticFiles mount) so the optional in-app Basic layer
    # gates them exactly like ``/`` and no directory listing is ever exposed.
    @app.get("/static/app.css")
    async def static_app_css(_: None = Depends(auth)) -> Response:
        return Response(_APP_CSS.read_text(encoding="utf-8"), media_type="text/css")

    @app.get("/static/app.js")
    async def static_app_js(_: None = Depends(auth)) -> Response:
        return Response(_APP_JS.read_text(encoding="utf-8"), media_type="application/javascript")

    # Throwaway proof-of-concept page for the conning redesign; same auth gate as the rest.
    @app.get("/static/conning-poc.html", response_class=HTMLResponse)
    async def static_conning_poc(_: None = Depends(auth)) -> HTMLResponse:
        return HTMLResponse(_CONNING_POC.read_text(encoding="utf-8"))

    @app.get("/healthz")
    async def healthz(_: None = Depends(auth)) -> Response:
        health = manager.health()
        ok = bool(health.get("ok")) and health.get("status") == "running"
        status_code = 200 if ok else 503
        return JSONResponse(health, status_code=status_code)

    @app.get("/api/config")
    async def api_config(_: None = Depends(auth)) -> dict[str, Any]:
        # M9: device paths (channel + input ``path``) are stripped so this endpoint can't leak the
        # adapter brand/serial + fs paths that /api/inputs and /api/profiles withhold.
        return _redacted_config_dict(manager.config)

    @app.get("/api/state")
    async def api_state(_: None = Depends(auth)) -> dict[str, Any]:
        state = manager.snapshot()
        if state is None:
            return {"running": False}
        out = _state_to_dict(state)
        # F1: attach route-playback progress when a route is active. Additive — absent (no key) for
        # every config with no route driver, so a non-route config's state dict is unchanged.
        route = manager.route_status()
        if route is not None:
            out["route"] = route
        # A3b: advisory list of fields the engine overwrites each tick (route/auto-RX/depth-sim) so
        # the editor can grey them out. Always a list (possibly empty) — absent-safe for the UI.
        out["driven_fields"] = _driven_fields(manager)
        return out

    @app.post("/api/control")
    async def api_control(body: ControlRequest, _: None = Depends(auth)) -> dict[str, Any]:
        action = body.action.strip().lower()

        if action == "start":
            async with control_lock:
                await asyncio.to_thread(manager.start)
            return {"running": True}

        if action == "stop":
            async with control_lock:
                await asyncio.to_thread(manager.stop)
            return {"running": False}

        if action == "update":
            if not manager.running:
                raise HTTPException(status_code=409, detail="engine is not running")
            changes = body.state_changes()
            if not changes:
                raise HTTPException(
                    status_code=400, detail="update requires at least one state field"
                )
            for field, value in changes.items():
                low, high = _UPDATE_RANGES[field]
                if (low is not None and value < low) or (high is not None and value > high):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{field}={value} out of range [{low}, {high}]",
                    )
            state = manager.update_state(**changes)
            return {"running": True, "state": _state_to_dict(state)}

        if action == "channel":
            if not manager.running:
                raise HTTPException(status_code=409, detail="engine is not running")
            if body.channel_id is None or body.enabled is None:
                raise HTTPException(
                    status_code=400, detail="channel action requires channel_id and enabled"
                )
            # Setting a flag on a live worker: no lock and no worker thread needed, unlike
            # start/stop (which join threads and open ports) — offloading would only add
            # latency to what is a single atomic write.
            if not manager.set_channel_enabled(body.channel_id, body.enabled):
                raise HTTPException(status_code=404, detail=f"unknown channel: {body.channel_id!r}")
            # The toggle reaches every client through the existing 1 Hz health broadcast.
            return {"running": True, "channel_id": body.channel_id, "enabled": body.enabled}

        if action == "input":
            # The RX mirror of "channel": mute one input SLOT so its lines stop feeding the router
            # (and the clock), letting an operator rehearse signal loss without unplugging a cable.
            # Same 409 -> 400 -> 404 ordering as the channel branch, and just as cheap — one flag
            # write, no port closed and no reader thread touched.
            if not manager.running:
                raise HTTPException(status_code=409, detail="engine is not running")
            if body.input_id is None or body.enabled is None:
                raise HTTPException(
                    status_code=400, detail="input action requires input_id and enabled"
                )
            if not manager.set_input_enabled(body.input_id, body.enabled):
                raise HTTPException(status_code=404, detail=f"unknown input: {body.input_id!r}")
            # The toggle reaches every client through the existing 1 Hz health broadcast.
            return {"running": True, "input_id": body.input_id, "enabled": body.enabled}

        if action == "route":
            # F1 runtime route control: pause/resume/reset the route cursor. A flag write, no
            # restart. The route driver exists only in simulate route mode, so a missing driver
            # (409) is the natural gate — no separate mode check needed.
            if not manager.running:
                raise HTTPException(status_code=409, detail="engine is not running")
            op = (body.op or "").strip().lower()
            if op not in ("start", "pause", "reset"):
                raise HTTPException(
                    status_code=400, detail=f"route op must be start|pause|reset, got {body.op!r}"
                )
            if not manager.route_control(op):
                raise HTTPException(status_code=409, detail="no active route to control")
            return {"running": True, "op": op, "route": manager.route_status()}

        if action == "fault":
            # F3 GPS-fault injection: a SIMULATE-ONLY testing tool. Each fault is an allow-listed,
            # range-checked write to an EXISTING state field (or the gps channel toggle) — never a
            # generic field poke. Refused outside simulate mode (fault injection is a bench tool).
            if not manager.running:
                raise HTTPException(status_code=409, detail="engine is not running")
            if manager.config.mode != "simulate":
                raise HTTPException(
                    status_code=409,
                    detail="fault injection is a simulate-mode testing tool",
                )
            fault = (body.fault or "").strip().lower()
            if fault in ("gps_kill", "gps_restore"):
                gps_id = manager.gps_channel_id()
                if gps_id is None:
                    raise HTTPException(status_code=409, detail="no gps channel to fault")
                enabled = fault == "gps_restore"
                manager.set_channel_enabled(gps_id, enabled)
                return {"running": True, "fault": fault, "channel_id": gps_id, "enabled": enabled}
            if fault == "no_fix":
                changes = {"fix_quality": 0.0}
            elif fault == "restore_fix":
                changes = {"fix_quality": 1.0}
            elif fault in _FAULT_BOUNDS:
                if body.value is None:
                    raise HTTPException(
                        status_code=400, detail=f"fault {fault!r} requires a numeric 'value'"
                    )
                low, high = _FAULT_BOUNDS[fault]
                if body.value < low or body.value > high:
                    raise HTTPException(
                        status_code=400,
                        detail=f"value={body.value} out of range [{low}, {high}]",
                    )
                field = "hdop" if fault == "hdop_spike" else "satellites"
                changes = {field: float(body.value)}
            else:
                raise HTTPException(status_code=400, detail=f"unknown fault: {body.fault!r}")
            state = manager.update_state(**changes)
            return {"running": True, "fault": fault, "state": _state_to_dict(state)}

        if action == "display_override":
            # A4: set/clear cosmetic display overrides (SSE-only; never wire-backed, emit no NMEA).
            # Requires a running engine (consistent with ``update``) since the overrides only
            # surface on the live state stream. ``overrides`` merges (validated keys/finite values);
            # ``clear`` reverts ALL to auto. Either may be used but at least one is required.
            if not manager.running:
                raise HTTPException(status_code=409, detail="engine is not running")
            if body.overrides is None and not body.clear:
                raise HTTPException(
                    status_code=400,
                    detail="display_override requires overrides or clear",
                )
            if body.overrides is not None:
                bad = sorted(k for k in body.overrides if k not in _DISPLAY_OVERRIDE_KEYS)
                if bad:
                    raise HTTPException(
                        status_code=400, detail=f"unknown display_override key(s): {bad}"
                    )
                for key, value in body.overrides.items():
                    if not math.isfinite(value):
                        raise HTTPException(
                            status_code=400,
                            detail=f"display_override {key} must be finite, got {value}",
                        )
                    lo_hi = _DISPLAY_OVERRIDE_BOUNDS.get(key)
                    if lo_hi is not None and not (lo_hi[0] <= value <= lo_hi[1]):
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"display_override {key}={value} out of range "
                                f"[{lo_hi[0]}, {lo_hi[1]}]"
                            ),
                        )
                manager.set_display_overrides(body.overrides)
            if body.clear:
                manager.clear_display_overrides()
            return {"running": True, "display_overrides": manager.get_display_overrides()}

        raise HTTPException(status_code=400, detail=f"unknown action: {body.action!r}")

    @app.get("/api/stream")
    async def api_stream(_: None = Depends(auth)) -> StreamingResponse:
        try:
            sub = broker.subscribe()
        except SubscriberLimitError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

        async def event_source() -> AsyncIterator[str]:
            # Disconnect handling is delegated to StreamingResponse: on client disconnect it
            # cancels this generator, so the ``finally`` runs and the subscriber is dropped.
            # We deliberately do NOT poll ``request.is_disconnected()`` here — under the
            # Starlette TestClient that consumes the same ASGI receive channel the response's
            # own disconnect listener uses and deadlocks the stream.
            try:
                while True:
                    # Frames arrive PRE-RENDERED from pump() (serialized once, EFF2) — yield as-is.
                    chunk = await sub.get()
                    yield chunk
            finally:
                broker.unsubscribe(sub)

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/config/initial-state")
    async def api_persist_initial_state(
        body: InitialStateRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        # Serialize the whole endpoint: two concurrent saves must not interleave their
        # validate -> save against the same file (the atomic writer only guarantees no torn file).
        async with persist_lock:
            # 1. Range-check every provided manual field with the SAME check the "update" action
            #    uses, so the persisted defaults can never be values a live update would reject.
            for field in _INITIAL_STATE_MANUAL_FIELDS:
                value = getattr(body, field)
                if value is None:
                    continue
                low, high = _UPDATE_RANGES[field]
                if (low is not None and value < low) or (high is not None and value > high):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{field}={value} out of range [{low}, {high}]",
                    )

            # 2. Merge onto a COPY of the running config's dict (never mutate the live config).
            merged = manager.config.to_dict()
            initial_state = dict(merged.get("initial_state", {}))
            for field in _INITIAL_STATE_MANUAL_FIELDS:
                value = getattr(body, field)
                if value is not None:
                    initial_state[field] = value
            merged["initial_state"] = initial_state

            if body.mode is not None:
                if body.mode not in ("simulate", "auto", "replay"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"mode must be simulate|auto|replay, got {body.mode!r}",
                    )
                merged["mode"] = body.mode

            # A5: time-source merge (skip-on-None; only touched when a field is supplied).
            # Deep validation (epoch required when simulated, rate > 0, ISO-parse) is done by the
            # config round-trip below; here we only reject a bad mode name loudly and mirror `mode`.
            if any(
                v is not None
                for v in (body.time_source_mode, body.time_source_epoch, body.time_source_rate)
            ):
                ts = dict(merged.get("time_source", {}))
                if body.time_source_mode is not None:
                    if body.time_source_mode not in ("system_utc", "simulated", "hold"):
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "time_source_mode must be system_utc|simulated|hold, got "
                                f"{body.time_source_mode!r}"
                            ),
                        )
                    ts["mode"] = body.time_source_mode
                if body.time_source_epoch is not None:
                    ts["epoch"] = body.time_source_epoch or None
                if body.time_source_rate is not None:
                    ts["rate"] = body.time_source_rate
                merged["time_source"] = ts

            # A4: display-overrides merge. Keys validated against the fixed cosmetic set; values'
            # finiteness is enforced by the config round-trip. Per-key merge so a partial save never
            # drops a prior override key, BUT a null value REMOVES that key (so a cleared override
            # can be persisted — otherwise a live clear would resurrect from config on the next
            # restart). When the block empties out entirely it is dropped so no stale block lingers.
            if body.display_overrides is not None:
                bad = sorted(k for k in body.display_overrides if k not in _DISPLAY_OVERRIDE_KEYS)
                if bad:
                    raise HTTPException(
                        status_code=400, detail=f"unknown display_override key(s): {bad}"
                    )
                do = dict(merged.get("display_overrides", {}))
                for key, value in body.display_overrides.items():
                    if value is None:
                        do.pop(key, None)
                    else:
                        lo_hi = _DISPLAY_OVERRIDE_BOUNDS.get(key)
                        if lo_hi is not None and not (lo_hi[0] <= value <= lo_hi[1]):
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    f"display_override {key}={value} out of range "
                                    f"[{lo_hi[0]}, {lo_hi[1]}]"
                                ),
                            )
                        do[key] = value
                if do:
                    merged["display_overrides"] = do
                else:
                    merged.pop("display_overrides", None)

            # A6: depth-sim toggle merge (skip-on-None). Only ``enabled`` is flipped; any existing
            # tuned sinusoid params (base/drift/shoal/ripple, floor) are preserved so a UI on/off
            # save never clobbers a hand-tuned bathymetry. Deep-validated by validate_or_raise.
            if body.depth_sim is not None:
                ds = dict(merged.get("depth_sim", {}))
                ds["enabled"] = body.depth_sim.enabled
                merged["depth_sim"] = ds

            # Steering-sim toggle merges (mirror depth_sim, skip-on-None). Only ``enabled`` is
            # flipped; any tuned amp/period is preserved so a UI on/off save never clobbers a
            # hand-tuned helm/wander. Deep-validated (_validate_rudder_sim/_validate_heading_sim).
            if body.rudder_sim is not None:
                rs = dict(merged.get("rudder_sim", {}))
                rs["enabled"] = body.rudder_sim.enabled
                merged["rudder_sim"] = rs
            if body.heading_sim is not None:
                hs = dict(merged.get("heading_sim", {}))
                hs["enabled"] = body.heading_sim.enabled
                merged["heading_sim"] = hs
            if body.wind_sim is not None:
                wnd = dict(merged.get("wind_sim", {}))
                wnd["enabled"] = body.wind_sim.enabled
                merged["wind_sim"] = wnd

            if body.channels is not None:
                by_id = {c["id"]: c for c in merged["channels"]}
                for ch in body.channels:
                    target = by_id.get(ch.id)
                    if target is None:
                        raise HTTPException(status_code=400, detail=f"unknown channel: {ch.id!r}")
                    target["enabled"] = ch.enabled
                    if ch.emit is not None:
                        # F4: flip enabled/rate on EXISTING emit sentences only. An unknown sentence
                        # is a 400 (never silently added); the over-budget re-enable/re-rate is
                        # caught by validate_or_raise below (the baud-budget guard), not here.
                        emit_by_sentence = {e["sentence"]: e for e in target.get("emit", [])}
                        for em in ch.emit:
                            emit_target = emit_by_sentence.get(em.sentence)
                            if emit_target is None:
                                raise HTTPException(
                                    status_code=400,
                                    detail=(
                                        f"unknown emit sentence {em.sentence!r} "
                                        f"on channel {ch.id!r}"
                                    ),
                                )
                            if em.enabled is not None:
                                emit_target["enabled"] = em.enabled
                            if em.rate_hz is not None:
                                emit_target["rate_hz"] = em.rate_hz

            if body.route is not None:
                # F1: merge the route block in RouteSpec.from_dict shape ([lat, lon] lists). The
                # R53/R54 preconditions (simulate + underway + >= 2 waypoints, not both route and
                # replay) are enforced by validate_or_raise below, not duplicated here.
                merged["route"] = {
                    "enabled": body.route.enabled,
                    "waypoints": [[lat, lon] for lat, lon in body.route.waypoints],
                    "speed_kn": body.route.speed_kn,
                    "loop": body.route.loop,
                }

            if body.replay is not None:
                # F2: merge the replay block. The "mode replay needs an enabled block whose file
                # exists" precondition (R54) is enforced by validate_or_raise below.
                merged["replay"] = {
                    "enabled": body.replay.enabled,
                    "file": body.replay.file,
                    "loop": body.replay.loop,
                    "speed": body.replay.speed,
                    "scope": body.replay.scope,
                }

            if body.ais_traffic is not None:
                # Scope B: merge the synthetic-traffic block onto the ais channel's ais.traffic.
                # Resolve the channel EXPLICITLY by role — 0 or >1 channels with role 'ais' is an
                # ambiguous config we refuse to guess at (clear 400), never a silent write to the
                # wrong channel. profile_path is a BASENAME resolved against the search dirs (or
                # null/'' for the neutral built-in default). The R-preconditions on an enabled
                # profile (must exist and load) are enforced by validate_or_raise below.
                ais_channels = [c for c in merged["channels"] if c.get("role") == "ais"]
                if len(ais_channels) != 1:
                    raise HTTPException(
                        status_code=400,
                        detail=f"expected exactly one channel with role 'ais', "
                        f"found {len(ais_channels)}",
                    )
                channel = ais_channels[0]
                ais_block = channel.get("ais")
                if ais_block is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"channel {channel.get('id')!r} has role 'ais' but no ais block",
                    )
                traffic = dict(ais_block.get("traffic") or {})
                traffic["enabled"] = body.ais_traffic.enabled
                name = body.ais_traffic.profile_path
                traffic["profile_path"] = _resolve_profile_basename(name) if name else None
                if body.ais_traffic.target_count is not None:
                    traffic["target_count"] = body.ais_traffic.target_count
                ais_block["traffic"] = traffic
                channel["ais"] = ais_block

            if body.inputs is not None:
                by_id = {i["id"]: i for i in merged["inputs"]}
                for slot in body.inputs:
                    target = by_id.get(slot.id)
                    if target is None:
                        raise HTTPException(status_code=400, detail=f"unknown input: {slot.id!r}")
                    target["function"] = slot.function
                    if slot.handle is not None:
                        # Map handle -> device path HERE, against the live adapter set. An unknown
                        # handle is a 400, never a silent no-op: a binding the operator thinks
                        # succeeded but did not would present later as a dead channel (ISSUE-020
                        # reports a failed open as "device absent"), which is exactly the confusion
                        # this feature exists to remove.
                        by_handle = dict(_enumerate_adapters())
                        resolved = by_handle.get(slot.handle)
                        if resolved is None:
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    f"input {slot.id!r}: unknown adapter handle — it may have been "
                                    f"unplugged; reload the adapter list and pick again"
                                ),
                            )
                        target["path"] = resolved

            # 3. Rebuild + deep-validate the merged config; any structural or cross-field problem
            #    (incl. a bad input function) becomes a 400 with the validator's own message.
            try:
                merged_config = EngineConfig.from_dict(merged)
                merged_config.validate_or_raise()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            # 4. Persist to the local override file (never the tracked baseline) via the ATOMIC
            #    writer, off the event loop (it fsyncs). save() mkdirs the parent if needed.
            await asyncio.to_thread(merged_config.save, persist_path)

            # 5. H4: swap the manager's config to the just-saved one under ``control_lock`` so the
            #    NEXT in-app Start builds its Engine from the saved values (not the stale boot
            #    config), and so a subsequent save merges onto THIS config rather than the boot
            #    config (which silently erased earlier saved blocks). Nesting control_lock inside
            #    persist_lock is safe: no path ever takes them in the opposite order. This does NOT
            #    hot-reload a running engine — structural changes still apply on the next (re)start.
            async with control_lock:
                manager.set_config(merged_config)
                # A4: mirror the LIVE override dict to exactly what was just persisted so a saved
                # cosmetic override (or a cleared one) takes effect on the next SSE frame without
                # waiting for a restart. Rebuild from the validated block (never the raw request, so
                # null-removals and finiteness are already resolved).
                if body.display_overrides is not None:
                    manager.clear_display_overrides()
                    if merged_config.display_overrides is not None:
                        manager.set_display_overrides(merged_config.display_overrides.to_dict())

        # 6. Echo the saved defaults. This does NOT hot-reload the running engine: mode / input /
        #    channel-default changes take effect on the next engine (re)start, consistent with how
        #    writer_backend and other structural fields are treated.
        return {
            "saved": True,
            "initial_state": merged_config.initial_state_raw,
            "mode": merged_config.mode,
            "hot_reloaded": False,
        }

    @app.get("/api/profiles")
    async def api_profiles(_: None = Depends(auth)) -> dict[str, Any]:
        # Read-only profile discovery for the Config tab's AIS-traffic dropdown. Returns *.json
        # basenames from the shipped profiles/ dir AND the writable data/profiles/ dir (missing dir
        # => empty, never an error), de-duplicated. Names ONLY — the search-dir prefix / absolute
        # path is never exposed (R19), consistent with how device paths are withheld elsewhere.
        return {"profiles": _list_profile_names()}

    @app.get("/api/inputs")
    async def api_inputs(_: None = Depends(auth)) -> list[dict[str, Any]]:
        # Read-only slot view. Info-leak hygiene (R19): only the slot id, declared function, and the
        # detection booleans are exposed — never the device path / by-id link / adapter serial.
        # Resolved kernel port name per slot (ttyUSB0), never the by-id link/adapter serial (R19).
        port_by_id = {i.id: _resolve_port(i.path) for i in manager.config.inputs}
        out: list[dict[str, Any]] = []
        for entry in manager.input_status():
            function = str(entry["function"])
            detected = entry["detected_class"]
            detected_class = str(detected) if detected is not None else None
            out.append(
                {
                    "id": entry["id"],
                    "function": function,
                    "detected_class": detected_class,
                    "live": bool(entry["live"]),
                    # Runtime RX mute flag. This projection is a fixed key set, so a new field
                    # added upstream is silently dropped unless it is named here too.
                    "enabled": bool(entry["enabled"]),
                    "mismatch": _input_mismatch(function, detected_class),
                    "port": port_by_id.get(str(entry["id"])),
                }
            )
        return out

    @app.get("/api/ports")
    async def api_ports(_: None = Depends(auth)) -> list[dict[str, Any]]:
        """Attached USB-serial adapters, for the Config port picker and the Maintenance table.

        R19: emits an OPAQUE handle plus the resolved kernel name (``ttyUSB0``) — never the
        ``/dev/serial/by-id/...`` link, which carries the adapter brand and per-unit serial. The
        handle is what the client sends back to bind a slot; the server does the handle -> path
        mapping. There is a regression test asserting no by-id string appears in this response.

        ``assigned_to`` lets the UI show which slot already owns an adapter, so an operator can see
        at a glance that a port is spoken for rather than double-binding it (which validate.py now
        catches via realpath, but seeing it beforehand is better than being told after).
        """
        adapters = _enumerate_adapters()
        # Which slot, if any, already points at each adapter — compared on the RESOLVED device so a
        # slot configured under a different alias for the same tty still matches.
        slot_by_device: dict[str, str] = {}
        for inp in manager.config.inputs:
            try:
                slot_by_device[os.path.realpath(inp.path)] = inp.id
            except OSError:  # pragma: no cover - realpath does not touch the FS for a plain string
                continue
        status_by_id = {str(e["id"]): e for e in manager.input_status()}

        out: list[dict[str, Any]] = []
        for handle, by_id_path in adapters:
            try:
                device = os.path.realpath(by_id_path)
            except OSError:  # pragma: no cover
                device = by_id_path
            slot = slot_by_device.get(device)
            status = status_by_id.get(slot or "", {})
            detected = status.get("detected_class")
            out.append(
                {
                    "handle": handle,
                    "port": _resolve_port(by_id_path),
                    "assigned_to": slot,
                    "detected_class": str(detected) if detected is not None else None,
                    "live": bool(status.get("live", False)),
                }
            )
        return out

    @app.get("/api/security")
    async def api_security(_: None = Depends(auth)) -> dict[str, Any]:
        # Non-secret posture only (R19). Each *_basic is a bare presence probe computed at the
        # narrowest scope; the *_HASH vars are NEVER read and no environment VALUE is ever returned.
        cfg = manager.config
        taps = [
            {"channel": ch.id, "port": ch.tcp_tap.port}
            for ch in cfg.channels
            if ch.tcp_tap is not None and ch.tcp_tap.enabled
        ]
        return {
            "tls": "internal",
            "caddy_basic": bool(os.environ.get("MOCKINGBUOY_BASIC_USER")),
            "app_basic": bool(os.environ.get("MOCKINGBUOY_APP_BASIC_USER")),
            # First-run posture: true = the auto-generated first-run web password has NOT been
            # changed in-app (marker absent). A bare existence probe — no secret, consistent with
            # R19. Drives the first-login "change your password" banner.
            "password_is_default": not (Path(_DIAG_DATA_DIR) / _WEBPASS_MARKER).exists(),
            # Report the actual bind so the posture is honest for the shipped unix-socket
            # bind. The systemd unit sets MOCKINGBUOY_APP_BIND to the socket path; default
            # stays "127.0.0.1" so dev / loopback-TCP output is unchanged.
            "app_bind": os.environ.get("MOCKINGBUOY_APP_BIND", "127.0.0.1"),
            "taps": taps,
            "tap_host": cfg.tcp_tap_host,
            "subscribers": broker.subscriber_count,
            "max_subscribers": _MAX_SUBSCRIBERS,
            "uptime_s": manager.uptime_s(),
            "running": manager.running,
            # The app sets no global security response headers of its own (TLS + security headers
            # are the reverse proxy's job); report the empty set honestly, not something misleading.
            "headers": [],
        }

    @app.post("/api/security/rotate-password")
    async def api_rotate_password(
        body: RotatePasswordRequest, request: Request, _: None = Depends(auth)
    ) -> dict[str, Any]:
        # In-app web-password change. The app is sandboxed (it cannot write ``secrets/service.env``
        # nor restart caddy); it hashes the new password IN-PROCESS via caddy (stdin, never argv),
        # drops a hash+nonce REQUEST in its one writable dir (``data/``), and a ROOT systemd
        # path-unit performs the privileged rewrite + caddy restart and writes back a RESULT here.
        #
        # §9: the plaintext lives ONLY in this handler's memory and on caddy's stdin, then is
        # discarded. It is NEVER logged, persisted, or in argv. Only a bcrypt HASH is written.
        new_password = body.new_password
        try:
            # 0. Re-authenticate: the caller must re-type the CURRENT password. Caddy already
            #    bcrypt-verified the Basic Authorization header, so comparing the submitted current
            #    password to that header (constant-time) proves the operator knows it — blocking a
            #    walk-up change on an unattended, already-authenticated browser. Fail closed if the
            #    header is somehow absent. The detail never distinguishes wrong-vs-missing.
            header_pw = _request_basic_password(request)
            if header_pw is None or not secrets.compare_digest(header_pw, body.current_password):
                raise HTTPException(status_code=400, detail="current password is incorrect")

            # 1. Length policy on the NEW password. The 400 detail names the POLICY, not the value.
            if len(new_password) < _WEBPASS_MIN_LEN:
                raise HTTPException(
                    status_code=400,
                    detail=f"new password must be at least {_WEBPASS_MIN_LEN} characters",
                )

            # 2. Hash off the event loop. On any hashing error, a generic 500 (no plaintext, no
            #    caddy stderr — which could echo the input — ever surfaces).
            try:
                hashed = await asyncio.to_thread(_caddy_hash, new_password)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=500, detail="password hashing failed") from None
        finally:
            # §9: drop the plaintext the instant hashing is done. ``del`` clears this local, but the
            # Pydantic ``body`` still references the SAME str and outlives us into the multi-second
            # poll below — so blank that field too. No plaintext reference then survives past the
            # hash (shrinks the memory-scrape / core-dump window to the hashing call itself).
            del new_password
            # Defensive: model is normally mutable, so this succeeds.
            with contextlib.suppress(Exception):
                object.__setattr__(body, "new_password", "")
                object.__setattr__(body, "current_password", "")

        # 3. Hand the HASH (never plaintext) + a fresh nonce to the root unit via a 0600 request.
        nonce = secrets.token_hex(16)
        record = {"hash": hashed, "nonce": nonce, "ts": time.time()}
        await asyncio.to_thread(_write_webpass_request, record)

        # 4. Write the "password changed" marker OPTIMISTICALLY now. The deferred caddy restart
        #    (~T+2s) drops this connection and cancels this handler BEFORE the root unit's 'ok'
        #    result exists (~T+3-9s), so a marker gated on observing 'ok' would essentially never
        #    run and the banner would nag forever after a real change. Mark now (while we still run)
        #    and UNDO it only if we survive long enough to observe a 'failure'.
        await asyncio.to_thread(_write_webpass_marker)

        # 5. Poll for the root unit's matching-nonce result. The dropped connection is expected; the
        #    frontend treats it as success (the browser re-prompts against the new hash).
        deadline = time.monotonic() + _WEBPASS_POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            result = await asyncio.to_thread(_read_webpass_result, nonce)
            if result is not None:
                status = result.get("status")
                if status == "ok":
                    return {"status": "ok"}
                if status == "failure":
                    # Rollback happened; the password is UNCHANGED — retract the optimistic
                    # marker so the banner correctly keeps nagging.
                    await asyncio.to_thread(_delete_webpass_marker)
                    return {
                        "status": "failure",
                        "detail": str(result.get("detail") or "rotation failed"),
                    }
            await asyncio.sleep(_WEBPASS_POLL_INTERVAL_S)
        return {"status": "pending"}

    @app.post("/api/security/dismiss-default-prompt")
    async def api_dismiss_default_prompt(_: None = Depends(auth)) -> dict[str, Any]:
        # "I've already changed it" — the operator rotated the password out-of-band (host CLI), so
        # write the marker to silence the first-login banner. No request body; auth-gated like the
        # rest. The marker is app-written (never root), per the C3 symlink-avoidance rule.
        await asyncio.to_thread(_write_webpass_marker)
        return {"status": "ok"}

    # -- diagnostics (Maintenance tab backend) ------------------------------
    # R23/R25 future home: a SAMPLED live raw-diag SSE stream (~10-20 lines/s/port, its OWN stream
    # separate from /api/stream) lands with the Maintenance UI phase. For now the monitor POLLS
    # GET /api/diag for rolling stats; we deliberately do NOT flood /api/stream with per-line diag
    # frames here, so the conning stream is never starved by a diagnostics flood.

    def _reject_non_target(slot: str) -> None:
        """R17 refusal for an ACTIVE action: an operational port, or any slot that is not a free
        unused target, is refused with 409 (the port is busy/ineligible, not a malformed request).
        This is the security boundary — a bench TX/reconfigure can never reach a live wire."""
        if manager.is_operational_port(slot):
            raise HTTPException(status_code=409, detail=f"slot {slot!r} is an operational port")
        if slot not in manager.targetable_slots():
            raise HTTPException(
                status_code=409, detail=f"slot {slot!r} is not a free unused target"
            )

    def _check_confirm(slot: str, confirm: str) -> None:
        """The confirm token is the slot id echoed back — a deliberate friction, never a secret.
        Missing token -> 400; present but wrong -> 403."""
        if not confirm:
            raise HTTPException(status_code=400, detail="confirm token required")
        if confirm != slot:
            raise HTTPException(status_code=403, detail="confirm token does not match slot")

    @contextlib.asynccontextmanager
    async def _single_flight(slot: str) -> AsyncIterator[None]:
        """Reserve ``slot`` for one in-flight action under a per-slot cooldown; release on exit.

        A second concurrent action on the same slot, or a follow-up inside the cooldown, is refused
        with 429. The check-and-reserve is atomic under ``diag_lock`` so two racing requests cannot
        both slip through.
        """
        async with diag_lock:
            now = time.monotonic()
            if slot in diag_inflight:
                raise HTTPException(status_code=429, detail=f"action already running on {slot!r}")
            last = diag_last_action.get(slot)
            if last is not None and now - last < _DIAG_COOLDOWN_S:
                raise HTTPException(status_code=429, detail=f"slot {slot!r} in cooldown")
            diag_inflight.add(slot)
        try:
            yield
        finally:
            async with diag_lock:
                diag_inflight.discard(slot)
                diag_last_action[slot] = time.monotonic()

    @app.get("/api/diag")
    async def api_diag(_: None = Depends(auth)) -> dict[str, Any]:
        # Read-only per-input rolling diagnostics. Empty ports when stopped or in simulate mode.
        return {"running": manager.running, "ports": manager.diagnostics_snapshot()}

    @app.post("/api/diag/decode")
    async def api_diag_decode(body: DecodeRequest, _: None = Depends(auth)) -> dict[str, Any]:
        # Read-only single-line inspector (click-to-decode). Never touches a port; never raises.
        return decode_line(body.line)

    @app.post("/api/diag/baud-sweep")
    async def api_diag_baud_sweep(
        body: BaudSweepRequest, _: None = Depends(auth)
    ) -> dict[str, Any]:
        _reject_non_target(body.slot)
        _check_confirm(body.slot, body.confirm)
        async with _single_flight(body.slot):
            result = await asyncio.to_thread(manager.baud_sweep, body.slot)
        return {"slot": body.slot, **result}

    @app.post("/api/diag/send")
    async def api_diag_send(body: SendRequest, _: None = Depends(auth)) -> dict[str, Any]:
        _reject_non_target(body.slot)
        _check_confirm(body.slot, body.confirm)
        async with _single_flight(body.slot):
            result = await asyncio.to_thread(manager.send_test, body.slot, body.line)
        return {"slot": body.slot, **result}

    @app.post("/api/diag/loopback")
    async def api_diag_loopback(body: LoopbackRequest, _: None = Depends(auth)) -> dict[str, Any]:
        _reject_non_target(body.slot)
        _check_confirm(body.slot, body.confirm)
        async with _single_flight(body.slot):
            result = await asyncio.to_thread(manager.loopback_test, body.slot)
        return {"slot": body.slot, **result}

    @app.post("/api/diag/capture")
    async def api_diag_capture(body: CaptureRequest, _: None = Depends(auth)) -> dict[str, Any]:
        # Capture is READ-ONLY recording, so it is NOT gated to unused targets (recording a live
        # feed is legitimate) and takes no confirm token — only quotas and a server-owned filename.
        action = body.action.strip().lower()
        if action == "stop":
            if not manager.running:
                raise HTTPException(status_code=409, detail="engine is not running")
            status = manager.stop_capture(body.slot)
            if status is None:
                raise HTTPException(status_code=404, detail=f"no active capture on {body.slot!r}")
            return {"action": "stop", **status}
        if action != "start":
            raise HTTPException(
                status_code=400, detail=f"action must be start|stop, got {body.action!r}"
            )
        if not manager.running:
            raise HTTPException(status_code=409, detail="engine is not running")
        # Web-layer quotas (R18): bound concurrent captures and the total data/ footprint before
        # arming; the per-file byte + wall-clock caps are the CaptureSession's own responsibility.
        active = [s for s in manager.capture_status() if s.get("active")]
        if len(active) >= _CAPTURE_MAX_CONCURRENT:
            raise HTTPException(status_code=429, detail="max concurrent captures reached")
        if _data_dir_bytes(_DIAG_DATA_DIR) >= _CAPTURE_DATA_QUOTA_BYTES:
            raise HTTPException(status_code=507, detail="data/ capture quota exhausted")
        try:
            status = await asyncio.to_thread(manager.start_capture, body.slot)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"unknown or non-read slot: {body.slot!r}"
            ) from None
        return {"action": "start", **status}

    return app


def _log_task_done(task: asyncio.Task[None]) -> None:
    """Done-callback for the SSE background tasks: stay quiet on shutdown cancellation, but log
    loudly if a task ever finishes on its own (a ``while True`` loop ending is always a defect)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("SSE task %r exited with an error", task.get_name(), exc_info=exc)
    else:
        logger.error("SSE task %r exited unexpectedly (loop should never return)", task.get_name())


async def _health_broadcast_loop(manager: EngineManager, broker: Broker) -> None:
    """Publish a ``health`` SSE frame roughly every :data:`_HEALTH_INTERVAL_S` seconds.

    Skips the (health-snapshot) work entirely when there are no subscribers (EFF2), and wraps each
    iteration so a transient error degrades gracefully instead of killing the loop forever (H10).
    """
    while True:
        try:
            if broker.subscriber_count > 0:
                broker.publish_health(manager.health())
        except Exception:
            logger.exception("health broadcast loop: skipping a tick after an error")
        await asyncio.sleep(_HEALTH_INTERVAL_S)


async def _state_broadcast_loop(manager: EngineManager, broker: Broker) -> None:
    """Publish a ``state`` SSE frame roughly every :data:`_STATE_INTERVAL_S` seconds (~4 Hz).

    Skips a tick when there are no subscribers (EFF2) or the engine is stopped (no snapshot). The
    drop-oldest subscriber queue keeps the NEWEST frames, so this fast conning stream is never
    starved by the nmea flood under normal load; a latest-wins / interest-filtered broker (a
    separate diag stream) is deferred to C3. Each iteration is wrapped so a transient error
    degrades gracefully instead of killing the loop forever (H10).
    """
    while True:
        try:
            if broker.subscriber_count > 0:
                snapshot = manager.snapshot()
                if snapshot is not None:
                    frame = _state_to_dict(snapshot)
                    # F1: carry route progress on the stream too (additive; absent with no route).
                    route = manager.route_status()
                    if route is not None:
                        frame["route"] = route
                    # A3b: advisory driven-fields list (route/auto-RX/depth-sim) so the editor's
                    # grey-out stays live off the same stream the conning display reads.
                    frame["driven_fields"] = _driven_fields(manager)
                    # Display-only instrument sim (propulsion/fuel/environment/autopilot):
                    # SSE-only, never on the wire; rendered amber so it is not mistaken for
                    # NMEA-backed truth. Grafted here, not in ``_state_to_dict``.
                    # A4: ``simulate_display_instruments`` stays pure. The engine-order telegraph
                    # (``engine_order_pct``) is threaded IN so it drives the rpm/load model; the
                    # post-hoc ``.update`` then applies every override (idempotent echo for
                    # engine_order, plus the six cosmetic keys). The manager dict only holds keys
                    # already in ``sim`` (the 7 override keys), so ``.update`` overwrites values and
                    # adds NO new key — the 19-key set is preserved. Overrides ride SSE only.
                    overrides = manager.get_display_overrides()
                    sim = simulate_display_instruments(snapshot, overrides)
                    sim.update(overrides)
                    frame["sim"] = sim
                    broker.publish_state(frame)
        except Exception:
            logger.exception("state broadcast loop: skipping a tick after an error")
        await asyncio.sleep(_STATE_INTERVAL_S)


# Module-level ASGI entrypoint (``web.app:app``).
app = create_app()
