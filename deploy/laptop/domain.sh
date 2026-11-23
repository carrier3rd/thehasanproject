#!/usr/bin/env bash
# Put the public site on your own domain with a Cloudflare Tunnel (free; no router changes; HTTPS by Cloudflare).
# Tailscale Funnel keeps working alongside it.
#
# Before running:
#   1. Buy a domain and put it on Cloudflare (buying it at Cloudflare Registrar does this; otherwise add the site to
#      a free Cloudflare account and change the domain's nameservers to the two Cloudflare gives you).
#   2. Run, as your normal user, from the repo:   deploy/laptop/domain.sh yourdomain.com
#      The first time it opens a browser link to authorise this laptop for that domain.
#
# What it does: installs cloudflared, creates a tunnel named vod-archive, points yourdomain.com (and www.) at it,
# sends traffic to the site on 127.0.0.1:8000 (never the admin app), and runs it as a system service.
# Safe to run again.
set -euo pipefail
DOMAIN=${1:?usage: deploy/laptop/domain.sh yourdomain.com}
[[ "$DOMAIN" =~ ^[a-z0-9-]+(\.[a-z0-9-]+)+$ ]] || { echo "that doesn't look like a domain: $DOMAIN" >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo "run as your normal user (it uses sudo where needed)" >&2; exit 1; }
cd "$(dirname "$0")/../.."
NAME=vod-archive
CF=$HOME/.cloudflared

if ! command -v cloudflared >/dev/null; then
  echo "== installing cloudflared"
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/cloudflared.deb" https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
  sudo dpkg -i "$tmp/cloudflared.deb"
  rm -rf "$tmp"
fi

if [ ! -f "$CF/cert.pem" ]; then
  echo "== authorise this laptop: open the link below, log in to Cloudflare, pick $DOMAIN"
  cloudflared tunnel login
fi

if ! cloudflared tunnel info "$NAME" >/dev/null 2>&1; then
  echo "== creating tunnel $NAME"
  cloudflared tunnel create "$NAME"
fi
ID=$(cloudflared tunnel info "$NAME" 2>/dev/null | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)
[ -n "$ID" ] && [ -f "$CF/$ID.json" ] || { echo "could not find the tunnel's credentials in $CF" >&2; exit 1; }

echo "== DNS: $DOMAIN and www.$DOMAIN -> tunnel"
cloudflared tunnel route dns --overwrite-dns "$NAME" "$DOMAIN"
cloudflared tunnel route dns --overwrite-dns "$NAME" "www.$DOMAIN"

echo "== service"
sudo mkdir -p /etc/cloudflared
sudo cp "$CF/$ID.json" "/etc/cloudflared/$ID.json"
sudo chmod 600 "/etc/cloudflared/$ID.json"
sudo tee /etc/cloudflared/config.yml >/dev/null <<EOF
tunnel: $ID
credentials-file: /etc/cloudflared/$ID.json
ingress:
  - hostname: $DOMAIN
    service: http://127.0.0.1:8000
  - hostname: www.$DOMAIN
    service: http://127.0.0.1:8000
  - service: http_status:404
EOF
if systemctl list-unit-files cloudflared.service >/dev/null 2>&1 && systemctl cat cloudflared.service >/dev/null 2>&1; then
  sudo systemctl restart cloudflared
else
  sudo cloudflared --config /etc/cloudflared/config.yml service install
fi
sleep 5
systemctl --no-pager --lines=0 status cloudflared | grep -E 'Active:' || true

cat <<MSG

Done. In a minute or two https://$DOMAIN should show the site (Cloudflare provides the certificate).
Then tell Claude the domain: it goes in the private repo's config.yaml as site_domain, so CLI links and share
previews use it. The Tailscale address keeps working too.
MSG
