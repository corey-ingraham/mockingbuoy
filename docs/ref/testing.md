# Testing & Verification

The consolidated verification plan for the whole project: what the automated gate proves, how to
exercise the engine and web UI with **no hardware**, the **hardware-in-the-loop** runbook for the
target host, and how to verify an **offline redeploy**. It complements the CI summary in
[architecture.md](architecture.md) and the deploy/hardening runbooks in
[deployment.md](deployment.md) and [security.md](security.md).

Four layers, cheapest first:

1. **Automated (CI)** — ruff/black/mypy/pytest on every push and PR. No hardware, no network.
2. **Dry run (no hardware)** — drive the real engine from a shell against a virtual or log backend.
3. **Hardware-in-the-loop** — a manual checkbox runbook on the target host with real adapters.
4. **Offline redeploy** — rebuild the venv from the wheelhouse with no network and confirm boot.

---

## Automated (CI)

`.github/workflows/ci.yml` runs on push/PR to `main`: install `-e ".[dev]"`, then **ruff → black →
mypy → pytest**. Any failure fails the build. The `lint-and-test` job runs on **Linux** (ubuntu) across a
**Python 3.11 + 3.13** matrix — the two versions the project actually runs on (the Pi appliance ships
3.11, the dev workstation runs 3.13); local dev is typically **Windows** — see the platform split below.

Alongside `lint-and-test`, the workflow runs separate **build-failing** gate jobs:
- **`scrub-scan` (R39 public-artifact gate)** — `git grep` over **tracked** files for banned tokens
  (assistant/tooling references, a stray codename, and offline-framing phrases); any match fails the
  build. Keep repo content on placeholders and synthetic values. (The workflow file itself is excluded,
  since it necessarily embeds the token list.)
- **`lock-install`** — installs the hash-locked `requirements.txt` with `--require-hashes` exactly as the
  Pi does (catches a broken/OS-mismatched lock that `-e .[dev]` would never surface).
- **`pip-audit`** — a real CVE gate over the **locked** runtime deps (`requirements.txt`), not the
  unpinned dev env.
- **`shellcheck`** — lints the root-run `setup.sh`/`bootstrap.sh` bash.

### The four checks

| Check | Command | Proves |
|---|---|---|
| Lint | `ruff check .` | style/bugs (`E,F,I,UP,B,SIM,C4`), line length 100 |
| Format | `black --check .` | formatting is canonical, line length 100 |
| Types | `mypy` | strict typing (`disallow_untyped_defs`) over `nmea_sim/`, `web/`, `main.py` |
| Tests | `pytest` | the suite in `tests/` (quiet mode, `testpaths=["tests"]`) |

mypy's `files` covers **product code only**; test modules are not type-checked by CI but are still
written strict and fully annotated (`from __future__ import annotations`, complete signatures).

### Platform split (critical)

The `pty` writer backend uses `os.openpty`, which is **UNIX-only**. Any test that opens a pty is
guarded so it **skips on Windows and runs on CI**:

```python
posix_only = pytest.mark.skipif(os.name != "posix", reason="requires POSIX pty")
```

To avoid a Windows-only skip silently dropping a core assertion, the *core* behavior is proven with
**platform-neutral** tests (sockets, the log/null backends, direct engine/API calls, the FastAPI
`TestClient`); pty is used only where it adds unique value (real serial-node round-trip). Result:
the same core guarantees hold on both Windows dev and Linux CI; CI additionally exercises the pty path.

### Test layers

- **Unit (no hardware)** — `test_checksum` (XOR known-answers), `test_navigation` (`dead_reckon` vs a
  geographiclib reference, coordinate conversion edges, magnetic-variation consistency),
  `test_gps_generator` / `test_heading_generator` / `test_ais_generator` (build → re-parse via
  `pynmea2.parse` / `pyais.decode`, valid checksum, correct talker, **RMC/VTG use `cog_deg` while
  HDT/HDG/HDM use `heading_*` — never cross-wired**), `test_config` / `test_validate` (load/save
  round-trip + the validation matrix), `test_budget` (baud-budget calculator), `test_realism`
  (deterministic spawn from a synthetic profile).
- **Engine** — `test_engine` and `test_traffic_engine` drive a real `Engine` with an injected
  `sink_hook` (a `CollectingWriter`) so emitted lines are captured **in-process, without hardware**:
  drift-free timing, strict-budget abort vs `--no-strict-budget` warn, per-channel fan-out and
  failure **isolation** (one writer raising must not stop siblings), and own-ship + synthetic-traffic
  interleaving. `test_state_writers` / `test_writers` cover the `Writer` backends; `test_rx` covers
  the RX parse + gated state-feed path.
- **Web** — `test_web` uses the FastAPI `TestClient` **as a context manager** (so the lifespan
  auto-starts/stops the engine): index serves HTML, `/healthz` 200↔503 transitions, `/api/config`
  and `/api/state`, `/api/control` (start/stop/update, 409 update-while-stopped, 400 unknown-action
  and out-of-range), and a deterministic `Broker` fan-out/drop-oldest unit test for the SSE bridge.
- **Integration / cross-phase** — headless runs via `main.py`, the TCP tap (`test_tcp_tap`: bind,
  accept, broadcast, drop-oldest, ignore inbound), and profile-driven traffic through the engine.
  These stitch config → engine → writer/tap together rather than repeating a single layer.
- **Config-validation matrix** — `test_validate` walks good and bad configs: missing/duplicate
  channel ids, unknown sentences, over-budget rates, bad `direction`/`framing`, malformed AIS/traffic
  blocks — asserting `config.validate()` returns the expected problem list (empty == valid).

**Anti-flake rule:** never assert an exact sentence **count** over a wall-clock duration (thread
timing varies across machines and CI). Assert **structural correctness** (valid checksum, correct
talker, decodes cleanly, position in-region) plus **at-least-one** within a generous bounded wait
(poll up to ~5 s). Every blocking wait is bounded so a hang can't stall CI.

### Run the gate locally

From the repo root, in the project venv:

```bash
python -m ruff check .
python -m black --check .
python -m mypy
python -m pytest -q
```

Or the single-line gate (stops at the first failure):

```bash
python -m ruff check . && python -m black --check . && python -m mypy && python -m pytest -q
```

`pre-commit` runs the fast checks on commit; see `.pre-commit-config.yaml`. On Windows the pty tests
report as **skipped** — that is expected; CI runs them.

---

## Dry run (no hardware)

Drive the real engine straight from the venv — no adapters, no browser. `main(argv) -> int`; exit
**2** = bad/missing config, **1** = invalid config or other error, **0** = ok.

```bash
# Deep-validate the config, print any problems, and exit (0 valid / 1 invalid / 2 unreadable).
python main.py --validate-only
python main.py --config config.local.json --validate-only

# Print every sentence to stdout — eyeball talkers, rates, and checksums.
python main.py --backend log

# Emit nowhere for a fixed duration — a pure timing/budget smoke test.
python main.py --backend null --duration 5

# Relax the strict baud-budget guard from abort to warn.
python main.py --backend log --no-strict-budget
```

**Virtual serial port (`pty`, UNIX host).** `--backend pty` opens a pseudo-terminal per channel and
prints each slave device path; point a reader at it:

```bash
python main.py --backend pty          # prints e.g. gps -> /dev/pts/N per channel
```

```python
# reader.py — read one channel's virtual port with pyserial
import serial
port = serial.Serial("/dev/pts/N", 4800, timeout=2)   # path printed above; baud per channel
for _ in range(20):
    print(port.readline().decode(errors="replace").rstrip())
```

**TCP tap.** When tap ports are configured, read a channel's raw NMEA stream over TCP — no serial
hardware at all:

```bash
nc <LAN_IP> <tap_port>        # raw NMEA 0183 for that channel (Ctrl-C to stop)
```

For each dry-run channel, confirm: sentences carry a **valid XOR checksum**, the **talker** is right
(`GP` GPS, `HE` heading, `!AIVDM`/`!AIVDO` AIS), lines **decode** (`pynmea2` / `pyais`), and position
stays **in-region**. Do not assert an exact count over a duration — assert structure + at-least-one.

---

## Hardware-in-the-loop checklist (manual, on the target host)

Run on the real host with adapters attached, after `setup.sh`. Tick each box.

**Serial output — three captures.**
- [ ] `ls -l /dev/nmea-*` (or `/dev/serial/by-id/...`) resolves to the intended adapters.
- [ ] GPS channel: a serial terminal shows `GP` sentences (`GGA`/`RMC`/`VTG`/`ZDA`) at the configured rates.
- [ ] Heading channel: shows `HE` sentences (`HDT` + `HDG`/`HDM`/`THS`) at the configured rate.
- [ ] Instrument channel: shows `II` sentences (`VHW`/`DPT`/`DBT`/`MWV`/`MWD`/`ROT`/`XDR`/`RSA`/`VDR`/`$PASHR`) at the configured rates.
- [ ] AIS channel: shows `!AIVDM`/`!AIVDO` at the configured rate; every `$` sentence has a valid checksum.

**Channel isolation.**
- [ ] Unplug **one** adapter → that channel marks **down** in the UI while **the others keep emitting**.
- [ ] Replug the same adapter → its stable `by-id` node returns and the tolerant open recovers the channel.

**Web UI + auth.**
- [ ] `https://<LAN_IP>/` prompts for HTTP **Basic** auth and, once authenticated, serves the tabbed UI
      with the **NMEA Streams** tab showing live per-channel sentence panes streaming over SSE.
- [ ] An **unauthenticated** request returns **401** (e.g. `curl -k https://<LAN_IP>/` with no creds).
- [ ] A control edit (position/course/speed/heading/AIS) is reflected live in all channels without a restart.

**TCP taps.**
- [ ] `nc <LAN_IP> <tap_port>` renders raw NMEA for each configured tap.
- [ ] OpenCPN (**Network → TCP**, `<LAN_IP>:<tap_port>`) renders each tap as a live data connection.

**Health → self-exit → restart.**
- [ ] Kill/pull a serial port so it can't recover → `/healthz` returns **503**.
- [ ] The service **self-exits non-zero** and **systemd restarts it** (`Restart=on-failure`).

**Autostart.**
- [ ] **Reboot** → both the app service and Caddy come back automatically (`systemctl enable --now`).

**Backups + restore drill.**
- [ ] The host **backup timer** runs and writes to the configured destination.
- [ ] Restore drill: on a clean host, re-run `setup.sh`, **repopulate `data/`** (+ `config.json`,
      `secrets/`, Caddy data dir, udev rules), fix ownership, restart — `/healthz`, live feed, and
      Basic login all pass.

**TLS trust.**
- [ ] Import the generated **CA root cert** on a client machine → `https://<LAN_IP>/` is trusted with
      no certificate warning.

---

## Offline redeploy verification

Prove there is **no runtime internet dependency**: rebuild the venv from the local **wheelhouse**
with the network **off**, then confirm the service starts.

```bash
# 1. Rebuild the venv from the wheelhouse only — no index, hash-verified.
python3 -m venv .venv
.venv/bin/pip install --no-index --find-links=wheelhouse --require-hashes -r requirements.txt

# 2. Validate + a headless smoke run (still no network).
.venv/bin/python main.py --validate-only
.venv/bin/python main.py --backend null --duration 5

# 3. Restart the service and confirm health.
systemctl restart mockingbuoy
curl -k https://<LAN_IP>/healthz     # 200 with Basic creds once the engine is running
```

- [ ] `pip install` completes with **no network access** (fails loudly if any wheel is missing from
      the wheelhouse — that is the signal to rebuild it).
- [ ] `--validate-only` exits 0 and the null-backend smoke run stays up for its full duration.
- [ ] After restart, `/healthz` returns 200 and the live feed streams — the redeploy needs only the
      repo checkout + wheelhouse.
