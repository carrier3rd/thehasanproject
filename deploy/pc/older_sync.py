"""Overnight job: line up public Twitch chat logs with YouTube re-uploads of older streams (no GPU).

For each video on @HasanAbiVODs3, newest first: fetch YouTube's auto-captions and the chat logs for the stream's
UTC day and the next (kept gzipped: they are the archive's chat source later), then find when the video's second 0
happened. Each rare caption word repeated in chat votes for (chat time - caption time); the peak, minus chat's
reaction lag, is the start. Validated 2026-11-11 on 9 streams with Twitch ground truth: lag 10-14 s (median ~12),
one trimmed upload detected by the per-hour fits. Resumable: finished videos are skipped. Polite: one log request
every LOG_GAP seconds, backs off on errors.
"""
import collections
import datetime as dt
import gzip
import json
import os
import re
import subprocess
import time
import urllib.request

BASE = os.path.expanduser("~/vod-archive/older")
YTDLP = os.path.expanduser("~/thehasanproject/.venv/bin/yt-dlp")
CHANNEL = "https://www.youtube.com/@HasanAbiVODs3/videos"
LOGS = ["https://logs.zonian.dev", "https://logs.ivr.fi"]
LAG = 12.0
LOG_GAP = 20
CAP_GAP = 5
RESULTS = f"{BASE}/sync.jsonl"
os.makedirs(f"{BASE}/chat", exist_ok=True)
os.makedirs(f"{BASE}/captions", exist_ok=True)


def tok(s):
    return re.findall(r"[a-z']{4,}", s.lower())


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


def video_list():
    path = f"{BASE}/videos.txt"
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 20 * 3600:
        out = subprocess.run([YTDLP, "--flat-playlist", "--print", "%(id)s|%(duration)s|%(title)s", CHANNEL],
                             capture_output=True, text=True, timeout=900).stdout
        if out.count("\n") > 100:
            with open(path, "w") as f:
                f.write(out)
    vids = []
    for line in open(path):
        p = line.rstrip("\n").split("|", 2)
        m = re.search(r"HasanAbi (\w+ \d+, \d{4})", p[2]) if len(p) == 3 else None
        if not m:
            continue
        try:
            day = dt.datetime.strptime(m.group(1), "%B %d, %Y").date()
            dur = float(p[1]) if p[1] not in ("", "NA", "None") else 0.0
        except ValueError:
            continue
        vids.append((p[0], dur, p[2], day))
    return vids


def chat_day(day):
    path = f"{BASE}/chat/{day}.txt.gz"
    if not os.path.exists(path):
        for base in LOGS:
            for attempt in range(3):
                try:
                    time.sleep(LOG_GAP)
                    req = urllib.request.Request(f"{base}/channel/hasanabi/{day.year}/{day.month}/{day.day}?raw",
                                                 headers={"User-Agent": "vod-archive-research/1.0"})
                    with urllib.request.urlopen(req, timeout=600) as r:
                        data = r.read()
                    if data.count(b"PRIVMSG") < 100:
                        raise ValueError("too few lines")
                    with gzip.open(path + ".part", "wb") as f:
                        f.write(data)
                    os.replace(path + ".part", path)
                    break
                except Exception as e:
                    log(f"log fetch {base} {day} failed ({e}); attempt {attempt + 1}")
                    time.sleep(120 * (attempt + 1))
            if os.path.exists(path):
                break
    if not os.path.exists(path):
        return None
    out = []
    for line in gzip.open(path, "rt", encoding="utf-8", errors="replace"):
        if "PRIVMSG" not in line:
            continue
        m = re.search(r"tmi-sent-ts=(\d+)", line)
        if m:
            out.append((int(m.group(1)) / 1000, line.split(" :", 2)[-1]))
    return out


def captions(ytid):
    path = f"{BASE}/captions/{ytid}.json3.gz"
    if not os.path.exists(path):
        time.sleep(CAP_GAP)
        tmp = f"{BASE}/captions/{ytid}"
        subprocess.run([YTDLP, "-q", "--no-warnings", "--skip-download", "--write-auto-subs", "--sub-langs", "en",
                        "--sub-format", "json3", "-o", tmp, f"https://www.youtube.com/watch?v={ytid}"],
                       capture_output=True, timeout=300)
        if not os.path.exists(tmp + ".en.json3"):
            return None
        with open(tmp + ".en.json3", "rb") as f, gzip.open(path, "wb") as g:
            g.write(f.read())
        os.remove(tmp + ".en.json3")
    words = []
    for ev in json.load(gzip.open(path, "rt")).get("events", []):
        for s in ev.get("segs") or []:
            for w in tok(s.get("utf8", "")):
                words.append(((ev.get("tStartMs", 0) + s.get("tOffsetMs", 0)) / 1000, w))
    return words


def fit(words, chat, lo, hi, binsz):
    fs = collections.Counter(w for _, w in words)
    fd = collections.Counter(w for _, w in chat)
    common = {w for w, _ in fd.most_common(1500)}
    idx = collections.defaultdict(list)
    for t, w in chat:
        if w not in common:
            idx[w].append(t)
    v = collections.Counter()
    for t, w in words:
        if w in common or fs[w] > 15 or not (2 <= len(idx.get(w, [])) <= 400):
            continue
        for c in idx[w]:
            if lo <= c - t <= hi:
                v[round((c - t) / binsz)] += 1
    if not v:
        return None, 0
    (b, n), = v.most_common(1)
    med = sorted(v.values())[len(v) // 2]
    pts = [(k, v[k]) for k in (b - 1, b, b + 1)]
    return sum(k * c for k, c in pts) / sum(c for _, c in pts) * binsz, n / max(med, 1)


def sync_one(ytid, dur, title, day):
    r = {"yt": ytid, "date": str(day), "title": title, "duration": dur}
    words = captions(ytid)
    if not words:
        r["status"] = "no_captions"
        return r
    d0, d1 = chat_day(day), chat_day(day + dt.timedelta(days=1))
    if d0 is None and d1 is None:
        r["status"] = "no_chat_log"
        return r
    chat = [(t, w) for t, txt in (d0 or []) + (d1 or []) for w in set(tok(txt))]
    lo = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp()
    est, strength = fit(words, chat, lo - 86400, lo + 2 * 86400, 2)
    if est is None:
        r["status"] = "no_fit"
        return r
    hours = []
    for h in range(int(max(t for t, _ in words) // 3600) + 1):
        e, s = fit([x for x in words if h * 3600 <= x[0] < (h + 1) * 3600], chat, est - 4 * 3600, est + 4 * 3600, 1)
        hours.append(None if e is None else [round(e - est, 1), round(s)])
    jumps = [i for i, x in enumerate(hours) if x and x[1] >= 8 and abs(x[0]) > 60]
    r.update(status="ok" if strength >= 20 else "weak", strength=round(strength), offset_raw=round(est, 1),
             start_utc=dt.datetime.fromtimestamp(est - LAG, dt.timezone.utc).isoformat(timespec="seconds"),
             start_unix=round(est - LAG, 1), hours=hours, trim_suspect_hours=jumps,
             chat_msgs=len(d0 or []) + len(d1 or []))
    return r


def main():
    done = set()
    if os.path.exists(RESULTS):
        done = {json.loads(line)["yt"] for line in open(RESULTS) if line.strip()}
    vids = video_list()
    log(f"{len(vids)} videos listed, {len(done)} already synced")
    for ytid, dur, title, day in vids:
        if ytid in done:
            continue
        try:
            r = sync_one(ytid, dur, title, day)
        except Exception as e:          # not recorded, so the next run retries it
            log(f"{ytid} {day} error: {e}")
            continue
        if r["status"] == "no_chat_log":  # likely a log site outage; retry next run
            log(f"{ytid} {day} no chat log yet")
            continue
        if r["status"] == "no_captions" and (dt.date.today() - day).days < 14:
            # YouTube can add auto-captions days after upload: drop the cached miss and retry on a later run
            log(f"{ytid} {day} no captions yet (recent upload); will retry")
            continue
        with open(RESULTS, "a") as f:
            f.write(json.dumps(r) + "\n")
        log(f"{ytid} {day} {r['status']} strength={r.get('strength')} start={r.get('start_utc')} "
            f"trims={r.get('trim_suspect_hours')}")
    log("all videos done")


if __name__ == "__main__":
    main()
