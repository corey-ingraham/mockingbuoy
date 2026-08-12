# Deployment

The runtime is **native**: a Python virtualenv driven by **systemd**, fronted by a
natively-installed **Caddy** reverse proxy. Deployment is just the repo checkout, a venv, and
two systemd units — nothing else to build or ship.

## Install (primary)

On the target host (needs internet only to install apt packages + Python wheels):
```bash
curl -fsSL https://raw.githubusercontent.com/corey-ingraham/mockingbuoy/main/bootstrap.sh | sudo bash
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
6. builds the offline **wheelhouse**. It installs the host backup units but deliberately does **not**
   enable the timer — the backup subsystem is non-functional, see [ISSUE-001](../issues.md#issue-001);
7. prints the web URL, the one-time password, the CA file to trust on clients, and the per-channel
   TCP-tap `host:port` list.

Both services run on boot via systemd and have **no runtime internet dependency**.

### Tuning the run (`setup.env`)

`setup.sh` reads an optional `setup.env` (copy `setup.env.example` and edit; git-ignored) for the
non-default knobs. Unset values fall back to sensible defaults:

| Variable | Purpose | Default |
|---|---|---|
| `MOCKINGBUOY_SITE` | Primary site hostname/IP for mockingbuoy's Caddy vhost (`:443`). Operator value wins; a persisted one is preserved on re-run; else defaults to the friendly hostname | `mockingbuoy.eemslab.internal` |
| `MOCKINGBUOY_SITE_ALIAS` | Auto-set raw-IP alias (box's primary LAN IP) so the UI stays reachable by IP before local DNS resolves the name; dedups to `127.0.0.1` | auto (box IP) |
| `ALLOW_SUBNET` | Management subnet allowed through UFW to `443` (and the tap ports) | LAN `<subnet>` |
| `APP_PORT` | Loopback port the app binds; Caddy reverse-proxies to it | `8000` |
| `TAP_PORTS` | Per-channel raw NMEA-over-TCP tap ports (`nc`/OpenCPN) | per-channel list |
| `CHRONY_SERVER` | NTP source for `chrony` time sync | distro pool |
| `BACKUP_DEST` | rsync destination for the host backup timer (writes `MOCKINGBUOY_BACKUP_DEST`). **Inert either way** — the timer is `static` and never fires ([ISSUE-001](../issues.md#issue-001)); setting this only warns | unset |
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
  families, `MemoryMax`/`TasksMax`, `Restart=on-failure`, `ReadWritePaths=/opt/mockingbuoy/data`. Device
  access is confined by the **activated cgroup device controller** — an explicit `DeviceAllow=` allowlist,
  nothing else on `/dev`. The app binds a **unix socket** (`unix//run/mockingbuoy/app.sock`, owned by the
  service user, group-shared with Caddy at mode `0660`); Caddy reaches it over that socket. **No host TCP
  port is published by the app.**
  > **⚠ Validate on target hardware:** the unix-socket bind + Caddy-over-socket wiring is **not yet
  > validated on the target hardware**. On first deploy, confirm the socket path, its ownership/group and
  > `0660` perms, and that Caddy can proxy to it before treating the socket path as proven.
- **`caddy.service`** (native package) — TLS termination + Basic auth (see security.md). **Multi-site
  coexistence:** the systemd drop-in points Caddy at the **shared `/etc/caddy/Caddyfile`**, which
  `import`s every site under `/etc/caddy/conf.d/*.caddy`. mockingbuoy owns ONLY
  `/etc/caddy/conf.d/mockingbuoy.caddy`, so any other reverse-proxy sites on the box survive a
  mockingbuoy redeploy (setup.sh writes only that one snippet + adds the `import` line once, never
  rewriting the shared file, and validates the combined config with rollback before restarting). The
  **admin API stays disabled** (`admin off` in the shared globals), so a config change is a **restart**,
  not an API reload — a mockingbuoy redeploy/rotation briefly (~1 s) bounces all sites on the shared
  Caddy. The drop-in keeps `--adapter caddyfile` and deliberately **omits `--environ`** (which would
  print the Basic-auth hash to the journal). Deploying on a foreign LAN with friendly names + trusted
  TLS + a DNS container: see `ops/lan/README.md`.

> **Serial gotcha:** do **not** set `PrivateDevices=yes` on the service — it hides `/dev/tty*`. Grant
> serial access via the `dialout` group plus explicit `DeviceAllow=` rules for the char devices.

## Device access

The service reaches serial adapters by their stable `by-id` path (a udev rule installed by `setup.sh`
also creates friendly `/dev/nmea-*` symlinks). Because it is a native process, device access is just the
`dialout` group plus explicit `DeviceAllow=` rules in the unit — a replug re-creates the same `by-id` node
and the port's tolerant open picks it back up. Point each channel's `path` at `/dev/serial/by-id/...`
(or `/dev/nmea-*`).

Grant the device nodes to the service in the unit (outputs, plus the input slots used by Auto/replay, plus
— only if enabled — the ADC I2C bus):
```ini
DeviceAllow=/dev/nmea-gps rw
DeviceAllow=/dev/nmea-heading rw
DeviceAllow=/dev/nmea-ais rw
DeviceAllow=/dev/nmea-instrument rw
DeviceAllow=/dev/nmea-in-1 rw
DeviceAllow=/dev/nmea-in-2 rw
# … one per input slot in use (nmea-in-1 … nmea-in-6)
# DeviceAllow=/dev/i2c-1 rw   # only if the optional ADC voltage-sense add-on is enabled
SupplementaryGroups=dialout
```

### Input slots (Auto / replay)

Auto and replay read real NMEA on physical **input** slots. `setup.sh` provisions stable by-id udev
symlinks `/dev/nmea-in-1 … /dev/nmea-in-6`; grant each one in use with a `DeviceAllow=` line as above.
Wire each input through an **isolated, listen-only** RS-422 adapter — NMEA 0183 is a differential A/B bus,
so a listen-only isolated tap lets the tool read a live bus without driving or ground-looping it (wiring +
grounding detail → security.md).

### Optional ADC voltage-sense add-on

The Maintenance diagnostics can optionally read A/B voltages via a small ADC (e.g. ADS1115 over I2C) to
*confirm* an electrical fault. It is off unless configured. A **protective analog front-end is required
before tapping A/B**, and the unit grants only the specific I2C bus (`DeviceAllow=/dev/i2c-1 rw`). The app
runs fine with no ADC present. Config + safety detail → security.md.

## Time / clock

Many small single-board hosts have no battery-backed RTC, so the clock may be wrong until sync. Keep the OS in UTC. The app's `time_source.mode`
controls dated sentences:
- `system_utc` (default) — use OS UTC. Dated sentences (ZDA/RMC) are **always emitted**, not gated; the
  Time Authority only *tags* the clock's provenance (`ntp` vs `system`). That tag comes from a cheap,
  file-only probe of the systemd-timesyncd runtime marker (`/run/systemd/timesync/synchronized`,
  `nmea_sim/ntpsync.py`) — no `timedatectl`/`chronyc` fork. The probe returns "unsynced" only when it can
  positively confirm it, and otherwise degrades to "assume disciplined". Because `setup.sh` installs
  **chrony** (which removes timesyncd), that marker is absent on the shipped appliance, so the probe is
  UNKNOWN and tags `ntp`; a caller-side plausibility guard (year ≥ 2020) is the real sanity check.
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

## Client display / browser

The appliance has no browser and no desktop session — the UI is viewed from a separate workstation.
That machine's browser matters more than it looks like it should:

- **Engine floor: Chromium 84 or newer** (or the matching Firefox/WebKit). What the conning layout
  actually needs: CSS custom properties (49), `grid-template-areas` (57), `min()`/`clamp()` (79) and
  `gap` on a **flex** container (84 — much later than grid `gap`, which is 66). `dvh` wants 108 but is
  already paired with a `100vh` fallback, and `:has()` is not used. Any reasonably current build
  clears this; a long-lived embedded browser on an appliance-style console may not.
- **Prefer a kiosk-mode Chromium or Firefox** (`--kiosk` / `--kiosk`) over an embedded browser. It is
  chromeless, so the conning view gets the full screen height, and it is what the UI is developed
  against. Recovering the app header is worth ~85px — about 8% at 1080p, roughly one density step, not
  the "15-20%" an older changelog entry claimed.
- **Trust the TLS root first, before switching to kiosk mode.** Caddy serves with an internal CA whose
  root must be distributed to clients (see [security.md](security.md)); with `Strict-Transport-Security`
  set, a click-through exception is not a workaround worth having. A browser installed from a snap or
  flatpak uses its **own** certificate store and does not inherit the system one, so importing into the
  system store is not enough. Verify with an ordinary windowed page load showing a clean padlock, then
  switch to `--kiosk` — a kiosk window has nowhere to display a certificate interstitial.
- **Sizing the display is an operator setting, not a deployment one.** Config → Display, or the
  browser's own zoom. See [user-guide.md](../user-guide.md).

### Two deploy gotchas for the static assets

- **Bump `?v=` on both `app.css` and `app.js` in `index.html` whenever either changes.** The HTML is
  served `Cache-Control: no-store` so it always refetches, but the assets are not versioned any other
  way — ship new HTML against a cached script and the new controls degrade to doing nothing, with no
  error. A test asserts the two values match, so they cannot drift apart.
- **Normalise line endings when copying from a Windows working tree.** The repo stores LF (enforced by
  `.gitattributes`), but a tar built on Windows can carry CRLF onto the appliance. Harmless for
  JS/HTML/Python, and `*.sh` is protected by attribute — but it defeats hash-based deploy verification,
  so compare against the committed blob rather than the local file:
  ```bash
  git show HEAD:web/static/app.js | ssh <host> 'diff - /opt/mockingbuoy/web/static/app.js'
  ```
- A static-only change needs **no service restart**: `index.html`, `app.css` and `app.js` are read from
  disk per request. Restarting stops every `PeriodicSender` and puts a real gap on the NMEA outputs, so
  do not do it for a CSS fix.

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
