#!/usr/bin/env bash
# User-level timers (no sudo needed; lingering is on). Added 2026-11-20: the hourly check, so a finished stream is
# downloaded within about an hour of the 2-hour cool-off and published as soon as its transcript arrives, instead of
# waiting for the 06:00 system run (vod-nightly.timer, which stays as the daily full run).
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.config/systemd/user
cp "$here"/user/vod-hourly.service "$here"/user/vod-hourly.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vod-hourly.timer
systemctl --user list-timers vod-hourly.timer --no-pager
