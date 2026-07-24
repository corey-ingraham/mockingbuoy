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
# shellcheck disable=SC2086  # HASH_FLAG is intentionally word-split (empty => no flag)
${PIP} download ${HASH_FLAG} \
    --dest "${WHEELHOUSE}" \
    --requirement "${REQ_FILE}"

echo "Done. Offline install with:"
echo "  pip install --no-index --find-links='${WHEELHOUSE}' ${HASH_FLAG} -r '${REQ_FILE}'"
