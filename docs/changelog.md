# Changelog

Dated record of substantive changes. Newest first. ISO 8601 dates.

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
Konqueror — it unblocks modern CSS, is chromeless (recovering ~15–20% of vertical budget), and
matches the browser the UI is developed against.

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
