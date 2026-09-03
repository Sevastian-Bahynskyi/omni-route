#!/usr/bin/env bash

OMNI_ROUTE_PATH_BEGIN="# >>> Omni Route PATH >>>"
OMNI_ROUTE_PATH_END="# <<< Omni Route PATH <<<"

omni_route_remove_path_block_from_file() {
  local profile="$1" tmp
  [[ -f "$profile" ]] || return 0
  tmp="$(mktemp "${TMPDIR:-/tmp}/omni-route-profile.XXXXXX")"
  awk -v begin="$OMNI_ROUTE_PATH_BEGIN" -v end="$OMNI_ROUTE_PATH_END" '
    $0 == begin { skip=1; next }
    skip && $0 == end { skip=0; next }
    !skip { print }
  ' "$profile" > "$tmp"
  cat "$tmp" > "$profile"
  rm -f "$tmp"
}

omni_route_path_profiles() {
  case "$(basename "${SHELL:-/bin/zsh}")" in
    zsh)
      printf '%s\n' "$HOME/.zprofile" "$HOME/.zshrc"
      ;;
    bash)
      printf '%s\n' "$HOME/.bash_profile" "$HOME/.bashrc"
      ;;
    *)
      printf '%s\n' "$HOME/.profile"
      ;;
  esac
}

omni_route_add_path() {
  local profile
  while IFS= read -r profile; do
    [[ -n "$profile" ]] || continue
    mkdir -p "$(dirname "$profile")"
    touch "$profile"
    omni_route_remove_path_block_from_file "$profile"
    cat >> "$profile" <<'BLOCK'
# >>> Omni Route PATH >>>
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
# <<< Omni Route PATH <<<
BLOCK
  done < <(omni_route_path_profiles)
}

omni_route_remove_path() {
  local profile
  for profile in \
    "$HOME/.zprofile" "$HOME/.zshrc" \
    "$HOME/.bash_profile" "$HOME/.bashrc" "$HOME/.profile"; do
    omni_route_remove_path_block_from_file "$profile"
  done
}

omni_route_install_brew_shim() {
  local target brew_bin shim linked
  target="$HOME/.local/bin/omni-rotate"
  command -v brew >/dev/null 2>&1 || return 0
  brew_bin="$(brew --prefix)/bin"
  shim="$brew_bin/omni-rotate"
  if [[ -e "$shim" || -L "$shim" ]]; then
    if [[ -L "$shim" ]]; then
      linked="$(readlink "$shim" 2>/dev/null || true)"
      [[ "$linked" == "$target" ]] && return 0
    fi
    echo "WARNING: $shim already exists and is not managed by Omni Route; leaving it untouched." >&2
    return 0
  fi
  if ! ln -s "$target" "$shim"; then
    echo "WARNING: could not create $shim; open a new shell to use the PATH entry." >&2
  fi
}

omni_route_remove_brew_shim() {
  local target brew_bin shim linked
  target="$HOME/.local/bin/omni-rotate"
  command -v brew >/dev/null 2>&1 || return 0
  brew_bin="$(brew --prefix)/bin"
  shim="$brew_bin/omni-rotate"
  [[ -L "$shim" ]] || return 0
  linked="$(readlink "$shim" 2>/dev/null || true)"
  if [[ "$linked" == "$target" ]]; then
    rm -f "$shim"
  fi
}
