#!/usr/bin/env bash
# Run transcription on a rented GPU machine from the laptop, over SSH.
#
#   export GPU_HOST=root@203.0.113.7  GPU_PORT=22      # from the provider's "connect via SSH" box
#   deploy/gpu/run_remote.sh setup                     # once per new machine: installs WhisperX + pyannote, checks the GPU
#   deploy/gpu/run_remote.sh enroll                    # sends voices/, builds voiceprints, brings them back
#   deploy/gpu/run_remote.sh process [process.py args] # sends audio that has no transcript, transcribes, brings results back
#   deploy/gpu/run_remote.sh pull                      # only bring results back (e.g. after a dropped connection)
#
# Options (environment): BATCH=N sends at most N pending VODs; GPU_DIR (default vod-archive) on the remote.
# Pilot:  deploy/gpu/run_remote.sh process --only <id> --model large-v3   (and again with --model large-v3-turbo)
# Safe to re-run: everything resumes. Run it inside tmux so a closed terminal doesn't stop it.
# Remember to STOP or DELETE the rented machine afterwards; most providers bill until you do.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${GPU_HOST:?set GPU_HOST=user@host (from your GPU provider)}"
RDIR=${GPU_DIR:-vod-archive}
BATCH=${BATCH:-0}
SSH=${GPU_SSH:-"ssh -p ${GPU_PORT:-22} -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=60"}
PRIV=${VOD_ARCHIVE_PRIVATE_DIR:-../thehasanprojectprivate}
cmd=${1:-process}; shift || true

rs() { rsync -az --partial -e "$SSH" "$@"; }
remote() { $SSH "$GPU_HOST" "cd $RDIR && set -a && { [ ! -f .env ] || . ./.env; } && set +a && { [ ! -f .venv/bin/activate ] || . .venv/bin/activate; } && VOD_ARCHIVE_PRIVATE_DIR=private $*"; }

push_code() {
  $SSH "$GPU_HOST" "mkdir -p $RDIR/private $RDIR/logs && touch $RDIR/.env && chmod 600 $RDIR/.env"
  git ls-files | rs --files-from=- ./ "$GPU_HOST:$RDIR/"
  [ -f "$PRIV/config.yaml" ] && rs "$PRIV/config.yaml" "$GPU_HOST:$RDIR/private/config.yaml"
  # only HF_TOKEN goes to the rented machine
  if [ -f .env ] && grep -q '^HF_TOKEN=' .env; then
    grep '^HF_TOKEN=' .env | $SSH "$GPU_HOST" "cat > $RDIR/.env"
  elif [ -n "${HF_TOKEN:-}" ]; then
    printf 'HF_TOKEN=%s\n' "$HF_TOKEN" | $SSH "$GPU_HOST" "cat > $RDIR/.env"
  else
    echo "warning: no HF_TOKEN in .env or the environment; diarization will fail" >&2
  fi
}

merge_log() {  # append the remote log's new lines to the local one
  local name=$1 tmp
  tmp=$(mktemp)
  if rs "$GPU_HOST:$RDIR/logs/$name" "$tmp" 2>/dev/null; then
    touch "logs/$name"
    cat "logs/$name" "$tmp" | awk '!seen[$0]++' > "logs/$name.new" && mv "logs/$name.new" "logs/$name"
  fi
  rm -f "$tmp"
}

pull() {
  mkdir -p data/transcripts logs
  # --ignore-existing: never overwrite local transcripts (they may have been re-labelled with --rename-only)
  rs --ignore-existing "$GPU_HOST:$RDIR/data/transcripts/" data/transcripts/
  merge_log process_times.tsv
  merge_log failed.tsv
  echo "transcripts: $(find data/transcripts -name '*.json' | wc -l) on this machine"
}

case "$cmd" in
  setup)
    push_code
    remote bash deploy/gpu/setup.sh ;;
  enroll)
    push_code
    rs --delete voices/ "$GPU_HOST:$RDIR/voices/"
    remote python pipeline/enroll_voices.py "$@" || echo "!!! enrollment self-test failed; see above" >&2
    mkdir -p voiceprints
    rs "$GPU_HOST:$RDIR/voiceprints/" voiceprints/ ;;
  process)
    push_code
    list=$(mktemp)
    python3 pipeline/pending.py --batch "$BATCH" "$@" > "$list"
    n=$(grep -c '\.opus$\|\.m4a$\|\.webm$\|\.mp3$\|\.wav$' "$list" || true)
    if [ "$n" -eq 0 ]; then echo "nothing to transcribe"; rm -f "$list"; exit 0; fi
    echo "sending $n VOD(s)"
    rs --files-from="$list" ./ "$GPU_HOST:$RDIR/"
    rm -f "$list"
    [ -d voiceprints ] && rs voiceprints/ "$GPU_HOST:$RDIR/voiceprints/"
    status=0
    remote python pipeline/process.py "$@" || status=$?
    pull
    exit $status ;;
  pull)
    pull ;;
  *)
    echo "usage: $0 setup|enroll|process|pull [args]" >&2; exit 2 ;;
esac
