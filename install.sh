#!/usr/bin/env bash
# Install ccs (Claude Code Sessions) with one command:
#   curl -fsSL https://raw.githubusercontent.com/linnykoleh/claude-code-sessions/main/install.sh | bash
# From a clone:  ./install.sh
# Uninstall:     ./install.sh --uninstall   (or: curl ... | bash -s -- --uninstall)
#
# What it does:
#   1. Installs uv and fzf if they are missing (with mise, else Homebrew).
#   2. Installs the ccs command with `uv tool install` into its own isolated environment.
# Running it again upgrades ccs to the latest version.
set -euo pipefail

REPO="${CCS_REPO:-git+https://github.com/linnykoleh/claude-code-sessions}"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
has()  { command -v "$1" >/dev/null 2>&1; }

# tools installed by mise are reachable through its shims
if has mise; then export PATH="$HOME/.local/share/mise/shims:$PATH"; fi

PACKAGE="claude-code-sessions-cli"
OLD_PACKAGE="cc-sessions"   # the old package name; its ccs command would clash with the new one

remove_tool() {   # remove_tool <package>: uninstall it if uv has it
  if has uv && uv tool list 2>/dev/null | grep -q "^$1 "; then
    uv tool uninstall "$1" >/dev/null 2>&1 && say "Removed $1"
  fi
}

if [ "${1:-}" = "--uninstall" ]; then
  remove_tool "$PACKAGE"
  remove_tool "$OLD_PACKAGE"
  rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/cc-sessions" && say "Removed the cache"
  say "uv and fzf were left installed."
  exit 0
fi

ensure() {   # ensure <command> <mise tool> <brew formula> <manual install hint>
  local cmd="$1" mise_tool="$2" brew_formula="$3" hint="$4"
  if has "$cmd"; then say "$cmd: found"; return; fi
  if has mise && mise use -g "$mise_tool" >/dev/null 2>&1 && has "$cmd"; then say "$cmd: installed with mise"; return; fi
  if has brew && brew install "$brew_formula" >/dev/null && has "$cmd"; then say "$cmd: installed with Homebrew"; return; fi
  die "Could not install $cmd. Install it manually ($hint) and run this script again."
}

ensure uv  uv@latest  uv  "https://docs.astral.sh/uv/"
ensure fzf fzf@latest fzf "https://github.com/junegunn/fzf"

# 2. the ccs command: from this checkout if we are inside one, otherwise from GitHub
src="$REPO"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)"
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$script_dir/pyproject.toml" ]; then src="$script_dir"; fi
remove_tool "$OLD_PACKAGE"
say "Installing ccs from $src"
uv tool install --force --reinstall --quiet "$src"

bin_dir="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) warn "$bin_dir is not on your PATH. Run: uv tool update-shell   (then open a new terminal)" ;;
esac
say "Done. Run: ccs"
