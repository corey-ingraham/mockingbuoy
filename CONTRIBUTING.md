# Contributing

## Development setup

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install                                 # optional: run linters on commit
```

## Checks (match CI)

```bash
ruff check .        # lint
black --check .     # format
mypy                # types
pytest              # tests
```

CI also enforces a **public-artifact scrub gate** (R39) that fails the build if any
tracked file contains operator/environment specifics, real coordinates/identifiers, or
tooling references — see `.github/workflows/ci.yml`. Keep tracked code, tests, and docs
generic: placeholders and synthetic values only. CI additionally installs the pinned
`requirements.txt` with `--require-hashes` and shellcheck-lints the ops scripts, so a
lockfile or shell regression fails CI even when the checks above pass locally.

## Run without hardware

```bash
python main.py --backend log     # print sentences to stdout
python main.py --backend pty     # emit to a virtual serial port for a test reader
```

## Architecture in one rule

Strict one-way layering — the engine core stays independent of the web layer:

```
web/ (FastAPI + SSE)  ->  nmea_sim/ (engine)  ->  serialport / writers / generators / state / config
```

`nmea_sim/` MUST NOT import the web layer, the ASGI server, or any GUI toolkit. See
[docs/ref/architecture.md](docs/ref/architecture.md) for module contracts and the threading model.

## Invariants to preserve

These are correctness- and safety-critical; tests enforce several of them.

1. **COG ≠ heading.** RMC/VTG use course-over-ground; HDT/HDG/HDM use heading. Never cross-wire.
2. **CRLF as explicit bytes** — write `b"\r\n"`; open serial ports in binary, never text mode.
3. **Valid XOR checksum** on every `$` sentence (chars between `$`/`!` and `*`, 2-digit upper hex).
4. **AIS via `pyais`** — never hand-roll 6-bit payload armoring. Class A → Type 1/2/3, Class B → Type 18.
5. **`write_timeout` > 0** (use `1.0`; `0` busy-loops the CPU). One port failing must not stop the others.
6. **Single ASGI worker** — the serial-driving threads are in-process and ports are opened exclusively;
   never run multiple workers or an auto-reloader.
7. **No secrets in the repo, image, or config** — the web credential hash and TLS CA are generated
   on-device at setup (see [docs/ref/security.md](docs/ref/security.md)).
8. **Baud budget** — enabled sentences × rates must stay under ~80% of a line's `baud/10` char/s;
   the config loader guards this. See [docs/ref/nmea-reference.md](docs/ref/nmea-reference.md).

## Hardware / deployment gotchas

Bind serial devices by stable `/dev/serial/by-id/...` paths; watch for the FTDI latency timer and
`brltty` grabbing ttys; the app self-exits non-zero when unrecoverable so systemd (`Restart=on-failure`)
recycles it. Details in [docs/ref/serial-hardware.md](docs/ref/serial-hardware.md) and
[docs/ref/deployment.md](docs/ref/deployment.md).

## Working files git does not carry

Several working files are **deliberately git-ignored**, so `git clone` / `git pull` does **not** give a
second machine a complete working setup. Hard rule #8 keeps the deployment's use case, site names,
adapter serials and local paths out of a public artifact — which is correct, but it means these have to
be synced out of band (a synced cloud folder, or a manual copy):

| Path | What it is | Why it is ignored |
|---|---|---|
| `CLAUDE.md` | repo assistant instructions, incl. the gotcha quicklist | carries use case + bench topology |
| `profiles/*.local.json` | realism profiles for a real area | location-specific statistics |
| `data/config.local.json` | the runtime config actually loaded | host-specific device paths |
| `private/` | raw AIS exports, design notes | source data, and large |
| `.claude/settings.json` | per-project assistant settings | machine-local |
| `secrets/` | web cred hash, CA key | generated on-device, never shared |

Only `data/config.local.json` and `secrets/` are genuinely per-host and should stay that way; the rest
are working artifacts you want on every machine you develop from.

**Setting up a second machine:**

```bash
git clone https://github.com/<you>/mockingbuoy.git && cd mockingbuoy
pip install -e ".[dev]"
ruff check . && black --check . && mypy && pytest     # should be all green
# then copy the out-of-band files above into place
```

Nothing in the ignored set is needed to run the test suite or the no-hardware modes
(`--backend log` / `--backend null`), so a bare clone is immediately useful — it just will not have the
local realism profile or the assistant instructions.

## Commits

Conventional-commit style is appreciated (`feat:`, `fix:`, `chore:`, `docs:`). Keep local editor and
tooling configuration out of the repo — it's git-ignored.
