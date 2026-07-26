# LAN appliance: friendly subdomains + trusted TLS on a foreign private network

This turns the box into a self-contained appliance you can drop on **someone else's private LAN** and reach
its services by name — `https://mockingbuoy.eemslab.internal`, `https://netbox.eemslab.internal` — with a
real green padlock, no dependency on the host network's DNS/DHCP beyond a stable IP.

Everything is box-local: a shared Caddy fronts every service by subdomain (`tls internal` = Caddy's own
local CA), and an AdGuard Home container answers a `*.eemslab.internal` wildcard pointing at the box.

## Go / no-go precondition (read first)

Name + green-padlock access needs, on **each client device**: (1) DNS pointed at this box, and (2) the box's
root CA installed & trusted. On a LAN you don't control (locked DHCP DNS, MDM-managed laptops/phones that
block manual DNS or CA installs, non-rooted phones where a hosts file is impossible) this may be **impossible**
per device. In that case the guaranteed fallback is **raw-IP access** (`https://<box-ip>/`, click through the
cert warning) — which Part A already provides via the IP-alias site address. Decide per deployment site.

Naming: this guide uses `.eemslab.internal` — `.internal` is ICANN-reserved for private use, so it can never
collide with a future real TLD. You can use a bare `eemslab` if you prefer the shorter name (change
`MOCKINGBUOY_SITE`, the wildcard rewrite, and each site block to match), at the cost of that collision risk.

## 1. Give the box a stable IP

Set a static IP or DHCP reservation on the deployment LAN and put it in `ops/lan/.env` as `BOX_IP`. DNS answers
and the raw-IP Caddy alias both point here; if it moves, everything breaks.

## 2. Shared Caddy + one snippet per service

`setup.sh` already makes mockingbuoy coexist: it points Caddy at the shared `/etc/caddy/Caddyfile`
(`Caddyfile.example` here), which `import`s `/etc/caddy/conf.d/*.caddy`, and writes only
`/etc/caddy/conf.d/mockingbuoy.caddy`. Add each further service as its own snippet:

```bash
sudo cp ops/lan/conf.d/netbox.caddy.example /etc/caddy/conf.d/netbox.caddy   # then edit the upstream port
sudo /opt/mockingbuoy/ops/bin/caddy-validate && sudo systemctl restart caddy
```

> Use `ops/bin/caddy-validate`, NOT a bare `caddy validate`. mockingbuoy.caddy references
> `{$MOCKINGBUOY_BASIC_HASH}` etc., which only exist in caddy.service's environment (from
> `secrets/service.env`); a plain-shell `caddy validate` sees them empty and fails with a misleading
> `basic_auth: username and password cannot be empty` error even when the config is fine. The helper
> injects that environment the same way the service does.

**Publish every fronted container on the loopback only** (`ports: ["127.0.0.1:8080:8080"]`) — Docker's port
rules bypass UFW, so a `0.0.0.0` publish exposes the app's plain-HTTP port straight to the LAN. Only DNS `:53`
should face the LAN.

## 3. DNS container (AdGuard Home)

```bash
cd ops/lan && cp .env.example .env    # set BOX_IP; run from ops/lan so compose picks up .env
docker compose up -d
```

- **Port 53 conflict:** the compose binds DNS to `BOX_IP:53` (not `0.0.0.0`), which normally avoids
  systemd-resolved's stub on `127.0.0.53:53`. If `sudo ss -lunp | grep :53` still shows a clash, set
  `DNSStubListener=no` in `/etc/systemd/resolved.conf` and `sudo systemctl restart systemd-resolved`.
- **Keep the box's own resolver upstream** (leave `/etc/resolv.conf` pointing at systemd-resolved / a public
  resolver, NOT at this container) so early boot — before Docker/AdGuard is up — still resolves for apt/chrony.
- **Wildcard rewrite:** finish the setup wizard at `http://127.0.0.1:3000` (via SSH tunnel), set an upstream
  (e.g. `1.1.1.1`), then **Filters → DNS rewrites → Add**: domain `*.eemslab.internal`, answer `${BOX_IP}`.
  Now every current/future `*.eemslab.internal` name resolves to the box (each still needs its own Caddy
  snippet from step 2).
- **Firefox DoH:** Firefox's default DNS-over-HTTPS bypasses local DNS, so `*.eemslab.internal` would
  NXDOMAIN. AdGuard Home answers the `use-application-dns.net` canary to signal Firefox to disable DoH — verify
  it's enabled (default), or set Firefox `network.trr.mode=5` on client devices.
- **UFW:** if `ENABLE_UFW=true`, also allow DNS: `sudo ufw allow 53/tcp && sudo ufw allow 53/udp` (restrict to
  the client subnet if you can). Leave the AdGuard UI (3000) closed — it's loopback-only.

## 4. Point LAN clients at the box DNS

However you can on that network: set the box IP as the DNS server in the LAN's DHCP (best), or per-device DNS,
or — last resort for a machine you can't repoint — a hosts-file entry per name. Mobile devices generally can't
use a hosts file, so for phones you need option (a) or (b).

## 5. Trust Caddy's local CA (for the green padlock)

Export the root once from the box and install it on each client:

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

## 6. Verify

```bash
# from a client using the box as DNS:
nslookup mockingbuoy.eemslab.internal      # -> BOX_IP
# browser: https://mockingbuoy.eemslab.internal  (padlock after CA trust; basic-auth prompt)
#          https://netbox.eemslab.internal
#          https://<box-ip>/                (always works — the raw-IP fallback)
# on the box (injects caddy.service's env; a bare `caddy validate` falsely fails on the empty hash):
sudo /opt/mockingbuoy/ops/bin/caddy-validate
```
