#!/usr/bin/env bash
# One-time setup of the always-on laptop. Run from the repo, as your normal user, with sudo:
#   sudo deploy/laptop/install.sh
# Installs systemd services (chat logger, public site, admin app, nightly pipeline, auto-update from GitHub).
# Safe to run again; it rewrites the units and restarts them.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo $0" >&2; exit 1; }
REPO=$(cd "$(dirname "$0")/../.." && pwd)
USER_NAME=${SUDO_USER:-$(stat -c %U "$REPO")}
[ "$USER_NAME" != root ] || { echo "run as your normal user with sudo, not as root" >&2; exit 1; }
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || { echo "no virtualenv at $REPO/.venv; follow the laptop setup in README.md first" >&2; exit 1; }
as_user() { runuser -u "$USER_NAME" -- "$@"; }

# Private folders the public site must never read. They must exist before the site starts (fail closed).
as_user mkdir -p "$REPO/archive" "$REPO/data" "$REPO/voices" "$REPO/voiceprints" "$REPO/logs" "$REPO/index"
as_user touch "$REPO/.env"
chmod 700 "$REPO/archive"
chmod 600 "$REPO/.env"
as_user "$PY" "$REPO/pipeline/common.py" --make-dirs --init-db >/dev/null

# Site password (kept out of git, readable only by you)
if [ ! -s "$REPO/site.env" ] || ! grep -q '^SITE_PASSWORD=.' "$REPO/site.env"; then
  echo "Choose the password visitors will need for the site (user name: viewer)."
  read -r -s -p "Site password: " pw; echo
  [ -n "$pw" ] || { echo "empty password" >&2; exit 1; }
  printf 'SITE_USER=viewer\nSITE_PASSWORD=%s\n' "$pw" > "$REPO/site.env"
  chown "$USER_NAME": "$REPO/site.env"
fi
chmod 600 "$REPO/site.env"

unit() { cat > "/etc/systemd/system/$1"; }

unit vod-chat-logger.service <<UNIT
[Unit]
Description=VOD archive live chat logger
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$REPO
EnvironmentFile=-$REPO/.env
ExecStart=$PY pipeline/chat_logger.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

unit vod-web.service <<UNIT
[Unit]
Description=VOD archive public site (public.db only)
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$REPO
EnvironmentFile=$REPO/site.env
ExecStart=$PY -m uvicorn web.app:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=5
# The public app cannot see the private archive, raw data, voices or pipeline secrets.
InaccessiblePaths=$REPO/archive $REPO/data $REPO/voices $REPO/voiceprints $REPO/.env
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

unit vod-admin.service <<UNIT
[Unit]
Description=VOD archive private admin app (archive.db, localhost only)
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$REPO
ExecStart=$PY -m uvicorn admin.app:app --host 127.0.0.1 --port 8001
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

unit vod-nightly.service <<UNIT
[Unit]
Description=VOD archive nightly pipeline

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$REPO
ExecStart=$REPO/deploy/nightly.sh
UNIT

unit vod-nightly.timer <<UNIT
[Unit]
Description=Run the VOD archive pipeline every morning

[Timer]
OnCalendar=*-*-* 06:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

unit vod-update.service <<UNIT
[Unit]
Description=Pull code and settings from GitHub and restart what changed
After=network-online.target

[Service]
Type=oneshot
Environment=REPO=$REPO USER_NAME=$USER_NAME
ExecStart=$REPO/deploy/laptop/update.sh
UNIT

unit vod-update.timer <<UNIT
[Unit]
Description=Check GitHub for updates every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now vod-chat-logger.service vod-web.service vod-admin.service vod-nightly.timer vod-update.timer
systemctl restart vod-chat-logger.service vod-web.service vod-admin.service
sleep 2
systemctl --no-pager --lines=0 status vod-chat-logger vod-web vod-admin | grep -E '^\S|Active:'

cat <<MSG

Services are running. The site is on http://127.0.0.1:8000 (user: viewer).
To put it on the internet with a free https address (no domain or router changes):

  curl -fsSL https://tailscale.com/install.sh | sh
  sudo tailscale up                                      # log in in the browser
  sudo tailscale funnel --bg 8000                        # public site; prints its https://<name>.ts.net address
  sudo tailscale serve --bg --https=8443 http://127.0.0.1:8001   # admin app, only on your own Tailscale devices

Check later with:  systemctl status vod-web   ·   journalctl -u vod-chat-logger -f
MSG
