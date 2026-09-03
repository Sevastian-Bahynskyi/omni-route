#!/bin/sh
set -eu

BASE="${HOME}/.local/share/omnigent-subscription-rotation"
PATCHED_OMNI="${BASE}/omnigent/.venv/bin/omni"
export PATH="${HOME}/.local/bin:${PATH}"

usage() {
  cat <<'TXT'
Omni Route

Usage:
  omni-rotate start [args...]     Start the routed session (ordered Codex + Claude accounts)
  omni-rotate test                Run full diagnostics
  omni-rotate status [args...]    Open the local route dashboard
  omni-rotate accounts            Add/configure subscription accounts
  omni-rotate sessions            Import/re-sync Codex + Claude session history
  omni-rotate help                Show this help

Compatibility:
  omni-rotate codex [args...]     Alias for `omni-rotate start`

Any other command is forwarded to the patched Omnigent CLI.
Examples:
  omni-rotate start
  omni-rotate test
  omni-rotate status --no-open
  omni-rotate accounts
  omni-rotate sessions
TXT
}

require_runtime() {
  if [ ! -x "$PATCHED_OMNI" ]; then
    echo "ERROR: patched Omnigent is not installed. Run install.sh first." >&2
    exit 1
  fi
}

command_name="${1:-help}"
case "$command_name" in
  help|-h|--help)
    usage
    ;;
  start|run|route|codex)
    shift
    require_runtime
    exec "${BASE}/omnigent/.venv/bin/python" "${BASE}/routed_start.py" "$@"
    ;;
  test|doctor|diagnose)
    shift
    exec python3 -S "${BASE}/diagnose.py" "$@"
    ;;
  status|dashboard)
    shift
    if { [ "$#" -eq 0 ] || { [ "$#" -eq 1 ] && [ "$1" = "--no-open" ]; }; } \
      && command -v curl >/dev/null 2>&1 \
      && curl -fsS "http://127.0.0.1:8787/api/status" >/dev/null 2>&1; then
      echo "Omni Route dashboard: http://127.0.0.1:8787/"
      if [ "$#" -eq 0 ]; then
        open "http://127.0.0.1:8787/" >/dev/null 2>&1 || true
      fi
      exit 0
    fi
    exec "${BASE}/omnigent/.venv/bin/python" -S "${BASE}/status_server_ext.py" "$@"
    ;;
  accounts|account|subscriptions|configure)
    shift
    exec python3 "${BASE}/configure_subscriptions.py" "$@"
    ;;
  sessions|history|import-sessions|sync-sessions)
    shift
    require_runtime
    exec "${BASE}/omnigent/.venv/bin/python" "${BASE}/import_sessions.py" "$@"
    ;;
  version)
    require_runtime
    exec "$PATCHED_OMNI" --version
    ;;
  *)
    require_runtime
    exec "$PATCHED_OMNI" "$@"
    ;;
esac
