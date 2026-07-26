# Sharing the box's Caddy + optional name-based LAN access

mockingbuoy fronts its own UI with a natively-installed Caddy. That same Caddy is meant to be
**shared** by every other service on the box (a NetBox stack, a DNS appliance, anything) — and a
mockingbuoy redeploy must never clobber their config. This doc is the contract for that coexistence,
plus an optional recipe for reaching services by friendly names with a trusted padlock.

Nothing here is service-specific, and **no container stacks live in this repo** — every stack lives
on the host under `/srv/docker/<name>/`, managed by you. mockingbuoy only owns the shared-Caddy
mechanism.

## The coexistence contract (how it works)

- The mockingbuoy systemd drop-in points Caddy at the shared **`/etc/caddy/Caddyfile`**, which holds
  only global options + `import /etc/caddy/conf.d/*.caddy` (see `Caddyfile.example`).
- mockingbuoy's own site is just one snippet, `/etc/caddy/conf.d/mockingbuoy.caddy`. `setup.sh`
  writes ONLY that file (and appends the `import` line to the main Caddyfile if it is missing) — it
  never rewrites the main Caddyfile or any other snippet. Your sites are safe across redeploys.
- Config is applied with `systemctl restart caddy` (a ~1 s blip on all sites), not a live admin-API
  reload.

## Add another service

1. **Publish it on the loopback only** — `ports: ["127.0.0.1:<port>:<port>"]` in the service's
   `/srv/docker/<name>/docker-compose.yml`. Docker's port publishing bypasses UFW; a `0.0.0.0`
   publish exposes the app's plain-HTTP port to the LAN behind Caddy's back.
2. **Add a Caddy snippet** (template: `conf.d/example-site.caddy.example`). Keep the real file beside
   the service's compose and symlink it into conf.d so the source of truth stays with its stack:
   ```bash
   # author /srv/docker/<name>/<name>.caddy  ->  reverse_proxy 127.0.0.1:<port>
   sudo ln -sfn /srv/docker/<name>/<name>.caddy /etc/caddy/conf.d/<name>.caddy
   sudo /opt/mockingbuoy/ops/bin/caddy-validate && sudo systemctl restart caddy
   ```
3. **If the app enforces its own allowed-hosts / CSRF** (Django apps like NetBox, etc.), add the
   site's name to that list or it returns HTTP 400 even though Caddy proxies fine.

> Always validate with `ops/bin/caddy-validate`, never a bare `caddy validate`: mockingbuoy.caddy
> references `{$MOCKINGBUOY_BASIC_HASH}` (and friends), which only exist in caddy.service's
> environment (from `secrets/service.env`). A plain-shell `caddy validate` sees them empty and fails
> with a misleading `basic_auth: username and password cannot be empty` error even when the config is
> valid. The helper injects that environment the same way the service does.

## Trust Caddy's local CA (green padlock for every `tls internal` site)

`tls internal` mints per-host certs from Caddy's own local root CA. Export the root once from the box
and install it on each client:

```bash
sudo cat /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt   # copy this file to the client
```

- **Windows:** `certutil -addstore -f Root root.crt` (admin), or Certmgr → Trusted Root Certification Authorities.
- **macOS:** Keychain Access → System → import → set "Always Trust".
- **Linux:** copy to `/usr/local/share/ca-certificates/` and `sudo update-ca-certificates`.
- **iOS:** install the .crt profile, then Settings → General → About → Certificate Trust Settings → enable it.
- **Android:** Settings → Security → install a CA cert. Note: user-installed CAs are honored by browsers but
  **ignored by most apps** — expect warnings inside apps, not the browser.

Until the CA is trusted (or if it can't be installed), the site still works — you just get a cert warning.

## Optional: reach services by name (LAN DNS)

`tls internal` gives real certs, but the **names** still have to resolve to the box. Two ways:

- **Per device** — a hosts-file entry (`<box-ip> mockingbuoy.eemslab.internal netbox.eemslab.internal …`).
  Fine for a laptop, impossible for most phones.
- **A DNS appliance on the box** answering a `*.eemslab.internal` wildcard → box IP, with LAN clients
  (or the LAN's DHCP) pointed at it. Run it as its own stack under `/srv/docker/adguard/` (AdGuard
  Home) or via `dnsmasq` — **not in this repo**. Gotchas when you build it:
    - Bind DNS to the box **LAN IP:53, not `0.0.0.0`**, to dodge systemd-resolved's stub on
      `127.0.0.53:53` (`DNSStubListener=no` in `/etc/systemd/resolved.conf` if they still clash).
    - Keep the box's own `/etc/resolv.conf` on an upstream resolver (not the container) so early boot
      — before Docker is up — still resolves for apt/chrony.
    - Firefox's default DNS-over-HTTPS bypasses local DNS — answer the `use-application-dns.net`
      canary (AdGuard does by default) or set `network.trr.mode=5` on clients.
    - Publish the DNS admin UI on the loopback only; front it with its own conf.d snippet if you want
      remote access.

Naming: `.internal` is ICANN-reserved for private use, so `*.eemslab.internal` can never collide with
a real TLD. Avoid `.local` — it's mDNS/Bonjour space and some clients (macOS especially) resolve it
by multicast, ignoring your DNS. Set `MOCKINGBUOY_SITE` and every service's snippet to the same
`*.eemslab.internal` scheme so one wildcard rewrite covers them all.

## Portability (a LAN you don't control)

The box may deploy on a foreign private LAN. Name + green-padlock access needs, per client: (1) DNS
pointed at the box and (2) the box's root CA installed & trusted — which can be **impossible** on
locked-down or MDM-managed devices (no manual DNS, no CA install, phones with no hosts file). The
guaranteed fallback is always **raw-IP access** (`https://<box-ip>/`, click through the cert
warning), which mockingbuoy's site already provides via its IP-alias address. Wherever it lands, give
the box a **static / DHCP-reserved IP** — the Caddy IP alias and any DNS answer both point at it.

## Verify

```bash
# from a client using the box as DNS (or with a hosts entry):
nslookup mockingbuoy.eemslab.internal      # -> box IP
# browser: https://mockingbuoy.eemslab.internal   (padlock after CA trust; basic-auth prompt)
#          https://<box-ip>/                       (always works — the raw-IP fallback)
# on the box (injects caddy.service's env; a bare `caddy validate` falsely fails on the empty hash):
sudo /opt/mockingbuoy/ops/bin/caddy-validate
```
