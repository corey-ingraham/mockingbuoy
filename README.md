# mockingbuoy

A multi-port **NMEA 0183 simulator/generator** for developing, testing, and demonstrating
marine electronics and NMEA-consuming systems.

One Python process drives **N independent USB serial channels** from a single synchronized
vessel state, and serves a small **web UI** (live sentence monitor + control forms) over the LAN.
It runs as a hardened native service (venv + systemd + Caddy) on a Raspberry Pi (or any Linux
host) and needs no desktop.

## What it does

- **GPS** channel — `GGA`, `RMC`, `VTG`, `ZDA` (+ `GLL`), `GP` talker
- **Heading** channel — `HDT` (+ `HDG`/`HDM`/`THS`), `HE` talker
- **Instrument** channel — motion/environment suite `VHW`, `DPT`, `DBT`, `MWV` (apparent wind),
  `MWD` (true wind), `ROT`, `XDR`, `RSA`, `VDR`, `$PASHR`, `II` talker
- **AIS** channel — `!AIVDM`/`!AIVDO` (own-ship + optional simulated targets), via a proven encoder
- **AIS area realism** — shape surrounding contacts from a statistics-only realism profile distilled
  from a public dataset or your own receiver capture (synthetic MMSIs, nothing real rebroadcast,
  own-ship always simulated); replay adds a **full vs ais-only scope** so contacts-only captures leave
  own-ship simulated
- One shared vessel state keeps position, course, speed, heading, **speed-through-water, depth, wind,
  rate-of-turn, rudder, and set/drift synchronized** across all channels; apparent wind is derived from
  true wind + vessel motion so the two can never disagree
- **Sea-state motion model (WMO 0–9)** — pitch/roll are derived so the hull is always gently in motion,
  never a dead-flat "stuck sensor"
- **Static** or **moving** modes (geodetically-correct dead reckoning); route/waypoint playback
- **Three operating modes** — **simulate** (fully synthetic), **auto** (priority-routed *verbatim*
  passthrough of real NMEA on physical inputs with sentence-class cross-routing and seamless failover to
  generation on input loss), and **replay** (re-inject a captured NMEA file through the same writer path)
- **Conning display** — glanceable SVG gauges (compass, rate-of-turn, inclinometer, wind rose), with
  colourblind-safe **LIVE / SIM / OFF** provenance tagging **per channel** (on the health event and the
  NMEA Streams panes)
- **Per-channel runtime toggle** — enable/disable any output channel live, no restart, with a persisted
  default
- **Maintenance diagnostics** — a bench NMEA workbench (multi-port monitor, per-port stats, auto-baud
  sweep, click-to-decode, and a guided fault advisor that infers causes like a reversed A/B pair),
  plus **`mockingbuoy-mon`**, a web-free CLI peer to the same diagnostics core for headless/SSH/DR use
- Runtime control from the browser — edit position/course/speed/heading/AIS/instruments without restarting
- **Hardware-agnostic:** any USB-serial adapter; each channel is **simplex (TX) or full-duplex (TX+RX)**
  purely by config. RX (where enabled) parses inbound sentences and can optionally feed the sim state.

## Key properties

- Valid NMEA XOR checksums on every `$` sentence; correct talker IDs; COG and heading kept distinct
- Independent, configurable per-sentence update rates with a baud-budget guard
- TLS + authentication on the web UI (reverse proxy); no unauthenticated control
- Runs headless under systemd with `Restart=on-failure`; **no runtime internet dependency**
- Local wheelhouse for rebuild-free offline redeploy

## Install (one command)

On a fresh Linux host (Raspberry Pi or any Debian/Ubuntu box), run:

```bash
curl -fsSL https://raw.githubusercontent.com/corey-ingraham/mockingbuoy/main/bootstrap.sh | sudo bash
```

`bootstrap.sh` installs `git`+`curl`, clones this repo, and hands off to `setup.sh`, which
provisions everything natively — the Python **venv**, the native **Caddy** reverse proxy, and the
**systemd** services — then configures the host, generates a local TLS **certificate authority** and
a **one-time web password**, and starts the service listening on `<LAN_IP>:443`. It needs internet
only to install (apt + pip); there is **no runtime internet dependency**. Tune the run with a
`setup.env` file (`MOCKINGBUOY_SITE`, `ALLOW_SUBNET`, `APP_PORT`, `TAP_PORTS`, …) — see
[docs/ref/deployment.md](docs/ref/deployment.md).

At the end `setup.sh` prints the web URL, the one-time password, the CA file to trust on client
machines, and the per-channel TCP-tap `host:port` list.

**Reach the web UI:** browse to `https://<LAN_IP>/` and authenticate with HTTP **Basic auth**
(username + the one-time password printed on install — change it with `caddy hash-password` and
update `secrets/service.env`). Trust the printed CA file on the client to silence the TLS warning.

**Raw NMEA over TCP:** each channel is also tapped as a plain TCP stream for chart plotters and
loggers. Read a channel with `nc`:

```bash
nc <LAN_IP> <port>          # raw NMEA 0183 sentences for that channel
```

or point OpenCPN at the same `<LAN_IP>:<port>` as a **Network → TCP** data connection. The tap ports
are printed on install and set via `TAP_PORTS` in `setup.env`.

## Update / offline redeploy

Update in place from the repo checkout:

```bash
cd /opt/src/mockingbuoy && git pull && sudo ./setup.sh
```

The re-run is idempotent — it rebuilds the venv and restarts the services. For a host with **no
runtime internet dependency**, rebuild the venv from the local **wheelhouse** instead of fetching
wheels; see the offline-redeploy steps in [docs/ref/deployment.md](docs/ref/deployment.md).

## Configure

Edit `config.json` (the tracked defaults) or a git-ignored `data/config.local.json` — the latter is what
the service actually reads when present (`MOCKINGBUOY_CONFIG` overrides it; otherwise
`data/config.local.json`, then `config.json`). Channels are a generic list — GPS, heading, and AIS are
just three configured instances. Set each channel's serial `path` to a stable
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
| [testing.md](docs/ref/testing.md) | Verification plan: CI gate, dry-run (no hardware), hardware-in-the-loop checklist, offline redeploy |

## License

MIT — see [LICENSE](LICENSE).
