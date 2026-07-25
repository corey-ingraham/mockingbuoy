# Design Decisions

An ADR-style decision log. Its purpose is to recover the **why** behind mockingbuoy —
the reasoning, constraints, and rejected alternatives that the code itself cannot record. Read it
when a design looks surprising, before you change something load-bearing, or when a future feature
must respect an existing invariant. It is a living document: append a new entry when a decision lands,
never rewrite history. Each entry states a **Decision**, its **Context**, the **Why**, the
**Alternatives rejected**, and a **Date** (`prior` for foundational decisions predating this log).

---

## Native systemd, no Docker

**Decision** Run as a native systemd service in a Python venv, not a container.

**Context** The host is a small single-purpose Linux box (often a Raspberry Pi) whose entire job is
to drive physical serial adapters and serve one LAN web UI. Deployment must survive a full rebuild
from a checkout with no runtime internet dependency.

**Why** systemd already owns process supervision, restart-on-failure, boot ordering, journald logging,
device access (`DeviceAllow`), and the sandboxing primitives we rely on. Serial hardware passthrough
and hotplug are first-class on the host and awkward through a container boundary. A venv plus a local
wheelhouse rebuilds deterministically offline; a container image adds a registry dependency, a larger
attack surface, and an extra abstraction over the very devices we exist to talk to.

**Alternatives rejected** Docker/Podman (device passthrough friction, image-distribution dependency,
no benefit for a single-tenant appliance); a bare `nohup`/init script (loses supervision, sandboxing,
and structured logging that systemd gives for free).

**Date** prior

---

## Loopback/unix-socket app behind native Caddy with an internal CA

**Decision** Bind the application only to loopback (or a unix socket) and put a natively-installed
Caddy reverse proxy in front, terminating TLS with `tls internal` — a Caddy-managed local certificate
authority.

**Context** The UI carries vessel-control authority over the LAN. It must be encrypted and
authenticated, but the deployment has no public DNS and no runtime internet dependency, so a public
ACME certificate is not an option.

**Why** The app never listens on a routable interface, so it cannot be reached except through the
proxy — TLS termination, auth, and security headers are enforced in exactly one place. `tls internal`
mints a host-local CA and leaf certificate at install time; operators trust the printed CA file once
per client and get real TLS with zero external dependency. Caddy is a single static binary with a
small, auditable configuration.

**Alternatives rejected** Serving the app directly on the LAN with self-signed TLS in application code
(scatters TLS/auth/header logic into the app, weaker crypto hygiene); a public ACME certificate
(requires public DNS and outbound internet the deployment deliberately does not have); nginx (heavier
configuration surface for no gain here).

**Date** prior

---

## HTTP Basic auth at the proxy

**Decision** Authenticate every request with HTTP Basic auth enforced by Caddy, in front of the app.

**Context** A single trusted operator (occasionally a few) on a LAN needs to gate all control actions.
There is no user directory, no multi-tenant model, and no external identity provider on an offline host.

**Why** Over TLS, Basic auth is simple, universally supported by browsers and tools (`nc`, `curl`,
OpenCPN companions), and adds no session state or login flow to maintain. Enforcing it at the proxy
means the app receives only already-authenticated requests and never handles credentials itself. The
password is a bcrypt hash rotated at the host, so the plaintext lives nowhere on disk.

**Alternatives rejected** App-level sessions/cookies/JWT (state and login UI for a single operator, and
moves auth into the app we want to keep credential-free); OAuth/OIDC/an identity provider (absurd for
an offline single-tenant appliance); no auth on a "trusted" LAN (the UI has control authority; an
unauthenticated control plane is unacceptable even on a private segment).

**Date** prior

---

## systemd sandboxing

**Decision** Confine the service with systemd hardening directives: `NoNewPrivileges`,
`ProtectSystem=strict`, an empty `CapabilityBoundingSet`, an explicit `DeviceAllow` allowlist for the
serial devices, and a `SystemCallFilter`.

**Context** The process handles untrusted bytes from external hardware on RX ports and is reachable
(behind the proxy) over the LAN. A compromise or a dependency defect should be contained.

**Why** Defense in depth around a service that needs almost no host privilege: it reads/writes a
handful of serial devices and a bind socket, nothing else. `ProtectSystem=strict` makes the filesystem
read-only outside declared write paths; an empty capability set drops every Linux capability;
`DeviceAllow` narrows device access to exactly the adapters in use; `SystemCallFilter` shrinks the
kernel attack surface; `NoNewPrivileges` blocks privilege escalation via setuid. These cost nothing at
runtime and dramatically reduce blast radius.

**Alternatives rejected** Running unconfined (needless privilege for a device-and-socket workload);
dropping privilege only via a non-root user (weaker — leaves the full syscall surface, capabilities,
and writable filesystem exposed).

**Date** prior

---

## Read-only, LAN-bound TCP taps

**Decision** Expose each channel as a plain TCP stream that is transmit-only to subscribers and bound
to the LAN subnet, with a subscriber cap.

**Context** Chart plotters, loggers, and OpenCPN consume raw NMEA over TCP. They need to read a
channel's sentences; they must never be able to inject into the bus or reconfigure anything.

**Why** A tap is a pure fan-out of the same sentences already on the wire — read-only by construction,
so a subscriber cannot corrupt vessel state or drive hardware. Binding to the configured subnet keeps
it off any other interface; a subscriber cap bounds resource use and prevents a slow or hostile reader
from exhausting the host. It is deliberately unauthenticated because it carries no authority and no
secret — only the same sentences a physical listener on the bus would see.

**Alternatives rejected** Bidirectional TCP (would let a network client inject sentences — a control
path we refuse to open); routing taps through the authenticated proxy (needless friction for read-only
telemetry that plotters expect as a bare TCP socket); binding to all interfaces (exposes the stream
beyond the intended segment).

**Date** prior

---

## Hash-locked dependencies and an offline wheelhouse

**Decision** Pin every dependency to a hash-locked requirement set and vendor a local wheelhouse for
offline rebuilds.

**Context** The appliance must rebuild deterministically after a disaster and has no runtime internet
dependency. Supply-chain integrity matters for a service that parses untrusted bytes.

**Why** Hash-locked pins make an install reproducible and detect a tampered or substituted artifact at
install time. The wheelhouse lets `setup.sh` recreate the venv with no package index reachable, which
is both a disaster-recovery posture and a hard guarantee that no build-time network fetch can change
what ships. Together they make "what runs" a fixed, auditable set.

**Alternatives rejected** Unpinned or version-range dependencies (non-reproducible builds, silent
drift, no tamper detection); pinned versions without hashes (still trusts the index to serve the same
bytes); assuming an index is always reachable at rebuild time (breaks the offline/DR guarantee).

**Date** prior

---

## Fail-loud engine

**Decision** Ordinary engine paths do not repair malformed payloads or paper over invalid state — they
error.

**Context** The engine synthesizes and forwards navigation and safety-relevant sentences. Silent
"best effort" correction of bad input or bad configuration produces plausible-looking wrong output,
which is worse than a visible failure in this domain.

**Why** A simulator whose defects are silent teaches consumers the wrong thing and hides real bugs. By
failing loudly — rejecting an invalid config, refusing to emit from inconsistent state — defects
surface at the boundary where they are cheap to diagnose, and every sentence that *does* go out is
trustworthy. Tolerance is reserved for explicitly designed seams (tolerant lazy port `open()`, RX
whitelists), not sprinkled through the core.

**Alternatives rejected** Defensive coercion/auto-repair of malformed payloads (masks bugs, emits
confidently wrong NMEA); broad try/except that swallows and continues (turns a diagnosable fault into a
mysterious wrong reading downstream).

**Date** prior

---

## Generic channel model

**Decision** Model every output as an instance of one generic channel type configured by capability;
`gps`, `heading`, and `ais` are simply three configured instances, not distinct classes.

**Context** The system drives N heterogeneous serial adapters, and the set of roles grows (this
expansion adds an `instrument` channel). Hard-coding one class per role does not scale and duplicates
logic.

**Why** A channel is defined by its electrical/logical capabilities — direction, baud, framing, talker,
emitted sentence set, RX behavior — not by a brand or role name. One model means new roles are
configuration, not code; the baud budget, RX whitelist, hotplug, and failure-isolation logic are
written once and apply uniformly. There is deliberately no `device_type`/brand field.

**Alternatives rejected** A class per device role (duplication, and every new role touches core code);
a brand/model field driving behavior (couples the engine to vendor specifics it should never know).

**Date** prior

---

## Tabbed web UI (five tabs)

**Decision** Organize the UI as five tabs — Conning, NMEA Streams, Config, Maintenance, Security — over
the shared engine state and stream.

**Context** The UI now spans four distinct jobs: watch synthesized instruments, watch raw sentences,
configure the vessel and channels, diagnose a real bench bus, and inspect security posture. One flat
page cannot hold these without becoming unreadable.

**Why** Each tab is a coherent task surface with its own audience and update cadence: Conning is a
glanceable instrument display, NMEA Streams is per-channel sentence monitoring, Config is the control
form, Maintenance is a bench diagnostic workbench, Security is a read-only posture panel. Separating
them keeps each view focused and lets the layout of one evolve without disturbing the others, while all
five read the same single source of truth.

**Alternatives rejected** A single scrolling page (overwhelming, mixes read-only telemetry with
destructive controls); separate apps per job (fractures the shared state and auth, multiplies
deployment surface).

**Date** 2026-07-24

---

## Instrument channel and true→apparent wind

**Decision** Add an optional `instrument` channel (talker `II`) that emits the motion/environment
sentence set — `VHW`, `DPT`, `DBT`, `MWV` (apparent), `MWD` (true), `ROT`, `XDR`, `RSA`, `VDR`, `$PASHR`
— and derive **apparent** wind from **true** wind plus vessel motion rather than storing both
independently. `THS` (true heading + status) rides the **heading** channel (talker `HE`) alongside
HDT/HDG/HDM, not the instrument channel — it is a heading sentence.

**Context** A realistic bench source needs speed-through-water, depth, rate-of-turn, wind, rudder
angle, and set/drift, not just position and heading. Wind in particular has a true and an apparent
form that are physically linked.

**Why** A dedicated `II` channel keeps instrument data on its own configured output with the correct
talker, cleanly separated from GPS/heading/AIS while reusing the generic channel model. True wind is
the physical driver stored in vessel state; apparent wind is a derived quantity — the vector sum of
true wind and the vessel's own motion. Deriving apparent from true guarantees the two can never
disagree, exactly as `cog` and `heading` are kept independent but consistent elsewhere.

**Alternatives rejected** Storing true and apparent wind as independent inputs (they would drift out of
physical agreement and mislead consumers); omitting the realism sentences (a less convincing bench
source for systems that expect a full instrument suite).

**Date** 2026-07-24

---

## Sea-state-driven pitch and roll

**Decision** Derive pitch and roll from a user-selectable WMO sea-state motion model (scale 0–9) so the
hull is always gently in motion — even at sea state 0 — rather than sitting perfectly still.

**Context** Inclinometer, `XDR`, and `$PASHR` consumers expect a live, moving attitude signal. A dead-
flat, unchanging roll/pitch reads as a frozen or failed sensor to the systems under test.

**Why** Real hulls never hold perfectly still; a small residual motion at calm sea states is more
realistic and exercises consumer smoothing/alarm logic that a constant value never would. A single
sea-state selector scales the whole motion model coherently — amplitude and period grow together with
the WMO scale — so one control drives believable, physically-plausible attitude across the range.

**Alternatives rejected** A static/zero attitude (looks like a stuck sensor, exercises nothing);
independent raw pitch/roll inputs (no coherent physical relationship to sea state, tedious to drive);
a full spectral wave model (far more complexity than a bench simulator needs).

**Date** 2026-07-24

---

## Per-channel runtime toggle with persisted default

**Decision** Every channel has a runtime on/off toggle that takes effect without an engine restart,
plus a separately persisted per-channel default enable state.

**Context** Operators need to silence or enable a single channel mid-session — to isolate a device
under test or quiet a bus — without disturbing the other channels or the synchronized vessel state.

**Why** The engine's per-channel sender isolation already makes a single channel's start/stop
independent, so a live toggle is cheap and disrupts nothing else. Separating the live toggle from the
persisted default lets an operator experiment transiently while the boot-time posture stays whatever
was deliberately saved — a temporary silence does not silently become the new default.

**Alternatives rejected** Requiring a restart to change channel state (interrupts every other channel
and the shared state for a per-channel change); conflating live state with the saved default (a
transient toggle would quietly rewrite boot behavior).

**Date** 2026-07-24

---

## Three operating modes: Simulate, Auto (priority-routed passthrough), and Replay

**Decision** Offer three modes (`mode` in `{simulate, auto, replay}`). **Simulate** is always-synthetic
per channel. **Auto** senses a real NMEA input on each channel: valid live NMEA passes through verbatim
to the matching output and updates the web display; on loss the channel seamlessly falls back to
simulating. Auto is a **priority-routed passthrough** — input lines are classified by sentence class
(`gnss`/`heading`/`ais`) and routed to the correct output per a per-output source-priority list, else the
channel simulates. **Replay** re-injects a captured NMEA file back through the **same single-writer worker
path** the live modes use, so a recorded session drives the outputs and the web display identically.

**Context** The tool is used both as a pure generator (bench, demo, no real sensors) and as an
inline realism aid alongside real electronics, where genuine sensor data should pass through untouched
and synthetic data should fill only the gaps — and, separately, to reproduce a previously-captured
session deterministically for regression and demo.

**Why** Passing valid live NMEA through **verbatim** means a real sensor's exact bytes reach the
consumer — the simulator never becomes a lossy transcoder of real data. Classifying by sentence class
and routing per an explicit priority list lets one physical input feed the correct logical output (and
supports cross-routing, below) while a clean fallback to simulation keeps every output alive across a
sensor dropout. Simulate mode stays a simple, fully-synthetic path for the no-hardware case. Replay reuses
the **one** writer path rather than a second emit route, so a captured file is indistinguishable
downstream from a live source and there is no parallel code path to drift — a recording is just another
line source feeding the single writer.

**Alternatives rejected** A single mode that always simulates (cannot pass real data through); blind
input→output patching without sentence-class routing (a mixed-content input could not fan its sentence
classes to their correct outputs); dropping outputs on sensor loss instead of falling back (consumers
would see a dead bus); a separate playback engine for replay (a divergent second emit path that could
drift from live behavior — reusing the single writer keeps replay byte-faithful).

**Date** 2026-07-24

---

## Sentence-class cross-routing

**Decision** In Auto, route by sentence class rather than by physical port, so one input can feed
multiple logical outputs — e.g. a satellite compass's position/time sentences cross-route to the GPS
output while its heading sentences go to the heading output.

**Context** Real marine sources are not one-role-per-wire. A satellite compass emits both a heading
solution and a position/time fix on one input; a combined GNSS unit likewise mixes classes.

**Why** Classifying each inbound line (`gnss`/`heading`/`ais`) and dispatching it to the output that
owns that class, per a source-priority list, models how the data actually flows: the right sentences
reach the right consumers regardless of which physical port they arrived on. This is what makes the GPS
position/time priority chain below possible.

**Alternatives rejected** Fixed port-to-output mapping (cannot split a multi-class source across its
correct outputs); parsing and re-emitting instead of routing verbatim lines (loses the exact original
bytes and adds a transcode step).

**Date** 2026-07-24

---

## Single-source GPS position and unified Time Authority

**Decision** The active GNSS source supplies **both** position and time to the GPS output from **one**
source, on a priority chain: GPS input → SAT-compass input → simulated (for position/time), unified
with time authority GPS → SAT → NTP → system. The program **always** provides a `ZDA` on the GPS
output; when a priority-1/2 source omits `ZDA`, it is synthesized from that **same** source's `RMC`
(identical time, add-only). The program never emits a divergent `ZDA`/`RMC` pair on one bus. In
passthrough it does not inject its own NTP-derived `ZDA`. Here "NTP" means reading the locally
chrony-disciplined system clock — no network query — so it adds no latency.

**Context** Position and time both reach a consumer on the GPS bus, potentially from `RMC`, `GGA`, and
`ZDA`. If those come from different sources they can silently disagree on the timestamp.

**Why** The load-bearing evidence: **no marine device cross-validates `ZDA` against `RMC`/`GGA`.** A
divergent time pair on one bus therefore fails *silently* — clock jitter, wrong timestamps, and
source-flapping downstream, with nothing to flag it. A single consistent source for both position and
time is the recognized-safe pattern. So we bind position and time to one active source, and when that
source lacks `ZDA` we synthesize it *from that source's own `RMC`* (same time, add-only) rather than
minting an independent time — guaranteeing the pair always agrees. In passthrough we stay out of the
way entirely and do not inject our own clock. Reading the chrony-disciplined system clock (not a
network query) keeps the "NTP" tier local and latency-free, consistent with no runtime internet
dependency.

**Alternatives rejected** Independent position and time sources (the exact divergent-pair failure the
whole decision exists to prevent); minting `ZDA` from the local clock even in passthrough (injects a
time that can disagree with the passed-through `RMC`); omitting `ZDA` when a source lacks it (leaves
time consumers without a dedicated sentence).

**Date** 2026-07-24

---

## Colourblind-safe LIVE/SIM/OFF tagging

**Decision** Tag every displayed value with its provenance — `LIVE` (green, real sensor), `SIM` (amber,
simulated), `OFF` (grey) — using **both** colour and text, never colour alone.

**Context** In Auto mode a single reading may be real one moment and simulated the next. An operator
must be able to tell, at a glance, which values are genuine sensor data and which are synthetic —
including operators with colour vision deficiency.

**Why** Provenance is safety-relevant here: acting on a simulated value as though it were live is the
mistake this tag exists to prevent. Pairing an explicit text label with the colour makes the state
unambiguous for everyone and satisfies accessibility — colour is redundant reinforcement, never the
sole signal. The same three-state vocabulary is reused on the stream badges for consistency.

**Alternatives rejected** Colour-only status (fails for colourblind operators and in poor lighting);
text-only (loses the fast pre-attentive scan colour gives); a binary live/sim without an explicit
`OFF` (cannot distinguish a disabled channel from a simulated one).

**Date** 2026-07-24

---

## Maintenance diagnostics tab

**Decision** Provide a Maintenance tab that is a bench NMEA diagnostic surface: a multi-port live
monitor (4–6 ports) with raw+hex, millisecond timestamps, per-line checksum colouring, filter, and
pause; per-port checksum-error rate, talker/sentence inventory, rate and bus-load; an auto-baud sweep;
click-to-decode; and a **guided fault advisor** that classifies the byte stream and advises likely
cause and fix (for example, a reversed A/B data pair). It is read-only by default; any
transmit/reconfigure action is opt-in, confirmed, and restricted to a non-operational port.

**Context** Bench-testing real NMEA hardware is otherwise a scatter of terminal tools, and the hardest
faults (wrong baud, reversed differential pair, marginal wiring) present as garbled bytes that no
common tool interprets. This is the deliberate differentiator: **no existing tool auto-detects a
reversed A/B pair**, so an *inferring* advisor is novel.

**Why** Consolidating the whole diagnostic workflow — monitor, classify, decode, sweep baud, advise —
into one surface built on the same diagnostics core turns guesswork into a guided procedure. The fault
advisor infers from stream characteristics (checksum-error patterns, framing, idle-line behavior) the
likely physical cause and the fix, which is exactly the expertise a field tech otherwise has to carry
in their head. Read-only-by-default protects a live bus from an accidental transmit; making any write
opt-in, confirmed, and limited to a non-operational port keeps a diagnostic tool from becoming a
hazard.

**Alternatives rejected** Leaving diagnostics to external terminal tools (no correlation, no
inference, no bus-load/error-rate context); a monitor without the advisor (shows the symptom but not
the cause — the reversed-A/B insight is the whole point); allowing writes to an operational port
(turns a diagnostic action into a live-bus hazard).

**Date** 2026-07-24

---

## Optional pluggable ADC voltage sensing

**Decision** Support an optional `VoltageProvider` add-on — off by default — that, when hardware is
present, reads per-line, differential, and common-mode voltages (for example, an ADS1115 over I2C
behind a protective analog front-end) and surfaces them as Maintenance tiles.

**Context** Some faults are electrical, not logical. The stream-based advisor *infers* a reversed A/B
pair; direct voltage measurement can *confirm* it, but most deployments have no ADC wired.

**Why** A pluggable provider keeps the electrical-sensing capability entirely optional: with no
hardware the feature is simply absent, adding nothing to the default footprint or attack surface. When
present, the differential idle sign positively confirms a reversed A/B pair that the byte-stream
advisor could only infer, and common-mode/per-line readings catch wiring and ground faults the logical
layer cannot see. The protective analog front-end keeps bus voltages from reaching the ADC directly.

**Alternatives rejected** Baking voltage sensing in as a hard dependency (forces hardware most
deployments lack); relying on stream inference alone (can suggest but never confirm an electrical
fault); reading the bus into an ADC without a protective front-end (risks the sensor and the host).

**Date** 2026-07-24

---

## mockingbuoy-mon: a web-free peer frontend

**Decision** Ship a CLI diagnostic monitor, `mockingbuoy-mon`, as a first-class peer frontend to the
same diagnostics core — no web server involved. It has a **standalone** mode that opens a serial port
directly (including baud sweep) and an **attach** mode that is a thin client to the running service's
local stream/diag endpoints. Renderers: a curses TUI, `--plain`, `--json`, `--decode`, `--baud-sweep`.
Read-only by default.

**Context** Field, headless, SSH, and disaster-recovery use often has no browser and sometimes no
running service — but still needs the same monitoring, decoding, and baud-sweep diagnostics the
Maintenance tab offers.

**Why** Because the diagnostics core is independent of the web layer (the strict one-way layering), a
terminal frontend can reuse it wholesale. Standalone mode works with nothing else running — ideal for
DR and bare-bench triage — while attach mode observes the live service without contending for its
ports. Multiple renderers cover interactive use (curses), scripting (`--json`), log capture
(`--plain`), and targeted tasks (`--decode`, `--baud-sweep`). Read-only-by-default carries the same
safety posture as the web tab.

**Alternatives rejected** A web-only diagnostic surface (useless on a headless host or during DR with
no browser); a separate diagnostics implementation for the CLI (duplication and drift from the web
tab); an attach-only client (cannot help when the service itself is down).

**Date** 2026-07-24

---

## Read-only Security tab with the Caddy admin API disabled

**Decision** Provide a Security tab that is a strictly read-only posture panel — it never renders a
secret, reporting each protection by **presence**, not value — and disable Caddy's admin API.

**Context** An operator needs to confirm the service is actually hardened — TLS active, which auth
layers are on, loopback-only bind, which TCP-tap ports are open, subscriber count against cap, uptime,
security headers — without that confirmation panel itself becoming an exposure or a control surface.

**Why** A posture panel that displayed secret values would defeat its own purpose; reporting only
presence ("TLS: active", "Basic auth: enabled") gives the operator assurance with nothing to leak. The
primary login is rotated at the host, not the browser, so the credential never transits the UI at all.
Disabling Caddy's admin API removes a live reconfiguration/control endpoint that would otherwise be an
attack surface on a box whose configuration should only change through a deliberate host-side redeploy.

**Alternatives rejected** Showing secret/credential values for "convenience" (turns the assurance panel
into a leak); allowing credential rotation from the browser (puts the primary secret on the wire and in
the UI); leaving the Caddy admin API enabled (an unnecessary live control endpoint and attack surface).

**Date** 2026-07-24

---

## AIS area realism via statistical profiles (deterministic, privacy-preserving) and a replay scope split

**Decision** Shape surrounding AIS traffic from a **realism profile** — a small JSON of area-neutral
statistics (bounding box, target count, ship-type mix, per-category speed distributions, motion model,
Class A share) distilled from AIS data by a deterministic tool (`nmea_sim.aisprofile`), never
hand-authored and never generated by any external service. Keep generated profiles local
(`profiles/*.local.json` are git-ignored; only a synthetic `example.json` is tracked). Separately,
give replay a **scope selector**: `full` replays own-ship and AIS together (the capture is the whole
source of truth), while `ais-only` replays just the contacts and leaves own-ship simulated.

**Context** A convincing bench source needs the *surrounding* traffic to resemble a real area — its
density, ship-type mix, and speeds — without embedding any specific place in the code, rebroadcasting
anyone's identity, or committing captured data. Sources come in two shapes: tabular public datasets and
AIVDM/AIVDO receiver captures. Separately, some captures carry own-ship nav and some are contacts-only,
and replaying a contacts-only capture as if it owned own-ship would strand the vessel with no position.

**Why** Statistics-only profiles let the *shape* of an area travel while nothing real does: contact
MMSIs are synthesized per run, positions are resampled inside the region, and no name or captured line
reaches the wire — a privacy-preserving seam that keeps every location value out of the code. A
deterministic distiller (a plain statistical reduction, no external service) keeps the pipeline
reproducible, testable, and free of runtime service reliance. Git-ignoring `*.local.json` means real-data profiles never land in
history while a synthetic example still documents the format. The `ais-only` scope keeps own-ship
**always simulated** and physics-owned even when replaying third-party contacts, so a contacts-only
source is usable without a bogus own-ship track; `full` remains available for self-recorded captures.

**Alternatives rejected** Hardcoding a specific area's traffic (bakes a real place into the code and
cannot travel); generating profiles with an external service (adds a runtime dependency,
non-determinism, and a data-egress path); rebroadcasting captured contacts verbatim (leaks real
identities and positions); committing real-data profiles (puts location data in history); a single replay
mode that always owns own-ship (makes contacts-only captures unusable and can strand own-ship).

**Date** 2026-07-24
