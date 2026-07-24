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
`brltty` grabbing ttys; a container that goes unhealthy but stays alive is restarted by having the app
self-exit. Details in [docs/ref/serial-hardware.md](docs/ref/serial-hardware.md) and
[docs/ref/deployment.md](docs/ref/deployment.md).

## Commits

Keep the project tool-agnostic: no editor/tool-specific config files in the repo (they are
git-ignored). Conventional-commit style is appreciated (`feat:`, `fix:`, `chore:`, `docs:`).
