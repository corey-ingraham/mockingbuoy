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

Caddyfile essentials:
```caddy
{
    admin off
}

<LAN_IP> {
    tls { issuer internal { ca lab_root } }
    basic_auth { operator <BCRYPT_HASH> }
    header {
        X-Content-Type-Options nosniff
        Content-Security-Policy "default-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        Referrer-Policy no-referrer
    }
    reverse_proxy unix//run/mockingbuoy/app.sock { flush_interval -1 }
}
```

## Read-only Security tab

The web UI's **Security** tab is a strictly **read-only posture panel** backed by `GET /api/security`,
which returns **booleans only** — it reports each protection by **presence, not value**, and **never
renders a secret** (no credential, hash, or key ever transits the UI). It surfaces: TLS active, which
auth layers are on, the unix-socket (no-host-port) bind, the open TCP-tap ports, subscriber count vs cap,
uptime, and the active security headers. The **primary login is rotated at the host, not the browser** —
change the web password with `caddy hash-password` and update the service env on the host; there is
deliberately no password-change form, so the primary secret never reaches the wire or the UI.

## Process sandboxing

The app runs under `mockingbuoy.service` as a dedicated non-login user, hardened with systemd
sandboxing — dropped privileges and a read-only rootfs: `NoNewPrivileges=true`,
`ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`, `ProtectKernelTunables=true`, empty
`CapabilityBoundingSet=`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `MemoryMax=`/`TasksMax=`,
and a single `ReadWritePaths=/opt/mockingbuoy/data`. Do **not** set `PrivateDevices=yes` — it hides
`/dev/tty*`; grant serial via `dialout` + explicit `DeviceAllow=`. **Device access is enforced via the
activated cgroup device controller** — the `DeviceAllow=` allowlist confines the service to exactly the
named char devices (the output adapters, the input slots, and — only if enabled — the ADC), nothing else
on `/dev`.

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

- Web credential = an argon2/bcrypt **hash** in `secrets/webauth.hash` (0600, git-ignored). The Caddy
  root CA key lives in `secrets/` too. Nothing secret is ever committed.
- **First-run password:** setup generates a random password, stores only its hash, and prints the
  plaintext **once**. `config.json` holds non-secret settings only.

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

A host systemd timer rsyncs these to a LAN share. Restore:
1. fresh host; re-run `bootstrap.sh`/`setup.sh` (rebuild the venv from the wheelhouse, no internet);
2. restore `config.json` + `secrets/` + the Caddy data dir + udev rules
   (`udevadm control --reload && udevadm trigger`); fix ownership to the `mockingbuoy` user;
3. `systemctl restart mockingbuoy caddy`; verify `/healthz`, the live feed, and Basic login (trust the
   restored CA on new clients). Drill the restore periodically.
