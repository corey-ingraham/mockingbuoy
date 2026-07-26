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
    # Pin the Caddy repo signing key by FINGERPRINT. Fetching a key over TLS still trusts
    # whatever the CDN (or a MITM / upstream compromise) serves; verifying the fingerprint
    # makes the trust anchor explicit and fails CLOSED on any key substitution.
    CADDY_GPG_FPR="65760C51EDEA2017CEA2CA15155B6D79CA56EA34"
    _caddy_key="$(mktemp)"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor > "${_caddy_key}"
    _got_fpr="$(gpg --show-keys --with-colons "${_caddy_key}" 2>/dev/null \
        | awk -F: '/^fpr:/ {print $10; exit}')"
    if [ "${_got_fpr}" != "${CADDY_GPG_FPR}" ]; then
        rm -f "${_caddy_key}"
        die "Caddy apt key fingerprint mismatch (got '${_got_fpr:-none}', expected '${CADDY_GPG_FPR}') — refusing to trust the repo"
    fi
    install -m 0644 "${_caddy_key}" /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    rm -f "${_caddy_key}"
    log "Caddy apt key fingerprint verified (${CADDY_GPG_FPR})"
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
        --exclude 'secrets/' \
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
# Best-effort pip self-upgrade. An OFFLINE redeploy (venv rebuilt from the on-device
# wheelhouse) has no package index, and the wheelhouse never carries pip itself — so this
# must NEVER be fatal, or set -e aborts the whole install before systemd units are placed.
# The venv's bundled pip is sufficient to install from the wheelhouse.
"${PIP}" install --quiet --upgrade pip \
    || warn "pip self-upgrade skipped (no index reachable?) — using the venv's bundled pip"

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

# Python 3.12+ venvs no longer seed setuptools, so the PEP 517 editable build below has no
# backend ("Cannot import 'setuptools.build_meta'"). Provide it — from the offline wheelhouse
# when present, else the index. Best-effort: the app still imports via WorkingDirectory even if
# this and the editable install are skipped, so an offline host lacking a setuptools wheel only
# loses the console-script shims, not the service.
if ls wheelhouse/setuptools-*.whl >/dev/null 2>&1; then
    "${PIP}" install --quiet --no-index --find-links=wheelhouse setuptools wheel \
        || warn "setuptools/wheel not installed from wheelhouse — editable console-scripts may be unavailable"
else
    "${PIP}" install --quiet setuptools wheel \
        || warn "setuptools/wheel not installed (no index?) — editable console-scripts may be unavailable"
fi

# Register the app so 'uvicorn web.app:app' imports cleanly. --no-build-isolation uses the
# venv's setuptools (installed just above; no index round-trip); --no-deps because deps are
# done above. Non-fatal: the unit also runs with WorkingDirectory=${APP_DIR}, so 'web' imports
# anyway.
log "installing the mockingbuoy package (editable) ..."
"${PIP}" install --quiet --no-build-isolation --no-deps -e . \
    || warn "editable install failed; app still importable via WorkingDirectory=${APP_DIR}"

# Privilege separation: the app user must NOT own the code, the systemd unit SOURCES under
# ops/, or the live Caddyfile — an app-writable copy of those is a root-escalation path on
# the next converge (the app runs the unit/proxy config). Root owns the tree; the app user
# owns ONLY the paths it legitimately writes: data/ (runtime state), wheelhouse/ (built as
# the app user), and secrets/ (its own 0600 service.env).
log "setting ownership: root owns code/ops/Caddyfile; ${APP_USER} owns only data/ + wheelhouse/ + secrets/ ..."
chown -R root:root "${APP_DIR}"
install -d -m 0755 -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}/wheelhouse"
chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}/data" "${APP_DIR}/wheelhouse" "${APP_DIR}/secrets"

# --------------------------------------------------------------------------- #
# 5. Host config: udev, brltty, time sync, optional firewall                  #
# --------------------------------------------------------------------------- #
log "installing udev rules for stable /dev/nmea-* symlinks ..."
# One rules file provisions BOTH roles: the TX output symlinks (nmea-gps/heading/ais)
# and the six LISTEN-ONLY RX input/diagnostic slots (nmea-in-1 .. nmea-in-6) that feed
# Auto-mode inputs and the Maintenance multi-port view. Installing the file installs all
# of them; the same --reload/--trigger applies to every slot.
#
# PRESERVE OPERATOR CUSTOMIZATION: the shipped file carries <VID>/<PID>/<*_SERIAL>
# placeholders that the operator MUST replace with real per-host serials for the symlinks to
# appear. Those serials are host-local and never live in the repo — so a re-converge must NOT
# clobber an edited rule with the pristine template (that silently deletes every /dev/nmea-*
# symlink on the next udev trigger, exactly as a first field deploy hit). Install the template
# only when the destination is absent or still byte-identical to it; otherwise leave the
# operator's edited rule untouched (mirrors how the web password is preserved on re-run).
DEST_UDEV_RULE="/etc/udev/rules.d/99-mockingbuoy.rules"
if [ -f "${DEST_UDEV_RULE}" ] && ! cmp -s "${APP_DIR}/ops/99-mockingbuoy.rules" "${DEST_UDEV_RULE}"; then
    log "udev rule already customized (differs from template) — preserving it, not overwriting"
else
    install -m 0644 "${APP_DIR}/ops/99-mockingbuoy.rules" "${DEST_UDEV_RULE}"
    log "NOTE: edit ${DEST_UDEV_RULE} and replace the ATTRS{serial}/<VID>/<PID>"
    log "      placeholders with YOUR adapters' serials — for the TX outputs"
    log "      (nmea-gps/heading/ais) AND for whichever RX input slots you use"
    log "      (nmea-in-1 .. nmea-in-6); then: udevadm control --reload && udevadm trigger"
fi
udevadm control --reload >/dev/null 2>&1 || warn "udevadm control --reload failed (no udev running?)"
udevadm trigger >/dev/null 2>&1 || warn "udevadm trigger failed"

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
# secrets/ is ROOT-owned 0700: systemd reads EnvironmentFile as root PRE-drop, so the app (which
# reads only os.environ) needs no access. Root ownership kills the app-owned-dir symlink/TOCTOU
# vector on the file the root path-unit rewrites.
install -d -m 0700 -o root -g root "$(dirname "${SVC_ENV}")"
touch "${SVC_ENV}"
chown root:root "${SVC_ENV}"
chmod 0600 "${SVC_ENV}"

# Upsert the NON-secret keys (never touches the bcrypt hash line). Delete-then-append so
# the value is never fed through sed's replacement (which would mishandle | & \ in a value);
# the keys are fixed constants, so the delete pattern is injection-safe.
upsert_env() {
    local key="$1" val="$2"
    sed -i "/^${key}=/d" "${SVC_ENV}"
    printf '%s=%s\n' "${key}" "${val}" >> "${SVC_ENV}"
}
# Site address(es) for the imported Caddy snippet. Precedence: an operator value passed THIS run
# (MOCKINGBUOY_SITE != placeholder) wins; else PRESERVE a persisted non-placeholder value (never
# silently flip a working IP install to a hostname before DNS exists); else default to the friendly
# hostname. MOCKINGBUOY_SITE is a single host/IP (no port) — the snippet adds :443. A raw-IP alias
# (the box's primary LAN IP) is always emitted so the UI stays reachable by IP even before local DNS
# resolves the name; it falls back to 127.0.0.1 when undetected or when it would duplicate the primary.
if [ "${MOCKINGBUOY_SITE}" != "<LAN_IP>" ]; then
    _site="${MOCKINGBUOY_SITE}"
else
    _existing_site="$(sed -n 's/^MOCKINGBUOY_SITE=//p' "${SVC_ENV}" | tr -d '"' | head -n1)"
    if [ -n "${_existing_site}" ] && [ "${_existing_site}" != "<LAN_IP>" ]; then
        _site="${_existing_site}"
    else
        _site="mockingbuoy.eemslab.internal"
    fi
fi
_site="${_site%:443}"   # snippet re-adds :443; guard against host:443:443
_box_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
_alias="${_box_ip:-127.0.0.1}"
[ "${_alias}" = "${_site}" ] && _alias="127.0.0.1"
upsert_env "MOCKINGBUOY_SITE" "${_site}"
upsert_env "MOCKINGBUOY_SITE_ALIAS" "${_alias}"
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
    # caddy reads the password as a newline-TERMINATED line: a bare `printf '%s'` (no trailing
    # newline) makes it hit EOF mid-read and abort ("Error: EOF"), so terminate the line. Pin
    # --algorithm bcrypt: the Caddyfile basic_auth directive validates bcrypt and the detection
    # above matches $2[aby]$ — don't depend on caddy's default staying bcrypt.
    _hash="$(printf '%s\n' "${_pw}" | caddy hash-password --algorithm bcrypt 2>/dev/null || true)"
    [ -n "${_hash}" ] || die "caddy hash-password failed (need Caddy v2 with stdin support)"
    # Append the hash to the file (never echoed to the terminal/journal).
    printf 'MOCKINGBUOY_BASIC_HASH="%s"\n' "${_hash}" >> "${SVC_ENV}"
    chmod 0600 "${SVC_ENV}"
    chown root:root "${SVC_ENV}"
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

# In-app web-password rotation: a root path-unit watches data/webpass-request.json (the only file
# the sandboxed app can write) and runs ops/bin/rotate-webpass to do the privileged rewrite +
# caddy restart the app itself cannot.
install -m 0644 "${APP_DIR}/ops/systemd/mockingbuoy-webpass.path" \
    /etc/systemd/system/mockingbuoy-webpass.path
install -m 0644 "${APP_DIR}/ops/systemd/mockingbuoy-webpass.service" \
    /etc/systemd/system/mockingbuoy-webpass.service
chmod 0755 "${APP_DIR}/ops/bin/rotate-webpass"
# caddy-validate: operator helper to validate the shared Caddy config with caddy.service's env
# injected (a bare `caddy validate` falsely fails on mockingbuoy.caddy's env-sourced basic_auth).
chmod 0755 "${APP_DIR}/ops/bin/caddy-validate" 2>/dev/null || true

# The app binds a unix socket at /run/mockingbuoy/app.sock (created by the unit's
# RuntimeDirectory=mockingbuoy, group-accessible via UMask=0007). Caddy reverse-proxies
# to that socket, so the caddy process must be in the mockingbuoy group. The caddy
# override already sets SupplementaryGroups=mockingbuoy; we ALSO add the caddy OS user to
# the group here (idempotent) so the membership holds regardless of how caddy is started.
if id -u caddy >/dev/null 2>&1; then
    log "adding caddy user to '${APP_GROUP}' group (unix-socket app bind access) ..."
    usermod -aG "${APP_GROUP}" caddy
else
    warn "caddy user not found — skipping group add; SupplementaryGroups in the override still applies"
fi

systemctl daemon-reload

log "installing mockingbuoy Caddy site snippet (coexists with other conf.d sites) ..."
CADDY_MAIN="/etc/caddy/Caddyfile"
CADDY_CONFD="/etc/caddy/conf.d"
CADDY_SNIPPET="${CADDY_CONFD}/mockingbuoy.caddy"
install -d -m 0755 "${CADDY_CONFD}"

# Ensure a SHARED main Caddyfile that imports every conf.d site. Create it (globals + import) ONLY
# if absent; if it already exists (e.g. another service set it up), append the import line only if missing and
# NEVER rewrite the file — that is what keeps the other reverse-proxy sites intact across a
# mockingbuoy redeploy.
_import_line="import ${CADDY_CONFD}/*.caddy"
if [ ! -f "${CADDY_MAIN}" ]; then
    printf '{\n\tadmin off\n}\n\n%s\n' "${_import_line}" > "${CADDY_MAIN}"
    chmod 0644 "${CADDY_MAIN}"
    log "created shared ${CADDY_MAIN} (admin off + conf.d import)"
elif ! grep -qxF "${_import_line}" "${CADDY_MAIN}"; then
    # guarantee a trailing newline so the appended line can't glue onto the last existing line
    [ -n "$(tail -c1 "${CADDY_MAIN}" 2>/dev/null)" ] && printf '\n' >> "${CADDY_MAIN}"
    printf '%s\n' "${_import_line}" >> "${CADDY_MAIN}"
    log "added conf.d import to existing ${CADDY_MAIN} (other sites untouched)"
fi

# Back up any existing snippet for rollback, then install ours.
_snippet_bak=""
if [ -f "${CADDY_SNIPPET}" ]; then _snippet_bak="$(mktemp)"; cp -p "${CADDY_SNIPPET}" "${_snippet_bak}"; fi
install -m 0644 "${APP_DIR}/Caddyfile" "${CADDY_SNIPPET}"

log "validating combined Caddy config (${CADDY_MAIN}) ..."
# Read the bcrypt hash as raw data (NOT by sourcing service.env — bash would expand the
# $-delimited hash segments and validate a corrupted value), then inject via `env` so no
# shell expansion touches it.
_val_hash="$(sed -n 's/^MOCKINGBUOY_BASIC_HASH=//p' "${SVC_ENV}" | tr -d '"')"
if env MOCKINGBUOY_SITE="${_site}" \
       MOCKINGBUOY_SITE_ALIAS="${_alias}" \
       MOCKINGBUOY_BASIC_USER="${MOCKINGBUOY_BASIC_USER}" \
       MOCKINGBUOY_APP_PORT="${APP_PORT}" \
       MOCKINGBUOY_BASIC_HASH="${_val_hash}" \
       caddy validate --config "${CADDY_MAIN}" --adapter caddyfile >/dev/null 2>&1; then
    log "combined Caddy config is valid"
    [ -n "${_snippet_bak}" ] && rm -f "${_snippet_bak}"
else
    # ROLLBACK: never leave a broken snippet armed to take down ALL sites at the next restart.
    # Restore the prior snippet (or remove ours if there was none), then fail loudly before restart.
    if [ -n "${_snippet_bak}" ]; then
        cp -p "${_snippet_bak}" "${CADDY_SNIPPET}"; rm -f "${_snippet_bak}"
    else
        rm -f "${CADDY_SNIPPET}"
    fi
    unset _val_hash
    die "combined Caddy config validation FAILED — rolled back mockingbuoy.caddy, refusing to restart. Inspect: sudo ${APP_DIR}/ops/bin/caddy-validate (injects the service env; a bare 'caddy validate' falsely fails on the empty basic_auth hash)"
fi
unset _val_hash

log "enabling + (re)starting services ..."
systemctl enable mockingbuoy.service caddy >/dev/null 2>&1 || true
# Start watching for in-app web-password rotation requests (root path-unit).
systemctl enable --now mockingbuoy-webpass.path >/dev/null 2>&1 || true
systemctl restart mockingbuoy.service
systemctl restart caddy

# Host backup timer is DISABLED pending the C3 redesign: the old backup staged the Caddy CA
# PRIVATE key into the app-writable data/ dir (RCE -> mint trusted certs), and neither
# documented rsync destination actually worked. The unit ships de-activated (the timer has
# no [Install] target), so setup.sh deliberately does NOT enable it here. Re-enable only
# after the redesign (root-owned CA staging outside data/ or re-mint on restore, provisioned
# transport, encrypted dated generations) lands.
if [ -n "${BACKUP_DEST}" ]; then
    warn "BACKUP_DEST is set, but the host backup timer is DISABLED pending redesign (C3) — NOT enabling"
else
    log "host backup timer not enabled (disabled pending C3 redesign)"
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
# Standardize on data/config.local.json — the ONLY local override the running app reads
# (web/app.py); the tracked config.json is just the baseline. Report from whichever the
# app would actually load.
CONFIG_FILE="${APP_DIR}/config.json"
[ -f "${APP_DIR}/data/config.local.json" ] && CONFIG_FILE="${APP_DIR}/data/config.local.json"

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
        # tcp_tap has no 'host' key in the schema; the tap binds on this appliance and is
        # addressed via MOCKINGBUOY_SITE. (The old tap.get("host") read a phantom key.)
        host = site
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
