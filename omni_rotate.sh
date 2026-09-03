#!/bin/sh
set -eu

BASE="${HOME}/.local/share/omnigent-subscription-rotation"
PATCHED_OMNI="${BASE}/omnigent/.venv/bin/omni"
export PATH="${HOME}/.local/bin:${PATH}"

usage() {
  cat <<'TXT'
Omni Route

Usage:
  omni-rotate codex [args...]     Start patched Omnigent with Codex routing
  omni-rotate test               Run full read-only diagnostics
  omni-rotate status [args...]   Open the local status dashboard
  omni-rotate accounts           Add/configure subscription accounts
  omni-rotate help               Show this help

Any other command is forwarded to the patched Omnigent CLI.
Examples:
  omni-rotate codex
  omni-rotate test
  omni-rotate status --no-open
  omni-rotate accounts
TXT
}

command_name="${1:-help}"
case "$command_name" in
  help|-h|--help)
    usage
    ;;
  test|doctor|diagnose)
    shift
    exec python3 -S "${BASE}/diagnose.py" "$@"
    ;;
  status|dashboard)
    shift
    exec python3 -S "${BASE}/status_server.py" "$@"
    ;;
  accounts|account|subscriptions|configure)
    shift
    exec python3 "${BASE}/configure_subscriptions.py" "$@"
    ;;
  version)
    if [ ! -x "$PATCHED_OMNI" ]; then
      echo "ERROR: patched Omnigent is not installed. Run install.sh first." >&2
      exit 1
    fi
    exec "$PATCHED_OMNI" --version
    ;;
  *)
    if [ ! -x "$PATCHED_OMNI" ]; then
      echo "ERROR: patched Omnigent is not installed. Run install.sh first." >&2
      exit 1
    fi
    exec "$PATCHED_OMNI" "$@"
    ;;
esac
