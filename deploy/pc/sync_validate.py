"""Validate chat->re-upload sync on Twitch VODs that also have a YouTube copy. Read-only on the archive."""
import json, re, sys, os, time, sqlite3, collections, statistics, subprocess, urllib.request, datetime as dt
HOME = os.path.expanduser("~/thehasanproject"); WORK = sys.argv[1]; MAXV = int(sys.argv[2]) if len(sys.argv) > 2 else 12
YT = os.path.expanduser("~/thehasanproject/.venv/bin/yt-dlp")
tok = lambda s: re.findall(r"[a-z']{4,}", s.lower())
db = sqlite3.connect(f"file:{HOME}/archive/archive.db?mode=ro", uri=True)
vids = db.execute("select v.id, v.stream_start_unix, v.duration from videos v where v.id in (select distinct video_id from turns) order by v.stream_start_unix desc").fetchall()
yt = {}
for l in open("/tmp/yt_videos.txt"):
    p = l.rstrip("\n").split("|", 2)
    m = re.search(r"HasanAbi (\w+ \d+, \d{4})", p[-1]) if len(p) == 3 else None
    if m: yt.setdefault(dt.datetime.strptime(m.group(1), "%B %d, %Y").date(), p[0])
def get(url, path):
    if not os.path.exists(path):
        time.sleep(4)
        with urllib.request.urlopen(url, timeout=300) as r, open(path, "wb") as f: f.write(r.read())
    return path
def justlog(day):
    p = get(f"https://logs.zonian.dev/channel/hasanabi/{day.year}/{day.month}/{day.day}?raw", f"{WORK}/log-{day}.txt")
    out = []
    for line in open(p, encoding="utf-8", errors="replace"):
        if "PRIVMSG" not in line: continue
        ts = re.search(r"tmi-sent-ts=(\d+)", line); uid = re.search(r"user-id=(\d+)", line)
        if ts and uid: out.append((int(ts.group(1)) / 1000, uid.group(1), line.split(" :", 2)[-1].rstrip("\r\n")))
    return out
def vote(src, dst, binsz, lo=-1e12, hi=1e12):
    """src: [(t, word)], dst: [(t, word)]; votes for dst_t - src_t of the same rare word."""
    fs = collections.Counter(w for _, w in src); fd = collections.Counter(w for _, w in dst)
    common = {w for w, _ in fd.most_common(1500)} | {w for w, _ in fs.most_common(300)}
    idx = collections.defaultdict(list)
    for t, w in dst:
        if w not in common: idx[w].append(t)
    v = collections.Counter()
    for t, w in src:
        if w in common or fs[w] > 15 or not (2 <= len(idx.get(w, [])) <= 400): continue
        for c in idx[w]:
            d = c - t
            if lo <= d <= hi: v[round(d / binsz)] += 1
    if not v: return None, 0
    (b, n), = v.most_common(1); med = sorted(v.values())[len(v) // 2]
    # refine: vote-weighted mean of the peak bin and neighbours
    pts = [(k, v[k]) for k in (b - 1, b, b + 1)]
    return sum(k * c for k, c in pts) / sum(c for _, c in pts) * binsz, n / max(med, 1)
rows = []
for vid, start, dur in vids[:MAXV]:
    day = dt.datetime.fromtimestamp(start, dt.timezone(dt.timedelta(hours=-5))).date()
    ytid = yt.get(day)
    replay = [json.loads(l) for l in open(f"{HOME}/data/chat/{vid}.jsonl")]
    utc0 = dt.datetime.fromtimestamp(start, dt.timezone.utc).date()
    log = justlog(utc0) + justlog(utc0 + dt.timedelta(days=1))
    # 1. truth: same (user, text) in replay and log
    first = {}
    for ts, uid, txt in log: first.setdefault((uid, txt), []).append(ts)
    diffs = [first[(m["user_id"], m["text"])][0] - m["t"] for m in replay if len(first.get((m["user_id"], m["text"]), [])) == 1]
    truth = statistics.median(diffs) if diffs else None
    spread = statistics.median(abs(d - truth) for d in diffs) if diffs else None
    # transcript words (turn-level; word times spread evenly in the turn)
    tw = []
    for s, e, text in db.execute("select start, end, text from turns where video_id=?", (vid,)):
        ws = tok(text)
        for i, w in enumerate(ws): tw.append((s + (e - s) * (i + 0.5) / max(len(ws), 1), w))
    chat = [(ts, w) for ts, _, txt in log for w in set(tok(txt))]
    estA, strA = vote(tw, chat, 2, truth - 3600, truth + 3600)
    r = dict(vid=vid, date=str(day), yt=ytid, matched=len(diffs), truth_vs_twitchinfo=round(truth - start, 1),
             spread=round(spread, 2), lagA=round(estA - truth, 1) if estA else None, strA=round(strA))
    if ytid:
        cp = f"{WORK}/{ytid}.en.json3"
        if not os.path.exists(cp):
            time.sleep(3)
            subprocess.run([YT, "-q", "--no-warnings", "--skip-download", "--write-auto-subs", "--sub-langs", "en", "--sub-format", "json3",
                            "-o", f"{WORK}/{ytid}", f"https://www.youtube.com/watch?v={ytid}"], capture_output=True)
        if os.path.exists(cp):
            cw = []
            for ev in json.load(open(cp)).get("events", []):
                for s in ev.get("segs") or []:
                    for w in tok(s.get("utf8", "")): cw.append(((ev.get("tStartMs", 0) + s.get("tOffsetMs", 0)) / 1000, w))
            shift, strS = vote(cw, tw, 1)              # twitch_t - yt_t for the same spoken word
            estB, strB = vote(cw, chat, 2, truth - 7200, truth + 7200)
            r.update(yt_vs_twitch_shift=round(shift, 1), strShift=round(strS), lagB=round(estB - (truth + shift), 1), strB=round(strB))
            # per-hour stability of the caption fit
            hrs = []
            for h in range(int(max(t for t, _ in cw) // 3600) + 1):
                e, _ = vote([x for x in cw if h * 3600 <= x[0] < (h + 1) * 3600], chat, 1, truth - 7200, truth + 7200)
                if e: hrs.append(round(e - (truth + shift)))
            r["lagB_by_hour"] = hrs
    rows.append(r); print(json.dumps(r), flush=True)
