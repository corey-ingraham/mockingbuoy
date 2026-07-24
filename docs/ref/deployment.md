# Deployment

## Install (primary)

On the target host (needs internet during install, like any Docker build):
```bash
curl -fsSL https://raw.githubusercontent.com/<you>/mockingbuoy/main/bootstrap.sh | bash
```
`bootstrap.sh` installs `git`+`curl`, clones the repo, and runs `sudo ./setup.sh`, which (idempotently):
1. installs Docker Engine + compose plugin; adds the user to `docker` and `dialout`;
2. host config: udev `by-id` symlinks (`/dev/nmea-*`), purge/mask `brltty`, set FTDI `latency_timer=1`,
   time sync (chrony) + `timedatectl set-timezone UTC`, optional firewall hardening (see security.md);
3. generates the Caddy custom root CA and a first-run web password (printed once);
4. `docker compose build`;
5. exports images (`docker save` → `./images/*.tar`) for rebuild-free redeploy;
6. `docker compose up -d` and enables the host backup timer;
7. prints the web URL, the one-time password, and the CA file to trust on clients.

The stack runs on boot via `restart: unless-stopped` and has **no runtime internet dependency**.

## Container stack

Two services + named volumes:
- **`app`** — uvicorn (`--workers 1`, no `--reload`) hosting the FastAPI UI and the engine threads.
  Hardened: non-root `user`, `group_add: ["20"]` (dialout), `cap_drop: [ALL]`,
  `security_opt: [no-new-privileges:true]`, `read_only: true` + tmpfs, single writable volume `appdata`,
  `init: true`, `stop_grace_period: 30s`, resource limits. No published host port (reached via Caddy).
- **`caddy`** — TLS termination + Basic auth (see security.md). Publishes `<LAN_IP>:443` only.

## Device passthrough

Bind serial devices by stable path and allow re-enumeration:
```yaml
devices:
  - "/dev/serial/by-id/<gps>:/dev/nmea-gps"
  - "/dev/serial/by-id/<heading>:/dev/nmea-heading"
  - "/dev/serial/by-id/<ais>:/dev/nmea-ais"
device_cgroup_rules:
  - "c 188:* rwm"     # USB-serial major; survives replug
group_add: ["20"]     # dialout GID (verify: getent group dialout)
```
Note: Docker `--device`/`devices:` resolves a symlink to its target at start; for live replug recovery
you may instead bind-mount `/dev` with the same cgroup rule. udev runs on the **host**, not the container.

## Time / clock

The Pi has no RTC; the clock may be wrong until sync. Keep the OS in UTC. The app's `time_source.mode`
controls dated sentences:
- `system_utc` (default) — use OS UTC; gate ZDA/RMC dates on `timedatectl … NTPSynchronized == yes`.
- `simulated` — fixed epoch + rate multiplier (repeatable scenarios).
- `hold` — freeze time.

## Redeploy without rebuild

Load the exported images on a fresh host and start:
```bash
docker load -i images/mockingbuoy-app.tar
docker load -i images/caddy.tar
docker compose up -d
```
Or build on any Linux host and export: `docker buildx build --platform linux/arm64 -t mockingbuoy:<ver> --load .`
then `docker save -o images/mockingbuoy-app.tar mockingbuoy:<ver>`.

## Updates

`git pull && docker compose build && docker compose up -d`, or redeploy from the exported image tarballs.

## Autostart

`restart: unless-stopped` + `docker compose up -d` brings the stack up on boot. No desktop session is
involved; the engine runs headless whether or not a browser is connected.

## Hardware-in-the-loop checklist (manual)

- `ls -l /dev/nmea-*` resolves to the intended adapters.
- Each channel emits the right talker at the configured rate (verify with a serial terminal).
- Unplug one adapter → that channel marks down while the others keep emitting.
- `/healthz` returns 503 when a port is down; the container self-exits + restarts if unrecoverable.
- Reboot → stack comes back automatically.
