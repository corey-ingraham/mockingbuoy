# mockingbuoy

A multi-port **NMEA 0183 simulator/generator** for developing, testing, and demonstrating
marine electronics and NMEA-consuming systems.

One Python process drives **N independent USB serial channels** from a single synchronized
vessel state, and serves a small **web UI** (live sentence monitor + control forms) over the LAN.
It runs as a hardened native service (venv + systemd + Caddy) on a Raspberry Pi (or any Linux
host) and needs no desktop.

## What it does

- **GPS** channel — `GGA`, `RMC`, `VTG`, `ZDA` (+ `GLL`), `GP` talker
- **Heading** channel — `HDT` (+ `HDG`/`HDM`), `HE` talker
- **AIS** channel — `!AIVDM`/`!AIVDO` (own-ship + optional simulated targets), via a proven encoder
- One shared vessel state keeps position, course, speed, and heading **synchronized** across all channels
- **Static** or **moving** modes (geodetically-correct dead reckoning)
- Runtime control from the browser — edit position/course/speed/heading/AIS without restarting
- **Hardware-agnostic:** any USB-serial adapter; each channel is **simplex (TX) or full-duplex (TX+RX)**
  purely by config. RX (where enabled) parses inbound sentences and can optionally feed the sim state.

## Key properties

- Valid NMEA XOR checksums on every `$` sentence; correct talker IDs; COG and heading kept distinct
- Independent, configurable per-sentence update rates with a baud-budget guard
- TLS + authentication on the web UI (reverse proxy); no unauthenticated control
- Runs headless under systemd with `Restart=on-failure`; **no runtime internet dependency**
- Local wheelhouse for rebuild-free offline redeploy

## Install (Raspberry Pi)

```bash
curl -fsSL https://raw.githubusercontent.com/<you>/mockingbuoy/main/bootstrap.sh | bash
```

This installs Caddy, builds the venv, configures the host, generates the web credential and a
local TLS certificate authority, and enables the systemd services. It prints the web URL, a one-time
password, and the CA file to trust on client machines. See [docs/ref/deployment.md](docs/ref/deployment.md).

## Configure

Edit `config.json` (or a git-ignored `config.local.json`). Channels are a generic list — GPS,
heading, and AIS are just three configured instances. Set each channel's serial `path` to a stable
`/dev/serial/by-id/...` value. See [docs/ref/serial-hardware.md](docs/ref/serial-hardware.md) and
[docs/ref/architecture.md](docs/ref/architecture.md).

## Develop / test (no hardware)

```bash
pip install -e ".[dev]"
pytest
python main.py --backend log     # print sentences to stdout
python main.py --backend pty     # emit to a virtual serial port for a test reader
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and the invariants to preserve.

## Docs

| Doc | Contents |
|---|---|
| [architecture.md](docs/ref/architecture.md) | Modules, threading, channels model, web↔engine bridge, testing |
| [nmea-reference.md](docs/ref/nmea-reference.md) | Sentence field maps, checksum, coordinate conversion, AIS, baud budget |
| [serial-hardware.md](docs/ref/serial-hardware.md) | USB-serial: simplex/duplex, persistent naming, latency, brltty |
| [deployment.md](docs/ref/deployment.md) | systemd + venv + Caddy, device access, offline wheelhouse redeploy, time sync, autostart |
| [security.md](docs/ref/security.md) | Threat model, TLS + CA trust, auth, secrets, firewall, backups |

## License

MIT — see [LICENSE](LICENSE).
