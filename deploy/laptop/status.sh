#!/usr/bin/env bash
# One-shot health report for the laptop. Run from the repo as your normal user:
#   deploy/laptop/status.sh
# Prints no secrets and no visitor IP addresses, so the output is safe to paste into a Claude session.
# Read-only: it changes nothing. A few checks are better with sudo; it says so when it lacks access.
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO"
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
ok()   { printf '[ ok ] %s\n' "$*"; }
bad()  { printf '[FAIL] %s\n' "$*"; }
info() { printf '[info] %s\n' "$*"; }
hdr()  { printf '\n== %s ==\n' "$*"; }
# root-only commands: try without sudo, then passwordless sudo
priv() { "$@" 2>/dev/null || sudo -n "$@" 2>/dev/null; }
# journal lines can quote .env lines (systemd prints rejected assignments in full): hide every value
redact() { sed -E 's/(assignment .)([A-Za-z_ ]*[A-Za-z_]+=)[^'"'"']*/\1\2<hidden>/; s/hf_[A-Za-z0-9]{8,}/hf_<hidden>/g'; }

hdr "machine"
info "$(date -Is)  up $(uptime -p 2>/dev/null | sed 's/^up //')"
info "disk free: $(df -h --output=avail "$REPO" | tail -1 | tr -d ' ')"
info "memory available: $(free -h | awk '/^Mem:/ {print $7}')"
for t in sleep.target suspend.target hibernate.target; do
  [ "$(systemctl is-enabled "$t" 2>/dev/null)" = masked ] && ok "$t masked" || bad "$t not masked (laptop may sleep)"
done

hdr "code"
for d in "$REPO" "$REPO/../thehasanprojectprivate"; do
  [ -d "$d/.git" ] || { bad "no git repo at $d"; continue; }
  info "$(basename "$(cd "$d" && pwd)"): $(git -C "$d" rev-parse --abbrev-ref HEAD) $(git -C "$d" log -1 --format='%h %cr: %s')"
  dirty=$(git -C "$d" status --porcelain)
  [ -z "$dirty" ] || { info "local changes (a pull fails if GitHub changes the same files):"; sed 's/^/       /' <<<"$dirty"; }
done

hdr "services"
for u in vod-chat-logger vod-web vod-admin vod-nightly.timer vod-update.timer; do
  s=$(systemctl is-active "$u" 2>/dev/null)
  [ "$s" = active ] && ok "$u active" || bad "$u ${s:-unknown}"
done
last=$(systemctl show vod-update.service -p ExecMainExitTimestamp --value 2>/dev/null)
info "last auto-update run: ${last:-never}"
last=$(systemctl show vod-nightly.service -p ExecMainExitTimestamp --value 2>/dev/null)
info "last nightly run: ${last:-never}"
log=$(ls -1t logs/nightly-*.log 2>/dev/null | head -1)
if [ -n "$log" ]; then
  n=$(grep -c '^!!!' "$log")
  [ "$n" -eq 0 ] && ok "$log: no failed steps" || { bad "$log: $n failed step(s):"; grep '^!!!' "$log" | sed 's/^/       /'; }
fi

hdr "chat logger"
hb=logs/chat_logger.heartbeat
if [ -f "$hb" ]; then
  age=$(( $(date +%s) - $(cat "$hb") ))
  [ "$age" -lt 600 ] && ok "heartbeat $((age / 60)) min ago" || bad "heartbeat $((age / 60)) min ago (stale)"
else
  bad "no heartbeat file ($hb): the logger has never run"
fi
"$PY" - <<'PYCODE' 2>&1 | sed 's/^/[info] /'
import sqlite3, sys, time
sys.path.insert(0, "pipeline")
from common import P, load_config
db = P(load_config(), "archive_db")
if not db.exists():
    print(f"no archive db at {db}"); raise SystemExit
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
now = time.time() * 1000
for label, since in (("last hour", now - 3.6e6), ("last 24 h", now - 8.64e7), ("all time", 0)):
    n, chans = con.execute("SELECT COUNT(*), COUNT(DISTINCT channel) FROM live_msgs WHERE ts_ms >= ?", (since,)).fetchone()
    print(f"live chat messages {label}: {n} ({chans} channel(s))")
row = con.execute("SELECT MAX(ts_ms) FROM live_msgs").fetchone()
if row[0]:
    print(f"newest message: {time.strftime('%Y-%m-%d %H:%M', time.localtime(row[0] / 1000))}")
for s in con.execute("SELECT channel, start_ms, end_ms FROM sessions ORDER BY start_ms DESC LIMIT 3"):
    print(f"session {s[0]} {time.strftime('%Y-%m-%d %H:%M', time.localtime(s[1] / 1000))} {(s[2] - s[1]) / 3.6e6:.1f} h")
PYCODE
journalctl -u vod-chat-logger -n 5 --no-pager -o cat 2>/dev/null | redact | sed 's/^/       /'

hdr "tools"
tdl=/opt/TwitchDownloaderCLI/TwitchDownloaderCLI
if [ -x "$tdl" ]; then ok "TwitchDownloaderCLI: $("$tdl" --version 2>&1 | head -1)"; else bad "TwitchDownloaderCLI missing at $tdl (chat replay falls back to chat-downloader)"; fi
for c in ffmpeg ffprobe; do command -v "$c" >/dev/null && ok "$c" || bad "$c missing"; done
[ -x .venv/bin/yt-dlp ] && ok "yt-dlp $(.venv/bin/yt-dlp --version)" || bad "yt-dlp missing from .venv"
grep -qE '^(export +)?HF_TOKEN=.' .env 2>/dev/null && ok "HF_TOKEN set in .env" || info "HF_TOKEN not set in .env yet (needed before the GPU run)"
grep -qE '^(export +)?GPU_HOST=.' .env 2>/dev/null && info "GPU_HOST set in .env: nightly will transcribe" || info "GPU_HOST not set: nightly skips transcription"
grep -qE '^export ' .env 2>/dev/null && bad ".env has 'export NAME=...' lines: systemd ignores them; write NAME=... without 'export'"

hdr "data"
"$PY" - <<'PYCODE' 2>&1 | sed 's/^/[info] /'
import sys
sys.path.insert(0, "pipeline")
from common import P, load_config
cfg = load_config()
def files(key, pattern):
    d = P(cfg, key)
    return sorted(d.glob(pattern)) if d.exists() else []
audio = files("audio", f"*.{cfg['audio_format']}")
print(f"audio files: {len(audio)} ({sum(p.stat().st_size for p in audio) / 2**30:.1f} GB)"
      + (": " + " ".join(p.stem for p in audio[:10]) if audio else ""))
print(f"muted-span files: {len(files('muted', '*.json'))}   chat replays: {len(files('chat', '*'))}")
print(f"transcripts: {len(files('transcripts', '**/*.json'))}")
try:
    from pending import pending
    print(f"waiting for the GPU: {len(pending(cfg))}")
except Exception as e:
    print(f"pending check failed: {e}")
voices = P(cfg, "voices")
if voices.exists():
    for d in sorted(p for p in voices.iterdir() if p.is_dir()):
        print(f"voice clips {d.name}: {len(list(d.glob('*.wav')))}")
for key in ("public_db", "archive_db"):
    p = P(cfg, key)
    print(f"{key}: " + (f"{p.stat().st_size / 2**20:.1f} MB" if p.exists() else "missing"))
PYCODE
f=logs/failed.tsv
[ -f "$f" ] && info "failed.tsv: $(($(wc -l < "$f") - 1)) row(s); last: $(tail -1 "$f" | cut -c1-160)"

hdr "web"
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$@"; }
c=$(http_code http://127.0.0.1:8000/)
[ "$c" = 401 ] && ok "site on :8000 asks for the password (401)" || bad "site on :8000 returned $c (expected 401)"
c=$(http_code http://127.0.0.1:8001/api/config)
[ "$c" = 200 ] && ok "admin app on :8001 answers" || bad "admin app on :8001 returned $c"
ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':(8000|8001)$' | grep -vqE '^(127\.0\.0\.1|\[::1\]):' \
  && bad "a site port listens beyond localhost" || ok "ports 8000/8001 listen on localhost only"

hdr "tailscale"
if command -v tailscale >/dev/null; then
  info "$(tailscale status --self --peers=false 2>&1 | head -1)"
  fs=$(priv tailscale funnel status)
  if [ -n "$fs" ]; then
    echo "$fs" | sed 's/^/       /'
    # the admin app must sit under a "(tailnet only)" address, never a "(Funnel on)" one
    echo "$fs" | awk '/^https?:/ {h=$0} /127\.0\.0\.1:8001/ && h ~ /Funnel on/ {f=1} END {exit !f}' \
      && bad "the admin app (8001) is on Funnel: it must be tailnet-only" \
      || { echo "$fs" | grep -q '127.0.0.1:8001' && ok "admin app is served tailnet-only" || info "admin app not served on the tailnet (tailscale serve --https=8443 not run)"; }
  else
    info "could not read funnel/serve status (try: sudo deploy/laptop/status.sh)"
  fi
else
  bad "tailscale not installed"
fi
# Does Funnel pass the visitor's address on? uvicorn (--proxy-headers) logs the forwarded address when it does,
# 127.0.0.1 when it does not. Only counts are printed, never the addresses.
acc=$(journalctl -u vod-web --since -7days --no-pager -o cat 2>/dev/null | grep -E '" [0-9]{3}' | awk '{print $2}' | sed 's/:[0-9]*$//')
if [ -n "$acc" ]; then
  local_n=$(grep -cE '^(127\.0\.0\.1|::1|\[::1\])$' <<<"$acc")
  tail_n=$(grep -cE '^100\.' <<<"$acc")
  other=$(grep -vE '^(127\.0\.0\.1|::1|\[::1\]|100\..*)$' <<<"$acc" | sort -u | wc -l)
  info "site requests, last 7 days: $(wc -l <<<"$acc") total; from 127.0.0.1: $local_n; from tailnet (100.x): $tail_n; distinct outside addresses: $other"
  if [ "$other" -gt 0 ]; then ok "Funnel passes visitor addresses (X-Forwarded-For): per-visitor rate limit works"
  else info "no outside addresses seen yet; open the site from a phone on mobile data (not Wi-Fi), then run this again"; fi
else
  info "no site access log lines readable (add yourself to group 'adm' or run with sudo)"
fi
