#!/bin/sh
# Launch Claude Desktop under an isolated per-account profile.
#
#   ./launch_claude_profile.sh <account-name> [--with-config-dir] [--quit-existing]
#
# Claude Desktop keeps its account in its Electron user-data directory, so each
# Omni Route account needs its own. `open -n` is used rather than running the
# binary directly: it hands the process to launchd, so it survives the shell
# that started it. A backgrounded `nohup` from a non-interactive shell does not.
#
# --with-config-dir additionally points CLAUDE_CONFIG_DIR at the matching CLI
# profile. Leave it OFF for a first sign-in: that profile already holds
# credentials, which makes it impossible to tell a fresh profile from a leaked
# one.
set -eu

APP="/Applications/Claude.app"
NAME="${1:-}"
[ -n "$NAME" ] || { echo "usage: $0 <account-name> [--with-config-dir] [--quit-existing]" >&2; exit 2; }
shift

WITH_CONFIG=0
QUIT_EXISTING=0
for arg in "$@"; do
  case "$arg" in
    --with-config-dir) WITH_CONFIG=1 ;;
    --quit-existing)   QUIT_EXISTING=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

UDD="${HOME}/.omnigent/claude-desktop/${NAME}"
CFG="${HOME}/.omnigent/claude-accounts/${NAME}"

[ -d "$APP" ] || { echo "ERROR: $APP is not installed" >&2; exit 1; }

if [ "$QUIT_EXISTING" -eq 1 ]; then
  echo "quitting any running Claude Desktop..."
  osascript -e 'tell application "Claude" to quit' >/dev/null 2>&1 || true
  i=0
  while pgrep -f "${APP}/Contents/MacOS/Claude" >/dev/null 2>&1 && [ "$i" -lt 15 ]; do
    sleep 1; i=$((i + 1))
  done
  pkill -f "^${APP}/Contents/MacOS/Claude" 2>/dev/null || true
  sleep 2
fi

mkdir -p "$UDD"
echo "profile      : $NAME"
echo "user-data-dir: $UDD"

if [ "$WITH_CONFIG" -eq 1 ]; then
  [ -d "$CFG" ] || { echo "ERROR: config dir $CFG does not exist" >&2; exit 1; }
  echo "config-dir   : $CFG"
  open -n -a "$APP" --env "CLAUDE_CONFIG_DIR=${CFG}" --args --user-data-dir="$UDD"
else
  echo "config-dir   : (not set - correct for a first sign-in)"
  open -n -a "$APP" --args --user-data-dir="$UDD"
fi

# Confirm the flag actually took. A window that opened without it is the
# default profile, which is a different account.
i=0
while [ "$i" -lt 20 ]; do
  if ps -Ao command= | grep -F -- "--user-data-dir=$UDD" | grep -qv grep; then
    echo ""
    echo "PASS: Claude is running under the isolated profile."
    ps -Ao command= | grep -F -- "--user-data-dir=$UDD" | grep -v grep | cut -c1-200 | head -1
    exit 0
  fi
  sleep 1; i=$((i + 1))
done

echo "" >&2
echo "FAIL: no Claude process is running with --user-data-dir=$UDD" >&2
echo "The window you see is probably the default profile (a different account)." >&2
exit 1
