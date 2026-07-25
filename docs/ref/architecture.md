# Architecture

## Layering (strict, one-way)

```
web/ (FastAPI, uvicorn, SSE)
  └─> nmea_sim/ (engine core)
        ├─ state.py        VesselState, AisTarget, SharedState
        ├─ navigation.py   dead reckoning + coordinate formatting
        ├─ checksum.py     NMEA XOR helpers
        ├─ gps_generator.py / heading_generator.py / ais_generator.py
        ├─ serialport.py   SerialPort (tx/rx/both), writers backends
        ├─ writers.py      Writer ABC: SerialWriter / LogWriter / NullWriter / PtyWriter
        ├─ engine.py       Engine, _ChannelWorker (per-channel sender), _PhysicsThread,
        │                  _ReplayThread, ZdaCarveout, StatusMsg
        └─ config.py       load/save/validate channels JSON
```

The engine **never** imports `web`, `uvicorn`, `fastapi`, or any GUI toolkit. The web layer imports
the engine. This guarantees a headless-capable core: the service is "engine + a thin web front end."

## Shared state (`state.py`)

- `@dataclass(frozen=True) VesselState`: `lat, lon, sog_kn, cog_deg, heading_true_deg,
  heading_mag_deg, mag_variation_deg, altitude_m, fix_quality, satellites, hdop, utc`, plus the
  instrument/motion fields: `stw_kn` (speed through water), `depth_m`, `rot_dpm` (rate of turn, deg/min),
  `wind_speed_kn` + `wind_dir_deg` (**true** wind, direction **FROM**, true-north referenced),
  `sea_state` (WMO scale 0–9), `pitch_deg` + `roll_deg` (**derived** from the sea-state motion model —
  see below), `rudder_angle_deg`, and `set_deg` + `drift_kn` (current set/drift).
  **`cog_deg` and `heading_*` are independent variables** — RMC/VTG read `cog_deg`; HDT/HDG/HDM/THS read
  `heading_*`. They diverge under set/drift and while turning; tests assert they are never cross-wired.
  **Apparent wind is not stored** — only true wind is; `MWV` is computed on read as the vector sum of
  true wind and the vessel's motion over ground (SOG at COG, not heading), while `MWD` reports the stored
  true wind (magnetic direction filled from `mag_variation_deg`). See `nmea-reference.md`.

### Sea-state motion model (`pitch_deg` / `roll_deg`)

`pitch_deg` and `roll_deg` are never stored as free inputs — they are produced by a single WMO
sea-state motion model driven by `sea_state` (0–9). One selector scales the whole model coherently:
amplitude and period grow together with the scale. The hull is **always gently in motion — even at sea
state 0** — so inclinometer / `XDR` / `$PASHR` consumers see a live, plausibly-moving attitude rather
than a dead-flat value that reads as a frozen or failed sensor.
- `@dataclass AisTarget`: `mmsi, lat, lon, sog_kn, cog_deg, heading_deg, nav_status, rot,
  class_type ("A"|"B"), ship_type, name, callsign, destination, imo`.
- `SharedState`: one `threading.Lock`. `snapshot() -> VesselState` (copy out under lock, release).
  `update(**changes)` = `dataclasses.replace` under lock. **Never hold the lock across a blocking
  `serial.write`** — snapshot, release, format, write.

Per-field valid ranges are defined **canonically in `nmea_sim/validate.py`**; the web API bounds
(`web/app.py`) and the UI form min/max (`web/static/app.js`) mirror it. Treat `validate.py` as the source
of truth and keep the three in sync.

## Hardware-agnostic `SerialPort` + channels model

One class serves any USB-serial device; behavior is capability config, not brand code.

- `direction: "tx" | "rx" | "both"`. Simplex out = `tx`; bidirectional = `both`; inbound-only = `rx`.
- Tolerant lazy `open()` — a missing device sets `present=false`, surfaces to the UI, and is retried
  (hotplug); it never crashes the process or blocks sibling channels. Opens with pyserial
  `exclusive=True`, `write_timeout=1.0`.
- **RX is first-class** (not started for `tx` channels): a reader thread verifies checksum, parses via
  `pynmea2`/`pyais`, then (a) always pushes the line to the web monitor ring buffer, and (b) feeds
  `VesselState` ONLY when `rx_feeds_state` is set and the field is in the channel's `rx_accept`
  whitelist — preventing loopback/rogue-talker corruption.

### Channel config shape

```jsonc
{
  "id": "gps", "role": "gps",
  "path": "/dev/serial/by-id/...", "baud": 4800, "framing": "8N1",
  "direction": "tx", "talker": "GP",
  "enabled": true,                       // runtime on/off; persisted default enable state
  "rx_feeds_state": false, "rx_accept": [],
  "sources": [ /* input ids, priority-ordered — Auto/routing, see below */ ],
  "emit": [ { "sentence": "GGA", "rate_hz": 1.0 }, ... ],
  "ais": { /* only for AIS channels — see nmea-reference.md */ }
}
```

There is **no `device_type`/brand field** — a channel is defined only by its electrical/logical
capabilities. GPS, heading, AIS, and the **instrument** channel are simply four configured instances of
this one model. The instrument channel uses talker `II` and emits the motion/environment suite
(`VHW`/`DPT`/`DBT`/`MWV`/`MWD`/`ROT`/`XDR`/`RSA`/`VDR`/`$PASHR`); `THS` rides the **heading** channel
(talker `HE`) alongside HDT/HDG/HDM. Field maps → `nmea-reference.md`.

### Per-channel runtime enable/disable

`ChannelSpec.enabled` is a live on/off toggle: `Engine.set_channel_enabled(id, bool)` starts/stops that
channel's sender **without an engine restart** and without disturbing sibling channels or the shared
vessel state (per-channel sender isolation makes this cheap). `ChannelHealth` carries `enabled` and the
current `source` (which input, or `sim`) so the UI can tag each output. The live toggle is separate from
the **persisted default** enable state — a transient silence never silently becomes the boot posture.

## Generators — uniform contract

Each generator is a pure function object: `build(snapshot) -> list[str]` returning finished,
checksummed NMEA lines (no line-ending; the writer owns `b"\r\n"`). No serial, threads, or locks.
`$` sentences are built with `pynmea2` (`str(msg)` emits `$…*HH`; talker = first ctor arg). AIS lines
come pre-checksummed from `pyais` (may be multi-fragment). Field maps → `nmea-reference.md`.

## Synthetic AIS traffic (profile seam)

An AIS channel emits **own-ship only** by default. An optional `ais.traffic` block turns on
profile-driven synthetic contacts without baking any location into tracked code or config:

```jsonc
"traffic": {
  "enabled": false,          // false => own-ship-only, byte-identical to no block at all
  "profile_path": null,      // path to a LOCAL, user-supplied JSON profile; null => neutral default
  "target_count": null,      // override the profile's own target_count when set
  "seed": null,              // fixed seed => deterministic spawn
  "max_advance_s": 10.0      // anti-teleport ceiling on per-step elapsed time; keep >= position period
}
```

When enabled, the AIS source loads a **region-neutral realism profile** (`realism.py`) — from
`profile_path` if set, otherwise a built-in neutral default — spawns a deterministic set of
contacts, and interleaves their reports with own-ship's: on each position tick own-ship's
`!AIVDO` goes out first, then every target's `!AIVDM`; on each static tick own-ship's static
report goes first, then each target's. Targets advance by the **real elapsed time** between
successive position builds, so their motion tracks the same wall clock as own-ship. The profile
is the only place location-specific values (a bounding box, a type mix, speed distributions)
live — it is **user-supplied local JSON, kept out of the repo**; the checked-in defaults are
area-neutral. Synthetic MMSIs are generated and never reference a real registered identity. The
extra sentences count against the channel's baud budget as normal (AIS at 38400 baud has ample
headroom for a sane contact count).

## Operating modes: `simulate` / `auto` / `replay`

The engine runs in one of three modes (`mode` in the config / set via the control endpoint):

- **`simulate`** — every enabled channel emits fully-synthetic sentences built from the shared vessel
  state. The no-hardware / bench / demo path.
- **`auto`** — **priority-routed verbatim passthrough** of real NMEA on physical **input** slots, with
  seamless failover to generation on input loss (below).
- **`replay`** — re-inject a captured NMEA file back through the **same single-writer worker path** the
  live modes use, so a recorded session drives the outputs (and the web display) identically to a live
  source. No separate emit path — replay is just a different line source feeding the one writer.

### Input slots and per-channel sources

Auto/replay add a top-level `inputs` list — physical input adapters, decoupled from outputs:

```jsonc
"inputs": [
  { "id": "in1", "path": "/dev/nmea-in-1", "function": "gps",  "baud": 4800 },
  { "id": "in2", "path": "/dev/nmea-in-2", "function": "sat",  "baud": 4800 },
  { "id": "in3", "path": "/dev/nmea-in-3", "function": "ais",  "baud": 38400 },
  { "id": "in4", "path": "/dev/nmea-in-4", "function": "unused" }
]
```

`function` is one of `gps | sat | ais | unused`. Each **output** channel names a priority-ordered
`sources: [input ids]`; the first live source satisfying that output wins, else the output simulates.

### Priority-routed passthrough + sentence-class cross-routing

In `auto`, inbound lines are **classified by sentence class** (`gnss` / `heading` / `ais`) and dispatched
to the output that owns that class — routing is **by sentence class, not by physical port**, so one input
can fan its classes to several outputs. The load-bearing case: a **satellite compass** on one input emits
both a heading solution and a position/time fix; its heading sentences cross-route to the **heading**
output (`HE`) while its GNSS position/time sentences cross-route to the **GPS** output. Valid live lines
pass through **verbatim** — the exact original bytes reach the consumer; the engine is never a lossy
transcoder of real data.

### Seamless failover

On loss of an output's highest-priority live source, that output **falls back to generation** from vessel
state with no restart and no gap; when the source returns, it resumes passthrough. Consumers never see a
dead bus. Provenance (LIVE / SIM / OFF) is surfaced **per channel** — as each channel's `source` badge on
the web `health` event and the NMEA Streams pane, not per value on the `state` event.

### Unified Time Authority (single-source position + time)

Position **and** time on the GPS output come from **one** source, never split, on the chain
**GPS input → SAT-compass input → NTP → system** — where "NTP" is the locally chrony-disciplined system
clock (no network query, no latency, no runtime internet dependency). The program **always** provides a
`ZDA` on the GPS output; when the active priority-1/2 source omits `ZDA`, it is **synthesized from that
same source's exact `RMC` time** (add-only, identical time) — never a divergent `ZDA`/`RMC` pair, and in
passthrough it does not inject its own NTP-derived `ZDA`. Rationale (no marine device cross-validates
`ZDA` vs `RMC`, so a divergent pair fails silently) → `nmea-reference.md`.

## Threading topology

- **`_PhysicsThread`** — commits new position (`dead_reckon`) + `utc` at `physics_hz`, so all senders read
  one authoritative snapshot and never diverge. Static mode only refreshes `utc`.
- **`_ChannelWorker`** (one per channel) — drift-free scheduling on `time.monotonic`: compute
  `next_tick += period` **before** doing work; sleep via `stop_event.wait(next_tick - now)`; if behind,
  resync to `now` (drop missed ticks, no burst). Per-port failure isolation: `WriterError` → mark down +
  reopen with backoff + continue. It is built to survive degraded input: a checksum-valid line with
  garbage fields is counted and skipped (not fatal), and a channel with no scheduled emitters blocks on
  its inbox rather than crashing — bad wire data never silently kills the worker.
- **`_ReplayThread`** (replay mode) — re-injects a captured file through the same single-writer path.
- **RX readers** (duplex channels) — see above.
- All threads share one `stop_event` and push `StatusMsg` + emitted lines onto a `janus` queue.

## Web ↔ engine bridge

The engine (threads) pushes to `janus.sync_q`; a single async `Broker.pump()` task drains
`janus.async_q` and fans out to per-client bounded `asyncio.Queue`s (drop-oldest, so a slow browser
can't stall the engine). SSE (`/api/stream`) streams to `EventSource`. Shutdown: FastAPI `lifespan`
`finally` → `engine.stop()` (joins worker threads within its timeout, then closes ports).

The app binds a **unix socket** (Caddy reaches it over that socket); no host TCP port is published.

### SSE events

The stream carries three named event types:

- **`nmea`** — every emitted sentence line, per channel (feeds the NMEA Streams tab).
- **`health`** — per-channel/port health + status transitions.
- **`state`** — a snapshot of the synchronized vessel state at **~4 Hz** (feeds the Conning gauges),
  as flat JSON values. Provenance (LIVE/SIM/OFF) is **not** carried per value here — it is per channel on
  the `health` event (per-value state provenance is a planned enhancement, not yet shipped).

### HTTP endpoints

| Method + path | Purpose |
|---|---|
| `GET /healthz` | liveness/health probe (200 ok / 503 degraded) — no auth needed by systemd |
| `GET /api/config` | the running config (as loaded at boot) |
| `GET /api/state` | current vessel-state snapshot (flat values) |
| `GET /api/stream` | SSE: `nmea` / `health` / `state` events |
| `POST /api/control` | actions: `start` / `stop` / `update` (vessel params) / `channel` (enable) / `route` (route-playback: `start`/`pause`/`reset` the route cursor) / `fault` (GPS-fault injection, simulate-only) |
| `POST /api/config/initial-state` | allow-listed **Save-as-defaults** → `data/config.local.json` |
| `GET /api/profiles` | discovered AIS realism-profile basenames |
| `GET /api/inputs` | discovered/assigned input slots + their function |
| `GET /api/security` | posture **booleans only** — never a secret value |
| `GET /api/diag` | Maintenance diagnostics snapshot (per-port analyzer, fault verdicts) |
| `POST /api/diag/decode` | decode a pasted/selected sentence to its field map |
| `POST /api/diag/baud-sweep` \| `send` \| `loopback` \| `capture` | **gated** diagnostic actions (opt-in, confirmed, non-operational port only) |

(The UI's static assets are served at `GET /static/app.js` and `GET /static/app.css`.)

## Diagnostics core + `mockingbuoy-mon` (web-free peer)

The diagnostics engine (rolling per-port analyzer, ranked fault advisor, auto-baud sweep, sentence
inspector, optional pluggable ADC voltage sensing) lives in the engine layer, **independent of `web`**.
That one-way layering lets a terminal frontend reuse it wholesale: **`mockingbuoy-mon`** is a first-class
CLI peer to the same core with no web server — a **standalone** mode that opens a serial port directly
(including baud sweep) for headless/DR/bare-bench triage, and an **attach** mode that is a thin client to
the running service's local stream/diag endpoints. Renderers: curses TUI, `--plain`, `--json`,
`--decode`, `--baud-sweep`. Read-only by default.

## Tabbed web UI

A single self-contained file presents **five tabs** over the one shared state/stream: **Conning** (SVG
gauges — compass, rate-of-turn, inclinometer, wind rose), **NMEA Streams** (per-channel sentence panes),
**Config** (mode + vessel/channel controls), **Maintenance** (the diagnostics surface above), and
**Security** (read-only posture). Provenance tagging is **colourblind-safe LIVE/SIM/OFF** — both colour
and text, never colour alone.

## Testing / CI

- Unit tests (no hardware): checksum known-answers; coordinate conversion edge cases; `dead_reckon` vs a
  geographiclib reference; GPS/heading sentences re-parse via `pynmea2.parse` (checksum valid);
  **RMC/VTG use cog, HDT uses heading**; AIS encode→`pyais.decode` round-trip; baud-budget calculator;
  config validation; magnetic-variation consistency.
- Virtual serial: `PtyWriter` / `os.openpty()` / `socat` loopback for port tests without hardware.
- CI (`.github/workflows/ci.yml`): ruff + black + mypy + pytest on push/PR. Hardware-in-the-loop steps
  stay a manual checklist (see `deployment.md`).
