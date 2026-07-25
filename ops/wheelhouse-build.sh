#!/usr/bin/env bash
# wheelhouse-build.sh — populate ./wheelhouse with the hash-locked dependency set so a
# fresh host can rebuild the venv with NO runtime internet dependency:
#
#   python3 -m venv /opt/mockingbuoy/.venv
#   /opt/mockingbuoy/.venv/bin/pip install \
#       --no-index --find-links=wheelhouse --require-hashes -r requirements.txt
#
# Idempotent: re-running only downloads what's missing (pip skips already-present
# artifacts). POSIX-safe, no bashisms beyond `set -o pipefail`.
set -euo pipefail

# Resolve repo root from this script's location (ops/ is one level down).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

REQ_FILE="${REPO_ROOT}/requirements.txt"
WHEELHOUSE="${REPO_ROOT}/wheelhouse"

if [ ! -f "${REQ_FILE}" ]; then
    echo "error: requirements.txt not found at ${REQ_FILE}" >&2
    exit 1
fi

# Prefer the project venv's pip if present, else the system python3.
if [ -x "${REPO_ROOT}/.venv/bin/pip" ]; then
    PIP="${REPO_ROOT}/.venv/bin/pip"
elif command -v python3 >/dev/null 2>&1; then
    PIP="python3 -m pip"
else
    echo "error: no python3 / venv pip available to run pip download" >&2
    exit 1
fi

mkdir -p "${WHEELHOUSE}"

# Adapt to whichever lock is present: a hashed lock (pip-compile --generate-hashes) enables
# --require-hashes; the plain convenience lock does not (hash mode rejects un-hashed reqs and
# would abort). Detect it so the script works as shipped either way.
if grep -q -- '--hash=' "${REQ_FILE}"; then
    HASH_FLAG="--require-hashes"
    echo "Detected hash-locked ${REQ_FILE}; enforcing digests."
else
    HASH_FLAG=""
    echo "note: ${REQ_FILE} is un-hashed; downloading without --require-hashes." >&2
    echo "      for a hash-pinned wheelhouse, regenerate it first:" >&2
    echo "      pip-compile --generate-hashes -o requirements.txt requirements.in" >&2
fi

echo "Downloading wheels from ${REQ_FILE} into ${WHEELHOUSE} ..."
# --only-binary=:all: forces built wheels for EVERY dependency. Without it, an sdist-only
# pin (or a platform with no matching wheel) captures a source distribution that then needs
# a compiler + build deps to install — which the OFFLINE DR host does not have. Fail here,
# at build time with an index, instead of at restore time with none.
# shellcheck disable=SC2086  # HASH_FLAG is intentionally word-split (empty => no flag)
${PIP} download ${HASH_FLAG} \
    --only-binary=:all: \
    --dest "${WHEELHOUSE}" \
    --requirement "${REQ_FILE}"

# Also capture the PEP 517 build backend (setuptools + wheel). Modern venvs (Python 3.12+) no
# longer seed setuptools, so an offline editable install of the app has no backend without
# these. They are build-time only (not in requirements.txt), so fetch them separately and
# un-hashed — universal pure-Python wheels. Non-fatal: the service imports via WorkingDirectory
# regardless; only the console-script shims need the editable install.
echo "Downloading build backend (setuptools, wheel) into ${WHEELHOUSE} ..."
# shellcheck disable=SC2086  # PIP may be `python3 -m pip`; intentional word-split
${PIP} download --only-binary=:all: --dest "${WHEELHOUSE}" setuptools wheel \
    || echo "warn: could not download setuptools/wheel — offline editable install may lack a backend" >&2

# Write a checksum manifest so wheelhouse rot (a truncated/corrupt/half-synced artifact) is
# detectable before an offline install trusts it: verify with
#   cd wheelhouse && sha256sum -c MANIFEST.sha256
echo "Writing ${WHEELHOUSE}/MANIFEST.sha256 ..."
(
    cd "${WHEELHOUSE}" || exit 1
    # List artifacts deterministically; exclude the manifest itself. Empty-safe.
    find . -maxdepth 1 -type f ! -name 'MANIFEST.sha256' -printf '%P\n' \
        | LC_ALL=C sort \
        | xargs -r sha256sum > MANIFEST.sha256
)

echo "Done. Offline install with:"
echo "  pip install --no-index --find-links='${WHEELHOUSE}' ${HASH_FLAG} -r '${REQ_FILE}'"
