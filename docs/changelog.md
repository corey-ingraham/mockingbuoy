# Changelog

Dated record of substantive changes. Newest first. ISO 8601 dates.

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
