#!/usr/bin/env bash
# Runs ON the rented GPU machine (run_remote.sh setup calls it). Safe to run more than once.
set -euo pipefail
cd "$(dirname "$0")/../.."
if ! command -v ffmpeg >/dev/null || ! command -v rsync >/dev/null; then
  SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO apt-get update -qq && $SUDO apt-get install -y -qq ffmpeg rsync
fi
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements-gpu.txt
set -a; [ -f .env ] && . ./.env; set +a
python check_env.py --role gpu
