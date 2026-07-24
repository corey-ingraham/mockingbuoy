# Deployment

The runtime is **native**: a Python virtualenv driven by **systemd**, fronted by a
natively-installed **Caddy** reverse proxy. Deployment is just the repo checkout, a venv, and
two systemd units — nothing else to build or ship.

## Install (primary)

On the target host (needs internet only to install apt packages + Python wheels):
```bash
curl -fsSL https://raw.githubusercontent.com/<you>/mockingbuoy/main/bootstrap.sh | sudo bash
```
`bootstrap.sh` installs `git`+`curl`, clones the repo into `/opt/src/mockingbuoy` (idempotent: it
`git pull`s if the clone already exists), and execs `./setup.sh`, which (idempotently):
1. installs `python3-venv`/`python3-pip`, **Caddy** (official apt repo), `chrony`, `ufw`; creates a
   dedicated non-login service user `mockingbuoy` and adds it to `dialout`;
2. builds the venv at `/opt/mockingbuoy/.venv` and `pip install --require-hashes` (or from the local
   wheelhouse if offline);
3. host config: udev `by-id` symlinks (`/dev/nmea-*`), purge/mask `brltty`, set FTDI `latency_timer=1`,
   time sync (chrony) + `timedatectl set-timezone UTC`, optional firewall hardening (see security.md);
4. generates the Caddy custom root CA and a first-run web password (printed once), and writes the
   non-secret runtime env to `secrets/service.env` (site, Basic-auth user, bcrypt hash — see below);
5. installs the systemd units (`mockingbuoy.service`, Caddy drop-in), `daemon-reload`, `enable --now`;
6. builds the offline **wheelhouse** and enables the host backup timer;
7. prints the web URL, the one-time password, the CA file to trust on clients, and the per-channel
   TCP-tap `host:port` list.

Both services run on boot via systemd and have **no runtime internet dependency**.

### Tuning the run (`setup.env`)

`setup.sh` reads an optional `setup.env` (copy `setup.env.example` and edit; git-ignored) for the
non-default knobs. Unset values fall back to sensible defaults:

| Variable | Purpose | Default |
|---|---|---|
| `MOCKINGBUOY_SITE` | LAN address Caddy publishes on `:443` (NIC IP or local hostname; never `0.0.0.0`) | `<LAN_IP>` |
| `ALLOW_SUBNET` | Management subnet allowed through UFW to `443` (and the tap ports) | LAN `<subnet>` |
| `APP_PORT` | Loopback port the app binds; Caddy reverse-proxies to it | `8000` |
| `TAP_PORTS` | Per-channel raw NMEA-over-TCP tap ports (`nc`/OpenCPN) | per-channel list |
| `CHRONY_SERVER` | NTP source for `chrony` time sync | distro pool |
| `BACKUP_DEST` | rsync destination for the host backup timer (writes `MOCKINGBUOY_BACKUP_DEST`) | unset (backup disabled) |
| `ENABLE_UFW` | Set to `true` to apply the default-deny UFW hardening (any other value skips it) | `false` (skipped) |

Runtime secrets/site values land in `secrets/service.env` (git-ignored; `0600`), read by both
`mockingbuoy.service` and the Caddy drop-in — see `secrets/service.env.example` for the exact
names (`MOCKINGBUOY_SITE`, `MOCKINGBUOY_BASIC_USER`, `MOCKINGBUOY_BASIC_HASH`,
`MOCKINGBUOY_BACKUP_DEST`). Never commit a plaintext password — store only the `caddy hash-password`
bcrypt hash.

## Service topology

Two native systemd services:
- **`mockingbuoy.service`** — runs `uvicorn web.app:app --workers 1 --no-access-log` (never `--reload`)
  from the venv, as the non-login `mockingbuoy` user (member of `dialout` for serial). Hardened with
  systemd sandboxing — drop privileges, make the filesystem read-only, and cap resources: `NoNewPrivileges`,
  `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, empty `CapabilityBoundingSet`, restricted address
  families, `MemoryMax`/`TasksMax`, `Restart=on-failure`, `ReadWritePaths=/opt/mockingbuoy/data`. The
  app binds **loopback only**; Caddy reaches it over localhost. No host port is published by the app.
- **`caddy.service`** (native package) — TLS termination + Basic auth (see security.md). Has
  `cap_net_bind_service` to bind 443 and publishes `<LAN_IP>:443` only.

> **Serial gotcha:** do **not** set `PrivateDevices=yes` on the service — it hides `/dev/tty*`. Grant
> serial access via the `dialout` group plus explicit `DeviceAllow=` rules for the char devices.

## Device access

The service reaches serial adapters by their stable `by-id` path (a udev rule installed by `setup.sh`
also creates friendly `/dev/nmea-*` symlinks). Because it is a native process, device access is just the
`dialout` group plus explicit `DeviceAllow=` rules in the unit — a replug re-creates the same `by-id` node
and the port's tolerant open picks it back up. Point each channel's `path` at `/dev/serial/by-id/...`
(or `/dev/nmea-*`).

Grant the device nodes to the service in the unit:
```ini
DeviceAllow=/dev/nmea-gps rw
DeviceAllow=/dev/nmea-heading rw
DeviceAllow=/dev/nmea-ais rw
SupplementaryGroups=dialout
```

## Time / clock

Many small single-board hosts have no battery-backed RTC, so the clock may be wrong until sync. Keep the OS in UTC. The app's `time_source.mode`
controls dated sentences:
- `system_utc` (default) — use OS UTC; gate ZDA/RMC dates on `timedatectl … NTPSynchronized == yes`.
- `simulated` — fixed epoch + rate multiplier (repeatable scenarios; requires `epoch`).
- `hold` — freeze time.

## Offline redeploy (no rebuild, no internet)

`setup.sh` builds a local **wheelhouse** (`pip download` of the hash-locked set). To rebuild the venv on
a fresh host with no internet:
```bash
python3 -m venv /opt/mockingbuoy/.venv
/opt/mockingbuoy/.venv/bin/pip install --no-index --find-links=wheelhouse --require-hashes -r requirements.txt
```
Nothing else to fetch — just the wheelhouse and the repo checkout, so the rebuild has no runtime
internet dependency.

## Updates

`git pull && sudo ./setup.sh` — the idempotent re-run rebuilds the venv and restarts the services. Or
rebuild the venv offline from the wheelhouse (above) and `systemctl restart mockingbuoy`.

## Autostart

`systemctl enable --now mockingbuoy caddy` makes both come up on boot. No desktop session is involved;
the engine runs headless whether or not a browser is connected.

## Dry run (no hardware, no web)

Exercise the engine straight from the venv before wiring adapters:
```bash
/opt/mockingbuoy/.venv/bin/python main.py --validate-only          # deep-validate the config
/opt/mockingbuoy/.venv/bin/python main.py --backend log            # print every sentence to stdout
/opt/mockingbuoy/.venv/bin/python main.py --backend null --duration 5   # timing smoke test
```

## Hardware-in-the-loop checklist (manual)

- `ls -l /dev/nmea-*` resolves to the intended adapters.
- Each channel emits the right talker at the configured rate (verify with a serial terminal).
- Unplug one adapter → that channel marks down while the others keep emitting.
- `/healthz` returns 503 when a port is down; the service self-exits and systemd restarts it if
  unrecoverable (`Restart=on-failure`).
- Reboot → both services come back automatically.
