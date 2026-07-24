# User Guide

A practical guide to how mockingbuoy works and how to operate it day to day. This is a
living document; it tracks the target design even where features land incrementally.

## Overview

mockingbuoy is a multi-port **NMEA 0183 simulator/generator**. One Python process drives
**N independent USB-serial channels** from a single synchronized vessel state, and serves a
small **web UI** over the LAN for monitoring and control.

It runs as a hardened **native service** — a Python virtualenv driven by systemd, fronted by a
natively-installed Caddy reverse proxy (TLS from an internal CA + HTTP Basic auth, and the app bound to a
**unix socket** — no host TCP port — that only Caddy can reach). It needs internet **only to install**;
there is **no runtime internet dependency**. Channels are a generic list — GPS, heading, and AIS are just three configured
channel instances, and any additional channel (for example an instrument channel) is another
instance of the same model.

The engine **fails loud**: ordinary paths do not silently repair malformed payloads, they error.
A wrong value surfaces immediately rather than being papered over.

## The Web UI: Five Tabs

Browse to `https://<LAN_IP>/` and authenticate with HTTP Basic auth (username + the web
password). The UI is organized into five tabs.

### 1. Conning

A generic, vendor-neutral instrument display — the at-a-glance picture of the synchronized
vessel state:

- **Heading compass dial**, **rate-of-turn** indicator, **inclinometer** (roll / pitch),
  and an **apparent + true wind rose**.
- **Digital readouts:** SOG, STW, COG, HDG, LAT, LON, DEPTH, UTC, SEA STATE.

The gauges update live from a **`state` server-sent event (~4 Hz)** carried on the same stream as the
raw `nmea` lines and the `health` events, so the display tracks the engine without polling.

Every value is tagged **LIVE / SIM / OFF** using **both colour and text** (never colour alone,
so the display stays colourblind-safe):

- **green + "LIVE"** — sourced from a real sensor on a live input.
- **amber + "SIM"** — synthesized from vessel state.
- **grey + "OFF"** — the source channel is disabled.

### 2. NMEA Streams

The live per-channel sentence streams as they go out on the wire. Each pane has a **per-pane
on/off switch** and its own **LIVE / SIM / OFF badge**, so you can see exactly what each output
channel is emitting and whether the data is real or simulated.

### 3. Config

Where you drive the simulation.

- **Operating-mode selector:** Simulate / Auto / Replay (see *Operating Modes* below).
- **Grouped manual vessel inputs:** position, SOG, COG, heading (true / mag), variation, STW,
  depth, rate-of-turn, true wind speed / direction, the **sea-state selector (0–9)**, and GPS
  detail (altitude, fix quality, satellites, HDOP).
- **Per-channel enable checkboxes** — turn each output channel on or off.
- **(Auto) input-slot function assignment** with smart validation — map each sensed input COM
  to its role.
- **Buttons:** Apply (live), Save as defaults, Load current (see *Setting and Saving Vessel
  Parameters*).

### 4. Maintenance

A bench NMEA diagnostic surface. See *Maintenance and Troubleshooting* below for the full
detail. In short: a multi-port live monitor, per-port statistics, an auto-baud sweep,
click-to-decode, and a guided fault advisor — **read-only by default**.

### 5. Security

A **read-only posture panel**. It reports the current security state **by presence, never by
value** — it never renders a secret. See *Security Posture* below.

## Operating Modes

Set the mode on the Config tab. The choice applies per run and is one of three:

### Simulate

Always-synthetic. Every enabled channel emits data generated from the shared vessel state.
Use this for development, demos, and repeatable scenarios where you want full control of every
value. The per-channel on/off toggle still applies.

### Auto

A priority-routed passthrough. Each channel **senses a real NMEA input COM**:

- **Valid live NMEA passes through verbatim** to the matching output and updates the web
  display.
- **On loss of the live source, the channel seamlessly falls back to simulating** from vessel
  state — no restart, no gap.

Input lines are classified by **sentence class** (gnss / heading / ais) and routed to the
correct output from a **per-output source-priority list**; if no live source satisfies an
output, that output simulates. Use Auto when you have real gear on the bench and want
mockingbuoy to relay it, fill the gaps, and give you one unified live/sim picture.

### Replay

Re-inject a **captured NMEA file** so a recorded session drives the outputs and the web display exactly
as a live source would. Replay runs the file back through the **same single writer path** the live modes
use — so downstream a recording is indistinguishable from a real source, and there is no separate playback
engine to behave differently. Use Replay to reproduce a captured scenario deterministically for
regression or demo.

## GPS Position, Time Routing, and Priorities

Position and time on the **GPS output COM** follow a single, ordered source priority. This keeps
position and time from ever splitting across sources.

### Position source priority (feeding the GPS output COM)

1. **GPS input live** → its GNSS sentences pass verbatim → GPS COM.
2. **SAT-compass input live** → its **position / time** sentences are **cross-routed verbatim**
   → GPS COM, *while its heading sentences continue to go to the heading COM*. A satellite
   compass reports both a fix and a heading; mockingbuoy sends each part to the output where it
   belongs.
3. **Simulate** position / time from vessel state.

### Time Authority (unified with position)

Time is single-source and unified with position: the **active GNSS source** (GPS, then SAT)
supplies **both** position and time to the GPS COM, so they never split. The full priority is:

**GPS → SAT compass → NTP → system.**

Here **NTP means reading the locally chrony-disciplined system clock** — there is no network
query, so it adds no latency and keeps the "no runtime internet dependency" posture.

### ZDA behavior

The program **always provides a ZDA** on the GPS output. When a priority-1 or priority-2 source
omits ZDA, ZDA is **synthesized from that same source's RMC** — identical time, add-only — so
you never get a **divergent ZDA / RMC pair** on one bus. In passthrough, the program does **not**
inject its own NTP ZDA.

**Why this matters:** no marine device cross-validates ZDA against RMC/GGA, so a divergent time
pair fails *silently* — clock jitter, wrong timestamps, source-flapping. A single consistent
time-and-position source is the recognized-safe pattern, which is why mockingbuoy keeps them
unified rather than mixing a fix from one source with a timestamp from another.

## Channels, Sea State, and Vessel Parameters

### Per-channel on/off toggles

Each channel has a **runtime on/off toggle** — flip it from the NMEA Streams pane switch or the
Config tab's per-channel enable checkbox and it takes effect **without an engine restart**. A
**persisted per-channel default** decides whether the channel comes up enabled on the next
start.

The optional **instrument channel** (talker **II**) carries the derived instrument data:
VHW, DPT, DBT, MWV (apparent), MWD (true), ROT, XDR, RSA, VDR, and `$PASHR`. Its values (pitch, roll,
STW, depth, rate-of-turn, true wind, rudder angle, set/drift) come from the shared vessel state; apparent
wind is derived from vessel motion (SOG at COG, not heading). The **THS** sentence (true heading + status)
rides the **heading channel** (talker **HE**) alongside HDT / HDG / HDM, not the instrument channel.

### The sea-state selector

A **WMO sea-state selector (0–9)** on the Config tab picks a motion model. The hull **gently
moves even in calm water** — roll and pitch are derived from the selected sea state, so the
inclinometer and the instrument channel never sit perfectly and unnaturally still.

### Setting and saving vessel parameters

Three buttons on the Config tab govern the lifecycle of your inputs:

- **Apply (live)** — push the current form values to the running engine immediately. Affects the
  live run only; nothing is written to disk.
- **Save as defaults** — persist the current values (and per-channel enable state) as the
  defaults the service loads on next start.
- **Load current** — pull the engine's current live values back into the form (useful before
  editing, or to discard un-applied edits).

## Reading the Indicators

Wherever a value or channel is shown, its provenance is labelled with **both colour and text**:

- **green + "LIVE"** — a real sensor on a live input is the source.
- **amber + "SIM"** — the value is simulated from vessel state.
- **grey + "OFF"** — the source channel is disabled.

Colour is never the only signal, so the readout is colourblind-safe. The Conning tab also shows
a **time-source label** reflecting the active Time Authority tier — GPS, SAT, NTP (local
disciplined clock), or system — so you always know where UTC is coming from.

## Maintenance and Troubleshooting

The Maintenance tab (and its CLI twin, below) is a bench NMEA diagnostic surface. It is
**read-only by default**: it only listens. Any transmit or reconfigure action is **opt-in,
individually confirmed, and restricted to a non-operational port** — you cannot accidentally
disturb a live output.

### Multi-port live monitor

Watch **4–6 ports** at once: **raw + hex**, **millisecond timestamps**, **per-line checksum
colouring**, a **filter**, and **pause**. Per port it also reports the **checksum-error rate**,
a **talker / sentence inventory**, the **sentence rate**, and the **bus load**.

### Fault advisor

A guided advisor **classifies the byte stream** and advises the **likely cause and fix**. The
canonical example: a stream that looks like framed NMEA but fails every checksum usually means a
**reversed A/B data pair** — the advisor names that and tells you to swap the wires.

### Auto-baud sweep

Don't know a port's baud rate? The **auto-baud sweep** cycles the common rates and reports which
one yields clean, checksum-valid framing.

### Click-to-decode

Click any captured sentence to expand it into its **decoded field map** — talker, sentence type,
and each field's meaning — without leaving the monitor.

### Cheat-sheets

Built-in reference panels for quick bench work:

- **Baud / sentence** — common rates and which sentences ride them.
- **Wiring / pinout** — how to land the differential pair and ground.
- **Talker ID** — the two-letter talker prefixes and what they mean.

### Optional ADC voltage tiles

If a **pluggable ADC voltage sensor** is present (off by default — a `VoltageProvider` add-on,
e.g. an ADS1115 over I2C behind a protective analog front-end), the Maintenance tab shows
per-line, differential, and common-mode **voltage tiles**. This positively confirms a reversed
A/B pair from the **differential idle sign** rather than inferring it from checksum failures
alone.

### The `mockingbuoy-mon` CLI

The same diagnostics core is available as a terminal frontend, `mockingbuoy-mon`, with **no web
server** — for headless, SSH, field, and disaster-recovery use.

- **Standalone mode** opens a serial port directly (including the baud sweep), independent of the
  service.
- **Attach mode** is a thin client to the running service's local stream / diagnostics
  endpoints.
- **Renderers:** a curses TUI, plus `--plain`, `--json`, `--decode`, and `--baud-sweep`.

Like the web surface, it is **read-only by default**. Over SSH, `--plain` or `--json` are the
easiest to consume (pipe `--json` into your own tooling); the curses TUI is the interactive
default on a real terminal.

## Connecting Downstream Gear

### Serial

Each output channel drives a USB-serial adapter. Point the consuming device (chart plotter,
autopilot, logger) at that adapter's line at the channel's configured baud. The channel opens the
port tolerantly, so a replug re-attaches without a restart.

### Raw NMEA over TCP (taps)

Every channel is also exposed as a **read-only, LAN-bound TCP tap** carrying the plain NMEA 0183
stream. Read one with `nc`:

```bash
nc <LAN_IP> <port>          # raw NMEA 0183 sentences for that channel
```

or point **OpenCPN** at the same `<LAN_IP>:<port>` as a **Network → TCP** data connection. The
tap `host:port` list is printed on install and set via `TAP_PORTS` in `setup.env`. The taps are
subscribe-only — they never accept control input.

## Security Posture

The **Security** tab is a read-only panel that reports the current posture **by presence, never
by value** — it **never renders a secret**. It shows:

- **TLS active** (served from the internal CA).
- **Which auth layers are enabled** — reported by presence only, never the credential itself.
- **Unix-socket app bind** (the engine publishes no host TCP port; Caddy reaches it over a local
  unix socket).
- **TCP-tap ports**, **subscriber count / cap**, **uptime**, and the active **security headers**.

The **primary login is rotated at the host, not in the browser** — you change the web password
with `caddy hash-password` and update the service env on the host; the UI has no
password-change form because nothing secret is ever surfaced there. Underneath, the service runs
with systemd sandboxing (`NoNewPrivileges`, `ProtectSystem=strict`, an empty
`CapabilityBoundingSet`, explicit `DeviceAllow` rules, a `SystemCallFilter`), and dependencies
are hash-locked with an offline wheelhouse for rebuild-free redeploy.

## Under the Hood: the HTTP Surface

You never need these to operate the UI, but scripted clients and `mockingbuoy-mon` (attach mode) use
them. The live stream is `GET /api/stream` (SSE: `nmea` / `health` / `state` events). Everything the tabs
do maps to a small endpoint set:

- **`POST /api/control`** — the Config-tab actions: `start` / `stop`, `update` (vessel params),
  `channel` (per-channel enable), `route` (mode + source routing), and `fault` (GPS-fault injection,
  simulate-only).
- **`POST /api/config/initial-state`** — *Save as defaults* (allow-listed → `data/config.local.json`).
- **`GET /api/inputs`** — the sensed/assigned input slots and their function.
- **`GET /api/security`** — the Security tab's posture booleans (never a secret).
- **`GET /api/diag`** + **`POST /api/diag/decode`** — the Maintenance snapshot and click-to-decode;
  the transmit-capable diagnostics (`/api/diag/baud-sweep` \| `send` \| `loopback` \| `capture`) are
  **gated** (opt-in, confirmed, non-operational port only).

Full detail (SSE events, the bridge, layering) → [architecture.md](ref/architecture.md).
