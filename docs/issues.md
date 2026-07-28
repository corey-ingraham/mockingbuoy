# Issues

Defect / incident log. Newest first. `ISSUE-NNN` shares one counter with `RM-NNN` in
[roadmap.md](roadmap.md) — next free number across **both** files.

Status ∈ {planned, in-progress, done, deferred}.

> **Provenance.** ISSUE-001..003 are the residue of a whole-repo red-team performed against
> `ca7b284` (branch `feat/tabbed-ui-instruments`). Everything else from that pass — 3 critical,
> 10 high, and 21 medium findings — was remediated across the commits landing `36bf3f6..bd5e37d`
> and re-verified in-tree on 2026-07-27. See [changelog.md](changelog.md#2026-07-27--red-team-triage).

> **Lab session 2026-07-27.** ISSUE-019..022 and [RM-023](roadmap.md#rm-023) come from bench testing
> on the `eemslab` appliance. Source notes live **only** on that box at `~/repos/eemslab/docs/` —
> that directory is **not under git** and has no remote, so it is one SD-card failure from gone.

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

**Fix:** split the except clauses. Absent device stays quiet; `PermissionError` and `ValueError` log
loudly and set a distinct status (`open_error` with the exception repr) that reaches `HealthReport`
and the UI. This is the root cause behind both bench failures — fix it before the cosmetic items.

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

### ISSUE-003 — Voltage sensing is half-wired: no provider is ever constructed · deferred (2026-07-27)

`nmea_sim/voltage.py:64` defines `AdcVoltageProvider` and the `voltage_sense` config block parses,
but nothing in `nmea_sim/engine.py` or `web/app.py` ever constructs a provider, so the path is inert.
`web/app.py:824` deliberately keeps `voltage_sense` out of the persist allow-list.

**Not a doc bug** — `docs/user-guide.md:317` correctly labels the tiles "(planned)", so no shipped
claim is false. Deferred as reserved-but-unbuilt design surface.

**Decide:** build it (needs ADS1115 hardware + analog front-end) or delete the dead
`AdcVoltageProvider`/config block. Do not leave it ambiguous a third time.

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
