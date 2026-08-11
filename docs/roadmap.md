# Roadmap

Forward-looking work. Newest first within a status band. `RM-NNN` shares one counter with
`ISSUE-NNN` in [issues.md](issues.md) — next free number across **both** files.

Status ∈ {planned, in-progress, done, deferred}. Effort: S / M / L.

> Ranked value-vs-effort, carried forward from the `ca7b284` red-team roadmap so the git-ignored
> report can be retired. Nothing here is committed work — these are candidates.

---

### RM-023 — Conning display + Config tab UX backlog · in-progress · M (2026-07-27)

**Done 2026-07-28:** monitor-resolution auto-scaling, lat/long hemisphere entry, and the
detected-adapter port picker. Remaining items are marked below.

Bench findings from the 2026-07-27 `eemslab` session, carried over from that box's RM-011.

**Display** — `web/static/app.js`, `app.css`, `index.html`
- Status **pills don't update** when a channel isn't SIMing or isn't emitting. The engine already
  publishes everything needed: each SSE status frame carries `channels[].source` (`"SIM"` /
  `"LIVE:<input_id>"`), `alive`, `emitted`, and `sinks[] {name, down, errors}`. Render from those.
  **Caveat:** per [ISSUE-020](issues.md#issue-020), `down: false` does **not** mean the port opened —
  fix that first or the pills will confidently show green on a dead port.
- **GPS IN and OUT don't scroll** in the NMEA Stream pane.
- **Full-screen mode** for the conning display (Fullscreen API). *(Still open — demoted: once the
  bridge runs a kiosk browser it is already chromeless, so this is a workstation convenience.)*
- ~~Monitor-resolution auto-scaling.~~ **DONE 2026-07-28** — container-aware floors + `--ui-scale`
  tiers; see [changelog](changelog.md).

**Config tab** — `web/static/index.html`
- ~~Lat/Long entry has no E/W or N/S control.~~ **DONE 2026-07-28** — magnitude + hemisphere select
  across all four client paths, with a guard for the negative-magnitude/opposite-hemisphere trap.
  *(Route-waypoints textarea still takes signed `"lat, lon"` lines — separate follow-up.)*
- **`movement.mode` cannot be set from the UI at all.** Enabling auto fails with a validator message
  about `movement.mode` needing `'static'` (`nmea_sim/validate.py:793`) while no control exists
  anywhere to change it — it must be hand-edited in `data/config.local.json`. **This cost real bench
  time.** Either expose it or make the error say plainly that it's JSON-only and name the file.
- ~~Port selection should be a picker of detected adapters.~~ **DONE 2026-07-28** — `GET /api/ports`
  is the safe enumeration seam this called for: the client picks an **opaque handle** labelled with
  the kernel port and what the adapter is receiving, and the server resolves handle → by-id path, so
  the brand/serial is never surfaced and a device path is never a client-supplied value. Slots also
  gained the explanation of what `function` vs adapter means.
  *(Still open: the read-only Maintenance "attached adapters" table.)*

### RM-032 — Operator-assigned own-ship MMSI (and AIS identity) · in-progress · M (2026-08-11)

**Done 2026-08-11:** scope items 1 and 2 — the Config-tab identity form and MID-aware warning are
live (`AisIdentityDefault` in `web/app.py`, the *Own-ship AIS identity* card in `index.html`,
`loadAisIdentityIntoConfig`/`refreshAisIdentityAdvice` in `app.js`). Items 3 and 4 remain open.

**Goal:** stop the simulator emitting an arbitrary hard-coded identity. Own-ship AIS identity is
config-only today, shipping as `mmsi 366000001` / `MOCKINGBUOY` / `ship_type 37` (`config.json:71`) and
reachable **only** by hand-editing JSON. `366` is an allocated US MID, so the default is not merely
fake — it is plausibly *someone's*. On a rig whose whole purpose is fidelity, the identity has to be
entered deliberately and match the vessel it stands in for.

Scope:
1. ~~**Config-tab entry for the AIS identity block**~~ **DONE 2026-08-11** — all six `AisOwnShip`
   fields persist through a dedicated `ais_identity` allow-list (`extra="forbid"`, skip-on-None per
   field so a partial save preserves the rest). Bounds are NOT duplicated in the web model:
   `validate._validate_ais_identity` stays the single gate and the merged config is deep-validated
   before the write, so an out-of-range value is a 400 quoting the real rule.
2. ~~**MID-aware warning**~~ **DONE 2026-08-11** — the card shows a `MID nnn` pill and warns while
   typing when the MMSI sits in an allocated administration range (201-775), plus advises on the
   9-digit shape and the AIS 6-bit charset before the save round-trip.
3. **Named-identity picker** so a rig can hold several known vessel identities and select one per test
   run instead of re-typing.
4. **Provenance on the AIS pane** — config / UI / default — so a fabricated identity is never mistaken
   for a real one.

**Note:** before this, `web/app.py` persisted only `ais_traffic` onto the `role == "ais"` channel —
the identity block had no write path at all, which is what item 1 added. Cf.
[ISSUE-002](issues.md#issue-002) — `ais_targets` and `channel_alternation` are parsed and persisted
with zero readers; the identity seam deliberately does not repeat that (every field it accepts is read
by `AisOwnShip` and reaches the wire).

### RM-035 — Own-ship motion in `auto` (dead-reckoning resumes on fallback) · planned · M (2026-08-11)

**Goal:** let own ship move in `auto` mode once no live GNSS owns position.

`validate` hard-requires `movement.mode: static` in `auto` so dead-reckoning cannot fight a live GNSS
fix — legitimate while a source is live, wrong after fallback. Today the post-fallback generator holds
the last seeded position while still reporting the last SOG/COG, so a generated RMC reports several
knots over a position that never advances.

The background sims were decoupled from the global mode on 2026-08-11 (see
[changelog](changelog.md)) — depth, wind and rudder now keep running in `auto` because nothing else can
write them. Motion is the one case that could not follow: `movement.mode` is read once at config time
whereas position ownership changes at runtime, so this needs the **physics tick** gated on
`router.any_live(gps_channel, "gnss")` rather than a config-time switch. Prereq for a rig that wants a
moving own ship while the AIS leg is in passthrough.

### RM-031 — Backup subsystem redesign + first proven restore · planned · M (2026-07-28)

**Goal:** make the appliance actually recoverable. Today nothing backs up: the units ship, the timer
is `static`, and neither documented destination works — the breakage inventory lives in
[ISSUE-001](issues.md#issue-001), which this item carries forward as scheduled work.

Scope, in order:
1. **Make one destination work.** Either `ReadWritePaths=<mount>` for the mounted-share form, or
   provision an `ssh-keygen` keypair + pre-seeded `known_hosts` in `setup.sh` for the rsync-over-ssh
   form (`ProtectHome=true` currently makes `~/.ssh` unwritable). Pick one and document the other as
   unsupported rather than half-shipping both.
2. **Encrypted dated generations.** Replace the single `rsync -aR --delete` mirror — which propagates
   corruption into the only copy — with `tar | age` and an N-kept retention sweep.
3. **A restore script.** None exists; `security.md`'s procedure is prose only. It should be runnable
   and idempotent, including ownership fixup and the Caddy CA re-mint.
4. **Prove it.** One end-to-end backup *and* restore on a clean host, which unblocks the
   [testing.md](ref/testing.md) restore drill (currently uncheckable).
5. **Only then add `[Install]`** to `mockingbuoy-backup.timer` and let `setup.sh` enable it when
   `BACKUP_DEST` is set.

**Tests:** the repo has **zero** backup/restore coverage. At minimum, cover the retention sweep and
the restore script's path/ownership handling; the systemd half stays a hardware-checklist item.

**Related:** [RM-018](#rm-018) (wheelhouse integrity — a DR redeploy depends on it),
[RM-005](#rm-005) (capture→replay, the other DR workflow), [ISSUE-024](issues.md#issue-024)
(the unimplemented self-exit path the DR story assumes).

### RM-018 — Wheelhouse `MANIFEST.sha256` · planned · S

**Goal:** verify the offline wheelhouse against the lock's own hashes at build time and again at
`setup.sh` pre-flight, so wheelhouse rot is detected before a DR redeploy depends on it.

### RM-017 — Docs-lint CI step · planned · S

**Goal:** grep backticked paths / env vars / endpoints / flags in `docs/` against the tree and fail the
build on a dangling reference. The red-team found 7+ confirmed doc drifts this would have caught.

### RM-016 — aisprofile polish · planned · S-M each, opt-in

**Goal:** degradation counters on stderr, TAG-block timestamp parsing (activates the currently inert
concurrency stat), MWV reference-T builder, transport fault-injection profile. All off by default.

### RM-015 — Time-authority health surface · planned · S

**Goal:** expose fix age, tier handovers, and authority-vs-host offset on the Maintenance tab.
`nmea_sim/timeauthority.py` already tracks the tier; nothing renders it.

### RM-014 — TCP tap subscriber stats in Streams tab · planned · S

**Goal:** surface `_Client.dropped` (already counted in `nmea_sim/tcp_tap.py`, never read).
**Note:** the subscriber *cap* half of this item is **done** — `_DEFAULT_MAX_CLIENTS = 8` at
`tcp_tap.py:37`, enforced at `:147`. Only the stats readout remains.

### RM-013 — Scripted deterministic AIS targets · planned · M

**Goal:** resurrect the `ais_targets` config key (see [ISSUE-002](issues.md#issue-002)) as real
scripted contacts, plus `nav_status`/`destination` variety.
**Prereq:** boundary reflect/respawn is already done (`nmea_sim/realism.py:32,251-254`).

### RM-012 — SSE interest filtering + latest-wins state/health slots · planned · M

**Goal:** the code's own deferred R23/R25 (`web/app.py:70-73`) — subscribers declare interest; state
and health frames collapse to latest-wins rather than queueing.

### RM-011 — `/api/meta` single-source · planned · S-M

**Goal:** serve field ranges, roles, and sentence lists from one endpoint. Kills the confirmed
triplication of the state-range table across `validate.py`, `web/app.py`, and `static/app.js`.
**Value:** compounding — every future field addition currently needs three edits.

### RM-010 — UDP-broadcast / TCP-client output sinks · planned · S

**Goal:** plotter-native transports with zero serial hardware. The Writer seam in `nmea_sim/engine.py`
is already shaped for it.

### RM-009 — Per-value LIVE/SIM/OFF provenance on the state stream · done (2026-07-28) · M

**Shipped per FIELD and per PANEL**, not yet per individual readout. Provenance is recorded at the
`SharedState` write choke point and resolved at read time against router liveness, so the conning
panel pills now report where their values came from and a live tag expires with its source. The
defect that motivated it — `pill-attitude` reading LIVE over always-simulated pitch/roll because it
tracked the heading *channel* — is fixed and guarded by a test. Badging each readout individually is
a UI-only follow-up (see RM-028). Details in `docs/ref/architecture.md`, *Per-field provenance*.

<details><summary>Original entry</summary>

**Goal:** tag each state field with its source. Safety-relevant, and `nmea_sim/state.py` is a single
choke point — every writer funnels through one locked `SharedState.update()`, so provenance can be
recorded in one place rather than threaded through five call paths.

Closes the doc gap filed as [ISSUE-027](issues.md#issue-027): the user guide and design register
promise per-value tagging that does not exist. (An earlier note here claimed this would "make
`architecture.md` true — it already describes this as shipped"; that is stale, architecture.md has
since been corrected to state plainly that per-value provenance is *not* shipped.)

**The hard part is expiry, not capture.** "Last writer wins" is wrong here and unsafe: auto mode
requires `movement.mode: static`, so once a live source dies nothing rewrites `lat`/`lon` and the
field holds its last live value indefinitely — a naive tag would keep reading `LIVE` on a frozen
position forever, precisely the mistake the tag exists to prevent. Store `(source, timestamp)` per
field and re-resolve at serialization against `Router.winner()`, which already tracks liveness.
The per-input toggle makes this directly testable: mute a slot and assert the tag decays.

**Open question:** what `OFF` means for a *value* (state fields are not 1:1 with channels).
*Resolved:* dropped. Per-field tags are LIVE/SIM only; `OFF` stays channel-level on the Streams tab,
where it means "this channel is muted" — a question about the wire, not about a value's origin.

</details>

### RM-028 — Per-readout provenance badges on the conning display · planned · S

**Goal:** badge individual conning readouts, not just their panel. The data already exists
(RM-009 puts a sparse per-field `provenance` map on the `state` frame), so this is UI-only.

Attach points are cheap and were scouted during RM-009: `.coord-row` has an **empty third grid
column** (LAT/LON/UTC), and `.v-live`/`.v-sim` classes already sit on nearly every readout but both
currently resolve to plain text colour — a wired-up hook at zero layout cost. Constraint: the conning
layout is a one-screen lock with only a few px of slack in places (ISSUE-025/026), so a badge must
ride an existing line; anything adding a line is not viable. `#heading-big` and the ship/fuel numbers
are inside SVG viewBoxes, where an in-viewBox badge costs no layout at all.

### RM-008 — Scenario scripting · planned · M

**Goal:** timed `{at_s, action}` sequences over the existing allow-listed control vocabulary in
`web/app.py`, for repeatable DR drills.

### RM-007 — pyais payload schema-check test · planned · S

**Goal:** assert every generator payload key against `pyais` `.fields()`, so a silently-dropped key can
never ship again. Kills the bug class behind the original `shiptype`/`ship_type` defect permanently.
**Highest value-per-hour item on this list.**

### RM-006 — systemd watchdog · planned · S

**Goal:** stdlib `sd_notify`, `Type=notify`, `WatchdogSec=30`. The stub already sits commented in
`ops/mockingbuoy.service`.
**Prereq:** met — `HealthReport` now folds `replay_alive`/`inputs_alive` into `ok`, so `ok` is
trustworthy enough to gate a watchdog ping on.

### RM-005 — Capture → replay round-trip in the UI · planned · S-M

**Goal:** `GET /api/captures` + a basename dropdown reusing `_resolve_profile_basename`. Closes the
flagship DR workflow and the `replay.file` path-probe hygiene gap.

### RM-004 — Close the config lifecycle loop · planned · S

**Goal:** reload-on-start + an explicit "Save & restart engine" action + a saved-vs-running diff.
**Note:** the *correctness* half is **done** — `EngineManager.set_config` (`web/app.py:1086`) is called
on successful persist (`:1877`), so saves no longer strand the boot config or destroy each other.
What remains is the UX: the operator still can't see that saved ≠ running, or apply it in one click.
