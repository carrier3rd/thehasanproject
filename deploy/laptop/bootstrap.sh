#!/usr/bin/env bash
# Laptop setup for Ubuntu (tested target: Ubuntu 26.04). Run as your normal user from the repo:
#   deploy/laptop/bootstrap.sh
# Installs system packages, the Python environment (CPU-only PyTorch), TwitchDownloaderCLI,
# and turns off sleep so the chat logger keeps running with the lid closed. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO=$(pwd)
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
say() { printf '\n== %s\n' "$*"; }

say "System packages"
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq python3-venv python3-dev ffmpeg git rsync openssh-client tmux unzip curl ca-certificates >/dev/null

say "Python environment (.venv)"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -q -r requirements.txt

say "TwitchDownloaderCLI"
TDL_DIR=/opt/TwitchDownloaderCLI
if [ -x "$TDL_DIR/TwitchDownloaderCLI" ]; then
  echo "already installed"
else
  url=$(curl -fsSL https://api.github.com/repos/lay295/TwitchDownloader/releases/latest \
        | .venv/bin/python -c "import json,re,sys; a=[x['browser_download_url'] for x in json.load(sys.stdin)['assets'] if re.search(r'CLI.*Linux-x64\.zip$', x['name'])]; print(a[0] if a else '')" 2>/dev/null || true)
  if [ -n "$url" ]; then
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/tdl.zip" "$url"
    $SUDO mkdir -p "$TDL_DIR"
    $SUDO unzip -o -q "$tmp/tdl.zip" -d "$TDL_DIR"
    $SUDO chmod +x "$TDL_DIR/TwitchDownloaderCLI"
    rm -rf "$tmp"
    echo "installed $(basename "$url")"
  else
    echo "could not find the Linux release; download TwitchDownloaderCLI-*-Linux-x64.zip from"
    echo "https://github.com/lay295/TwitchDownloader/releases and unzip it into $TDL_DIR"
  fi
fi

say "Keep the laptop awake (lid closed or idle)"
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
  $SUDO mkdir -p /etc/systemd/logind.conf.d
  printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\nIdleAction=ignore\n' \
    | $SUDO tee /etc/systemd/logind.conf.d/vod-archive-awake.conf >/dev/null
  $SUDO systemctl mask -q sleep.target suspend.target hibernate.target hybrid-sleep.target
  echo "sleep disabled (takes full effect after the next reboot; undo: sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target)"
else
  echo "no systemd here; skipped"
fi

say "Settings"
PRIV=$(.venv/bin/python -c "import sys; sys.path.insert(0, 'pipeline'); from common import load_config; print(load_config().get('_private', ''))")
if [ -n "$PRIV" ]; then echo "private settings: $PRIV"
else echo "WARNING: private settings repo not found next to this one. Clone it: gh repo clone carrier3rd/thehasanprojectprivate ../thehasanprojectprivate"; fi
touch .env && chmod 600 .env

say "Check"
.venv/bin/python check_env.py --role laptop || true
echo
echo "Next: sudo deploy/laptop/install.sh"
