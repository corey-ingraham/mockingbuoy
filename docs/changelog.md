# Changelog

Dated record of substantive changes. Newest first. ISO 8601 dates.

---

## 2026-08-12 — Conning: ENV panel resized and its readings aligned to the dials [ISSUE-041]

Follow-up to the same day's ENV/depth work, from a desktop screenshot.

- Each pair of readings moved **inside** its dial column, so WATER TEMP / AIR TEMP land on the same
  two centres as SPEED KTS / DIRECTION above them. Verified by measuring centres, not by eye.
- Dial cap 158 → 230px, column cap 170 → 460px. Dial 116 → **147px** at 1920x1080 and 110 → **230px**
  on 1440-tall displays, where the `.p-env` floor is tiered up (`250*s` base, `350*s` above
  `min-height: 1200px`) because a 1440-tall column has ~400px of slack parked in the depth chart.
- `flex-grow` on `.p-env` is a dead end — the left column has no free space, so the `min-height`
  floor is the only lever and every px comes out of the depth chart.
- Two new guards: balanced CSS comment fences, and the readings-inside-the-columns invariant.
  Both mutation-verified. The fence one exists because appending prose after a closing `*/` silently
  invalidated three separate rules during this fix, presenting each time as "the change did nothing".

## 2026-08-12 — Conning: ENV overlap fixed, depth chart fills its panel [ISSUE-039, ISSUE-040]

Both defects came off the first full-screen lab run. Neither was visible to the existing fit probe,
which printed `FITS` over the broken display — so the probe was extended *first*, confirmed red on
current code, and only then were the fixes made.

| metric @1920×1080 s1 | before | after |
|---|---|---|
| ENV `env-dials`/`env-readings` overlap | 19px | 0 |
| depth chart fill (x/y of its panel) | letterboxed | 1.0 / 1.0 |
| wind dial drawn diameter | 158px (42px of it stolen from the readings row) | 116px |

- **ENV** — `height: 100%` on the dial SVGs never resolved (no definite containing height anywhere
  up the chain), so it fell back to the intrinsic 158px and the column spilled into the readings.
  Fixed by taking the SVG out of flow: `position: absolute; inset: 0` against a `relative` wrapper.
- **Depth** — a `.depth-fill` wrapper claims the panel height, the viewBox width is rewritten per
  render to match the box aspect (killing the `meet` letterboxing), and the unused 34-unit left plot
  margin is reclaimed. `y0` and the right margin are load-bearing and unchanged.
- **Probe** — added intra-panel overlap on **ink** boxes (a layout-box test reads 0 on a real
  collision, because the dial paints outside a box that never grows), a drawn-diameter `dialMin`
  guard against trading a visible overlap for an invisible legibility loss, and `depthFill`.
  Overlap runs on its own 6px threshold: at the 2px overflow tolerance, normal label/value line-box
  tightness reports 3–4px on every run, before and after any fix.
- **`.p-env` floor re-derived** (measured, not by eye). It now *binds*, because the out-of-flow SVG
  dropped the panel's content basis — so it is what sets dial size. Left unchanged: at the true
  content minimum the dials collapse to their 72px guard to buy the depth panel 34px, and the
  chart's fill ratio gets worse, not better.
- Fixed in passing: `#ship-schem` collapsed in both scroll tiers (pre-existing, unrelated).

## 2026-08-11 — Conning display: the one-screen layout actually fits now [ISSUE-025, ISSUE-026, RM-023]

The display looked wrong on the lab monitor and there was **no way to fix it in the field**. Measured
with a new rig (`ops/conning-fit-probe.js`: a same-origin iframe sized to exact CSS pixels, so the
media-query tier is whatever you ask for) rather than by resizing a window — a real 1080p window has
`innerHeight ≈ 940`, which is how the original work measured the 0.74 tier and concluded the 1.0 tier
was clean.

| viewport | density | before | after |
|---|---|---|---|
| 1920×1080 | 1 | coords 10, time 10, **env 33** | clean |
| 1920×1000 | 0.82 | env 14 | clean |
| 1920×940 | 0.74 | clean | clean |
| 2560×1440 | 1 | clean | clean |
| 1280×1024 | 1 | coords 6, time 6, **env 36** | clean |
| 1920×921 | 0.74 | unfittable band | clean |
| 1920×900 / 885 | 0.74 | silently clipped | clean (scrolling) |
| 1100×900 | 0.74 | p-primary 129, p-ship 33 | clean (scrolling) |

- **The floors were the root cause, and they had to become scale-relative.** They were fixed pixels
  while everything around them scaled, so no single set can serve 1080p at density 1 *and* 940 at 0.74:
  raising them to fit 1080 pushes 940's column over by 59px — the "floors that oversubscribe would
  merely relocate the clipping" failure the CSS comment warned about while committing it. A plain
  `Npx * scale` overshoots the other way (env clips 17px at 0.74) because part of each panel is
  scale-*invariant*: `.coord-label` is a hard 15px and the dials are `vh`-capped. Each floor is now an
  **affine** function of `--ui-scale` fitted through measured-good points across the whole usable
  density range. **1080p therefore keeps full-size gauges** — an auto-fit search alone would have
  driven it to 0.76 to satisfy one panel, which *is* the "everything too small" complaint.
- **ISSUE-025 was a bug class with four instances.** Besides the documented `max-height: none` reset,
  `.ins-panel { overflow: visible }` was dead in *both* scroll tiers, and `.ins-panel.p-primary
  { overflow: hidden }` at (0,2,0) still beat the reset on specificity. All consolidated into one
  SCROLL-TIER RESETS block that must stay last in the conning section. `@media` adds no specificity;
  a later equal-specificity rule wins regardless of the query.
- **The 820px scroll threshold was wrong** — derived from a floor sum that had already changed. The
  layout cannot fit at any density at or below ~905px tall (885 fails by 20px, 900 by 5px, 910 clean;
  sweeping 0.74→0.66 shows overflow *plateau* near 17px, the residual being scale-invariant). Raised to
  920 so the unfittable band scrolls instead of clipping, with margin for a different engine's fonts.
- **Config → Display card** (per-browser, never sent to the appliance): an Auto toggle, a density
  slider, a fullscreen button, a fit badge that reads LIVE only when nothing is clipped, and a Copy
  diagnostics button. Auto **removes** the inline property so the CSS tiers govern; it must never set
  `1`, which pins full density and looks identical on a tall screen while breaking every short one.
- **`autoFitScale()` refines, it does not replace, the tiers** — the tiers work with no JS and are
  already correct before first paint. It steps density down only when the baseline does not fit, skips
  the scroll tiers, and **reverts rather than pinning the minimum** when nothing fits.

Two traps found the hard way and now encoded in the rig, the tests and the comments:

- **Per-panel overflow is not a sufficient fit metric.** At 1920×830 no panel overflowed *and* the
  Alerts panel hung 169px below its column. Columns and each column's last-panel delta must be
  measured too, or the check prints a green tick over a visibly broken display.
- **`requestAnimationFrame` does not fire in a backgrounded tab**, so an rAF-sequenced measurement
  hangs instead of returning. Reading `scrollHeight` forces layout synchronously and works even when
  hidden — which is also why the auto-fit is *not* gated on `visibilityState`: that gate stopped it
  running in a background tab, so a page loaded out of focus stayed clipped.

Also corrected: this changelog's own claim that hiding the app chrome recovers "~15-20% of vertical
budget" (2026-07-28 entry). The header is ~85px — about **8%** at 1080p, roughly one density step.

Ten new client-contract tests, including two structural guards that fail if the cascade-order bug is
reintroduced or a `dvh` is left unpaired. Gate: ruff, black, mypy clean; full suite green.

---

## 2026-08-11 — AIS simulator review: findings recorded [ISSUE-036, ISSUE-037]

Docs and registers only; no runtime change. A review of the AIS *generation* path ahead of a bench
session, with every claim checked against a live appliance rather than inferred. The simulator itself
was found working — a 400 s capture gave 80 Type 1 own-ship reports at exactly the configured 0.2 Hz
plus one Type 5 static burst, decoding to the configured MMSI, with radio channel alternating A/B.
What was wrong was placement, two suppressed fields, and several docs.

- **[ISSUE-037](issues.md#issue-037)** — synthetic contacts are placed at **absolute** profile
  coordinates; `TargetSpawner` never reads own-ship state. Both the built-in default and the shipped
  `example.json` region sit on Null Island, so enabling traffic looks healthy (lines flow, counters
  climb) while nothing appears near own ship. Worse in `auto`, where a live GNSS fix moves own ship but
  not the contacts. Verified both ways: a region-correct profile put 32 contacts at a median 13.5 nm.
- **[ISSUE-036](issues.md#issue-036)** — own-ship `nav_status` and `rot` are hard-wired to the
  "not defined"/"not available" sentinels. `nav_status` has no config key at all; `rot` is a straight
  wiring miss, since `VesselState.rot_dpm` is actively simulated and simply never passed through.
- **[ISSUE-002](issues.md#issue-002) amended** — `AisSpec.mode` is a **third** dead config key
  (shipped as `"ownship"`, zero readers, validated against no enum). Also recorded that unknown keys
  inside the `ais` block are silently dropped, unlike the top level — so `include_type_5` (typo)
  validates clean and does nothing.
- **`ref/architecture.md`** — documented that the AIS `emit` list is largely decorative: the
  `sentence` string is never read, only the first enabled entry's `rate_hz` is used, and `AIS_STATIC`
  is derived from `type5_period_s` rather than schedulable from `emit`. This is the most likely
  operator misunderstanding on the channel and was previously undocumented.
- **`user-guide.md`** — click-to-decode is a single-line decoder, so **no AIS static report (Type 5 or
  Type 24) will decode there**; added the `pyais` fragment-pair command that does work.
- **`ref/testing.md`** — corrected: `--backend pty` does **not** print its slave device paths.
  `PtyWriter.slave_name` has no production reader, so the documented `gps -> /dev/pts/N` output has
  never existed.

---

## 2026-08-11 — Own-ship AIS identity is operator-settable [RM-032]

Pulled forward from the roadmap and landed the same day it was filed, because the shipped default was
about to go on a bench wire: MMSI `366000001` / `MOCKINGBUOY` sits in an **allocated** US MID, so it is
not merely synthetic — it is plausibly a real vessel's, and it was reachable only by hand-editing JSON
on the appliance.

- **`web/app.py`** — new `AisIdentityDefault` allow-list on `POST /api/config/initial-state`, merging
  onto the `role == "ais"` channel's `ais.own_ship`. All six `AisOwnShip` fields (`mmsi`, `class`,
  `name`, `call_sign`, `ship_type`, `imo`) and nothing else: `extra="forbid"` keeps the traffic/type5
  keys out, they have their own allow-list. Skip-on-None per field, so a one-field correction from the
  UI cannot blank the rest. `class` uses a pydantic alias — it is a Python keyword.
- **Bounds are not duplicated.** `validate._validate_ais_identity` stays the single gate and the merged
  config is deep-validated before the write, so an out-of-range MMSI is a 400 quoting the real rule
  rather than a second copy of the bound that can drift. Each rejected case is one pyais silently
  corrupts on the wire (MMSI wraps at 30 bits, `ship_type` truncates, text clips).
- **`index.html` / `app.js`** — an *Own-ship AIS identity* card with a `MID nnn` pill that warns while
  typing when the MMSI falls in an allocated administration range (201-775), plus advice on the 9-digit
  shape and the AIS 6-bit character set before the save round-trip.
- **`ops/ais-bench-check.py`** (new) — a bench acceptance script that observes the *running* service
  through its own instrumentation (aggregate tap + `input_nmea` SSE + `/healthz`) rather than opening
  ports the engine already holds exclusively. Checks ports are genuinely open via `lsof` (the ISSUE-020
  guard — `sinks[].down` cannot distinguish a dead port), that transparent relay is wired and stamps no
  liveness, that arriving traffic verifies, that transparency holds sentence-body-wise, and that a mute
  flips to SIM within 2 s rather than after `liveness_timeout_s`. Restores the mute state even on
  failure.

RM-032 items 3 (named-identity picker) and 4 (identity provenance on the AIS pane) remain open.

Gate: `ruff`, `black`, `mypy` clean; **848 passed, 5 skipped** (9 new tests).

---

## 2026-08-11 — Background sims decoupled from `mode`; transparent relay; mutes act immediately

Prep for landing an AIS transponder's talker pair *in series* through the program on a lab IBNS rig.
Two behaviour changes plus two new registers entries. Both new config surfaces default **off/absent**,
so an untouched config behaves exactly as before.

### Background sims are gated per-field, not by mode (`nmea_sim/config.py`)

All four `effective_*_sim` resolvers returned `None` whenever `mode != "simulate"`, so selecting `auto`
for one channel froze depth, wind, rudder **and** heading rig-wide — pinned at `initial_state` and still
streaming to the `instrument` channel and its TCP tap. On a rig whose purpose is raising IBNS fidelity
that was a net fidelity *loss* traded for one channel's passthrough.

The guard was over-broad rather than wrong: it was reaching for "don't fight a real writer", but keyed
on the mode instead of on what actually writes the field. New `rx_fed_fields(cfg)` + `_sim_suppressed`
key it on the config:

- `simulate` — never suppressed (unchanged; no router, no passthrough).
- `replay` — always suppressed (the capture file is the source of truth).
- `auto` — suppressed only when something can really write the field: a channel that RX-feeds it, or,
  for heading alone, a heading channel with `sources` (HDT/HDG are in `_STATE_FORMATTERS`).

**Depth, wind and rudder therefore keep simulating in `auto`** — no input `function` exists for them and
`rx.parse_line` supports no depth/wind/rudder sentence, so the conflict the guard prevented could not
occur. Own-ship **motion** could not follow (`movement.mode` is config-time, position ownership is
runtime) — filed as [RM-035](roadmap.md#rm-035).

### Transparent relay: forward what the router does not model (`router.py`, `engine.py`, `config.py`)

`classify.sentence_class` knows three classes, so every other sentence on an input wire — AIS
`ALR`/`ALF`/`ALC`/`ABK`/`TXT`/`VER`, vendor `$P...`, query responses — classified as `None` and was
**dropped**. In series on a real talker's wire that silently made this program a black hole for
everything it does not model, including the bus's AIS alarm path.

New `ChannelSpec.rx_transparent_relay` (default `false`, omitted from `to_dict` when false, mirroring
`tap_only`). `Router.note_rx` now returns an `RxDecision` dataclass instead of a
`tuple[str, str, str]`, because forwarding and liveness had to stop being one decision:

- `arbitrated` — classified, input is in that channel's `sources`. Stamps liveness (as before).
- `transparent` — anything the channel would otherwise drop, **including classified-but-unroutable**
  lines. Forwarded **only while the channel is LIVE**, and **never** stamps liveness.

Never stamping is the load-bearing part: if status chatter counted as a live signal, a talker emitting
only alarms would hold the channel in passthrough and the rig would never simulate on signal loss. The
LIVE gate reuses the same `any_live` predicate `_fire` uses, so the two are exact complements — on
fallback the relayed chatter stops and the consumer sees a clean simulated picture.

Two silent no-ops are rejected at validate time (the flag with empty `sources`, and the flag on an
un-arbitrated role that can never be LIVE), as is an input relayed by more than one channel — which
would otherwise resolve by config order.

### Muting an input now acts immediately (`engine.py`, `router.py`)

`set_input_enabled` was "a flag write and nothing more", so the stamps a slot had already made kept it
winning until they aged out — a mute lagged by the slot's whole `liveness_timeout_s`. It now calls the
new `Router.clear_liveness(input_id)` when muting, so the flip to SIM is instant and deterministic (and
the transparent-relay window shuts at the same moment). Unmuting needs no counterpart; the next valid
line re-stamps.

This makes the mute the signal-loss test instrument instead of a timeout-tuning exercise:
`liveness_timeout_s` now only governs genuinely unexpected loss, so it can be sized purely to avoid
flapping on a sparse feed — AIS own-ship reports stretch toward minutes when moored, making the 3 s
default far too short for an AIS input.

### Docs and registers

- **`ref/architecture.md`** — the three-category decision table, the per-field sim gating, immediate
  mutes, and the checksum/TAG-block limits. Dropped a false claim that the channel RX reader "parses
  via `pynmea2`/`pyais`": it is `pynmea2` only, only under `rx_feeds_state`, and **no `pyais` decode
  runs on any serial RX path**. Also states plainly that a duplex channel's RX never reaches the router.
- **`ref/serial-hardware.md`** — `direction: "both"` is not passthrough; an output channel and an input
  slot may never share a device path; wire pairs are invisible to the software, but a pair carries one
  talker, so in-series insertion means breaking the run.
- **`ref/security.md`** — scoped "listen-only is the only supported way" to *input slots*, and recorded
  the `targetable_slots` transmit hazard.
- **`user-guide.md`** — Auto-mode relay behaviour, and prefer the mute over a cable pull.
- New: [ISSUE-033](issues.md#issue-033) (TAG-block lines dropped at the checksum gate),
  [ISSUE-034](issues.md#issue-034) (`function: "unused"` makes a slot a transmit target),
  [RM-032](roadmap.md#rm-032) (operator-assigned own-ship MMSI), [RM-035](roadmap.md#rm-035).

Gate: `ruff`, `black`, `mypy` clean; **839 passed, 5 skipped** (25 new tests).

---

## 2026-07-28 — Docs no longer claim backups work; backup redesign gets a roadmap slot [RM-031, ISSUE-001]

Docs-only. A review of the backup/DR state found the reference docs describing a subsystem that has
never completed a single successful run. Nothing about the runtime changed — this makes the docs
match `ops/mockingbuoy-backup.timer` being `static` and unenableable.

- **`ref/deployment.md`** — step 6 claimed `setup.sh` "enables the host backup timer". It does the
  opposite (`setup.sh:485-495` warns and refuses). Corrected, and the `BACKUP_DEST` row now says the
  variable is inert either way rather than implying set = enabled.
- **`ref/security.md`** — the "Backups & restore" section now opens with the fact that there is no
  working backup, points at the manual copy as the only real procedure today, and marks the restore
  steps as never drilled.
- **`ref/testing.md`** — the "backup timer runs" checkbox was uncheckable by construction; struck it
  and rewrote the restore drill to run from a hand-copied backup, which is worth doing regardless.
- **`issues.md` ISSUE-029** — the repro instructions told the reader to diff against a
  `config.local.json.bak-preauto` "the app writes". **No code in this repo writes any `.bak` file.**
  The observed file's provenance is unknown (older build, or a manual copy); the repro now says to
  take the copy by hand. This mattered — following it as written would have produced no baseline and
  looked like the bug failing to reproduce.
- **New [RM-031](roadmap.md#rm-031)** — the redesign had no roadmap item at all; the only forward
  tracking was a deferred issue with no owner. Sequenced: one working destination → encrypted dated
  generations → an actual restore script → a proven end-to-end drill → `[Install]` last.

---

## 2026-07-28 — Per-field provenance: the conning pills now tell the truth [RM-009, ISSUE-027]

The conning panel pills were derived from the owning channel's **role**, not from who wrote the
value — `roleLive("gps")` asks *"is the GPS channel's source live?"*, never *"where did this number
come from?"* Two failures followed, both verified before being fixed:

- **`pill-attitude` claimed LIVE over wholly simulated values.** It tracked `roleLive("heading")`,
  but `pitch_deg`/`roll_deg` come from the sea-state model, are excluded from `_UPDATE_RANGES`, and
  appear nowhere in `rx.parse_line`. A live compass made the Attitude panel read LIVE while every
  number in it was synthetic — exactly what `design-decisions.md` says the tag exists to prevent.
- **A frozen position kept reading LIVE.** Auto mode requires `movement.mode: static`, so when a
  source dies nothing rewrites lat/lon; the pill only moved when the *channel* fell back.

Provenance is now recorded at the `SharedState` write choke point as a `(source, cls, ts)` side-map
and resolved at read time.

- **Expiry is the feature, not capture.** `Engine.provenance()` returns a sparse `{field: "LIVE"}`
  map (absent means SIM, so a new field defaults to the safe answer). A `live:<input>` tag survives
  only while the router still names that input the winner for the class the value arrived on —
  storing the class at write time is what makes that check possible without a field→class table.
- **A side-map, not a `VesselState` field** — not for constructor breakage (defaulted trailing
  fields are supported), but because a tag inside the frozen snapshot would be carried forward by
  every `replace()` that didn't touch the field, so stale tags would ride along on unrelated writes.
- **`update()` takes a required keyword-only `_sources`**, string or per-key dict. Required so a new
  writer can't silently inherit `sim`. The per-key form is load-bearing: the physics tick commits a
  possibly-live-GNSS `utc` alongside simulated pitch/roll in **one** atomic swap, and splitting that
  would tear the swap a generator relies on to read time and position off a single snapshot.
- **The clock tag is an injected callable defaulting to `"simulated"`.** The tick's clock is a
  protocol — a `TimeAuthority` only in auto mode, a bare `TimeSource` otherwise with no
  `source_tag()`. Reaching for it unconditionally raises `AttributeError`, which the run loop's
  blanket `except` converts into a dead physics thread and every channel frozen. Guarded by a test.
- **Single-lock `snapshot_with_provenance()`** — reading value and tag separately would tear at
  4 Hz against a 10-20 Hz writer.
- Pills aggregate over **live-capable fields only**. `stw_kn`/`rot_dpm` are shown in the Heading
  panel but can never be live-seeded (`_STATE_FORMATTERS` is `{RMC,GGA,VTG,HDT,HDG}`), so including
  them would have pinned that panel to SIM forever — a regression no simulate-mode test could catch.
  An automated two-input test now asserts each pill's field group really can reach LIVE.
- Carried on the SSE `state` frame **and** `GET /api/state`, since the UI fetches the latter for its
  first paint.

**Follow-up the same day — `pill-time` brought into line.** The first cut deliberately left the Time
pill on its old health-based predicate ("keep it, don't regress it"), which turned out to leave the
one pill contradicting the mechanism the change introduced: it treated anything not
`SYSTEM`/`SIM`/`OFF` as live, so an **NTP-disciplined clock rendered green while the engine resolved
that same `utc` as SIM**. One value, two answers, on one screen. It also contradicted its own code
comment ("LIVE only when disciplined by a live NMEA source" — NTP is the local chrony clock). It now
reads the same `utc` provenance as every other pill: GPS/SAT are LIVE, NTP/system/simulated/hold are
SIM. Caught on the bench, not by a test — hence the new clock-tier resolution test.

Per-readout badges (as opposed to per-panel) remain outstanding as RM-028; the data is now on the
wire, so that follow-up is UI-only.

---

## 2026-07-28 — Conning: off-course / off-track bars rescaled (wide + flat, not just bigger)

After [74a2c50](#) the two autopilot deviation strips read as undersized against every neighbouring
gauge, **at every window size**. Measured cause, not inferred: each conning gauge's *render scale*
(rendered px ÷ viewBox user units) was depth graph 1.88×, propulsion tach 1.73×, attitude dials
1.47× — and these strips **0.95×**. Their cap was `min(86%, 210px * --ui-scale)`, and at any panel
wider than ~244px the pixel term won, so the 86% never bound and the strips rendered ~1:1 with their
user units. That is why `--ui-scale` appeared to be the only thing that ever moved them.

The naive fix — raise the width cap — is not free: the box was a fixed 5:1 strip with `height:auto`,
so width *is* the height governor, and matching the tach's 380px would have added ~68px to the right
column whose budget `74a2c50` had just balanced. Instead the strip was **redrawn wide and flat**,
220×44 (5:1) → **360×28 (~13:1)**, which decouples apparent size from height cost.

Measured at 1920×1080, `--ui-scale` 1 (the real kiosk tier — 1080 does *not* match the
`max-height:1000px` query):

| | before | after |
|---|---|---|
| bar | 210 × 42 px | **578 × 45 px** |
| render scale | 0.95× | **1.57×** |
| axis labels | 7.6 px | **12.5 px** |
| minor tick spacing | 9.6 px | **26 px** |

- **Geometry is now one shared set of constants** (`AP_W/AP_H/AP_CX/AP_CY/AP_X0/AP_X1/AP_SPAN`).
  `buildLinearIndicator` drew the ticks while `setLinearMarker` *independently* hardcoded the same
  `cx=110, cy=22` and half-span `100` — the marker that must land on those ticks was free to drift
  from them. `buildLinearIndicator` also now writes the `viewBox` itself, so it cannot disagree with
  `index.html`. Verified: majors at 12/96/180/264/348 match the marker positions exactly.
- **`min()` terms reordered** to `min(96%, 620px * --ui-scale)` so the *percentage* binds at full
  scale and the pixel term takes over only as `--ui-scale` steps down — the inverse of before.
- **`.p-autopilot` floor 292px → 304px.** Measurement found the panel was *already* scrolling
  silently by 3px at the kiosk tier under `.ins-panel { overflow: auto }`; the wider strips added
  ~4px. 304 clears both, costing Attitude ~5px (dials 135 → 130px). Verified `scrollHeight ==
  clientHeight` at 1920×1080, 1920×1000, 1920×940, 1440×900, 2560×1440 and 1280×1024.
- Deliberately **no `max-height`** on `.ap-ind svg`: for a fixed-aspect SVG the width cap already
  determines height, and a `max-height` there would be silently overridden exactly the way the
  narrow-screen reset block is — see [ISSUE-025](issues.md#issue-025).
- Dead `max-width: 260px` rule removed (it was overridden by the next line at equal specificity).

Gate: ruff + black + mypy clean, full suite green.

---

## 2026-07-28 — Per-input On/Off toggle (rehearse signal loss without unplugging)

Input slots had no enable control at all. The input panes' **Freeze view** only pauses the display —
the reader thread, router liveness, state feed and diagnostics all keep running behind it — so the
only way to test how the app handles a live feed dying was to physically pull a cable. The nearest
existing feature, `gps_kill` fault injection, mutes an *output* channel and never touches the input.

Added `InputSpec.enabled` plus `Engine.set_input_enabled()`, an `action: "input"` branch on
`POST /api/control`, and an **Input: ON / OFF** button in each input pane, in the same position as
the output pane's toggle. Runtime-only: the config key is a startup default, not a persisted flip.

- **The gate is deliberately placed, not blanket.** `SerialPort` fans each RX line to three
  consumers; only `on_line` (the SSE pane feed) and `on_rx` (router + Time Authority) are gated.
  `on_raw` stays ungated so port diagnostics and armed captures keep working — the toggle mutes the
  *simulator*, not the wire. Both gated seams are engine-supplied closures, so `serialport.py` is
  untouched and the transport stays dumb.
- **No new fallback logic.** Muting simply stops liveness being stamped; `Router.winner()` ages the
  source out, `_fire` stops being suppressed and resumes generating from the values
  `_feed_passthrough_state` had already seeded, and `TimeAuthority` demotes itself to the base clock
  because it resolves its winner through the same router.
- **The enable map is built for every mode**, unlike the adjacent auto-only `_diagnostics` dict —
  `input_status()` walks `config.inputs` unconditionally, so an auto-only map would `KeyError` on a
  simulate config that declares slots (the shipped `config.json` shape).
- **Reconciliation rides the 1 Hz health frame** (`HealthReport.inputs`, id + flag only — R19), not
  the tab-gated `/api/diag` poll, so a flip by one client corrects every client on every tab.
- **`renderInputStats` suppresses "receiving" when muted.** Diagnostics stay live by design, so
  without this the 2 s repaint would paint "receiving" over a disabled input — the exact confusion
  the toggle exists to remove.
- Two pre-existing exact-assertion tests updated for the new field (`InputSpec` round-trip and the
  `/api/inputs` shape guard).

Documented, because both read as bugs during the test they are most likely to be seen in: the
channel emits **nothing** between the mute and liveness expiring, and auto mode requires
`movement.mode: static`, so after fallback position freezes while SOG/COG keep reporting.

No conning-tab LIVE/SIM badge — the observable is the Streams output-pane `source` badge. Per-value
provenance on the conning display remains **RM-009**.

---

## 2026-07-28 — Conning display fits any screen [RM-023]

At a 1920×1080 lab monitor the depth chart rendered as a ~40×20 postage stamp and the attitude
gauges were clipped away entirely. Diagnosed by measuring rendered geometry in a browser, not by
reading CSS — an initial diagnosis blaming the `vh` caps was wrong, since at 1080p the gauge heights
are **width**-derived from their SVG viewBox ratios and almost none of the `vh` caps bind.

- **`height: 100dvh` was unpaired** and is the app shell's only height declaration. `dvh` needs
  Chromium 108+; the bridge display ran Konqueror/QtWebEngine, whose Chromium lags well behind. A
  dropped declaration leaves the root with an **indefinite height**, so every `1fr` / `minmax(0,Nfr)`
  row in the conning grid resolves against nothing. Now `100vh` first, `100dvh` second.
- **`.ap-ind svg` had no height governor** — two 220×44 (5:1) strips rendered ~104px tall each,
  ~240px of panel for two thin bars, directly starving Attitude beneath them.
- **Panels were `flex: 0 0 auto`**, so one flexible sibling absorbed the whole shortfall. All panels
  are now `0 1 auto` with floors sized to real content; floors set *below* content merely pin the
  panel and hand the slack to a growing sibling.
- **`min-height: 0` down the dial chains** — flex items default to `min-height: auto` and refuse to
  shrink below content, so squeezed dial rows scrolled instead of shrinking.
- **No height breakpoint existed at all**: 1920×1080 got the one-screen lock purely for being *wide*.
  Added one, plus a `--ui-scale` density knob with tiers measured by sweeping each viewport.

Measured at a 1920×1080 shell: depth SVG 10–25px → **222px**; attitude dials clipped → **138px each**;
zero panels overflowing. Verified clean at kiosk-1080, windowed-940, 1440p, and both scale overrides.

Recommended alongside: run the bridge display on Chromium/Firefox in kiosk mode rather than
Konqueror — it unblocks modern CSS, is chromeless, and matches the browser the UI is developed
against. *(Corrected 2026-08-11: this originally claimed chromeless recovers "~15–20% of vertical
budget". The header is ~85px — about **8%** at 1080p, roughly one density step. Do not plan against
the larger figure.)*

---

## 2026-07-28 — Config/Maintenance usability: hemisphere entry, adapter binding [RM-023]

- **N/S + E/W selectors** for lat/long. Config stores signed decimal degrees, so a western longitude
  meant typing `-122.47`. Touched four client paths, not one — builder, `readCfgField`,
  `loadStateIntoConfig`, and the driven-field greying that keys on `cfg-<field>` ids. Guards the trap
  the range check cannot see: a negative magnitude with W selected combines to a positive value in
  the opposite hemisphere.
- **Input slots can now be bound to a physical adapter.** The control previously set only
  `function`; the slot→port binding lived solely in `data/config.local.json` and no enumeration
  existed. New `GET /api/ports` returns opaque handles + kernel name + what each adapter is
  receiving — never the by-id link (brand + serial, R19). The client posts a handle; the server
  resolves it. Card renamed *Input Slots* and given the explanation it never had.
- **Catch-all `dialout` udev rule.** An unruled adapter landed outside `dialout`, the app could not
  open it, and ISSUE-020 reported that as "device absent" — eemslab ISSUE-008, a lost bench session.
  Replaces an earlier design in which the web app wrote udev rules via a root helper; adversarial
  review killed it (udev rules hold code that runs as root, and a programmable USB device presents
  an arbitrary `iSerial`).
- **Duplicate device paths are now compared by `realpath`.** ⚠ *May reject configs that previously
  "worked"* — two aliases for the same tty used to pass validation, then the second exclusive open
  failed and was misreported as absent hardware. Surfacing it is the fix, not a regression.
- **`docs/ref/security.md`** — corrected the `DeviceAllow=` claim (it is a char-major *class* grant,
  not per-device) and added the missing R19 section, including the accepted exception that
  ISSUE-020's error text may carry a by-id path.

---

## 2026-07-27 — Lab-session findings; Windows serial fix [ISSUE-019..022, RM-023]

Bench testing on the `eemslab` appliance surfaced four app-level defects that the red-team pass had
missed. Source notes live only on that box at `~/repos/eemslab/docs/` — **not under git**.

- **Fixed [ISSUE-019](issues.md#issue-019)** — `nmea_sim/serialport.py:163` passed `exclusive=False`
  on win32, which pyserial rejects with `ValueError`; the port silently never opened and all output
  was dropped while status read healthy. Now `exclusive=(True if os.name == "posix" else None)`.
  POSIX behaviour unchanged. Tests + ruff/black/mypy green; **not yet re-tested on Windows hardware.**
  Supersedes an unpushed duplicate fix (`147a083`) stranded on the `obsidian` clone.
- **Filed [ISSUE-020](issues.md#issue-020)** — the root cause that hid ISSUE-019 for months: all serial
  open failures collapse to `present = False`, making EACCES and programming errors indistinguishable
  from an unplugged cable. Highest-value fix on the list.
- **Filed [ISSUE-021](issues.md#issue-021)** — a wrong system clock silently poisons dated sentences;
  `CLAUDE.md`'s "gated on time sync" claim is not implemented, and `setup.sh` installs chrony, which
  deletes the very marker `ntpsync.py` probes.
- **Filed [ISSUE-022](issues.md#issue-022)** — shipped `CHANGE-ME` input slots parse clean and fail
  silently.
- **Filed [RM-023](roadmap.md#rm-023)** — conning-display and Config-tab UX backlog.

---

## 2026-07-27 — Red-team triage; tracked issue/roadmap registers [ISSUE-001..003, RM-004..018]

Appliance moved from the lab bench to the home LAN (static `192.168.20.111`). Reachability, TLS
front door, and service health re-verified; local `pytest` green.

- Triaged the whole-repo red-team report (written against `ca7b284`, branch
  `feat/tabbed-ui-instruments`) against `bd5e37d` on `main`. **Of 34 numbered findings, 31 are
  remediated in-tree** — verified by direct inspection, not by trusting the commit messages:
  - **C1** — lock regenerated via `uv pip compile --universal --python-version 3.11`; `uvloop` present
    with a `sys_platform != 'win32'` marker, `colorama` correctly win32-gated.
  - **C2** — CSP now `script-src 'self'`; JS/CSS externalized to `web/static/app.js` + `app.css`
    (zero inline `<script>`/`<style>` remain). `docs/ref/security.md` matches the shipped header.
  - **C3** — CA private-key staging removed (`*.key` deleted from the staging dir). Remainder
    tracked as [ISSUE-001](issues.md#issue-001).
  - **H1** — rx guards widened to `(ParseError, ValueError, TypeError, AttributeError)` at every call
    site plus per-field conversion inside `nmea_sim/rx.py`.
  - **H2** — empty-emitter guard (`if self._emitters:`) before the `min()`.
  - **H3** — `ship_type` key corrected at both Type 5 and Type 24B call sites.
  - **H4** — `EngineManager.set_config` swaps the live config on successful persist.
  - **H5** — `create_app()` calls `config.validate_or_raise()`.
  - **H6** — `replay_alive` / `inputs_alive` added to `HealthReport`; replay failures set
    `replay_error` instead of dying under a whole-loop suppress.
  - **H7** — `threading.Lock` over `PortDiagnostics` feed/snapshot.
  - **H8/H9** — `math.isfinite` gates across validate/rx/engine; `allow_nan=False` on save.
  - **H10** — `self._engine` captured into a local once per method, killing the SSE-loop race.
  - **M1, M2, M3, M5-M9, M11, M17, M19-M21** — all confirmed fixed.
  - **NMEA domain 2, 3, 4, 6** — own-ship VDO, sentinel constants in engineering units, RMC
    status-`V` rejection, THS mode — all confirmed fixed.
- **Not exhaustively re-verified:** the report's 7 efficiency wins and its test-coverage-gap list.
  EFF1 (parse-once at ingress) carries an in-code reference and appears done; the rest were spot
  checks only. Treat that section as unaudited.
- Created tracked registers per the repo doc template: `docs/issues.md`, `docs/roadmap.md`, and this
  file. The git-ignored `.redteam-report.md` is now superseded and can be deleted once
  [ISSUE-001..003](issues.md) and [RM-004..018](roadmap.md) are confirmed to capture everything worth
  keeping.
