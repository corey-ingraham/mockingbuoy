#!/usr/bin/env bash
# setup.sh — idempotent, root-run installer that turns a fresh Debian / Raspberry Pi
# OS Lite (Bookworm, arm64 or amd64) host into a running mockingbuoy appliance.
#
#   sudo ./setup.sh
#
# The runtime is NATIVE: a Python venv driven by systemd, fronted by a natively
# installed Caddy reverse proxy (TLS + Basic auth). No containers.
#
# Internet is required only to INSTALL (apt packages + Python wheels). After install
# the appliance has NO runtime internet dependency; an offline redeploy rebuilds the
# venv from the on-device wheelhouse. Re-running this script converges without
# duplicating the repo, the service user, apt keys, or the web password.
#
# Reads optional ./setup.env (see setup.env.example) for tunables. Nothing required.
set -euo pipefail

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #
APP_USER="mockingbuoy"
APP_GROUP="mockingbuoy"
APP_DIR="/opt/mockingbuoy"
VENV_DIR="${APP_DIR}/.venv"
SVC_ENV="${APP_DIR}/secrets/service.env"
# Caddy's local root CA (tls internal). Operators distribute root.crt to clients.
CADDY_CA_ROOT="/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt"

log()  { printf '[mockingbuoy-setup] %s\n' "$*"; }
warn() { printf '[mockingbuoy-setup][warn] %s\n' "$*" >&2; }
die()  { printf '[mockingbuoy-setup][error] %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 0. Must run as root; resolve own dir; load optional setup.env                #
# --------------------------------------------------------------------------- #
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        log "re-executing under sudo ..."
        exec sudo -E bash "$0" "$@"
    fi
    die "must run as root (install sudo, or run: su -c './setup.sh')"
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
log "repo clone (source):   ${SRC_DIR}"
log "install prefix:        ${APP_DIR}"

if [ -f "${SRC_DIR}/setup.env" ]; then
    log "loading tunables from ${SRC_DIR}/setup.env"
    set -a
    # shellcheck disable=SC1091
    . "${SRC_DIR}/setup.env"
    set +a
else
    log "no setup.env found; using built-in defaults (see setup.env.example)"
fi

# Safe defaults for every tunable — nothing is required.
MOCKINGBUOY_SITE="${MOCKINGBUOY_SITE:-<LAN_IP>}"
MOCKINGBUOY_BASIC_USER="${MOCKINGBUOY_BASIC_USER:-<you>}"
APP_PORT="${APP_PORT:-8000}"
ALLOW_SUBNET="${ALLOW_SUBNET:-<subnet>}"
ENABLE_UFW="${ENABLE_UFW:-false}"
TAP_PORTS="${TAP_PORTS:-}"
CHRONY_SERVER="${CHRONY_SERVER:-}"
BACKUP_DEST="${BACKUP_DEST:-}"

# --------------------------------------------------------------------------- #
# 1. Preflight — warn (don't hard-fail) on unexpected OS / arch                #
# --------------------------------------------------------------------------- #
log "preflight: checking OS family and architecture ..."
if ! command -v apt-get >/dev/null 2>&1; then
    warn "apt-get not found — this installer targets Debian-family hosts; continuing anyway"
fi
ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
case "${ARCH}" in
    arm64|amd64) log "architecture ${ARCH}: supported" ;;
    *) warn "architecture '${ARCH}' is untested (expected arm64/amd64); continuing" ;;
esac

# --------------------------------------------------------------------------- #
# 2. apt packages + Caddy from its official apt repo (idempotent)              #
# --------------------------------------------------------------------------- #
export DEBIAN_FRONTEND=noninteractive
APT="apt-get -qq -o Dpkg::Use-Pty=0"

log "apt-get update ..."
${APT} update

log "installing base packages ..."
${APT} -y install \
    python3-venv python3-pip git curl chrony ufw ca-certificates gnupg rsync

if command -v caddy >/dev/null 2>&1; then
    log "caddy already installed: $(caddy version 2>/dev/null | head -n1)"
else
    log "adding Caddy official apt repo (key + source) ..."
    install -d -m 0755 /usr/share/keyrings
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    log "apt-get update (caddy repo) ..."
    ${APT} update
    log "installing caddy ..."
    ${APT} -y install caddy
fi

# --------------------------------------------------------------------------- #
# 3. Dedicated non-login service user + directories                           #
# --------------------------------------------------------------------------- #
if id -u "${APP_USER}" >/dev/null 2>&1; then
    log "service user '${APP_USER}' already exists"
else
    log "creating system user '${APP_USER}' (home ${APP_DIR}, nologin) ..."
    useradd --system --user-group \
        --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi
# dialout is required to open serial adapters (idempotent).
usermod -aG dialout "${APP_USER}"

install -d -m 0755 -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}"
install -d -m 0750 -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}/data"
install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}/secrets"

# --------------------------------------------------------------------------- #
# 4. Sync repo into /opt, build venv, install deps (+ the app)                #
# --------------------------------------------------------------------------- #
if [ "${SRC_DIR}" = "${APP_DIR}" ]; then
    log "source is already the install prefix; skipping repo sync"
else
    log "syncing repo into ${APP_DIR} ..."
    rsync -a \
        --exclude '.git' \
        --exclude '.venv' \
        --exclude '__pycache__' \
        --exclude '*.py[cod]' \
        --exclude '.mypy_cache' \
        --exclude '.ruff_cache' \
        --exclude '.pytest_cache' \
        --exclude 'private/' \
        --exclude 'data/' \
        --exclude 'setup.env' \
        "${SRC_DIR}/" "${APP_DIR}/"
fi

cd "${APP_DIR}"

if [ -x "${VENV_DIR}/bin/python" ]; then
    log "venv already present at ${VENV_DIR}"
else
    log "creating venv at ${VENV_DIR} ..."
    python3 -m venv "${VENV_DIR}"
fi
PIP="${VENV_DIR}/bin/pip"
"${PIP}" install --quiet --upgrade pip

# A hash-locked requirements.txt (pip-compile --generate-hashes) enables --require-hashes;
# the plain convenience lock does not (hash mode rejects un-hashed reqs). Detect it.
HASH_FLAG=""
if grep -q -- '--hash=' requirements.txt; then
    HASH_FLAG="--require-hashes"
fi

if ls wheelhouse/*.whl >/dev/null 2>&1; then
    log "installing dependencies from local wheelhouse (offline, no index) ..."
    # shellcheck disable=SC2086
    "${PIP}" install --quiet --no-index --find-links=wheelhouse ${HASH_FLAG} -r requirements.txt
else
    log "no wheelhouse present; installing dependencies from the package index ..."
    # shellcheck disable=SC2086
    "${PIP}" install --quiet ${HASH_FLAG} -r requirements.txt
fi

# Register the app so 'uvicorn web.app:app' imports cleanly. --no-build-isolation uses
# the venv's setuptools (no index round-trip); --no-deps because deps are done above.
# Non-fatal: the unit also runs with WorkingDirectory=${APP_DIR}, so 'web' imports anyway.
log "installing the mockingbuoy package (editable) ..."
"${PIP}" install --quiet --no-build-isolation --no-deps -e . \
    || warn "editable install failed; app still importable via WorkingDirectory=${APP_DIR}"

log "fixing ownership of ${APP_DIR} to ${APP_USER}:${APP_GROUP} ..."
chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"

# --------------------------------------------------------------------------- #
# 5. Host config: udev, brltty, time sync, optional firewall                  #
# --------------------------------------------------------------------------- #
log "installing udev rules for stable /dev/nmea-* symlinks ..."
install -m 0644 "${APP_DIR}/ops/99-mockingbuoy.rules" /etc/udev/rules.d/99-mockingbuoy.rules
udevadm control --reload >/dev/null 2>&1 || warn "udevadm control --reload failed (no udev running?)"
udevadm trigger >/dev/null 2>&1 || warn "udevadm trigger failed"
log "NOTE: edit /etc/udev/rules.d/99-mockingbuoy.rules and replace the ATTRS{serial}"
log "      placeholders with YOUR adapters' serials, then: udevadm control --reload && udevadm trigger"

# brltty grabs FTDI ttys out from under the service — mask + purge idempotently.
log "neutralizing brltty (it steals FTDI serial adapters) ..."
systemctl mask brltty.service brltty.path >/dev/null 2>&1 || true
${APT} -y purge brltty >/dev/null 2>&1 || true

# Time sync: chrony on, host clock in UTC.
log "enabling chrony + setting timezone to UTC ..."
systemctl enable --now chrony >/dev/null 2>&1 || warn "could not enable chrony"
if [ -n "${CHRONY_SERVER}" ]; then
    install -d -m 0755 /etc/chrony/conf.d
    printf 'server %s iburst\n' "${CHRONY_SERVER}" > /etc/chrony/conf.d/mockingbuoy.conf
    log "configured upstream NTP server: ${CHRONY_SERVER}"
    systemctl restart chrony >/dev/null 2>&1 || warn "chrony restart failed"
fi
timedatectl set-timezone UTC >/dev/null 2>&1 || warn "could not set timezone (no systemd-timedated?)"

# Optional firewall hardening.
if [ "${ENABLE_UFW}" = "true" ]; then
    log "ENABLE_UFW=true — applying UFW rules (allow ${ALLOW_SUBNET} -> :443 + taps) ..."
    ufw --force default deny incoming
    ufw --force default allow outgoing
    # Guarantee an SSH allow rule BEFORE enabling a default-deny firewall, or we lock out
    # remote admin of a headless host. Fall back to raw 22/tcp if the OpenSSH profile is absent.
    if ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1; then
        ufw allow from "${ALLOW_SUBNET}" to any port 443 proto tcp
        # TAP_PORTS may be space- or comma-separated.
        for _p in ${TAP_PORTS//,/ }; do
            [ -n "${_p}" ] || continue
            ufw allow from "${ALLOW_SUBNET}" to any port "${_p}" proto tcp
        done
        ufw --force enable
    else
        warn "ufw: could not add an SSH allow rule — NOT enabling the firewall (would lock out SSH)"
    fi
else
    log "ENABLE_UFW not 'true' — skipping firewall changes (bind guidance stays ${MOCKINGBUOY_SITE}-scoped)"
fi

# --------------------------------------------------------------------------- #
# 6. Secrets: on-device web password/hash (0600, git-ignored)                 #
# --------------------------------------------------------------------------- #
touch "${SVC_ENV}"
chown "${APP_USER}:${APP_GROUP}" "${SVC_ENV}"
chmod 0600 "${SVC_ENV}"

# Upsert the NON-secret keys (never touches the bcrypt hash line). Delete-then-append so
# the value is never fed through sed's replacement (which would mishandle | & \ in a value);
# the keys are fixed constants, so the delete pattern is injection-safe.
upsert_env() {
    local key="$1" val="$2"
    sed -i "/^${key}=/d" "${SVC_ENV}"
    printf '%s=%s\n' "${key}" "${val}" >> "${SVC_ENV}"
}
upsert_env "MOCKINGBUOY_SITE" "${MOCKINGBUOY_SITE}"
upsert_env "MOCKINGBUOY_BASIC_USER" "${MOCKINGBUOY_BASIC_USER}"
upsert_env "MOCKINGBUOY_BACKUP_DEST" "${BACKUP_DEST}"
upsert_env "MOCKINGBUOY_APP_PORT" "${APP_PORT}"

PW_JUST_GENERATED="false"
# bcrypt hashes start with $2a$/$2b$/$2y$. Present (non-empty) => leave auth untouched.
if grep -Eq '^MOCKINGBUOY_BASIC_HASH="?\$2[aby]\$' "${SVC_ENV}"; then
    log "web auth already configured — leaving existing password/hash untouched"
else
    log "generating first-run web password ..."
    # Stream /dev/urandom so `head` collects a FULL 24 chars (tr -dc keeps ~24% of bytes,
    # so a fixed 48-byte source would average only ~12). `|| true` absorbs tr's SIGPIPE.
    _pw="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom 2>/dev/null | head -c 24 || true)"
    [ "${#_pw}" -eq 24 ] || die "failed to generate a 24-char password"
    # Feed the plaintext via stdin, never argv (argv is world-readable in /proc/<pid>/cmdline).
    # No --plaintext fallback: that would put the secret on the command line. Fail instead.
    _hash="$(printf '%s' "${_pw}" | caddy hash-password 2>/dev/null || true)"
    [ -n "${_hash}" ] || die "caddy hash-password failed (need Caddy v2 with stdin support)"
    # Append the hash to the file (never echoed to the terminal/journal).
    printf 'MOCKINGBUOY_BASIC_HASH="%s"\n' "${_hash}" >> "${SVC_ENV}"
    chmod 0600 "${SVC_ENV}"
    chown "${APP_USER}:${APP_GROUP}" "${SVC_ENV}"
    unset _hash
    PW_JUST_GENERATED="true"
    printf '\n'
    printf '  ============================================================\n'
    printf '   STORE THIS NOW — the web password is shown ONE TIME ONLY:\n'
    printf '     username: %s\n' "${MOCKINGBUOY_BASIC_USER}"
    printf '     password: %s\n' "${_pw}"
    printf '   Only its bcrypt hash is stored on-device; it cannot be re-shown.\n'
    printf '  ============================================================\n\n'
    unset _pw
fi

# --------------------------------------------------------------------------- #
# 7. systemd units + validate Caddyfile + enable/start                        #
# --------------------------------------------------------------------------- #
log "installing systemd units ..."
install -m 0644 "${APP_DIR}/ops/mockingbuoy.service" /etc/systemd/system/mockingbuoy.service
install -d -m 0755 /etc/systemd/system/caddy.service.d
install -m 0644 "${APP_DIR}/ops/caddy.service.d/override.conf" \
    /etc/systemd/system/caddy.service.d/override.conf
install -m 0644 "${APP_DIR}/ops/mockingbuoy-backup.service" /etc/systemd/system/mockingbuoy-backup.service
install -m 0644 "${APP_DIR}/ops/mockingbuoy-backup.timer"   /etc/systemd/system/mockingbuoy-backup.timer

systemctl daemon-reload

log "validating Caddyfile ..."
# Read the bcrypt hash as raw data (NOT by sourcing service.env — bash would expand the
# $-delimited hash segments and validate a corrupted value), then inject via `env` so no
# shell expansion touches it.
_val_hash="$(sed -n 's/^MOCKINGBUOY_BASIC_HASH=//p' "${SVC_ENV}" | tr -d '"')"
if env MOCKINGBUOY_SITE="${MOCKINGBUOY_SITE}" \
       MOCKINGBUOY_BASIC_USER="${MOCKINGBUOY_BASIC_USER}" \
       MOCKINGBUOY_APP_PORT="${APP_PORT}" \
       MOCKINGBUOY_BASIC_HASH="${_val_hash}" \
       caddy validate --config "${APP_DIR}/Caddyfile" --adapter caddyfile >/dev/null 2>&1; then
    log "Caddyfile is valid"
else
    warn "caddy validate reported problems — inspect: caddy validate --config ${APP_DIR}/Caddyfile"
fi
unset _val_hash

log "enabling + (re)starting services ..."
systemctl enable mockingbuoy.service caddy >/dev/null 2>&1 || true
systemctl restart mockingbuoy.service
systemctl restart caddy

if [ -n "${BACKUP_DEST}" ]; then
    log "BACKUP_DEST set — enabling the daily host backup timer ..."
    systemctl enable --now mockingbuoy-backup.timer >/dev/null 2>&1 \
        || warn "could not enable mockingbuoy-backup.timer"
else
    log "BACKUP_DEST empty — backup timer NOT enabled (set BACKUP_DEST to enable)"
fi

# --------------------------------------------------------------------------- #
# 8. Build the offline wheelhouse (best-effort)                               #
# --------------------------------------------------------------------------- #
log "building offline wheelhouse (best-effort) ..."
if sudo -u "${APP_USER}" bash "${APP_DIR}/ops/wheelhouse-build.sh" >/dev/null 2>&1; then
    log "wheelhouse ready at ${APP_DIR}/wheelhouse (offline redeploy: no runtime internet dependency)"
else
    warn "wheelhouse build skipped/failed (no index reachable?) — offline redeploy needs it; re-run later"
fi

# --------------------------------------------------------------------------- #
# 9. Final summary                                                            #
# --------------------------------------------------------------------------- #
CONFIG_FILE="${APP_DIR}/config.json"
[ -f "${APP_DIR}/config.local.json" ] && CONFIG_FILE="${APP_DIR}/config.local.json"

printf '\n'
log "==================== install complete ===================="
log "Web UI (HTTPS, Basic auth):  https://${MOCKINGBUOY_SITE}/"
log "Basic-auth username:         ${MOCKINGBUOY_BASIC_USER}"
if [ "${PW_JUST_GENERATED}" = "true" ]; then
    log "Password:                    shown ONCE above — store it now"
else
    log "Password:                    unchanged (previously set; not re-shown)"
fi
log "Trust this CA on client machines (copy + import as a trusted root):"
log "    ${CADDY_CA_ROOT}"
log "Per-channel TCP taps (from $(basename "${CONFIG_FILE}")):"
python3 - "${CONFIG_FILE}" "${MOCKINGBUOY_SITE}" <<'PY' || warn "could not parse channel taps"
import json, sys
path, site = sys.argv[1], sys.argv[2]
try:
    cfg = json.load(open(path))
except Exception as exc:  # noqa: BLE001
    print(f"    (config unreadable: {exc})")
    sys.exit(0)
taps = []
for ch in cfg.get("channels", []):
    tap = ch.get("tcp_tap")
    if isinstance(tap, dict) and tap.get("enabled"):
        host = tap.get("host") or site
        port = tap.get("port")
        if port:
            taps.append((ch.get("id", "?"), host, port))
if not taps:
    print("    (none configured — add channels[].tcp_tap {enabled, port} to expose raw TCP feeds)")
else:
    for cid, host, port in taps:
        print(f"    {cid}: {host}:{port}")
PY
log "Service status:              systemctl status mockingbuoy caddy"
log "=========================================================="
