#!/usr/bin/env bash
# Nightly update. On the laptop it runs from vod-nightly.timer at 06:00 (deploy/laptop/install.sh sets that up).
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
# While logs/hub-elsewhere exists (2026-11-26: the laptop's RAM corrupts archive.db), another machine builds and
# publishes (win/hub.py on the PC pulls these downloads and pushes public.db back), so this machine stops here.
if [ -f logs/hub-elsewhere ]; then
  echo "=== downloads done; build/publish/backup run elsewhere (logs/hub-elsewhere); end $(date -Is) ==="
  exit 0
fi
# Hourly runs (QUICK=1, from the vod-hourly user timer, 2026-11-20): new streams are fetched every hour, but the
# rebuild and publish below only run when a download or a transcript arrived since the last publish.
sig=$(ls -l --time-style=+%s data/audio data/transcripts 2>/dev/null | md5sum | cut -c1-32)
if [ "${QUICK:-0}" = 1 ] && [ -f logs/published.sig ] && [ "$sig" = "$(cat logs/published.sig)" ]; then
  python pipeline/reel.py auto || echo "!!! step failed: reel.py auto"   # collects finished Claude batches too
  python pipeline/backup.py run || echo "!!! step failed: backup.py run"  # daily; the chat logger keeps writing
  echo "=== quick run: nothing new since the last publish; end $(date -Is) ==="
  exit 0
fi
step pipeline/align_chat.py
step pipeline/build_db.py

# publish and embed gate the public site: stop if the publish (opt-out test) fails
echo "--- publish"
if python pipeline/publish.py; then
  step pipeline/embed.py
  echo "$sig" > logs/published.sig
  step pipeline/reel.py auto     # weekly reel: no-op with the subscription backend (reel-weekly.timer builds it)
  step pipeline/backup.py run    # encrypted backup, at most once a day (backup.every_hours)
  if [ -n "${WEB_HOST:-}" ]; then
    rsync -a --delay-updates public.db index "${WEB_HOST}:${WEB_DIR:-/opt/vod-archive}/" \
      && ssh "$WEB_HOST" sudo systemctl restart web.service
  fi
else
  echo "!!! publish failed; public.db left unchanged"
fi
echo "=== nightly end $(date -Is) ==="
