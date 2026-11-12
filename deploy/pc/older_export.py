"""Turn older_sync.py results into archive inputs for older streams (YouTube re-uploads, no GPU, no audio).

For each synced video it writes, under older/export/:
  audio/<yt>.meta.json   platform/embed + stream_start_unix + chat_anchors (piecewise offsets; re-uploads have cuts)
  audio/<yt>.info.json   title, stream date, duration
  chat/<yt>.jsonl.gz     public-log chat on the stream clock (t = seconds since stream_start_unix), with
                         mod-deleted and timed-out/banned users' messages marked deleted like the live logger does
  transcripts/<yt>.json  interim transcript from YouTube auto-captions, speaker "Auto-captions" (replaced when the
                         audio is transcribed; build_db reloads on the hash change)
Then `rsync older/export/{audio,chat,transcripts}/ laptop:~/thehasanproject/data/...` and the nightly run (align_chat,
build_db, publish) puts them on the site.

Anchors: 10-minute windows of captions are fitted against chat (same vote as older_sync.fit). Runs of windows that agree
(within JUMP s) become one anchor; a run must have 2+ windows, so a single stray window cannot invent a cut. A cut is
placed midway between the last window of one run and the first of the next (so within ~5 minutes).

Skips videos that duplicate a stream the archive already has from Twitch (--have: JSON list of [start_unix, duration]).
"""
import argparse
import datetime as dt
import gzip
import json
import math
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import older_sync as osy  # noqa: E402  (fit, captions cache, chat cache, LAG)

OUT = f"{osy.BASE}/export"
WIN = 600
JUMP = 20.0
MIN_STRENGTH = 5.0
CAPTION_SPEAKER = "Auto-captions"


def windows(words, chat, est, dur):
    out = []
    for a in range(0, int(dur) + 1, WIN):
        w = [x for x in words if a <= x[0] < a + WIN]
        if len(w) < 50:
            continue
        e, s = osy.fit(w, chat, est - 4 * 3600, est + 4 * 3600, 1)
        if e is not None and s >= MIN_STRENGTH:
            out.append((a + WIN / 2, e - est, s))
    return out


def runs_of(ws):
    """[(center, delta, strength)] -> [[...], ...] consecutive windows agreeing within JUMP; strays dropped."""
    runs = []
    for w in ws:
        if runs and abs(w[1] - statistics.median(x[1] for x in runs[-1])) <= JUMP:
            runs[-1].append(w)
        else:
            runs.append([w])
    good = [r for r in runs if len(r) >= 2]
    merged = []
    for r in good:   # dropping a stray can leave two neighbours with the same offset: join them
        if merged and abs(statistics.median(x[1] for x in r) - statistics.median(x[1] for x in merged[-1])) <= JUMP:
            merged[-1].extend(r)
        else:
            merged.append(r)
    return merged


def anchors_for(words, chat, est, dur):
    """-> (anchors [(transcript_t, delta, confidence)], n_windows)"""
    ws = windows(words, chat, est, dur)
    runs = runs_of(ws)
    if not runs:
        return [(0.0, 0.0, 0.3)], len(ws)
    out = []
    for i, r in enumerate(runs):
        delta = statistics.median(x[1] for x in r)
        conf = 0.9 if len(r) >= 3 else 0.6
        t = 0.0 if i == 0 else round((runs[i - 1][-1][0] + r[0][0]) / 2, 1)
        out.append((t, round(delta, 2), conf))
    return out, len(ws)


def parse_tags(s):
    return dict(kv.split("=", 1) if "=" in kv else (kv, "") for kv in s.split(";"))


def chat_rows(day, lo, hi, start):
    """Public log lines between unix lo..hi -> stream-clock rows, deletions applied."""
    rows, by_id, by_user = [], {}, {}
    for d in (day, day + dt.timedelta(days=1)):
        path = f"{osy.BASE}/chat/{d}.txt.gz"
        if not os.path.exists(path):
            continue
        for line in gzip.open(path, "rt", encoding="utf-8", errors="replace"):
            if not line.startswith("@"):
                continue
            tags_s, _, rest = line.rstrip("\r\n").partition(" ")
            tags = parse_tags(tags_s[1:])
            try:
                ts = int(tags.get("tmi-sent-ts", "")) / 1000
            except ValueError:
                continue
            if not lo <= ts <= hi:
                continue
            m = re.match(r":(\S+) (PRIVMSG|CLEARMSG|CLEARCHAT) #\S+(?: :(.*))?$", rest)
            if not m:
                continue
            prefix, cmd, arg = m.group(1), m.group(2), m.group(3) or ""
            if cmd != "PRIVMSG":
                arg = arg.lstrip(":")   # the logs write some targets as "::name"
            if cmd == "PRIVMSG":
                text = arg
                if text.startswith("\x01ACTION ") and text.endswith("\x01"):
                    text = "/me " + text[8:-1]
                badges = tags.get("badges", "")
                r = {"t": round(ts - start, 3), "user_id": tags.get("user-id"), "author": prefix.split("!")[0],
                     "text": text, "badges": badges, "subscriber": int(tags.get("subscriber") == "1"), "kind": "msg"}
                if tags.get("first-msg") in ("0", "1"):
                    r["first_msg"] = int(tags["first-msg"])
                if tags.get("returning-chatter") in ("0", "1"):
                    r["returning_chatter"] = int(tags["returning-chatter"])
                rows.append(r)
                if tags.get("id"):
                    by_id[tags["id"]] = r
                by_user.setdefault(r["user_id"], []).append(r)
            elif cmd == "CLEARMSG":
                r = by_id.get(tags.get("target-msg-id"))
                if r:
                    r["deleted"] = 1
            elif cmd == "CLEARCHAT" and arg and tags.get("target-user-id"):
                dur = tags.get("ban-duration")
                rows.append({"t": round(ts - start, 3), "user_id": tags["target-user-id"], "author": arg,
                             "text": f"timeout {dur}s" if dur else "ban", "kind": "mod_action"})
                for r in by_user.get(tags["target-user-id"], []):   # Twitch hides their earlier messages
                    r["deleted"] = 1
    rows.sort(key=lambda r: r["t"])
    return rows


def caption_segments(ytid):
    """json3 auto-captions -> segments split at pauses (> 1.2 s) or every ~12 s."""
    doc = json.load(gzip.open(f"{osy.BASE}/captions/{ytid}.json3.gz", "rt"))
    words = []
    for ev in doc.get("events", []):
        t0 = ev.get("tStartMs", 0)
        for s in ev.get("segs") or []:
            w = (s.get("utf8") or "").strip()
            if w and w != "\n":
                words.append(((t0 + s.get("tOffsetMs", 0)) / 1000, w))
    words.sort(key=lambda x: x[0])
    segs, cur = [], []
    for t, w in words:
        if cur and (t - cur[-1][0] > 1.2 or t - cur[0][0] > 12):
            segs.append(cur)
            cur = []
        cur.append((t, w))
    if cur:
        segs.append(cur)
    out = []
    for i, s in enumerate(segs):
        end = min(s[-1][0] + 0.6, segs[i + 1][0][0]) if i + 1 < len(segs) else s[-1][0] + 0.6
        out.append({"start": round(s[0][0], 2), "end": round(max(end, s[0][0] + 0.1), 2), "speaker": CAPTION_SPEAKER,
                    "speaker_raw": "captions", "score": None, "text": " ".join(w for _, w in s), "words": []})
    return out


def overlaps(start, dur, have):
    return any(start < s + d and s < start + dur for s, d in have)


def write_json(path, obj, gz=False):
    tmp = path + ".part"
    with (gzip.open(tmp, "wt", encoding="utf-8") if gz else open(tmp, "w", encoding="utf-8")) as f:
        if isinstance(obj, list):
            for r in obj:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        else:
            json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def export_one(r, have, force=False):
    yt = r["yt"]
    meta_path = f"{OUT}/audio/{yt}.meta.json"
    if os.path.exists(meta_path) and not force:
        return "kept"
    dur = float(r["duration"] or 0)
    if not dur:
        return "no duration"
    start = r["start_unix"]
    if overlaps(start, dur, have):
        return "skip (archive has this stream from Twitch)"
    day = dt.date.fromisoformat(r["date"])
    words = osy.captions(yt)
    d0, d1 = osy.chat_day(day), osy.chat_day(day + dt.timedelta(days=1))
    chat = [(t, w) for t, txt in (d0 or []) + (d1 or []) for w in set(osy.tok(txt))]
    est = r["offset_raw"]
    anchors, nwin = anchors_for(words, chat, est, dur)
    s_int = math.floor(start)
    frac = start - s_int
    anchors = [(t, round(o + frac, 2), c) for t, o, c in anchors]
    offs = [o for _, o, _ in anchors]
    rows = chat_rows(day, s_int + min(offs) - 60, s_int + max(offs) + dur + 60, s_int)
    segs = caption_segments(yt)
    write_json(f"{OUT}/chat/{yt}.jsonl.gz", rows, gz=True)
    write_json(f"{OUT}/transcripts/{yt}.json",
               {"video_id": yt, "model": "youtube-auto-captions", "chunks": [], "speaker_map": {},
                "speaker_scores": {}, "unknown_clusters": {}, "segments": segs})
    write_json(f"{OUT}/audio/{yt}.info.json",
               {"title": r["title"], "upload_date": day.strftime("%Y%m%d"), "duration": int(dur),
                "webpage_url": f"https://www.youtube.com/watch?v={yt}"})
    write_json(meta_path,
               {"platform": "youtube", "source_url": f"https://www.youtube.com/watch?v={yt}", "embed_type": "youtube",
                "embed_id": yt, "stream_start_unix": s_int, "stream_start_source": "caption_sync",
                "chat_anchors": anchors, "chat_source_note": "public logs (logs.zonian.dev / logs.ivr.fi)",
                "sync": {"strength": r["strength"], "windows": nwin, "lag_assumed": osy.LAG}})
    return f"ok: {len(anchors)} anchor(s) from {nwin} windows, {len(rows)} chat rows, {len(segs)} caption segments"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--have", help="JSON [[start_unix, duration], ...] of streams already in the archive")
    ap.add_argument("--limit", type=int, default=0, help="export at most N new videos (newest first)")
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    for d in ("audio", "chat", "transcripts"):
        os.makedirs(f"{OUT}/{d}", exist_ok=True)
    have = json.load(open(a.have)) if a.have else []
    recs = [json.loads(line) for line in open(osy.RESULTS) if line.strip()]
    recs = [r for r in recs if r["status"] == "ok" and (not a.only or r["yt"] == a.only)]
    recs.sort(key=lambda r: r["date"], reverse=True)
    n = 0
    for r in recs:
        try:
            res = export_one(r, have, a.force)
        except Exception as e:
            res = f"error: {e!r}"
        if res != "kept":
            osy.log(f"{r['yt']} {r['date']} {res}")
        if res.startswith("ok"):
            n += 1
            if a.limit and n >= a.limit:
                break
    osy.log(f"exported {n} new video(s)")


if __name__ == "__main__":
    main()
