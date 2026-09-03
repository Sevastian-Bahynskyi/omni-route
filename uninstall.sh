#!/usr/bin/env bash
set -euo pipefail
rm -f \
  "${HOME}/.local/bin/omni-rotate" \
  "${HOME}/.local/bin/omni-rotate-accounts" \
  "${HOME}/.local/bin/omni-rotate-status"
rm -rf "${HOME}/.local/share/omnigent-subscription-rotation"
echo "Patched Omnigent / Omni Route removed."
echo "Normal Omnigent and subscription login profiles under ~/.omnigent were left untouched."
