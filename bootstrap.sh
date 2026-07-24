#!/usr/bin/env bash
#
# bootstrap.sh — one-command entrypoint fetched on a fresh host.
#
# Canonical install one-liner:
#   curl -fsSL https://raw.githubusercontent.com/<you>/mockingbuoy/main/bootstrap.sh | sudo bash
#
# It gets the repo onto the box and hands off to setup.sh, which does the real
# provisioning (venv + native Caddy + systemd, CA + one-time web password). The
# box needs internet only to INSTALL (apt + pip); nothing here runs at runtime.
#
# Any arguments passed to bootstrap.sh are forwarded to setup.sh, e.g.:
#   curl -fsSL .../bootstrap.sh | sudo bash -s -- --help
#
set -euo pipefail

REPO_URL="${MOCKINGBUOY_REPO_URL:-https://github.com/<you>/mockingbuoy}"
SRC_DIR="${MOCKINGBUOY_SRC_DIR:-/opt/src/mockingbuoy}"

# --- must run as root ---------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    echo "bootstrap: re-executing under sudo..." >&2
    exec sudo -E bash "$0" "$@"
  fi
  echo "bootstrap: must run as root (install sudo, or run as root)." >&2
  exit 1
fi

# --- base tools ---------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git curl ca-certificates

# --- fetch the repo (idempotent: clone once, pull thereafter) -----------------
if [ -d "$SRC_DIR/.git" ]; then
  echo "bootstrap: repo present at $SRC_DIR — updating..." >&2
  git -C "$SRC_DIR" pull --ff-only
else
  echo "bootstrap: cloning $REPO_URL -> $SRC_DIR" >&2
  mkdir -p "$(dirname "$SRC_DIR")"
  git clone "$REPO_URL" "$SRC_DIR"
fi

# --- hand off to the provisioner ----------------------------------------------
cd "$SRC_DIR"
chmod +x ./setup.sh 2>/dev/null || true
echo "bootstrap: handing off to setup.sh" >&2
exec ./setup.sh "$@"
