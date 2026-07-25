#!/usr/bin/env bash
#
# bootstrap.sh — one-command entrypoint fetched on a fresh host.
#
# Canonical install one-liner:
#   curl -fsSL https://raw.githubusercontent.com/corey-ingraham/mockingbuoy/main/bootstrap.sh | sudo bash
#
# It gets the repo onto the box and hands off to setup.sh, which does the real
# provisioning (venv + native Caddy + systemd, CA + one-time web password). The
# box needs internet only to INSTALL (apt + pip); nothing here runs at runtime.
#
# Any arguments passed to bootstrap.sh are forwarded to setup.sh, e.g.:
#   curl -fsSL .../bootstrap.sh | sudo bash -s -- --help
#
set -euo pipefail

REPO_URL="${MOCKINGBUOY_REPO_URL:-https://github.com/corey-ingraham/mockingbuoy}"
SRC_DIR="${MOCKINGBUOY_SRC_DIR:-/opt/src/mockingbuoy}"
# SUPPLY-CHAIN PIN: what to check out after fetch. This installer is meant to be piped into
# root (`curl ... | sudo bash`), so running whatever happens to be at an unpinned branch HEAD
# is a supply-chain risk — a compromised or mid-flight push executes as root. Pin to an
# immutable ref (a signed release TAG or a full commit SHA) via MOCKINGBUOY_REF and the box
# runs exactly that. Defaults to 'main' (mutable) only for convenience; a warning fires and,
# unless MOCKINGBUOY_ALLOW_UNPINNED=1, the install refuses to proceed unpinned.
MOCKINGBUOY_REF="${MOCKINGBUOY_REF:-main}"
MOCKINGBUOY_ALLOW_UNPINNED="${MOCKINGBUOY_ALLOW_UNPINNED:-0}"

# A ref is "pinned" if it is a full 40-hex commit SHA or a version tag (vX.Y[.Z]).
is_pinned_ref() {
  case "$1" in
    v[0-9]*.[0-9]*) return 0 ;;
    *) printf '%s' "$1" | grep -Eq '^[0-9a-f]{40}$' ;;
  esac
}

if ! is_pinned_ref "${MOCKINGBUOY_REF}"; then
  echo "bootstrap: WARNING — MOCKINGBUOY_REF='${MOCKINGBUOY_REF}' is an unpinned/mutable ref." >&2
  echo "bootstrap:   Piping a mutable branch into root runs whatever HEAD is at fetch time." >&2
  echo "bootstrap:   Pin a release tag or commit SHA, e.g.:" >&2
  echo "bootstrap:     curl -fsSL .../bootstrap.sh | sudo MOCKINGBUOY_REF=v1.2.3 bash" >&2
  if [ "${MOCKINGBUOY_ALLOW_UNPINNED}" != "1" ]; then
    echo "bootstrap: refusing to install from an unpinned ref (set MOCKINGBUOY_ALLOW_UNPINNED=1 to override)." >&2
    exit 1
  fi
  echo "bootstrap: MOCKINGBUOY_ALLOW_UNPINNED=1 — proceeding with the unpinned ref." >&2
fi

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

# --- fetch the repo, then check out the PINNED ref (idempotent) ---------------
# Always fetch into a bare-ish checkout and hard-set to the requested ref, rather than
# `pull --ff-only` onto whatever branch is checked out — so a pinned tag/SHA is honored on
# both first clone and re-converge, and a moved branch can't silently fast-forward the box.
if [ ! -d "$SRC_DIR/.git" ]; then
  echo "bootstrap: cloning $REPO_URL -> $SRC_DIR" >&2
  mkdir -p "$(dirname "$SRC_DIR")"
  git clone "$REPO_URL" "$SRC_DIR"
else
  echo "bootstrap: repo present at $SRC_DIR — fetching..." >&2
fi
echo "bootstrap: fetching + checking out ref '${MOCKINGBUOY_REF}'" >&2
git -C "$SRC_DIR" fetch --tags --force origin
# Resolve the ref to a concrete commit and check it out detached (works for tag or SHA).
if ! _sha="$(git -C "$SRC_DIR" rev-parse --verify --quiet "${MOCKINGBUOY_REF}^{commit}")"; then
  # Not present locally as a tag/SHA — try it as a remote branch tip.
  _sha="$(git -C "$SRC_DIR" rev-parse --verify --quiet "origin/${MOCKINGBUOY_REF}^{commit}")" \
    || { echo "bootstrap: ref '${MOCKINGBUOY_REF}' not found in $REPO_URL" >&2; exit 1; }
fi
git -C "$SRC_DIR" -c advice.detachedHead=false checkout --force "$_sha"
echo "bootstrap: checked out ${MOCKINGBUOY_REF} @ ${_sha}" >&2
# Optional integrity gate: set MOCKINGBUOY_REF_SHA256 to the expected commit SHA to fail hard
# on any mismatch (belt-and-suspenders against a rewritten tag).
if [ -n "${MOCKINGBUOY_REF_SHA256:-}" ] && [ "${MOCKINGBUOY_REF_SHA256}" != "${_sha}" ]; then
  echo "bootstrap: FATAL — checked-out commit ${_sha} != expected ${MOCKINGBUOY_REF_SHA256}" >&2
  exit 1
fi

# --- hand off to the provisioner ----------------------------------------------
cd "$SRC_DIR"
chmod +x ./setup.sh 2>/dev/null || true
echo "bootstrap: handing off to setup.sh" >&2
exec ./setup.sh "$@"
