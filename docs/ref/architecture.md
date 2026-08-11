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
- **RX is first-class** (not started for `tx` channels): a reader thread verifies checksum, then
  (a) always pushes the line to the web monitor ring buffer, and (b) feeds `VesselState` ONLY when
  `rx_feeds_state` is set and the field is in the channel's `rx_accept` whitelist — preventing
  loopback/rogue-talker corruption. Parsing is `pynmea2` only (`rx.parse_line`), and only on the
  `rx_feeds_state` path; **no `pyais` decode runs on any serial RX path.**
- **A duplex channel's own RX never reaches the AUTO router.** Channels wire `on_rx` to
  `_rx_monitor` -> `_feed_state` (tagged `rx:<channel_id>`); only INPUT slots wire it to
  `_dispatch_rx` -> `router.note_rx`. So `direction: "both"` gives you the monitor and `rx_accept`
  state — never passthrough arbitration. Passthrough needs an `inputs[]` slot plus `sources`, and an
  output channel and an input slot may **never** share a device path (one `realpath`-keyed
  namespace, rejected by `_validate_cross_channel`).

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

#### Background sims are gated per-field, NOT by mode

The default-ON background sims (depth, wind, rudder, heading — `config.effective_*_sim`) exist so the
hull is never unnaturally still. They must not fight a *real* writer of the same field, but the guard
for that is **what is configured to write the field**, not the global mode:

- **`simulate`** — never suppressed. No router, no passthrough, so nothing else writes state.
- **`replay`** — always suppressed. The capture file is the source of truth.
- **`auto`** — suppressed only when something can actually write that field: a channel that RX-feeds
  it (`rx_feeds_state` + `rx_accept`, via `config.rx_fed_fields`), or — for heading alone — a heading
  channel with `sources`, since HDT/HDG are in `_STATE_FORMATTERS` and so a live source seeds
  `heading_*` through `_feed_passthrough_state`.

**Depth, wind and rudder therefore keep simulating in `auto`.** No input `function` exists for them
(`gps` / `sat` / `ais` / `unused`) and `rx.parse_line` supports no depth/wind/rudder sentence, so they
have no possible live writer unless explicitly RX-fed. The previous blanket "not simulate" guard
suppressed all four for a conflict that cannot occur, which froze the instrument picture rig-wide the
moment `auto` was selected for one unrelated channel.

Own-ship **motion** is not covered by this: `auto` still hard-requires `movement.mode: static` so
dead-reckoning cannot fight live GNSS position. Resuming motion on fallback needs the physics gated on
GNSS liveness at runtime — tracked as RM-035.

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

### Per-input runtime enable/disable (RX mute)

`InputSpec.enabled` is the RX mirror of the channel toggle: `Engine.set_input_enabled(id, bool)` mutes
one input slot at runtime. Like the channel toggle it is **a flag write and nothing more** — the reader
thread keeps running and the serial port stays open, so it cannot fail the way a close/reopen cycle can.

**Where the gate sits is the whole design.** `SerialPort._read_loop` fans a received line to three
consumers, and only two are gated:

| Seam | Feeds | Gated? |
|---|---|---|
| `on_raw(chunk)` | `PortDiagnostics` + bounded raw captures | **No** — it observes the *wire*, not the sim |
| `on_line(line)` | SSE `input_nmea` → the input pane feed | **Yes** — the pane reads as a dead wire |
| `on_rx(line)` | router liveness → channel inbox → Time Authority | **Yes** — liveness ages out |

Both gated seams are **engine-supplied closures** (`_make_input_line_feed`, `_make_dispatch`), so
`serialport.py` is untouched: the transport stays dumb and the policy stays in the engine. The gate in
`_dispatch_rx` sits at the very **top** of the method, which is what also silences the Time Authority
feed at its end — a muted slot supplies no clock fix either.

Everything downstream then falls out of existing behaviour, with no teardown logic anywhere:
`_ChannelWorker._fire` stops being suppressed by `any_live` and resumes generating,
`_feed_passthrough_state` has already seeded state so generation resumes from the last real values,
and `TimeAuthority.advance` (which resolves its winner through the same router) demotes itself to the
base clock.

**A mute takes effect immediately.** Muting stops new lines reaching the router, but the stamps it
already made would keep that input winning until they aged out — so a mute used to lag by the slot's
whole `liveness_timeout_s`. `set_input_enabled` therefore calls `Router.clear_liveness(input_id)` on
the way down, dropping that input's stamps so the flip to SIM is instant and deterministic. Unmuting
needs no counterpart: the next valid line re-stamps by itself.

That is what makes the mute the **signal-loss test instrument** rather than a timeout-tuning
exercise: `liveness_timeout_s` then only governs genuinely *unexpected* loss, so it can be sized
purely to avoid flapping on a sparse feed (AIS own-ship reports stretch toward minutes when moored)
without also lengthening every deliberate test.

The enable map is built **unconditionally** for every mode, deliberately unlike the adjacent
auto-only `_diagnostics` dict: `input_status()` walks `config.inputs` in all modes, so a simulate
config that still declares slots must not `KeyError`. `HealthReport.inputs` carries the flag on the
1 Hz health frame (id + flag only, never the device path — R19) so the input toggle reconciles on the
same path and cadence as the output toggle, on every tab and across clients.

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
the web `health` event and the NMEA Streams pane. Per-FIELD provenance rides the `state` event
separately; see *Per-field provenance* below.

Failover is reachable **on demand** via the per-input RX mute above, which is how signal loss is
rehearsed without unplugging a cable — and since the mute clears liveness, the flip is immediate.
One window remains: because auto mode requires `movement.mode: static`, the post-fallback generator
holds the last seeded position while still reporting the last SOG/COG (see RM-035).

### Transparent relay: forwarding what the router does not model

`classify.sentence_class` knows exactly three classes (`gnss` / `heading` / `ais`). **Everything else
returns `None` and is dropped** — AIS status and alarm sentences (`ALR`, `ALF`, `ALC`, `ABK`, `TXT`,
`VER`), vendor `$P...` proprietary sentences, query responses. Wired *in series* on a real talker's
wire, that silently makes this program a black hole for every sentence type it does not model.

`ChannelSpec.rx_transparent_relay` (default **off**) fixes that, and `Router.note_rx` returns an
`RxDecision` rather than a bare tuple so the two questions stay separate:

| Decision | What lands here | Forwarded? | Stamps liveness? |
|---|---|---|---|
| `arbitrated` | a classified line for a channel that lists this input in `sources` | yes, if still the winner | **yes** |
| `transparent` | anything that channel would otherwise drop — unclassified, or classified-but-unroutable | **only while the channel is LIVE** | **no** |
| (`None`) | nothing claims it | no | no |

**Never stamping liveness is the load-bearing part.** If status chatter counted as liveness, a talker
emitting only alarms would hold the channel LIVE forever, `_fire` would stay suppressed by `any_live`,
and the rig would never simulate on signal loss — the exact opposite of the point.

The LIVE gate on transparent lines is the *same* `any_live` predicate `_fire` uses to suppress
generation, so the two are exact complements: while a source feeds the channel the real talker's
traffic flows and generation is silent; once it dies generation takes over **and the chatter stops**,
so the consumer sees a clean simulated picture instead of alarms about a feed that is gone.

The transparent branch also covers the *classified-but-unroutable* case — a GNSS sentence arriving on
an AIS wire classifies fine, but the GPS channel does not list that input in `sources`, so it is
dropped today. "Don't swallow anything" has to cover that too.

Two shapes are rejected at validate time because they would be silent no-ops: the flag with empty
`sources` (nothing to relay), and the flag on an un-arbitrated role such as `instrument` (whose
`channel_class` is `None`, so it can never be LIVE). An input may also be relayed by **at most one**
channel, or which channel received its unclassified lines would depend on config order.

**Not covered:** a line failing `checksum.verify` is dropped before the router sees it
(`serialport._handle_rx_line`), so this program is a checksum filter where a plain wire is not. Nor
does it help TAG-block-prefixed lines — `checksum.split` requires a leading `$`/`!`, so a `\s:...\`
prefix fails verification and dies at the same gate (see ISSUE-033).

### Per-field provenance (RM-009)

`SharedState` is the single write choke point for vessel state, so provenance is recorded there: a
`dict[str, Prov]` side-map holding `(source, cls, ts)` per field, written under the **same lock** as
the snapshot. `snapshot_with_provenance()` returns both in one lock acquisition — reading them
separately would tear (value from one commit, tag from another) at the 4 Hz serialization cadence
against a 10-20 Hz physics writer.

It is a side-map and **not** a `VesselState` field on purpose: embedded in the frozen dataclass a tag
would be carried forward by every `dataclasses.replace` that did not touch the field, so a stale tag
would ride along on unrelated writes.

`update()` takes a **required keyword-only** `_sources` — a string tagging every key, or a dict
tagging per key. Required rather than defaulted so a new writer cannot silently inherit `"sim"`. The
per-key form is load-bearing: the physics tick commits `utc` alongside pitch/roll in ONE atomic swap,
and `utc` may be a live GNSS instant while its siblings are simulated. Splitting that into two calls
would tear the swap a generator depends on to read time and position off one snapshot.

Writer tags: `sim` (physics), `clock:<tier>` (`utc`), `live:<input_id>` + class (auto passthrough),
`rx:<channel_id>` (a duplex channel's own RX), `replay`, `manual`, `config` (initial).

**Expiry happens at read time, and is the whole point.** `Engine.provenance()` returns a SPARSE map
(`{field: "LIVE"}`; absent means SIM, so a new field defaults to the safe answer). A `live:<input>`
tag only survives while the router still names that input the winner for the class the value arrived
on — the stored class is what makes that check possible without a field→class table. The moment the
source dies the value freezes at its last reading and the tag degrades to SIM; without that re-check
a frozen position would read LIVE forever. The `rx:` path cannot use the router (liveness is keyed by
`(input_id, cls)` and a duplex channel has no such row) so it ages on its write timestamp instead.

The clock tag reaches the physics tick as an **injected callable**, defaulting to `"simulated"`. The
tick's clock is a `_Clock` protocol — a `TimeAuthority` only in auto mode, a bare `TimeSource`
otherwise, which has no `source_tag()`. Reaching for it unconditionally would raise `AttributeError`,
and the run loop's blanket `except` turns that into a dead physics thread with every channel frozen.

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
  as flat JSON values, plus a sparse `provenance` map (`{field: "LIVE"}`; absent means SIM) resolved
  per frame — see *Per-field provenance*. The same map rides `GET /api/state` so the UI's first paint
  is correct before any stream frame arrives. Channel-level LIVE/SIM/OFF stays on the `health` event.

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
| `GET /api/ports` | attached USB-serial adapters: opaque handle, kernel port, detected class, live, owning slot. Never the by-id link (brand + serial, R19) — the client binds a slot by *handle* and the server resolves it |
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
