#!/usr/bin/env bash
set -euo pipefail

PINNED="2b13f2d7d85431c06e510d3c707c0c6d9a191a44"
BASE="${HOME}/.local/share/omnigent-subscription-rotation"
SRC="${BASE}/omnigent"
BIN="${HOME}/.local/bin"
HERE="$(cd "$(dirname "$0")" && pwd)"
source "${HERE}/path_helpers.sh"

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

tailscale_installed() {
  command -v tailscale >/dev/null 2>&1 \
    || [[ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]] \
    || [[ -x "${HOME}/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]]
}

ensure_tailscale() {
  if tailscale_installed; then
    return 0
  fi
  if ! command -v brew >/dev/null 2>&1; then
    echo "ERROR: Tailscale is missing and Homebrew is unavailable." >&2
    exit 1
  fi
  echo "Installing Tailscale..."
  brew install --cask tailscale-app
  if ! tailscale_installed; then
    echo "ERROR: Tailscale installation completed but the app/CLI could not be found." >&2
    exit 1
  fi
}

ensure_brew_pkg git git
ensure_brew_pkg uv uv
ensure_brew_pkg tmux tmux
ensure_tailscale

if ! command -v codex >/dev/null 2>&1 && ! command -v claude >/dev/null 2>&1; then
  echo "ERROR: Install Codex CLI or Claude Code first." >&2
  exit 1
fi

echo "Ensuring Python 3.12 for Omnigent..."
uv python install 3.12 >/dev/null

mkdir -p "${BASE}" "${BIN}"

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
PYTHONPATH="${SRC}" "${SRC}/.venv/bin/python" "${HERE}/test_routing.py"
python3 "${HERE}/test_setup.py"
python3 "${HERE}/test_dashboard.py"
python3 "${HERE}/import_sessions.py" --self-test
python3 -m py_compile \
  "${HERE}/status_server.py" \
  "${HERE}/status_server_ext.py" \
  "${HERE}/remote_access.py" \
  "${HERE}/switch_provider.py" \
  "${HERE}/import_sessions.py"

echo "Compiling patched modules..."
(
  cd "${SRC}"
  "${SRC}/.venv/bin/python" -m compileall -q \
    omnigent/claude_account_integration.py \
    omnigent/claude_native_bridge.py \
    omnigent/claude_native_forwarder.py \
    omnigent/codex_account_pool.py \
    omnigent/codex_account_rotation.py \
    omnigent/codex_native_app_server.py \
    omnigent/inner/codex_native_executor.py \
    omnigent/codex_native_forwarder.py \
    omnigent/runner/native/orchestration.py
)

"${SRC}/.venv/bin/omni" --version >/dev/null

"${HERE}/setup_accounts.sh"

cp "${HERE}/configure_subscriptions.py" "${BASE}/configure_subscriptions.py"
cp "${HERE}/diagnose.py" "${BASE}/diagnose.py"
cp "${HERE}/status_server.py" "${BASE}/status_server.py"
cp "${HERE}/status_server_ext.py" "${BASE}/status_server_ext.py"
cp "${HERE}/remote_access.py" "${BASE}/remote_access.py"
cp "${HERE}/dashboard.html" "${BASE}/dashboard.html"
cp "${HERE}/switch_provider.py" "${BASE}/switch_provider.py"
cp "${HERE}/routed_start.py" "${BASE}/routed_start.py"
cp "${HERE}/import_sessions.py" "${BASE}/import_sessions.py"
chmod 700 \
  "${BASE}/configure_subscriptions.py" \
  "${BASE}/diagnose.py" \
  "${BASE}/status_server.py" \
  "${BASE}/status_server_ext.py" \
  "${BASE}/remote_access.py" \
  "${BASE}/dashboard.html" \
  "${BASE}/switch_provider.py" \
  "${BASE}/import_sessions.py"

cp "${HERE}/omni_rotate.sh" "${BIN}/omni-rotate"
chmod 755 "${BIN}/omni-rotate"

# Backward-compatible aliases. New usage is `omni-rotate <subcommand>`.
cat > "${BIN}/omni-rotate-accounts" <<EOF_ACCOUNTS
#!/bin/sh
exec "${BIN}/omni-rotate" accounts "\$@"
EOF_ACCOUNTS
cat > "${BIN}/omni-rotate-test" <<EOF_TEST
#!/bin/sh
exec "${BIN}/omni-rotate" test "\$@"
EOF_TEST
cat > "${BIN}/omni-rotate-status" <<EOF_STATUS
#!/bin/sh
exec "${BIN}/omni-rotate" status "\$@"
EOF_STATUS
chmod 755 \
  "${BIN}/omni-rotate-accounts" \
  "${BIN}/omni-rotate-test" \
  "${BIN}/omni-rotate-status"

omni_route_add_path
omni_route_install_brew_shim

echo
echo "Importing existing Codex and Claude sessions into Omnigent projects..."
set +e
"${SRC}/.venv/bin/python" "${BASE}/import_sessions.py"
SESSION_IMPORT_RC=$?
set -e
if [[ $SESSION_IMPORT_RC -ne 0 ]]; then
  echo "WARNING: session history import failed (exit ${SESSION_IMPORT_RC})." >&2
  echo "You can retry later with: omni-rotate sessions" >&2
fi

rm -rf "${SRC}.previous"
trap - EXIT

echo
echo "============================================================"
echo "INSTALL COMPLETE"
echo "============================================================"
echo "Use Omni Route from any directory:"
echo "  omni-rotate start"
echo "  omni-rotate test"
echo "  omni-rotate status"
echo "  omni-rotate accounts"
echo "  omni-rotate sessions"
echo
echo "Compatibility aliases are still installed:"
echo "  omni-rotate codex / omni-rotate-test / omni-rotate-status / omni-rotate-accounts"
echo
echo "Your existing normal 'omni' installation was NOT modified."
echo "Tailscale is installed for optional tailnet-only dashboard access; sign in to Tailscale before enabling it in the dashboard."

# Keep the dashboard available after the installer exits. If a dashboard is
# already serving on the default port, reuse it; otherwise start one detached.
DASHBOARD_URL="http://127.0.0.1:8787/"
pkill -f "${BASE}/status_server_ext.py" >/dev/null 2>&1 || true
pkill -f "${BASE}/status_server.py" >/dev/null 2>&1 || true
nohup "${BIN}/omni-rotate" status --no-open >"${BASE}/status-dashboard.log" 2>&1 </dev/null &
sleep 0.7
echo "Opening Omni Route dashboard: ${DASHBOARD_URL}"
open "${DASHBOARD_URL}" >/dev/null 2>&1 || true
