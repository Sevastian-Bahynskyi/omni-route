#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/path_helpers.sh"
YES=false
if [[ "${1:-}" == "--yes" ]]; then
  YES=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--yes]" >&2
  exit 2
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: this script supports macOS only." >&2
  exit 1
fi

if [[ "$YES" != true ]]; then
  cat <<'WARNING'
This permanently removes Omnigent and Omni Route from this Mac, including:
  - normal Omnigent CLI
  - Omnigent Desktop app and Desktop data
  - ~/.omnigent sessions, credentials and state
  - Omni Route patched runtime and registered Codex account profiles
  - Omni Route PATH entries and Homebrew command shim
  - Omnigent backups under ~/.omnigent-backups
  - ~/omnigent workspace, if present
  - this cloned omni-route repository, when its origin is verified

Shared tools such as Homebrew, Git, uv, tmux, Node, Codex CLI and Claude CLI are kept.
WARNING
  printf '\nType DELETE to continue: '
  read -r confirmation
  if [[ "$confirmation" != "DELETE" ]]; then
    echo "Cancelled."
    exit 0
  fi
fi

echo "Stopping Omnigent Desktop and local processes..."
osascript -e 'tell application "Omnigent" to quit' >/dev/null 2>&1 || true
pkill -f '/Omnigent[^/]*/Contents/MacOS/' >/dev/null 2>&1 || true
pkill -f "$HOME/.local/share/omnigent-subscription-rotation/omnigent" >/dev/null 2>&1 || true
pkill -f "$HOME/.local/share/omnigent-subscription-rotation/status_server.py" >/dev/null 2>&1 || true
sleep 1

bundle_ids=()
for app in /Applications/Omnigent*.app "$HOME"/Applications/Omnigent*.app; do
  [[ -d "$app" ]] || continue
  if [[ -f "$app/Contents/Info.plist" ]]; then
    bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist" 2>/dev/null || true)"
    if [[ -n "$bundle_id" ]]; then
      bundle_ids+=("$bundle_id")
    fi
  fi
done

echo "Running official Omnigent cleanup..."
set +e
curl -fsSL https://raw.githubusercontent.com/omnigent-ai/omnigent/main/scripts/uninstall_oss.sh \
  | sh -s -- all --purge --purge-workspace --yes --force --no-backup \
      --modify-external-config --assume-inferred
uninstall_rc=$?
set -e
if [[ $uninstall_rc -ne 0 ]]; then
  echo "Official uninstaller returned ${uninstall_rc}; continuing with forced local cleanup."
fi

if command -v uv >/dev/null 2>&1; then
  uv tool uninstall omnigent >/dev/null 2>&1 || true
fi
if command -v brew >/dev/null 2>&1; then
  brew uninstall --force omnigent-ai/tap/omnigent >/dev/null 2>&1 || true
  brew untap omnigent-ai/tap >/dev/null 2>&1 || true
fi
rm -f "$HOME/.local/bin/omni" "$HOME/.local/bin/omnigent"

omni_route_remove_brew_shim || true
omni_route_remove_path || true

rm -f \
  "$HOME/.local/bin/omni-rotate" \
  "$HOME/.local/bin/omni-rotate-accounts" \
  "$HOME/.local/bin/omni-rotate-test" \
  "$HOME/.local/bin/omni-rotate-status"
rm -rf "$HOME/.local/share/omnigent-subscription-rotation"
rm -rf "$HOME/.omnigent" "$HOME/.omnigent-backups" "$HOME/omnigent"

for app in /Applications/Omnigent*.app; do
  [[ -e "$app" ]] || continue
  sudo rm -rf "$app"
done
for app in "$HOME"/Applications/Omnigent*.app; do
  [[ -e "$app" ]] || continue
  rm -rf "$app"
done

rm -rf \
  "$HOME/Library/Application Support/Omnigent" \
  "$HOME/Library/Caches/Omnigent" \
  "$HOME/Library/Logs/Omnigent" \
  "$HOME/Library/WebKit/Omnigent" \
  "$HOME/Library/HTTPStorages/Omnigent"

for bundle_id in "${bundle_ids[@]}"; do
  defaults delete "$bundle_id" >/dev/null 2>&1 || true
  rm -f "$HOME/Library/Preferences/${bundle_id}.plist"
  rm -rf \
    "$HOME/Library/Application Support/${bundle_id}" \
    "$HOME/Library/Caches/${bundle_id}" \
    "$HOME/Library/Logs/${bundle_id}" \
    "$HOME/Library/WebKit/${bundle_id}" \
    "$HOME/Library/HTTPStorages/${bundle_id}" \
    "$HOME/Library/Saved Application State/${bundle_id}.savedState" \
    "$HOME/Library/Containers/${bundle_id}" \
    "$HOME/Library/Application Scripts/${bundle_id}"
done

remove_repo=false
if [[ -d "$HERE/.git" ]]; then
  origin="$(git -C "$HERE" remote get-url origin 2>/dev/null || true)"
  case "$origin" in
    *Sevastian-Bahynskyi/omni-route.git|*Sevastian-Bahynskyi/omni-route)
      remove_repo=true
      ;;
  esac
fi

if [[ "$remove_repo" == true ]]; then
  echo "Removing cloned Omni Route repository: ${HERE}"
  cd "$HOME"
  rm -rf "$HERE"
else
  echo "Repository directory was not auto-deleted because its Git origin could not be verified: ${HERE}"
fi

echo
echo "Full Omnigent + Omni Route cleanup complete."
echo "Omni Route dashboard process, PATH entries and command shim were removed."
echo "Kept shared tools: Homebrew, Git, uv, tmux, Node, Codex CLI, Claude CLI."
