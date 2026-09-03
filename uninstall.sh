#!/usr/bin/env bash
set -euo pipefail
rm -f "${HOME}/.local/bin/omni-rotate" "${HOME}/.local/bin/omni-rotate-accounts"
rm -rf "${HOME}/.local/share/omnigent-subscription-rotation"
echo "Patched Omnigent removed."
echo "Normal Omnigent and the two Codex login profiles were left untouched."
