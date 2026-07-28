# Security

## Threat model

The host is on a LAN. Anything on that LAN can reach the web port. The UI can rewrite the simulated
GPS/heading/speed/AIS being fed to connected equipment, so the priorities are: (1) no unauthenticated
control, (2) TLS for confidentiality/integrity on the wire, (3) minimize who can reach the port.

## TLS + authentication (Caddy reverse proxy)

- **TLS:** Caddy `tls internal` issuing from a **stable custom root CA** generated once at setup and
  backed up. Distribute the root cert to client machines (trust it once). IP-only SANs work; a local
  hostname is nicer if available.
- **Auth:** HTTP **Basic** enforced at Caddy (`caddy hash-password`, bcrypt). No cookies → classic CSRF
  does not apply. Basic serves both the browser UI and scripted clients.
- **App exposure:** the app binds a **unix socket** (`unix//run/mockingbuoy/app.sock`) — no host TCP
  port at all, reachable only via Caddy over that socket. Caddy publishes on the **LAN NIC IP only**
  (never `0.0.0.0`). The socket's perms chain confines who can reach it: the socket directory and node
  are owned by the `mockingbuoy` service user and group-shared with Caddy (mode `0660`), so only the two
  service accounts can open it. **⚠ Validate the socket wiring on the target hardware** — the unix-socket
  bind + Caddy-over-socket path is **not yet validated on target hardware**; confirm the socket path,
  ownership, and group perms on the real host before relying on it.
- **Caddy admin API off:** disable Caddy's admin endpoint (`admin off`) — it is a live
  reconfiguration/control surface with no place on an appliance whose config should only change through a
  deliberate host-side redeploy. With admin off, a config change is a **restart**, not an API reload.
- **SSE + auth gotcha:** `EventSource` can't set headers. Basic-at-proxy makes the live stream
  authenticate automatically from the browser's cached credentials — zero client code. Set
  `reverse_proxy … { flush_interval -1 }` so Caddy doesn't buffer the stream.

Caddyfile essentials (user and hash come from the environment — see `secrets/service.env`):
```caddy
{
    admin off
}

{$MOCKINGBUOY_SITE:<LAN_IP>}:443 {
    tls internal
    basic_auth {
        {$MOCKINGBUOY_BASIC_USER:<you>} {$MOCKINGBUOY_BASIC_HASH}
    }
    header {
        X-Content-Type-Options nosniff
        Referrer-Policy no-referrer
        # Strict script-src: the UI's JS + CSS are served as same-origin files under /static
        # (no inline <script>), so no 'unsafe-inline' for scripts. style-src keeps 'unsafe-inline'
        # only for the page's inline style="" layout attributes.
        Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
        X-Frame-Options DENY
        Strict-Transport-Security "max-age=31536000"
        -Server
    }
    reverse_proxy unix//run/mockingbuoy/app.sock { flush_interval -1 }
}
```

## Read-only Security tab

The web UI's **Security** tab is a **read-only posture panel** backed by `GET /api/security`,
which returns **booleans only** — it reports each protection by **presence, not value**, and **never
renders a secret** (no credential, hash, or key ever transits the UI). It surfaces: TLS active, which
auth layers are on, the unix-socket (no-host-port) bind, the open TCP-tap ports, subscriber count vs cap,
uptime, and the active security headers. It also reports `password_is_default` (whether the auto-generated
first-run password has been changed in-app), which drives a one-time first-login prompt.

The one exception to "read-only" is the **web-password change** — the Security tab has a **"Change web
password"** card (and a first-login banner that links to it). It never displays a secret; it only accepts
a *new* password as input. The mechanism is deliberately structured so the sandboxed app never gains the
privilege to write the secret or restart the proxy:

1. The app **hashes the new password in-process** by shelling to `caddy hash-password --algorithm bcrypt`,
   feeding the plaintext on **stdin (never argv)** — no new Python dependency. The plaintext lives only in
   the single request handler's memory and on caddy's stdin, then is discarded; it is never logged, never
   persisted, never placed in a process argument.
2. The app writes **only a bcrypt hash + a nonce** to `data/webpass-request.json` (mode 0600) — its one
   writable directory. No plaintext ever touches disk.
3. A **root systemd path-unit** (`mockingbuoy-webpass.path` → `mockingbuoy-webpass.service` →
   `ops/bin/rotate-webpass`) watches that file and performs the privileged work the app sandbox forbids:
   it strictly validates the hash (anchored bcrypt regex) and nonce, rewrites `MOCKINGBUOY_BASIC_HASH` in
   `secrets/service.env`, runs `caddy validate`, defers a `systemctl restart caddy` (a restart, not a
   reload, because `admin off` — see above), health-probes that caddy is serving again, and **rolls back
   to the prior hash on any failure**. It then writes `data/webpass-result.json`, which the app polls.
   Because the deferred restart drops the in-flight connection, the browser re-prompts for Basic auth with
   the new password — that reauth is the confirmation of success.

**`secrets/` is now root-owned (0700, `service.env` root:root 0600).** systemd reads the `EnvironmentFile`
as root *before* dropping to the `mockingbuoy` user, so the app needs no access to it — it only reads
`os.environ` — and root ownership removes the app-owned-directory symlink/TOCTOU vector on the very file
the root path-unit rewrites.

**Security trade-off:** the new password crosses the authed TLS wire **exactly once** (in the
`POST /api/security/rotate-password` body). From there on, **only a bcrypt hash** ever touches disk or the
root path-unit; **plaintext never leaves the app process** except onto caddy's stdin for hashing, and is
then discarded (Global CLAUDE.md §9). This is a conscious concession — the strictest posture would keep the
secret entirely off the wire — traded for the operational value of rotating the password from the browser
without host shell access. The host CLI method below remains available as a fallback.

## Information-leak posture (R19)

A `/dev/serial/by-id/...` link carries the adapter **brand and per-unit serial**, and full filesystem
paths describe the host layout. Neither is needed by the UI, so the API withholds them:

| Surface | Emits | Never emits |
|---|---|---|
| `GET /api/config` | resolved kernel name (`ttyUSB0`) via `_redacted_config_dict` | `path`, by-id link |
| `GET /api/inputs` | slot id, function, detection flags, kernel name | device path |
| `GET /api/ports` | opaque handle, kernel name, detected class, live | by-id link, brand, serial |
| Persist endpoints | — | device paths are not accepted as input either |

The `/api/ports` handle is deliberately opaque: the client picks an adapter by handle and the **server**
maps it to a path, so a device path is never a client-supplied value. Accepting one would be an
arbitrary-device-open primitive — bounded by the cgroup grant above to serial ttys, but still a wider
primitive than any UI needs.

### Accepted exception — error text (decided 2026-07-27)

`ISSUE-020` surfaces serial-open failures to the operator, and a `serial.SerialException` repr embeds
the **full configured path** — including the by-id link for a TX channel. This is **accepted**: the
diagnostic value of seeing the real failing path outweighs the leak, given the reader is already
authenticated on a LAN-only appliance and can read the config anyway.

Recorded here so the contradiction is explicit rather than discovered later. The exception covers
**error/diagnostic text only** — the structured endpoints above stay strict, and there is a regression
test asserting `/api/ports` emits no by-id string.

## Process sandboxing

The app runs under `mockingbuoy.service` as a dedicated non-login user, hardened with systemd
sandboxing — dropped privileges and a read-only rootfs: `NoNewPrivileges=true`,
`ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`, `ProtectKernelTunables=true`, empty
`CapabilityBoundingSet=`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `MemoryMax=`/`TasksMax=`,
and a single `ReadWritePaths=/opt/mockingbuoy/data`. Do **not** set `PrivateDevices=yes` — it hides
`/dev/tty*`; grant serial via `dialout` + `DeviceAllow=`. **Device access is enforced via the activated
cgroup device controller** — but be precise about what it actually grants:

```ini
DeviceAllow=char-ttyUSB rw
DeviceAllow=char-ttyACM rw
```

These are **char-major CLASS grants, not per-device grants** — every USB-serial and CDC-ACM tty on the
box is reachable, not only the ones named in config. That is deliberate (M19): a per-symlink grant like
`DeviceAllow=/dev/nmea-gps rw` is resolved to a major:minor pair *at unit start*, so any adapter plugged
or replugged afterwards gets EPERM until a restart — which would break the hotplug failover the appliance
exists for.

So the boundary this enforces is **"serial ttys only"**, not "these specific adapters". It is still the
control that matters: no configured path can reach a block device, a fifo, or `/dev/random`, whatever
ends up in `data/config.local.json`. Per-adapter confinement is *not* provided and should not be
assumed.

### Input-slot provisioning + wiring

Auto/replay read real NMEA on physical **input** slots. `setup.sh` provisions stable by-id udev symlinks
`/dev/nmea-in-1 … /dev/nmea-in-6` and the unit grants each one it uses with an explicit `DeviceAllow=`:

```ini
DeviceAllow=/dev/nmea-in-1 rw
DeviceAllow=/dev/nmea-in-2 rw
# … one per input slot in use
```

**Wiring the inputs safely:** NMEA 0183 is **RS-422 differential (A/B)**, not TTL/RS-232 single-ended.
Land each input on an **isolated, listen-only adapter** (opto/galvanically isolated, RX-only) so the tool
can only *listen* on a live bus — it cannot drive or load the differential pair, and isolation keeps
ground loops off the host. **Mind grounding:** tie signal ground per the talker's convention, never bond
a floating shield to host ground through the adapter. A listen-only isolated tap is the only supported way
to read a real bus.

## Optional ADC voltage-sense add-on

The Maintenance diagnostics can optionally read per-line, differential, and common-mode voltages to
*confirm* an electrical fault (e.g. a reversed A/B pair) the byte-stream advisor can only *infer*. It is
**off by default** — the app runs fine with no ADC present and adds nothing to the footprint or attack
surface when absent.

- **A protective analog front-end is required before tapping A/B** — never wire bus lines straight into
  the ADC. The front-end clamps/divides bus voltages into the ADC's safe range and isolates it from the
  differential pair; without it the sensor and the host are at risk.
- **Scoped I2C DeviceAllow:** grant only the I2C bus the ADC sits on, nothing wider —
  `DeviceAllow=/dev/i2c-1 rw` (adjust the bus number). No blanket `/dev` access.
- **Config:** enable via a `voltage_sense` block (device address, bus, channel map); absent block = the
  feature simply does not exist at runtime.

## Secrets

- Web credential = a bcrypt **hash** (from `caddy hash-password`) carried in the `MOCKINGBUOY_BASIC_HASH`
  env var inside `secrets/service.env` (0600, git-ignored) — never a `webauth.hash` file, never a
  plaintext password. `setup.sh` generates it on first run and writes it there. The Caddy internal root
  CA is managed by Caddy in its own data dir. Nothing secret is ever committed.
- **First-run password:** setup generates a random password, stores only its hash, and prints the
  plaintext **once**. `config.json` holds non-secret settings only.
- **Rotating the hash afterward:** the same `MOCKINGBUOY_BASIC_HASH` value (same bcrypt format) can be
  rotated **in-app** from the Security tab — the app hashes via `caddy hash-password` (stdin) and a root
  systemd path-unit (`mockingbuoy-webpass.path` → `.service` → `ops/bin/rotate-webpass`) rewrites the env,
  validates, restarts caddy, and rolls back on failure (see *Read-only Security tab* above). Only a bcrypt
  hash is ever written; plaintext never reaches disk. The host CLI path remains the fallback: run
  `caddy hash-password`, edit `MOCKINGBUOY_BASIC_HASH` in `secrets/service.env`, then
  `systemctl restart caddy` (a restart, not a reload, is required because `admin off`). Note `secrets/` is
  now root-owned, so editing `service.env` by hand requires root.

## Network hardening (optional, config-driven)

Native processes obey UFW normally — there is no firewall-bypass special case to work around.
- Host UFW default-deny with an allow for the management subnet on 443 (and the TCP-tap ports, and 22
  if used), e.g. `ufw allow from <subnet> to any port 443 proto tcp`.
- If a dedicated management interface exists, keep the web port off other interfaces.

## Supply chain

- Lock deps with `pip-compile --generate-hashes`; install into the venv with `--require-hashes`.
- Audit deps before go-live: `pip-audit -r requirements.txt`.
- Non-root service user + systemd sandboxing (above); the app never runs `pip` at runtime.

## Backups & restore

Back up: `config.json` + `data/` (config + state), the Caddy data dir (root CA + certs), the
`secrets/` files, the udev rules, and the `Caddyfile` (optionally the wheelhouse).

> **There is no working backup today.** `ops/mockingbuoy-backup.{service,timer}` are installed by
> `setup.sh` but the timer is `static` (no `[Install]`) and cannot be enabled, and neither documented
> destination works — see [ISSUE-001](../issues.md#issue-001) and [RM-031](../roadmap.md#rm-031).
> Until that lands, **copy the paths above off the box by hand**. The restore procedure below is the
> intended design and has never been drilled end to end.

Restore (untested):
1. fresh host; re-run `bootstrap.sh`/`setup.sh` (rebuild the venv from the wheelhouse, no internet);
2. restore `config.json` + `secrets/` + the Caddy data dir + udev rules
   (`udevadm control --reload && udevadm trigger`); fix ownership to the `mockingbuoy` user;
3. `systemctl restart mockingbuoy caddy`; verify `/healthz`, the live feed, and Basic login (trust the
   restored CA on new clients). Drill the restore periodically.
