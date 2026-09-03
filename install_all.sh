#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: this script supports macOS only." >&2
  exit 1
fi

if ! command -v brew >/dev/null 2>&1; then
  echo "ERROR: Homebrew is required. Install it from https://brew.sh and rerun." >&2
  exit 1
fi

ensure_formula() {
  local command_name="$1"
  local formula="$2"
  if command -v "$command_name" >/dev/null 2>&1; then
    return 0
  fi
  echo "Installing ${formula} with Homebrew..."
  brew install "$formula"
}

ensure_formula git git
ensure_formula curl curl
ensure_formula uv uv
ensure_formula tmux tmux

node_ok=false
if command -v node >/dev/null 2>&1; then
  node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  if [[ "$node_major" =~ ^[0-9]+$ ]] && (( node_major >= 22 )); then
    node_ok=true
  fi
fi
if [[ "$node_ok" != true ]]; then
  echo "Installing Node.js 22 with Homebrew..."
  brew install node@22
  export PATH="$(brew --prefix node@22)/bin:${PATH}"
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: npm is unavailable after Node setup." >&2
  exit 1
fi

if ! command -v codex >/dev/null 2>&1; then
  echo "Installing the official Codex CLI..."
  npm install -g @openai/codex
fi

echo "Installing/upgrading the normal Omnigent CLI..."
curl -fsSL https://omnigent.ai/install.sh | sh

install_desktop_app() {
  local arch download_url tmp_dir dmg mount_dir app_source target_app
  arch="$(uname -m)"
  case "$arch" in
    arm64) download_url="https://omnigent.ai/download/mac" ;;
    x86_64) download_url="https://omnigent.ai/download/mac-x64" ;;
    *)
      echo "ERROR: unsupported macOS architecture: ${arch}" >&2
      return 1
      ;;
  esac

  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/omnigent-desktop.XXXXXX")"
  dmg="${tmp_dir}/omnigent.dmg"
  mount_dir="${tmp_dir}/mount"
  mkdir -p "$mount_dir"
  trap "hdiutil detach '$mount_dir' -quiet >/dev/null 2>&1 || true; rm -rf '$tmp_dir'" EXIT

  echo "Downloading the Omnigent Desktop app..."
  curl -fL --retry 3 --retry-delay 1 "$download_url" -o "$dmg"
  echo "Mounting Desktop app image..."
  hdiutil attach "$dmg" -nobrowse -readonly -mountpoint "$mount_dir" -quiet

  app_source="$(find "$mount_dir" -maxdepth 2 -type d -name 'Omnigent*.app' -print -quit)"
  if [[ -z "$app_source" ]]; then
    app_source="$(find "$mount_dir" -maxdepth 2 -type d -name '*.app' -print -quit)"
  fi
  if [[ -z "$app_source" ]]; then
    echo "ERROR: no .app bundle found in the Omnigent DMG." >&2
    return 1
  fi

  target_app="/Applications/$(basename "$app_source")"
  osascript -e 'tell application "Omnigent" to quit' >/dev/null 2>&1 || true
  sleep 1

  echo "Installing Desktop app to ${target_app}..."
  if [[ -e "$target_app" ]]; then
    if [[ -w "$(dirname "$target_app")" ]]; then
      rm -rf "$target_app"
    else
      sudo rm -rf "$target_app"
    fi
  fi
  if [[ -w "/Applications" ]]; then
    ditto "$app_source" "$target_app"
  else
    sudo ditto "$app_source" "$target_app"
  fi

  hdiutil detach "$mount_dir" -quiet
  rm -rf "$tmp_dir"
  trap - EXIT
}

install_desktop_app

echo "Installing Omni Route..."
"${HERE}/install.sh"

echo
echo "============================================================"
echo "FULL OMNIGENT + OMNI ROUTE INSTALL COMPLETE"
echo "============================================================"
echo "Normal CLI:       omni"
echo "Omni Route:       omni-rotate codex"
echo "Diagnostics:      omni-rotate test"
echo "Status dashboard: omni-rotate status"
echo "Accounts:         omni-rotate accounts"
echo "Desktop app:      /Applications/Omnigent.app (or matching Omnigent app bundle)"
