#!/usr/bin/env bash
# Bring a Colab run's results back to the laptop.
#
#   deploy/colab/unpack.sh <download>...
#
# <download> is the Drive folder  My Drive/vod-archive/out  as downloaded: the folder itself, or the .zip file(s)
# Drive makes when you download it (big folders come as several zips; pass them all).
# Same rules as run_remote.sh pull: existing local transcripts are never overwritten (they may have been
# re-labelled with --rename-only); voiceprints from an enroll run replace the local ones; logs are merged.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: $0 <out folder or zip>..." >&2; exit 2; }
REPO=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
for src in "$@"; do
  if [ -d "$src" ]; then cp -r "$src"/. "$tmp/"
  else unzip -q -o "$src" -d "$tmp"; fi
done
out=$tmp; [ -d "$tmp/out" ] && out=$tmp/out
cd "$REPO"
mkdir -p data/transcripts logs
if [ -d "$out/transcripts" ]; then
  before=$(find data/transcripts -name '*.json' | wc -l)
  rsync -a --ignore-existing "$out/transcripts/" data/transcripts/
  echo "transcripts: $(( $(find data/transcripts -name '*.json' | wc -l) - before )) new, $(find data/transcripts -name '*.json' | wc -l) on this machine"
fi
if [ -n "$(find "$out/voiceprints" -name '*.npy' 2>/dev/null | head -1)" ]; then
  mkdir -p voiceprints
  cp "$out"/voiceprints/*.npy voiceprints/
  echo "voiceprints: $(cd voiceprints && ls *.npy | tr '\n' ' ')"
fi
for name in process_times.tsv failed.tsv; do
  [ -f "$out/logs/$name" ] || continue
  touch "logs/$name"
  cat "logs/$name" "$out/logs/$name" | awk '!seen[$0]++' > "logs/$name.new" && mv "logs/$name.new" "logs/$name"
  echo "merged logs/$name"
done
