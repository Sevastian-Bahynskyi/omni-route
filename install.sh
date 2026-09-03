#!/usr/bin/env bash
set -euo pipefail

PINNED="2b13f2d7d85431c06e510d3c707c0c6d9a191a44"
BASE="${HOME}/.local/share/omnigent-subscription-rotation"
SRC="${BASE}/omnigent"
BIN="${HOME}/.local/bin"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: this installer is currently packaged for macOS." >&2
  exit 1
fi

ensure_brew_pkg() {
  local cmd="$1"
  local pkg="$2"
  if command -v "${cmd}" >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v brew >/dev/null 2>&1; then
    echo "ERROR: '${cmd}' is missing and Homebrew is unavailable." >&2
    exit 1
  fi
  echo "Installing ${pkg}..."
  brew install "${pkg}"
}

ensure_brew_pkg git git
ensure_brew_pkg uv uv
ensure_brew_pkg tmux tmux

if ! command -v codex >/dev/null 2>&1; then
  echo "ERROR: Codex CLI is required. Install Codex first." >&2
  exit 1
fi
echo "Ensuring Python 3.12 for Omnigent..."
uv python install 3.12 >/dev/null

mkdir -p "${BASE}" "${BIN}"

# Keep the previous patched checkout as one rollback copy until the new one
# has passed validation.
rm -rf "${SRC}.previous"
if [[ -d "${SRC}" ]]; then
  mv "${SRC}" "${SRC}.previous"
fi

cleanup_failed_install() {
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo
    echo "INSTALL FAILED. Restoring previous patched checkout if present." >&2
    rm -rf "${SRC}"
    if [[ -d "${SRC}.previous" ]]; then
      mv "${SRC}.previous" "${SRC}"
    fi
  fi
  exit $rc
}
trap cleanup_failed_install EXIT

echo "Cloning pinned Omnigent ${PINNED}..."
git clone --quiet https://github.com/omnigent-ai/omnigent.git "${SRC}"
git -C "${SRC}" checkout --quiet "${PINNED}"

actual="$(git -C "${SRC}" rev-parse HEAD)"
if [[ "${actual}" != "${PINNED}" ]]; then
  echo "ERROR: expected ${PINNED}, got ${actual}" >&2
  exit 1
fi

echo "Applying subscription-rotation extension..."
python3 "${HERE}/apply_patch.py" "${SRC}"

echo "Installing Omnigent dependencies in an isolated uv environment..."
(
  cd "${SRC}"
  uv sync --python 3.12
)

echo "Running extension self-tests..."
PYTHONPATH="${SRC}" "${SRC}/.venv/bin/python" "${HERE}/self_test.py"

echo "Compiling patched modules..."
(
  cd "${SRC}"
  "${SRC}/.venv/bin/python" -m compileall -q \
    omnigent/codex_account_pool.py \
    omnigent/codex_account_rotation.py \
    omnigent/codex_native_app_server.py \
    omnigent/inner/codex_native_executor.py \
    omnigent/codex_native_forwarder.py \
    omnigent/runner/native/orchestration.py
)

# Verify the actual installed entry point before asking for account logins.
"${SRC}/.venv/bin/omni" --version >/dev/null

"${HERE}/setup_accounts.sh"

cat > "${BIN}/omni-rotate" <<EOF_LAUNCHER
#!/bin/sh
exec "${SRC}/.venv/bin/omni" "\$@"
EOF_LAUNCHER
chmod +x "${BIN}/omni-rotate"

cp "${HERE}/configure_subscriptions.py" "${BASE}/configure_subscriptions.py"
chmod 700 "${BASE}/configure_subscriptions.py"

cat > "${BIN}/omni-rotate-accounts" <<EOF_ACCOUNTS
#!/bin/sh
exec python3 "${BASE}/configure_subscriptions.py" "\$@"
EOF_ACCOUNTS
chmod +x "${BIN}/omni-rotate-accounts"

rm -rf "${SRC}.previous"
trap - EXIT

echo
echo "============================================================"
echo "INSTALL COMPLETE"
echo "============================================================"
echo "Run patched Omnigent with:"
echo "  ${BIN}/omni-rotate codex"
echo
echo "Your existing normal 'omni' installation was NOT modified."
