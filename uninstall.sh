#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "${HERE}/path_helpers.sh"

omni_route_remove_brew_shim || true
omni_route_remove_path || true

rm -f \
  "${HOME}/.local/bin/omni-rotate" \
  "${HOME}/.local/bin/omni-rotate-accounts" \
  "${HOME}/.local/bin/omni-rotate-test" \
  "${HOME}/.local/bin/omni-rotate-status"
rm -rf "${HOME}/.local/share/omnigent-subscription-rotation"
echo "Patched Omnigent / Omni Route removed."
echo "Omni Route PATH entries/shims were removed."
echo "Normal Omnigent and subscription login profiles under ~/.omnigent were left untouched."
