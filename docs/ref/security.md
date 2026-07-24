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
- **App exposure:** the app binds **loopback only** (`127.0.0.1:<app_port>`) — reachable only via Caddy
  over localhost. Caddy publishes on the **LAN NIC IP only** (never `0.0.0.0`).
- **SSE + auth gotcha:** `EventSource` can't set headers. Basic-at-proxy makes the live stream
  authenticate automatically from the browser's cached credentials — zero client code. Set
  `reverse_proxy … { flush_interval -1 }` so Caddy doesn't buffer the stream.

Caddyfile essentials:
```caddy
<LAN_IP> {
    tls { issuer internal { ca lab_root } }
    basic_auth { operator <BCRYPT_HASH> }
    header {
        X-Content-Type-Options nosniff
        Content-Security-Policy "default-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        Referrer-Policy no-referrer
    }
    reverse_proxy 127.0.0.1:8000 { flush_interval -1 }
}
```

## Process sandboxing

The app runs under `mockingbuoy.service` as a dedicated non-login user, hardened with systemd
sandboxing — dropped privileges and a read-only rootfs: `NoNewPrivileges=true`,
`ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`, `ProtectKernelTunables=true`, empty
`CapabilityBoundingSet=`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `MemoryMax=`/`TasksMax=`,
and a single `ReadWritePaths=/opt/mockingbuoy/data`. Do **not** set `PrivateDevices=yes` — it hides
`/dev/tty*`; grant serial via `dialout` + explicit `DeviceAllow=`.

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
