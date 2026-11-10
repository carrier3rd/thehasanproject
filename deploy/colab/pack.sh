#!/usr/bin/env bash
# Laptop side of a Google Colab GPU run (used instead of deploy/gpu/run_remote.sh, which needs SSH).
#
#   deploy/colab/pack.sh enroll                  # voices/ only, for building voiceprints
#   deploy/colab/pack.sh process [process args]  # audio with no transcript yet (+ muted spans, voiceprints, voices/)
#        e.g. deploy/colab/pack.sh process --model large-v3          (BATCH=N: at most N VODs)
#
# Writes colab/bundle.tar. Upload it to Google Drive as  My Drive/vod-archive/bundle.tar  (replace the old one),
# then run deploy/colab/transcribe.ipynb in Colab, then bring the results back with deploy/colab/unpack.sh.
# The bundle never contains .env: the notebook reads HF_TOKEN from Colab's Secrets.
set -euo pipefail
cd "$(dirname "$0")/../.."
PRIV=${VOD_ARCHIVE_PRIVATE_DIR:-../thehasanprojectprivate}
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
mode=${1:-process}; shift || true
mkdir -p colab
list=$(mktemp); trap 'rm -f "$list"' EXIT

case "$mode" in
  enroll)
    [ -d voices ] && [ -n "$(find voices -name '*.wav' | head -1)" ] || { echo "no clips in voices/ (cut them with enroll_voices.py --cut)"; exit 1; }
    find voices -type f -name '*.wav' | sort > "$list" ;;
  process)
    "$PY" pipeline/pending.py --batch "${BATCH:-0}" "$@" > "$list"
    n=$(grep -c '\.opus$' "$list" || true)
    [ "$n" -gt 0 ] || { echo "nothing to transcribe"; exit 0; }
    [ -d voiceprints ] && find voiceprints -name '*.npy' | sort >> "$list"
    # voice clips too, so one Colab session can enroll and then transcribe (TASK = "both")
    [ -d voices ] && find voices -type f -name '*.wav' | sort >> "$list"
    grep -qE '^(voiceprints|voices)/' "$list" || echo "warning: no voiceprints or voice clips yet; every speaker will be Unknown" >&2 ;;
  *) echo "usage: $0 enroll | process [process.py args]" >&2; exit 2 ;;
esac

rm -f colab/bundle.tar
tar -cf colab/bundle.tar -T "$list"
[ -f "$PRIV/config.yaml" ] && tar -rf colab/bundle.tar -C "$PRIV" --transform 's,^,private/,' config.yaml
echo "colab/bundle.tar: $(du -h colab/bundle.tar | cut -f1), $(grep -c '\.opus$' "$list" || true) audio file(s), mode $mode"
tar -tf colab/bundle.tar | sed 's/^/  /'
echo "Next: upload it to Google Drive as My Drive/vod-archive/bundle.tar, then run deploy/colab/transcribe.ipynb with TASK=\"$mode\" (or \"both\" if the bundle has voice clips)."
