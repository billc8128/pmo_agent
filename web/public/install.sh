#!/usr/bin/env bash
# pmo-agent installer.
#
# Usage:
#   curl -fsSL https://pmo-agent-sigma.vercel.app/install.sh | bash
#
# What it does:
#   1. Detects your OS + CPU architecture
#   2. Downloads the matching binary from the latest GitHub release
#   3. Installs it to /usr/local/bin/pmo-agent (or ~/.local/bin if no sudo)
#   4. If the install dir isn't already on PATH, prints a single
#      copy-paste line that fixes the user's current shell session
#      AND a one-shot line that makes the change permanent.
#
# We never modify the user's shell rc on their behalf — too many subtle
# ways that goes wrong (multiple shells, multiple rc files, exec
# wrappers). The instructions are explicit and copy-pastable.

set -euo pipefail

REPO="billc8128/pmo_agent"
WEB_URL="https://pmo-agent-sigma.vercel.app"

# ─── style helpers ──────────────────────────────────────────────────────
bold()    { printf '\033[1m%s\033[0m' "$*"; }
dim()     { printf '\033[2m%s\033[0m' "$*"; }
green()   { printf '\033[32m%s\033[0m' "$*"; }
yellow()  { printf '\033[33m%s\033[0m' "$*"; }
red()     { printf '\033[31m%s\033[0m' "$*"; }
say()     { printf '%s\n' "$*"; }
err()     { printf '%s %s\n' "$(red ERROR)" "$*" >&2; }

# ─── platform detection ─────────────────────────────────────────────────
case "$(uname -s)" in
  Darwin) os="darwin" ;;
  Linux)  os="linux"  ;;
  *)
    err "unsupported OS: $(uname -s). Build from source: https://github.com/$REPO"
    exit 1
    ;;
esac

case "$(uname -m)" in
  arm64|aarch64) arch="arm64" ;;
  x86_64|amd64)  arch="amd64" ;;
  *)
    err "unsupported architecture: $(uname -m). Build from source: https://github.com/$REPO"
    exit 1
    ;;
esac

asset="pmo-agent-${os}-${arch}"
say "$(bold "pmo-agent") installer  $(dim "($os/$arch)")"
say ""

# ─── pick install dir ───────────────────────────────────────────────────
#
# Priority:
#   1. /usr/local/bin if writable (no sudo needed)
#   2. /usr/local/bin via sudo  if sudo is available AND we're on a TTY
#      (sudo can prompt for a password; that's only useful interactively)
#   3. ~/.local/bin              fallback that doesn't need any
#                                privilege, but may not be on PATH
#
# Whether the chosen dir is on PATH is tracked separately so we can
# show the right post-install instructions.
need_path_hint=0
if [ -w "/usr/local/bin" ]; then
  install_dir="/usr/local/bin"
  use_sudo=""
elif command -v sudo >/dev/null 2>&1 && [ -d "/usr/local/bin" ] && [ -t 0 ]; then
  install_dir="/usr/local/bin"
  use_sudo="sudo"
  say "  $(dim "/usr/local/bin needs sudo; you may be prompted.")"
else
  install_dir="$HOME/.local/bin"
  use_sudo=""
  mkdir -p "$install_dir"
  case ":$PATH:" in
    *":$install_dir:"*) ;;
    *) need_path_hint=1 ;;
  esac
fi

bin_path="$install_dir/pmo-agent"

# ─── resolve latest version ─────────────────────────────────────────────
say "  Looking up latest release…"
# Use the redirect from /releases/latest rather than the API to avoid
# unauthenticated rate limits on the API.
version=$(curl -fsSL -o /dev/null -w '%{url_effective}' \
  "https://github.com/$REPO/releases/latest" \
  | sed 's#.*/tag/##')
if [ -z "$version" ]; then
  err "couldn't determine latest version. Check https://github.com/$REPO/releases."
  exit 1
fi
say "  Latest is $(bold "$version")."

download_url="https://github.com/$REPO/releases/download/$version/$asset"

# ─── download to temp, move into place ──────────────────────────────────
tmp=$(mktemp -t pmo-agent.XXXXXX)
trap 'rm -f "$tmp"' EXIT

say "  Downloading ${asset}…"
if ! curl -fsSL -o "$tmp" "$download_url"; then
  err "download failed: $download_url"
  exit 1
fi
chmod +x "$tmp"

say "  Installing to $(bold "$bin_path")…"
$use_sudo mv "$tmp" "$bin_path"
trap - EXIT

# ─── post-install ───────────────────────────────────────────────────────
installed_version=$("$bin_path" version 2>&1 | awk '{print $NF}')
say ""
say "$(green "✓") Installed pmo-agent $installed_version at $bin_path"
say ""

# Decide which shell rc file to suggest. The user might be using bash
# or zsh; on macOS zsh is the default since Catalina. We pick by
# inspecting $SHELL, which is set even when this script runs under
# `curl | bash`.
case "${SHELL:-}" in
  */zsh)  rc_file="$HOME/.zshrc" ;;
  */bash) rc_file="$HOME/.bash_profile" ;;
  *)      rc_file="$HOME/.profile" ;;
esac

if [ "$need_path_hint" = "1" ]; then
  # The PATH problem is almost certainly going to bite the user, so
  # make this section IMPOSSIBLE to miss. We re-print at the end after
  # the "next steps" because it's a prerequisite to those steps.
  say "$(yellow "⚠")  $bin_path is not on your PATH yet."
  say "    Run this in your terminal $(bold "right now") to fix it for this session:"
  say ""
  say "      $(bold "export PATH=\"$install_dir:\$PATH\"")"
  say ""
  say "    And one-shot to make it permanent for new terminals:"
  say ""
  say "      $(bold "echo 'export PATH=\"$install_dir:\$PATH\"' >> $rc_file")"
  say ""
fi

say "$(bold "Next:")"
say "  1. $(bold "pmo-agent login")     — opens a browser to authorize this machine"
say "  2. $(bold "pmo-agent install")   — runs in the background, restarts on boot"
say ""
say "Then visit $WEB_URL/u/<your-handle> to see your timeline."

# Final reminder if PATH still needs fixing — easy to miss the warning
# above when the "Next:" block is the last thing on screen otherwise.
if [ "$need_path_hint" = "1" ]; then
  say ""
  say "$(yellow "⚠")  $(bold "BEFORE running the commands above"), fix your PATH:"
  say "      $(bold "export PATH=\"$install_dir:\$PATH\"")"
fi
