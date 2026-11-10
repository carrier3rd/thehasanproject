#!/usr/bin/env bash
# Nightly update. Cron example (06:00 local, after the stream usually ends + skip_if_ended_within_hours):
#   0 6 * * *  /opt/vod-archive/deploy/nightly.sh
# Optional: WEB_HOST=user@server WEB_DIR=/opt/vod-archive to push public.db + index/ to a separate web box.
set -u
cd "$(dirname "$0")/.."
mkdir -p logs
exec 9> logs/nightly.lock
flock -n 9 || { echo "nightly already running"; exit 0; }
exec >> "logs/nightly-$(date +%F).log" 2>&1
[ -f .env ] && . ./.env            # HF_TOKEN etc.
[ -d .venv ] && . .venv/bin/activate
echo "=== nightly start $(date -Is) ==="

step() {  # keep going after a failed step: later steps work on whatever is already on disk
  echo "--- $* ($(date +%T))"
  python "$@" || echo "!!! step failed: $*"
}
step pipeline/inventory.py
step pipeline/download.py            # refuses (and logs) if more than 5 VODs are pending; run the backfill by hand with --yes
step pipeline/muted.py
step pipeline/chat_replay.py
# transcription needs an NVIDIA GPU: only runs when a rented GPU machine is up (GPU_HOST in .env)
if [ -n "${GPU_HOST:-}" ]; then
  echo "--- transcribe on $GPU_HOST"
  deploy/gpu/run_remote.sh process || echo "!!! remote transcription failed"
else
  echo "--- transcribe: skipped (no GPU_HOST); waiting: $(python pipeline/pending.py | grep -vc '/muted/' || true) VOD(s)"
fi
step pipeline/align_chat.py
step pipeline/build_db.py

# publish and embed gate the public site: stop if the publish (opt-out test) fails
echo "--- publish"
if python pipeline/publish.py; then
  step pipeline/embed.py
  if [ -n "${WEB_HOST:-}" ]; then
    rsync -a --delay-updates public.db index "${WEB_HOST}:${WEB_DIR:-/opt/vod-archive}/" \
      && ssh "$WEB_HOST" sudo systemctl restart web.service
  fi
else
  echo "!!! publish failed; public.db left unchanged"
fi
echo "=== nightly end $(date -Is) ==="
