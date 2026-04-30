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
#   4. Reminds you to run `pmo-agent login` and `pmo-agent install` next.
#
# It does NOT auto-run login or install — those open a browser and
# need TTY interaction, which is awkward inside `curl | bash`.

set -euo pipefail

REPO="billc8128/pmo_agent"
WEB_URL="https://pmo-agent-sigma.vercel.app"

# ─── style helpers ──────────────────────────────────────────────────────
bold()  { printf '\033[1m%s\033[0m' "$*"; }
dim()   { printf '\033[2m%s\033[0m' "$*"; }
green() { printf '\033[32m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }
say()   { printf '%s\n' "$*"; }
err()   { printf '%s %s\n' "$(red ERROR)" "$*" >&2; }

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
if [ -w "/usr/local/bin" ]; then
  install_dir="/usr/local/bin"
  use_sudo=""
elif command -v sudo >/dev/null 2>&1 && [ -d "/usr/local/bin" ]; then
  install_dir="/usr/local/bin"
  use_sudo="sudo"
  say "  $(dim "/usr/local/bin needs sudo; you may be prompted.")"
else
  install_dir="$HOME/.local/bin"
  use_sudo=""
  mkdir -p "$install_dir"
  case ":$PATH:" in
    *":$install_dir:"*) ;;
    *)
      say ""
      say "$(bold note) $install_dir is not on your PATH."
      say "  Add this to your shell rc to fix:"
      say "    $(bold "export PATH=\"$install_dir:\$PATH\"")"
      ;;
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
say "$(bold "Next:")"
say "  1. $(bold "pmo-agent login")     — opens a browser to authorize this machine"
say "  2. $(bold "pmo-agent install")   — runs in the background, restarts on boot"
say ""
say "Then visit $WEB_URL/u/<your-handle> to see your timeline."
