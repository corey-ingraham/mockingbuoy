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
- **App exposure:** the app has **no published host port** — reachable only via Caddy over the docker
  network. Caddy publishes on the **LAN NIC IP only** (never `0.0.0.0`).
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
    reverse_proxy app:8000 { flush_interval -1 }
}
```

## Secrets

- Web credential = an argon2/bcrypt **hash** in `secrets/webauth.hash` (0600, git-ignored,
  bind-mounted). The Caddy root CA key lives in `secrets/` too. Nothing secret is ever committed or
  baked into the image.
- **First-run password:** setup generates a random password, stores only its hash, and prints the
  plaintext **once**. `config.json` holds non-secret settings only.

## Network hardening (optional, config-driven)

- **Docker bypasses UFW** for published ports (rules land in `DOCKER-USER`, evaluated before UFW's
  INPUT). Restrict the subnet there, e.g.:
  ```bash
  iptables -I DOCKER-USER -i <lan_nic> ! -s <subnet> -p tcp --dport 443 -j DROP
  ```
  or use `ufw-docker`.
- Host UFW default-deny with an allow for the management subnet on 443 (and 22 if used).
- If a dedicated management interface exists, keep the web port off other interfaces.

## Supply chain

- Lock deps with `pip-compile --generate-hashes`; install `--require-hashes`.
- Scan the image before go-live: `trivy image --severity HIGH,CRITICAL mockingbuoy:<ver>`.
- Minimal slim base, non-root user, read-only rootfs.

## Backups & restore

Back up: the `appdata` volume (config + state), the `caddy_data` volume (root CA + certs), the
`secrets/` files, udev rules, `docker-compose.yml` + `Caddyfile`, and the exported image tarballs.

A host systemd timer rsyncs these to a LAN share (quiesce `app` around the volume copy). Restore:
1. fresh host + Docker; recreate the project dir;
2. `docker load` the image tarballs (no rebuild/internet);
3. restore configs + `secrets/` + udev rules (`udevadm control --reload && udevadm trigger`);
4. recreate volumes and repopulate from the share; fix ownership to the app uid;
5. `docker compose up -d`; verify `/healthz`, the live feed, and Basic login (trust the restored CA on
   new clients). Drill the restore periodically.
