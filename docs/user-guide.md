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

Provenance is tagged **LIVE / SIM / OFF** using **both colour and text** (never colour alone, so
the display stays colourblind-safe):

- **green + "LIVE"** — sourced from a real sensor on a live input.
- **amber + "SIM"** — synthesized from vessel state.
- **grey + "OFF"** — the source channel is disabled.

On the conning tab the tag is **per panel**, and it reflects the provenance of the values that panel
actually displays — not merely whether some channel has a live source. A panel reads **LIVE** only
when *every* one of its readings that could come from a sensor really is coming from one right now;
otherwise it reads SIM. Two consequences worth knowing:

- **A panel degrades on its own when a source dies.** Because the tag is re-checked continuously,
  a position that freezes when its GNSS goes quiet flips to **SIM** within the input's
  `liveness_timeout_s` — it does not keep claiming LIVE over a stale number.
- **Some panels are always SIM, correctly.** Attitude (pitch/roll), Environment (wind, weather) and
  Depth are synthesized by the simulator — no NMEA sentence feeds them — so they never claim LIVE.

Individual *readouts* are not badged separately yet; the panel pill is the unit. Per-readout tagging
is tracked as a follow-up. The Streams tab keeps the three-state channel badges (`LIVE`/`SIM`/`OFF`),
which answer a different question: what each channel is putting on the wire.

### 2. NMEA Streams

The live per-channel sentence streams as they go out on the wire. Each pane has a **per-pane
on/off switch** and its own **LIVE / SIM / OFF badge**, so you can see exactly what each output
channel is emitting and whether the data is real or simulated.

In **auto** mode an **Inputs** section sits above the outputs, one pane per configured input slot,
each with its own **Input: ON / OFF** switch in the same position as an output pane's. See
*Rehearsing signal loss* below.

### 3. Config

Where you drive the simulation.

- **Operating-mode selector:** Simulate / Auto / Replay (see *Operating Modes* below).
- **Display card** — how the conning display is sized on *this* browser. See below; it is the second
  card, directly under Operating Mode, because it is what you reach for when the display looks wrong.
- **Grouped manual vessel inputs:** position, SOG, COG, heading (true / mag), variation, STW,
  depth, rate-of-turn, true wind speed / direction, the **sea-state selector (0–9)**, and GPS
  detail (altitude, fix quality, satellites, HDOP).
- **Per-channel enable checkboxes** — turn each output channel on or off.
- **(Auto) input-slot function assignment** with smart validation — map each sensed input COM
  to its role.
- **Buttons:** Apply (live), Save as defaults, Load current (see *Setting and Saving Vessel
  Parameters*).

#### Sizing the conning display (the Display card)

The conning tab is a **one-screen layout**: it is designed to fill the display exactly, with no page
scrolling. When it does not fit, it fails *quietly* — a panel clips its own content, or a column's last
panel sits just outside the visible box. There is no error and the scrollbars are easy to miss. The
Display card exists so you are not guessing.

- **Fit badge** — the only signal that the layout is intact. **FITS** means nothing is clipped
  anywhere; **CLIPPED** names the worst offender and by how many pixels. It reads *NOT MEASURED* until
  you have opened the Conning tab once, because a hidden tab genuinely cannot be measured.
- **Auto** (default) sizes the display from the screen height and, if that still would not fit, steps
  the density down until it does. If *nothing* fits — some window heights cannot be satisfied at any
  density — it goes back to the standard sizing and the badge tells you, rather than shrinking
  everything for no benefit.
- **Density slider** — turn Auto off to pin a density by hand, roughly 66% to 120%. Above 100% is for a
  large display where the default reads too small from a distance.
- **Fullscreen** — needs a click, because browsers refuse fullscreen without one; `Esc` leaves it. If
  the display runs a kiosk browser it is already fullscreen and this button is redundant. Some embedded
  browsers refuse the request outright; the button label only ever reflects what actually happened.
- **Copy diagnostics** — puts the browser identification, screen geometry, density and fit numbers on
  the clipboard as one block. Paste it into a bug report instead of describing the symptom.

These settings live **in this browser only**. They are deliberately not part of *Save as defaults*: the
right density for a bridge monitor is not the right density for a laptop, and one appliance serves both.
That also means the appliance never needs to know, and a different browser starts from Auto.

**Your browser's own zoom is an equally good escape hatch** and needs nothing from this app:
`Ctrl` + `−` to shrink, `Ctrl` + `0` to reset. Chromium and Firefox remember it per site, so a display
you zoom once stays zoomed across restarts.

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

**Depth, wind and rudder keep simulating in Auto.** They have no possible live source, so only
the channels that actually have one fall back to passthrough. Own ship, however, stays
stationary in Auto (`movement.mode: static`) so dead-reckoning cannot fight a live GPS fix.

#### Relaying sentences mockingbuoy does not model

Auto only understands three sentence classes, so everything else on an input wire — AIS status and
alarm sentences (`ALR`, `ALF`, `ALC`, `ABK`, `TXT`, `VER`), vendor `$P...` sentences, query
responses — is **dropped by default**. Inserted *in series* on a real talker's wire, that makes
mockingbuoy a black hole for those sentence types.

Set **`rx_transparent_relay: true`** on a channel and it instead forwards them verbatim, **while
that channel is LIVE**. Two consequences worth knowing:

- Relayed status traffic **never** counts as a live signal. A talker emitting nothing but alarms
  will not hold the channel in passthrough — it still falls back and simulates, which is the point.
- On fallback the relayed chatter **stops**. So when the real source dies, the consumer sees a
  clean simulated picture rather than alarms about a feed that is no longer there.

Two caveats no setting changes: a sentence failing its **checksum** is dropped before routing (a
plain wire would have passed the corrupt bytes on), and a **TAG-block** prefixed line (`\s:...\`)
is dropped at the same gate — see ISSUE-033.

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

### Per-input on/off toggles — rehearsing signal loss

Each **input slot** has its own **Input: ON / OFF** switch, in the same place on an input pane that
the output toggle occupies on an output pane. It is the RX mirror of the output toggle: a runtime
flag, no engine restart, and — unlike **Freeze view**, which only pauses the display — it genuinely
stops that slot feeding the simulator.

Use it to rehearse a live feed dying and coming back without unplugging anything:

1. With a source live, the channel it feeds shows **LIVE:&lt;input&gt;** on its output pane badge.
2. Switch the input **OFF**. Its pane goes silent and reads **input off**.
3. The badge flips to **SIM** **immediately** — the channel has resumed generating.
4. Switch the input back **ON**; the badge returns to **LIVE:&lt;input&gt;** on the next valid line.

**Prefer the mute over pulling a cable.** Muting clears that slot's liveness the instant you flip it,
so the changeover is immediate and repeatable. A real cable pull instead waits out the slot's
`liveness_timeout_s`, because nothing tells the program the wire died — it can only notice the
silence. That also means `liveness_timeout_s` should be sized purely to avoid *flapping* on a sparse
feed (AIS own-ship reports stretch toward minutes when moored, so the 3 s default is far too short for
an AIS input) without that choice lengthening every deliberate test.

Two things to expect, neither a fault:

- **The ship stops moving but still reports way.** Auto mode requires `movement.mode: static`, so
  after fallback the generator resumes from the last values the live feed seeded and position never
  advances — a generated RMC can report several knots over a position that does not change. This is
  pre-existing auto-mode behaviour; the toggle just makes it easy to reach. Tracked as RM-035.
- **Diagnostics keep running.** The toggle gates the *simulator*, not the wire. Maintenance-tab
  port statistics keep updating and an armed raw capture **keeps recording** while the pane reads
  "input off" — deliberate, so you can still prove bytes are arriving.

If the channel also has `rx_transparent_relay` set, muting closes that window at the same instant, so
the relayed status/alarm traffic stops the moment the badge flips to SIM.

The setting is **runtime-only**: an input toggled off returns to its configured default on restart.
An input slot's `enabled` key in `config.json` sets that startup default (absent means on).

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

## Custom AIS area realism

By default the AIS channel emits own-ship plus an optional handful of synthetic contacts shaped
by a **built-in region-neutral profile**. If you want the surrounding traffic to *resemble* a
particular kind of area — its target density, ship-type mix, and per-category speed behaviour —
you point the AIS channel at a **realism profile**: a small JSON file of area-neutral statistics.

A profile carries **only statistics** — a lat/lon bounding box, a target count, a ship-type mix,
per-category speed distributions, a motion model, and the Class A share. It contains **no real
identities and nothing captured verbatim**. A tracked example lives at
[`profiles/example.json`](../profiles/example.json); copy it and edit the numbers, or generate
one from data as below.

### Where to get the data

You distil a profile from an AIS *export* or *capture* — you never hand-write the statistics:

- A **public AIS dataset** — for example the Marine Cadastre historical AIS export — gives you a
  tabular CSV of positions and ship types you can reduce to statistics.
- **Your own receiver or log capture** — an AIVDM/AIVDO capture (for example one saved from the
  Maintenance monitor or a bench AIS receiver) works equally well.

Both are input to the same tool; the result is statistics-only.

### Distilling a profile: `python -m nmea_sim.aisprofile`

The `nmea_sim.aisprofile` tool reads a file (or a directory of files) and writes a profile JSON
that `RealismProfile.from_dict` accepts. It handles **both** input shapes and sniffs the format
from the first non-empty line (a line beginning with `!AIVDM`/`!AIVDO` is treated as a capture,
anything else as tabular CSV):

```bash
# Tabular CSV export (map the logical fields to your export's column names):
python -m nmea_sim.aisprofile <export_dir_or_file> --out profiles/<you>.local.json \
    --format csv \
    --columns lat=Latitude lon=Longitude sog=SOG ship_type=VesselType \
    --motion-model transiting

# AIVDM/AIVDO capture from your own receiver or the Maintenance monitor:
python -m nmea_sim.aisprofile <capture.nmea> --out profiles/<you>.local.json --format aivdm

# Let the tool sniff the format (the default):
python -m nmea_sim.aisprofile <input> --out profiles/<you>.local.json
```

Optional `--min-lat/--max-lat/--min-lon/--max-lon` bounds crop the region before the statistics
are computed. The output is pretty-printed JSON you can inspect and edit by hand.

> **Local-only:** the repository ignores `profiles/*.local.json`, so a profile you generate from
> real data stays on your host and is never committed. Only the synthetic `example.json` is
> tracked.

### Selecting the profile in the UI

On the **Config** tab, the **AIS Traffic** group turns synthetic contacts on and points the
channel at a profile file (`profile_path`). Leave it empty to use the built-in region-neutral
profile, or set it to your generated `profiles/<you>.local.json`. An optional target-count
override lets you scale the contact density without editing the profile.

### Privacy

Profiles are **statistics-only**. They record distributions — how many contacts, the ship-type
mix, per-category speed spreads, a bounding box — and **nothing real is rebroadcast**. Contact
**MMSIs are synthetic** (generated per run), positions are freshly sampled inside the region, and
no name, identity, or captured line from the source data ever reaches the wire. The tool exists
precisely so you can share the *shape* of an area's traffic without sharing anyone's data.

### Own-ship is always simulated

Whatever the profile does, **own-ship is always simulated** from vessel state — the profile only
governs the *surrounding contacts*. Own-ship position, course, speed, and its own AIS position
report always come from the engine, never from a profile.

### Replay scope: full vs ais-only

Replay mode (see *Operating Modes*) has a **scope selector** that decides how much of a capture
drives the run:

- **`full`** (the default) treats the capture as the entire source of truth — **own-ship and AIS
  are both replayed** and every generator is suppressed. Use this only when the capture
  **includes own-ship nav** (its own GPS/heading/position sentences).
- **`ais-only`** replays just the AIS contacts while **own-ship is simulated** from config/route
  (the GPS and heading channels generate own-ship nav and physics owns own-ship position). Use
  this for a contacts-only source that has no own-ship track.

For a **contacts-only** source — a public dataset or a receiver capture of other vessels with no
own-ship nav — use `ais-only` (or skip replay entirely and drive the contacts from a realism
profile). Reserve `full` for a capture you recorded yourself that already carries own-ship.

## Reading the Indicators

Wherever a **channel** is shown, its provenance is labelled with **both colour and text**:

- **green + "LIVE"** — a real sensor on a live input is the source.
- **amber + "SIM"** — the channel is generating from vessel state.
- **grey + "OFF"** — the channel is disabled.

On the conning tab the same vocabulary applies **per panel**, reflecting where that panel's values
came from — see *The Conning Display* above. Individual readouts are not badged separately yet.

Colour is never the only signal, so the readout is colourblind-safe. The Conning tab also shows
a **time-source label** reflecting the active Time Authority tier — GPS, SAT, NTP (local
disciplined clock), or system — so you always know where UTC is coming from.

The Time panel's **pill** and that **label** answer different questions, deliberately. The label
names the tier supplying UTC; the pill says whether that tier is a *sensor on the wire*. Only **GPS**
and **SAT** read LIVE. **NTP reads SIM** — it is real, externally disciplined time, but it comes from
the local clock rather than a vessel sensor, so it is held to the same standard as every other value
on the tab. A box with no GNSS attached therefore shows `Time source NTP` with a **SIM** pill, and
that pairing is correct, not a fault.

## Maintenance and Troubleshooting

The Maintenance tab (and its CLI twin, below) is a bench NMEA diagnostic surface. It is
**read-only by default**: it only listens. Any transmit or reconfigure action is **opt-in,
individually confirmed, and restricted to a non-operational port** — you cannot accidentally
disturb a live output.

### Per-port live diagnostics

The monitor polls `GET /api/diag` for rolling **per-port statistics**: the **checksum-error rate**, a
**talker / sentence inventory**, the **sentence rate**, and the **bus load**, plus the fault verdict. A
full **multi-port raw + hex line view** (millisecond timestamps, per-line checksum colouring, filter, and
pause) is a **planned** enhancement — it will ride its own sampled diagnostics stream rather than the
conning `state`/`nmea` stream — and is not yet shipped.

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

**It is a single-line decoder**, so a **multi-fragment AIS message will not decode here** — that means
every AIS **static** report: Type 5 (Class A) and Type 24 (Class B). You get
`"single line only — multi-fragment AIVDM needs the full fragment list"` instead. Single-fragment
position reports (Type 1 / Type 18) decode normally. The same limit applies to
`nmea_sim.cli_monitor --decode`, since both call `diagnostics.decode_line`.

To decode a static report, capture it and hand the fragments to `pyais` together:

```bash
nc <host> <tap_port> | grep -m40 '^!AIV' > /tmp/ais.nmea
python -c "
from pyais.stream import FileReaderStream
for m in FileReaderStream('/tmp/ais.nmea'): print(m.decode())
"
```

### Cheat-sheets

Built-in reference panels for quick bench work:

- **Baud / sentence** — common rates and which sentences ride them.
- **Wiring / pinout** — how to land the differential pair and ground.
- **Talker ID** — the two-letter talker prefixes and what they mean.

### Optional ADC voltage tiles (planned)

The design reserves an optional **pluggable ADC voltage sensor** path (off by default — a
`VoltageProvider` add-on, e.g. an ADS1115 over I2C behind a protective analog front-end) that would show
per-line, differential, and common-mode **voltage tiles** to positively confirm a reversed A/B pair from
the **differential idle sign** rather than inferring it from checksum failures alone. The config block,
driver, and tests exist, **but nothing wires a provider yet**: `/api/diag` exposes no voltage data and the
Maintenance tab currently renders a **"voltage sensing not installed"** chip. Treat this as planned, not
shipped.

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

### Changing the web password

You **can** change the web password from the browser: the Security tab has a **"Change web password"**
card, and on first login a banner prompts you to change the auto-generated setup password (with
**"Change now"** and **"I've already changed it"** buttons). Enter the new password (**minimum 12
characters**) and submit. After you submit, **the page will drop and re-prompt you to sign in with the new
password within a few seconds** — that is expected: the reverse proxy restarts to pick up the new hash, so
your current connection is dropped and the browser asks you to re-authenticate. Sign in with the new
password. If the change fails, the prior password still works and the banner stays up.

The card never displays a secret — it only takes a *new* password. Under the hood the app hashes it
locally (via `caddy hash-password`) and hands only the resulting **bcrypt hash** to a privileged host
service that swaps it in and restarts the proxy; the **new password crosses the authenticated wire only
once, and only a hash is ever stored** — the plaintext is discarded immediately after hashing.

**Host CLI fallback:** you can still rotate the password on the host instead — run `caddy hash-password`,
update `MOCKINGBUOY_BASIC_HASH` in the service env (`secrets/`, now root-owned), and `systemctl restart
caddy`.

Underneath, the service runs with systemd sandboxing (`NoNewPrivileges`, `ProtectSystem=strict`, an empty
`CapabilityBoundingSet`, explicit `DeviceAllow` rules, a `SystemCallFilter`), and dependencies
are hash-locked with an offline wheelhouse for rebuild-free redeploy.

## Under the Hood: the HTTP Surface

You never need these to operate the UI, but scripted clients and `mockingbuoy-mon` (attach mode) use
them. The live stream is `GET /api/stream` (SSE: `nmea` / `health` / `state` events). Everything the tabs
do maps to a small endpoint set:

- **`POST /api/control`** — the Config-tab actions: `start` / `stop`, `update` (vessel params),
  `channel` (per-channel enable), `route` (route-playback control — `start` / `pause` / `reset` the route
  cursor), and `fault` (GPS-fault injection, simulate-only).
- **`POST /api/config/initial-state`** — *Save as defaults* (allow-listed → `data/config.local.json`).
- **`GET /api/inputs`** — the sensed/assigned input slots and their function.
- **`GET /api/security`** — the Security tab's posture booleans (never a secret).
- **`GET /api/diag`** + **`POST /api/diag/decode`** — the Maintenance snapshot and click-to-decode;
  the transmit-capable diagnostics (`/api/diag/baud-sweep` \| `send` \| `loopback` \| `capture`) are
  **gated** (opt-in, confirmed, non-operational port only).

Full detail (SSE events, the bridge, layering) → [architecture.md](ref/architecture.md).
