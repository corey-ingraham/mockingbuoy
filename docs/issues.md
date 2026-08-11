# Issues

Defect / incident log. Newest first. `ISSUE-NNN` shares one counter with `RM-NNN` in
[roadmap.md](roadmap.md) — next free number across **both** files.

Status ∈ {planned, in-progress, done, deferred}.

> **Provenance.** ISSUE-001..002 are the residue of a whole-repo red-team performed against
> `ca7b284` (branch `feat/tabbed-ui-instruments`). Everything else from that pass — 3 critical,
> 10 high, and 21 medium findings — was remediated across the commits landing `36bf3f6..bd5e37d`
> and re-verified in-tree on 2026-07-27. See [changelog.md](changelog.md#2026-07-27--red-team-triage).

> **Lab session 2026-07-27.** ISSUE-019..022 and [RM-023](roadmap.md#rm-023) come from bench testing
> on the `eemslab` appliance. Host-side counterparts live in the separate
> [eemslab](https://github.com/corey-ingraham/eemslab) repo (RM-007 clock/NTP, ISSUE-008 udev).

---

### ISSUE-034 — `function: "unused"` makes a slot an active-diagnostics TRANSMIT target · planned (2026-08-11)

`engine.targetable_slots` (`nmea_sim/engine.py`) whitelists a slot for send / loopback / baud-sweep
exactly when `function == "unused"` **and** no channel names it in `sources`. The intent — read from
its docstring — is that "everything carrying real traffic is excluded, so a bench action can never
drive a wire the running config depends on". The hole is a wire the *config* does not depend on but
the *world* does: a passive monitor tap landed on live equipment matches the whitelist exactly.

The `SerialPort` receive-only guard does not save it. `write_line` early-returns for
`direction == "rx"`, but the transmit path never touches the input's port object — `web.app._tx_probe`
opens the slot's device path **fresh** with `direction="tx"`, so that guard is not on this path at all.

**Failure:** `POST /api/diag/send` or `/api/diag/loopback` against such a slot writes bytes onto live
equipment. Mitigating factors: a confirm-token echo (the slot id, typed back), a per-slot single-flight
cooldown, and `_reject_non_target` refusing anything operational — so it is deliberate, not a stray
click. It is still a transmit onto a wire the operator believed was read-only.

**Workaround (documented in `ref/security.md`):** give any physically-landed slot a real `function`.
Any non-`unused` value makes `port_is_operational` true and the endpoints refuse with 409.

**Fix options:** (a) require an explicit per-slot `diag_target: true` opt-in rather than inferring
"free" from `function == "unused"`; or (b) refuse when the slot's device path resolves to a device that
exists, since a real node means something is plugged in. (a) is preferred — presence is not consent.

---

### ISSUE-033 — TAG-block prefixed sentences are dropped before routing · planned (2026-08-11)

`checksum.split` requires the line to start with `$` or `!`; anything else raises and `verify` returns
`False`. A sentence carrying an IEC 61162-1 **TAG block** (`\s:GP,c:1234*hh\$GPRMC,...`) starts with
`\`, so it fails verification and is dropped at `serialport._handle_rx_line` — **before** classify, the
router, or `rx_transparent_relay` ever see it. No config setting changes this.

**Failure:** wired in series on a bus whose talkers emit TAG blocks, this program is a black hole for
every tagged sentence, and the symptom is a rising `rx_bad_checksum` counter rather than anything
naming TAG blocks. The failure mode is identical to a wrong baud, which is what it will be mistaken
for.

**Scope note:** the repo already knows about TAG blocks, but only as an offline aspiration —
[RM-016](roadmap.md#rm-016) lists "TAG-block timestamp parsing" for the `aisprofile` distiller, which
reads files, not wires. There is no live-path handling anywhere.

**Fix:** strip a leading TAG block (and verify its own checksum separately) in
`serialport._handle_rx_line` before the sentence checksum test, preserving the original bytes for
verbatim forwarding. Worth doing only if a bench capture actually shows a leading `\`.

---

### ISSUE-029 — Switching to `auto` may silently drop input slots and channel sources · planned (2026-07-28)

**Not root-caused — observed on a deployed host, data restored, save path not yet traced.** Filed so
the evidence is not lost; confirm or refute before acting on it.

Observed: two runtime configs three minutes apart, the earlier one present on the host as
`data/config.local.json.bak-preauto`.

> **Provenance of that file is unknown.** The original note said the app auto-wrote it before the
> mode switch — **no code in this repo writes any `.bak*` file** (repo-wide grep for `preauto` hits
> only this file). It was either produced by hand or by an older deployed build. Do not rely on one
> appearing; capture a `cp` of `data/config.local.json` yourself before reproducing.

| | `bak-preauto` | after the switch |
|---|---|---|
| `mode` | `simulate` | `auto` |
| `inputs` | `gps_in`, `satcompass_in`, `ais_in` | **`gps_in` only** |
| `gps.sources` | `[gps_in, satcompass_in]` | `[gps_in]` |
| `heading.sources` | `[satcompass_in]` | **`[]`** |
| `ais.sources` | `[ais_in]` | **`[]`** |

**Why it matters:** a channel with an empty `sources` list can never pass through live data — it
silently simulates forever, which is exactly the failure the LIVE/SIM tagging exists to make
visible. A dropped slot also has no input pane and no RX toggle, so it cannot be exercised at all.
Nothing errors; the config validates cleanly in the reduced state.

**Leading hypothesis (unverified):** the save path may persist only slots that resolve to a present
device, dropping any whose `path` does not currently exist. All three slots' paths were absent at
the time (adapters unplugged), yet only one survived — which does not fit that theory cleanly, so it
may instead be the mode-switch conversion rather than device binding. A manual edit cannot be ruled
out either.

**To investigate:** trace the `auto` conversion in the Config-tab save path (`/api/config/initial-state`
in `web/app.py`) and check whether `inputs` / `sources` are rebuilt from the posted body rather than
merged into the existing config. Reproduce by defining several slots in `simulate`, switching to
`auto`, and diffing `data/config.local.json` against a copy taken by hand beforehand.

**Related trap:** verifying this with `--validate-only` gives false confidence — see
[ISSUE-030](#issue-030).

---

### ISSUE-030 — `--validate-only` checks the base config, not the runtime override · planned (2026-07-28)

**Confirmed by direct observation.** `python main.py --validate-only` validates `config.json`, but
the app at runtime prefers **`data/config.local.json`** whenever that file exists (the target the web
UI writes on "Save as defaults"); `config.json` is then never loaded. So the documented health check
can report `config.json is valid` while the config actually in force is untouched by that check —
and could be broken.

Confirmed hierarchy (`web/app.py`, `create_app`): explicit `config_path` arg → `MOCKINGBUOY_CONFIG`
env var → `data/config.local.json` if present → `config.json`.

**Failure mode:** an operator edits the live config (or a save path mangles it — see
[ISSUE-029](#issue-029)), runs `--validate-only`, sees a pass, and ships. The check validated a file
the service will not read. On any host where the UI has ever saved defaults, the default invocation
is checking the wrong file.

**Workaround today:** `python main.py --config data/config.local.json --validate-only`.

**Fix:** make `--validate-only` resolve the config by the SAME precedence the app uses, and print the
resolved path it validated (it already prints the filename, which is what makes the mismatch
noticeable once you know to look). `docs/ref/deployment.md` and `CLAUDE.md`'s dev-commands block
should then state which file is checked.

---

### ISSUE-027 — Docs promise per-value LIVE/SIM/OFF tagging that does not exist · done (2026-07-28)

**Resolved by [RM-009](roadmap.md#rm-009).** Provenance is now tracked per state field and the
conning panel pills report it, so the documents no longer overstate the product. Investigation also
found the original framing here was itself too harsh: the conning tab *did* have per-panel pills and
a channel source-chip strip — they were simply derived from the channel's ROLE rather than from who
wrote the value, which is why `pill-attitude` could read LIVE over always-simulated pitch/roll.
Per-readout badges remain outstanding as [RM-028](roadmap.md#rm-028). Original report below.

Same class as [ISSUE-024](#issue-024) (documented mechanisms with no implementation), but
**safety-relevant**: the docs tell an operator they will be warned when a displayed value is
simulated, and no such warning is ever shown.

Two documents assert it as shipped behaviour:

- `docs/user-guide.md:39-44` — "Every value is tagged **LIVE / SIM / OFF** using **both colour and
  text**", with a per-state legend, in the *Conning display* section.
- `docs/user-guide.md:303-308` — "Wherever a value or channel is shown, its provenance is labelled".
- `docs/design-decisions.md:371` — recorded as a made decision: "Tag **every displayed value** with
  its provenance", justified explicitly on safety ("acting on a simulated value as though it were
  live is the mistake this tag exists to prevent").

Verified absent, by reading the whole path rather than inferring:

| Hop | State |
|---|---|
| `web/app.py:243` `_state_to_dict` | flat scalars only — carries no provenance field at all |
| `state` SSE frame → conning paint | consumes those scalars; no per-value badge exists in `app.js` |
| `web/app.py:290` `driven_fields` | the nearest thing, and it is **not** this: config-derived (not live), advisory, and it only greys **Config-tab inputs** — it never reaches the conning display |

`docs/ref/architecture.md:203-204, 251-252` states the truth ("not per value on the `state` event";
"a planned enhancement, not yet shipped"), so the doc set currently **contradicts itself** — which
is how the claim survived: architecture.md was corrected while the user guide was not.

What *is* implemented is **per-channel** provenance: each channel's `OFF` / `LIVE:<input>` / `SIM`
`source` badge on the 1 Hz `health` frame, rendered on the NMEA Streams panes.

**Fix:** the feature itself is [RM-009](roadmap.md#rm-009). Until it lands, the two documents above
must describe what exists (channel-level badges) and mark per-value tagging as planned — done in
this commit, so the register entry stands only for the missing *feature*, not the false claim.

---

### ISSUE-026 — Left conning column overflows silently at `--ui-scale: 1` · planned (2026-07-28)

Found while measuring the [ISSUE-025](#issue-025) sibling; **pre-existing and unrelated to that
change** — confirmed by A/B, the numbers are byte-identical with the off-course/off-track edit
reverted in-page.

At a 1920×1080 shell with `--ui-scale: 1` — the **real kiosk tier**, since 1080 does not match the
`max-height: 1000px` query in `app.css:440` — three left-column panels exceed their box and scroll
silently under `.ins-panel { overflow: auto }`:

| panel | `--ui-scale: 1` | `--ui-scale: 0.82` |
|---|---|---|
| `.p-env` | **59px** | 25px |
| `.p-coords` | 15px | 0 |
| `.p-time` | 15px | 0 |

`74a2c50` reported "zero panels overflowing (bar a 2-4px sub-pixel artifact in Environment)" at
1080p. That measurement appears to have been taken at `--ui-scale: 0.82` — i.e. a *windowed* 1080p
browser (inner height <1000) — and assumed to cover the fullscreen case, where the scale tier is 1
and the content is correspondingly larger. The result is that the display Corey actually runs is the
one case not covered.

This is the exact failure mode `74a2c50` set out to fix (silent clipping, deliberately not masked
with `overflow: hidden`), so it should not be left standing.

**Fix:** re-measure the left column at `--ui-scale: 1` and raise `.p-env` / `.p-coords` / `.p-time`
floors (`app.css:476-480`) to their real content heights, the same way `.p-autopilot` was corrected
to 304px. Confirm on the bridge display, not only in a desktop browser — `74a2c50`'s own lesson is
that the tier you measure at determines whether you see the bug.

---

### ISSUE-025 — The narrow-screen `max-height: none` reset never takes effect · planned (2026-07-28)

`app.css:515-516`, inside `@media (max-width: 1100px)`, intends to uncap every gauge's height when
the layout abandons the one-screen lock and scrolls as a single column:

```css
.ins-panel svg, .env-dial svg, .incl-dial svg,
#cog-arc, #ship-schem, #rudder-arc, #depth-graph, .p-ship #compass.repeater, #rot { max-height: none; }
```

**Media queries add no specificity.** Every selector in that list has an identical-specificity
counterpart declared *later* in the file, so the later declaration wins and the reset is dead:

| selector | reset | later rule that beats it | cap that survives |
|---|---|---|---|
| `.ins-panel svg` | `:515` | `:576` | `20vh` |
| `.env-dial svg` | `:515` | `:641` | `18vh` |
| `.incl-dial svg` | `:515` | `:688` | `24vh` |
| `#rot` | `:516` | `:577` | `10vh` |
| `#depth-graph` | `:516` | `:663` | `26vh` |
| `#ship-schem` | `:516` | `:673` | `100%` |

(`#cog-arc` and `#rudder-arc` are separately dead — no such ids exist in `index.html` any more.)

**Failure:** on a narrow screen the gauges stay vh-capped and letterbox instead of growing to the
now-full-width column — the opposite of the block's stated intent. Invisible because the narrow
layout is still *usable*, just under-sized.

**Consequence already absorbed:** `.ap-ind svg` deliberately carries **no** `max-height` precisely
so it cannot join this club — see the comment at `app.css:740`.

**Fix:** move the reset block after the per-gauge rules (simplest), or raise its specificity, or
fold `max-height` into the per-gauge rules as a custom property the media query overrides. Whichever
is chosen, verify by measuring a gauge below 1100px — the current block gives no signal either way.

---

### ISSUE-024 — Four documented mechanisms do not exist in the code · planned (2026-07-27)

Surfaced while red-teaming [ISSUE-020](#issue-020) and each verified by direct grep, not inference.
These are not stale wording — they describe behaviour an operator or contributor would rely on and
that is simply absent. Grouped because they share a cause: docs written against an intended design
that was never finished.

**1. `StatusMsg` never reaches the web layer.** `docs/ref/architecture.md:196` states threads "push
`StatusMsg` + emitted lines onto a `janus` queue". Only emitted lines and the 1 Hz health snapshot
do. `Engine._status` (`engine.py:1278`, exposed at `:1710`) has **no consumer anywhere** — repo-wide
there is no `.get()` on it and `web/app.py` never imports `StatusMsg`. Producers exist at
`engine.py:1041` (`sink_error`), `:1134` (`replay_error`), `:1655` (`budget_warning`); all three
accumulate to `maxsize=10000` and are then dropped silently by the suppressed `queue.Full`.

**Impact:** `replay_error` — added specifically so replay death stops being invisible ([H6] in the
red-team pass) — is invisible again by this route. Anyone wiring new status through `StatusMsg` is
writing to a dead letter queue.
**Fix:** either consume the queue in the web layer and fan it to SSE, or delete it and route
everything through health. Then correct `architecture.md:196`.

**2. The "service self-exits when unrecoverable" mechanism is not implemented.**
`docs/ref/deployment.md:168`, `docs/ref/testing.md:188-190` (a hardware checklist item), and
`CLAUDE.md:60` all describe the service exiting non-zero so systemd's `Restart=on-failure` recycles
it. Repo-wide, `sys.exit`/`SystemExit` appear only in `nmea_sim/aisprofile/__main__.py` and
`cli_monitor.py` — **never in the engine or web layer**. Nothing self-exits.

**Impact:** the `testing.md` checklist item cannot pass as written; the DR story assumes a recovery
path that does not exist.
**Fix:** implement it or strike the claim from all three files. Note `CLAUDE.md` is **git-ignored**
in this repo, so a correction there never reaches the remote or the appliance.

**3. `/healthz` requires auth, contrary to the docs.** `docs/ref/architecture.md:221` says
"no auth needed by systemd". `web/app.py:1457` is `async def healthz(_: None = Depends(auth))`. The
dependency is a no-op only when both credential env vars are unset — and the appliance sets them via
`secrets/service.env` (`ops/mockingbuoy.service:33`).

**Impact:** the [RM-006](roadmap.md#rm-006) watchdog, or any probe following the docs, gets **401**
and reads it as unhealthy. This would be blamed on the app, not the docs.
**Fix:** decide whether `/healthz` is public (exempt it, it leaks little) or authenticated (fix the
doc and give RM-006 a credential path).

**4. `testing.md` "pull a port → 503" cannot pass.** Per [ISSUE-020](#issue-020), pulling a port
flips no health field at all — `sinks[].down` is unreachable for serial sinks — so `ok` never goes
false and `/healthz` never returns 503. The checklist item is untestable until ISSUE-020 lands, and
should be marked blocked on it rather than silently failing.

---

### ISSUE-022 — Shipped `config.json` has `CHANGE-ME` input slots that look configured · planned (2026-07-27)

Seven device paths in `config.json` are `/dev/serial/by-id/CHANGE-ME-*` (`:29,47,62,84,108,117,126`).
In auto mode the engine opens **one reader per `InputSpec`**, so every leftover placeholder is a
guaranteed failed open — and by [ISSUE-020](#issue-020) each one fails *silently*.

**Fix:** either ship the input list empty, or have `validate()` reject a literal `CHANGE-ME` path with
"placeholder device path — set it or remove the slot". A placeholder that parses clean and fails
silently is the worst of both.

---

### ISSUE-021 — A wrong system clock silently poisons every dated sentence · planned (2026-07-27)

Observed on the appliance: both Pi 5s booted to 1970 (no RTC cell, no reachable NTP) and ran
**two days slow**. `time_source.mode` is `system_utc`, so GGA/RMC/ZDA carried stale UTC — and the
captured NMEA **looked entirely normal**. No error, no warning, no health degradation.

`CLAUDE.md`'s gotcha list claims dated sentences are "gated on time sync (NTP or GPS)". They are not:
`nmea_sim/ntpsync.py` probes `/run/systemd/timesync/synchronized` to **tag provenance only**, and
nothing consumes that to suppress or flag output. Compounding: `setup.sh` installs **chrony**, which
removes `systemd-timesyncd`, so on the shipped appliance the marker never appears at all.

**PROVEN ON THE APPLIANCE (2026-07-27).** After enabling chrony and rebooting, the box's clock is
genuinely correct and the OS knows it — yet the probe still cannot see it:

```
$ ls /run/systemd/timesync/synchronized   ->  No such file or directory
$ timedatectl show -p NTPSynchronized --value  ->  yes
$ chronyc tracking  ->  Stratum 4, System time 0.000001014 seconds fast of NTP time
```

So `ntpsync.py` reports unsynced **forever** on a correctly-synced appliance. The marker is
timesyncd-specific and chrony (which `setup.sh` installs) removes timesyncd entirely.

**Fix:** decide the contract and make code and docs agree — either genuinely gate dated sentences on
a synced clock, or surface an explicit "clock unsynced" warning in `HealthReport` and the UI. Either
way, replace the marker probe with a source that actually works on the shipped appliance:
`timedatectl show -p NTPSynchronized --value` (daemon-agnostic, preferred) or `chronyc tracking`.
Keep the marker as a fallback for timesyncd hosts.

**Host-side prevention** is tracked as eemslab RM-007 (RTC batteries + LAN NTP); this entry is the
**app-side** half. Until either lands: check `timedatectl` before trusting any capture.

---

### ISSUE-020 — Every serial open failure reports as "device absent" · planned (2026-07-27)

`nmea_sim/serialport.py:166` catches `(serial.SerialException, OSError, ValueError)` and collapses all
three to `present = False` + silent backoff-retry. No log line, no error counter. So these are
indistinguishable in every status surface:

- unplugged cable (genuinely absent — the intended case)
- **EACCES** — wrong group on the tty (caused a lost bench hour; see eemslab ISSUE-008, where an
  adapter with no udev rule landed in `plugdev` instead of `dialout`)
- **a programming error** — the `ValueError` of [ISSUE-019](#issue-019) hid behind this for months

**Failure:** a channel reports `sinks: [{"down": false, "errors": 0}]` — reads healthy — while its port
was **never opened**. `down: false` means "no error was counted", not "the port is open".

> ⚠ **The fix originally written here was wrong.** It said *"`PermissionError` and `ValueError` log
> loudly."* An `except PermissionError` clause **can never fire**. Corrected below after two
> independent adversarial reviews (2026-07-27); the first verified the taxonomy against the
> installed pyserial 3.5 source rather than inferring it.

**It is worse than described above.** `sinks[].down` flips only when `sink.writer.write_line()`
*raises* (`engine.py:1022-1030`), and `SerialPort.write_line` **never raises** — it swallows and calls
`_mark_down` (`serialport.py:198-216`). So for a serial sink `down: false / errors: 0` is the
permanent steady state through never-opened, unplugged, **and actively-failing** ports. A port that
opened and then died also stays green; the failure lands in `PortStats.tx_errors`, which no health
surface reads.

**Fix — four parts. Any one omitted makes the others pointless.**

**1. Classify by errno, not by exception class.** pyserial re-raises the underlying `OSError` as
`SerialException`, and `SerialException` subclasses `IOError`/`OSError` — CPython's errno→subclass
mapping applies only to the *exact* `OSError` type, so subclasses keep their class. Verified against
pyserial 3.5:

| Failure | What is actually raised |
|---|---|
| absent device | `SerialException(errno=ENOENT/ENXIO)` — `serialposix.py:325` |
| **EACCES** | `SerialException(errno=EACCES)` — `serialposix.py:325`, **not `PermissionError`** |
| held by another process | `SerialException(errno=EAGAIN)` — `serialposix.py:387`, **not EBUSY** |
| bad baud | `ValueError` — `serialposix.py:438` |
| win32 `exclusive=False` | `ValueError` — `serialwin32.py:475` ([ISSUE-019](#issue-019)) |

So: classify on `exc.errno` *inside* the `SerialException`/`OSError` handler. `ENOENT/ENODEV/ENXIO`
→ quiet absent; `EACCES/EPERM` → loud; `EAGAIN/EWOULDBLOCK/EBUSY` → loud "port held by another
process". Also mind clause order — `except OSError` placed before `except serial.SerialException`
silently absorbs everything, and ruff will not flag it.

**Windows degrades:** `serialwin32.py:64` builds a single-arg `SerialException` with `errno is None`
for both absent and access-denied, so errno classification is POSIX-only. Do not claim otherwise.

**2. Report, never throw.** `_Sink.down` is a **one-way latch** — set at `engine.py:1029` and reset
nowhere — and `_fan_out` skips down sinks permanently (`:1023-1025`). Routing open-errors by making
`write_line` raise would kill the sink on first EACCES and destroy the self-heal contract that
`tests/test_serialport.py:239-264` pins.

**3. Thread the state to a pixel.** The truthful primitive already exists — `SerialPort.present`,
true only after a successful constructor — and reaches no status surface. Every hop must change or
the state is invisible:

`serialport.py` classify + store → expose via a locked accessor → `SinkHealth` (`engine.py:351`) →
`_ChannelWorker.health()` (`:1062`) → `Engine.input_status()` (`:1728`, a **separate** surface for
auto-mode readers, easy to miss) → `_health_to_dict` (`web/app.py:369`, strips anything not listed) →
SSE health frame → `app.js:1360` sink dot and `:1289` `deriveAlerts`, both of which key on
`s.down === true` today.

**Do not use `StatusMsg`.** `Engine._status` (`engine.py:1278`, exposed `:1710`) has **no consumer** —
nothing calls `.get()`, `web/app.py` never imports it, and messages drop silently at 10,000. The 1 Hz
health snapshot is the only engine→UI status path that works. (See [ISSUE-024](#issue-024).)

**4. Three states, not two.** A `CHANGE-ME` placeholder fails ENOENT, i.e. the *quiet* class — so it
will not scream, but it also stays indistinguishable from an unplugged real device, which is
[ISSUE-022](#issue-022)'s complaint. Needed:

- **unconfigured** — `validate()` rejects the literal `CHANGE-ME` (the ISSUE-022 fix); grey, never red
- **absent** — ENOENT on a real path; amber "waiting for device", hotplug is a supported flow
- **error** — EACCES/EAGAIN/`ValueError`; red + repr. Only this one is an operator-action alarm

**Two decisions the implementation must make explicitly:**

- **Does `open_error` fold into `HealthReport.ok`?** Recommended **no**. Nothing consumes `ok` today,
  but [RM-006](roadmap.md#rm-006) plans to gate an sd_notify watchdog on it — fold open-errors in and
  auto mode with placeholder paths yields `ok=false` forever → watchdog starves → `Restart=on-failure`
  → a restart loop that can never fix an EACCES. Keep `ok` = thread liveness; surface port state
  separately, red in the UI, ignored by the watchdog.
- **Log on transition only.** Backoff caps at 5 s (`serialport.py:41-42`), so a permanent failure is
  ~720 lines/hour/port — ~5,000/hour across 7 ports, onto an SD card. Log once on first failure, once
  on class change, once on recovery; reset alongside `_backoff` (`:175`). Note `nmea_sim/` imports
  `logging` nowhere today, so this introduces the engine layer's first logger.

**Constraints that must survive:** retries must continue after `open_error` (an EACCES is fixable
live via `chmod`/udev reload and must recover exactly like a replug); `_open` holds `self._lock` for
its whole body (`:154`), so do not fire callbacks or queue puts from inside it — set state and let
`health()` pull; `tests/test_serialport.py:36-44`, `:54-72`, `:239-264` and `test_cli_monitor.py:200-214`
pin the current contract and must pass unmodified, and CI depends on placeholder paths opening
quietly on hardware-less runners (`test_auto_mode.py:4`, `test_web.py:913`).

**Acceptance:** after the fix, exactly one field answers "is this port really open" —
`sinks[].state == "open"`. Today no field on any surface means that: `down` means "a writer raised",
`alive` means the worker thread, `emitted` counts fan-out attempts, and `ok` means thread liveness.

**Diagnosis shortcut until then:** `sudo lsof /dev/ttyUSBn` is the only reliable proof the app opened
a port.

---

### ISSUE-019 — Windows: serial never transmits (`exclusive=False` rejected by pyserial) · fixed, untested on hardware (2026-07-27)

`nmea_sim/serialport.py:163` passed `exclusive=(os.name == "posix")` — i.e. **`False` on win32**.
pyserial's win32 backend accepts only `True` or `None` and raises
`ValueError: win32 only supports exclusive access (not: False)`. That was swallowed by the
[ISSUE-020](#issue-020) catch-all, so the port was marked absent and **all output was dropped**.

**Observed:** a Windows instance generated NMEA correctly (aggregate tap flowed, `emitted` > 900) but
put nothing on the wire, while reporting `sinks: [{"name": "serial", "down": false, "errors": 0}]`.
The COM port stayed openable by other processes — proof the app never held it.

**FIXED (2026-07-27):** now `exclusive=(True if os.name == "posix" else None)`. POSIX behaviour is
unchanged; `None` is pyserial's win32-correct value.

- **Duplicate work exists.** The same one-line fix was committed independently on the `obsidian`
  Windows clone as `147a083` (branch `fix/windows-serial-exclusive`) and **never pushed** — that box
  has no GitHub credentials. A patch was exported to `0001-fix-serial-open-serial-ports-on-Windows-pyserial-rej.patch`
  in that clone's parent dir. **That branch is now redundant and should be discarded**, not merged.
- **Not yet verified on hardware** — the fix is reasoned from the pyserial contract and matches the
  observed symptom exactly, but no Windows box with a COM port has been re-tested since.
- **eemslab was never affected** — POSIX accepts the bool fine. Windows-only.

---

### ISSUE-002 — Dead config keys parsed, persisted, and shipped with zero readers · planned (2026-07-27)

`channel_alternation` (`nmea_sim/config.py:295,306,316`; shipped in `config.json:72`) and
`ais_targets` (`nmea_sim/config.py:811,879,974`; shipped in `config.json:134`) are round-tripped
through load/save but **no code anywhere reads either** — verified by repo-wide grep excluding
`config.py` and tests.

**Failure:** an operator sets `channel_alternation: false`, `validate()` passes, save succeeds, and
nothing changes on the wire. Silent no-op — the opposite of the project's fail-loud posture.

**Fix:** either delete both keys (and drop them from `config.json`) or wire them up. If `ais_targets`
is being held for [RM-013](roadmap.md#rm-013), keep it but reject a non-empty value in `validate()`
with "not yet implemented" rather than accepting and ignoring it.

---

### ISSUE-001 — Backup subsystem is non-functional on every documented destination · deferred (2026-07-27)

`ops/mockingbuoy-backup.service:72-75` documents its own deferral. Current state:

- **CA private-key leak: FIXED.** `:53` now deletes `*.key` from the staging dir; only the public
  `root.crt` is staged. This was the security half of the original finding.
- **Still broken — mounted-share destination:** the unit declares no `ReadWritePaths=`, so
  `ProtectSystem=strict` makes the mount read-only → `EROFS`.
- **Still broken — rsync-over-ssh destination:** no keypair is ever provisioned (`setup.sh` creates a
  nologin service user and never runs `ssh-keygen`), and `ProtectHome=true` makes `~/.ssh/known_hosts`
  unwritable.
- **Design gaps:** single generation, unencrypted at rest, and `rsync -aR --delete` (`:62`) propagates
  corruption into the only copy.

**Currently safe, not currently useful:** `mockingbuoy-backup.timer` is `static` (no `[Install]`
section), so it cannot be enabled and never fires. Confirmed on the appliance 2026-07-27.

**Fix:** provision a keypair + pre-seeded `known_hosts` (or add `ReadWritePaths=<mount>` for the share
form), switch to encrypted dated generations (`tar | age`, N kept) instead of a single mirrored copy,
then prove it with one end-to-end backup **and restore**. Add `[Install]` only once that passes.
Now tracked as scheduled work in [RM-031](roadmap.md#rm-031).
