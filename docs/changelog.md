# Changelog

Dated record of substantive changes. Newest first. ISO 8601 dates.

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
