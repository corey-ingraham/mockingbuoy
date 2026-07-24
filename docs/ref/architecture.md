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
        ├─ engine.py       PeriodicSender, PhysicsThread, Engine, StatusMsg
        └─ config.py       load/save/validate channels JSON
```

The engine **never** imports `web`, `uvicorn`, `fastapi`, or any GUI toolkit. The web layer imports
the engine. This guarantees a headless-capable core: the service is "engine + a thin web front end."

## Shared state (`state.py`)

- `@dataclass(frozen=True) VesselState`: `lat, lon, sog_kn, cog_deg, heading_true_deg,
  heading_mag_deg, mag_variation_deg, altitude_m, fix_quality, satellites, hdop, utc`.
  **`cog_deg` and `heading_*` are independent variables** — RMC/VTG read `cog_deg`; HDT/HDG/HDM read
  `heading_*`. They diverge under set/drift and while turning; tests assert they are never cross-wired.
- `@dataclass(frozen=True) AisTarget`: `mmsi, name, call_sign, ship_type, lat, lon, sog_kn, cog_deg,
  heading_true_deg, nav_status, turn_rate, dims..., ais_class ("A"|"B"), moving`.
- `SharedState`: one `threading.Lock`. `snapshot() -> VesselState` (copy out under lock, release).
  `update(**changes)` = `dataclasses.replace` under lock. Targets held as an immutable tuple; upsert /
  remove / update by mmsi. **Never hold the lock across a blocking `serial.write`** — snapshot, release, format, write.

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
  "rx_feeds_state": false, "rx_accept": [],
  "emit": [ { "sentence": "GGA", "rate_hz": 1.0 }, ... ],
  "ais": { /* only for AIS channels — see nmea-reference.md */ }
}
```

There is **no `device_type`/brand field** — a channel is defined only by its electrical/logical
capabilities. GPS, heading, and AIS are simply three configured instances of this one model.

## Generators — uniform contract

Each generator is a pure function object: `build(snapshot) -> list[str]` returning finished,
checksummed NMEA lines (no line-ending; the writer owns `b"\r\n"`). No serial, threads, or locks.
`$` sentences are built with `pynmea2` (`str(msg)` emits `$…*HH`; talker = first ctor arg). AIS lines
come pre-checksummed from `pyais` (may be multi-fragment). Field maps → `nmea-reference.md`.

## Threading topology

- **PhysicsThread** — commits new position (`dead_reckon`) + `utc` at `physics_hz`, so all senders read
  one authoritative snapshot and never diverge. Static mode only refreshes `utc`.
- **PeriodicSender** (one per channel) — drift-free scheduling on `time.monotonic`: compute
  `next_tick += period` **before** doing work; sleep via `stop_event.wait(next_tick - now)`; if behind,
  resync to `now` (drop missed ticks, no burst). Per-port failure isolation: `WriterError` → mark down +
  reopen with backoff + continue; any other exception → log + continue. The thread never dies.
- **RX readers** (duplex channels) — see above.
- All threads share one `stop_event` and push `StatusMsg` + emitted lines onto a `janus` queue.

## Web ↔ engine bridge

The engine (threads) pushes to `janus.sync_q`; a single async `Broker.pump()` task drains
`janus.async_q` and fans out to per-client bounded `asyncio.Queue`s (drop-oldest, so a slow browser
can't stall the engine). SSE (`/api/stream`) streams to `EventSource`. Controls are `POST /api/control`.
Shutdown: FastAPI `lifespan` `finally` → `engine.request_stop()` → `join()` → `close_ports()`.

## Testing / CI

- Unit tests (no hardware): checksum known-answers; coordinate conversion edge cases; `dead_reckon` vs a
  geographiclib reference; GPS/heading sentences re-parse via `pynmea2.parse` (checksum valid);
  **RMC/VTG use cog, HDT uses heading**; AIS encode→`pyais.decode` round-trip; baud-budget calculator;
  config validation; magnetic-variation consistency.
- Virtual serial: `PtyWriter` / `os.openpty()` / `socat` loopback for port tests without hardware.
- CI (`.github/workflows/ci.yml`): ruff + black + mypy + pytest on push/PR. Hardware-in-the-loop steps
  stay a manual checklist (see `deployment.md`).
