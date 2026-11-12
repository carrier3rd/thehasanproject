"""Load transcripts, muted spans and chat into archive.db (hash-based incremental)."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import time
from collections import defaultdict

from common import Anchors, P, connect_archive, get_logger, load_config, log_failure, make_dirs

log = get_logger("build_db")


def media_flags(segments: list[dict], cfg: dict) -> list[int]:
    unknown, total = defaultdict(float), defaultdict(float)
    for s in segments:
        t = s["start"]
        while t < s["end"]:
            m = int(t // 60)
            nxt = min(s["end"], (m + 1) * 60.0)
            total[m] += nxt - t
            if s["speaker"].startswith("Unknown_"):
                unknown[m] += nxt - t
            t = nxt
    hot = {m for m in total if total[m] > 0 and unknown[m] / total[m] >= cfg["media_minute_share"]}
    flags = []
    for s in segments:
        long_unknown = s["speaker"].startswith("Unknown_") and s["end"] - s["start"] >= cfg["media_segment_min_seconds"]
        flags.append(int(long_unknown or int((s["start"] + s["end"]) / 2 // 60) in hot))
    return flags


def merge_turns(segments: list[dict], flags: list[int], gap: float, max_len: float = float('inf')) -> list[dict]:
    turns = []
    for s, f in zip(segments, flags):
        t = turns[-1] if turns else None
        if t and t["speaker"] == s["speaker"] and t["is_media"] == f and s["start"] - t["end"] < gap \
                and s["end"] - t["start"] <= max_len:
            t["end"] = max(t["end"], s["end"])
            t["text"] += " " + s["text"]
            if s.get("score") is not None:
                t["score"] = s["score"] if t["score"] is None else min(t["score"], s["score"])
        else:
            turns.append({"start": s["start"], "end": s["end"], "speaker": s["speaker"],
                          "speaker_raw": s.get("speaker_raw"), "score": s.get("score"),
                          "is_media": f, "text": s["text"]})
    return turns


def load_turns(con, cfg: dict, vid: str, doc: dict) -> int:
    con.execute("INSERT INTO turns_fts(turns_fts, rowid, text, speaker) "
                "SELECT 'delete', id, text, speaker FROM turns WHERE video_id=?", (vid,))
    con.execute("DELETE FROM turns WHERE video_id=?", (vid,))
    segs = doc["segments"]
    turns = merge_turns(segs, media_flags(segs, cfg), cfg["turn_merge_gap_seconds"], cfg.get("turn_max_seconds", float("inf")))
    for t in turns:
        cur = con.execute("INSERT INTO turns(video_id, start, end, speaker, speaker_raw, speaker_score, is_media, text) "
                          "VALUES(?,?,?,?,?,?,?,?)", (vid, t["start"], t["end"], t["speaker"], t["speaker_raw"],
                                                      t["score"], t["is_media"], t["text"]))
        con.execute("INSERT INTO turns_fts(rowid, text, speaker) VALUES(?,?,?)", (cur.lastrowid, t["text"], t["speaker"]))
    con.execute("DELETE FROM muted WHERE video_id=?", (vid,))
    mp = P(cfg, "muted") / f"{vid}.json"
    if mp.exists():
        con.executemany("INSERT INTO muted VALUES(?,?,?)", [(vid, a, b) for a, b in json.loads(mp.read_text())])
    return len(turns)


def chat_plan(con, cfg: dict, vid: str, duration: float):
    """Decide the chat source and a signature of its inputs. Live wins over replay."""
    row = con.execute("SELECT stream_start_unix FROM videos WHERE id=?", (vid,)).fetchone()
    start = row["stream_start_unix"] if row else None
    if start:
        anchors = Anchors.load(con, vid)
        lo = start + min(anchors.to_chat(0.0), 0.0) - 5
        hi = start + max(anchors.to_chat(duration), duration) + 5
        n, d = con.execute("SELECT COUNT(*), COALESCE(SUM(deleted),0) FROM live_msgs WHERE channel=? AND ts_ms BETWEEN ? AND ?",
                           (cfg["channel_name"], lo * 1000, hi * 1000)).fetchone()
        if n:
            return "live", f"live:{start}:{int(lo)}:{int(hi)}:{n}:{d}", (start, lo, hi)
    for rp in (P(cfg, "chat") / f"{vid}.jsonl", P(cfg, "chat") / f"{vid}.jsonl.gz"):  # .gz: older streams' public logs
        if rp.exists():
            st = rp.stat()
            return "replay", f"replay:{st.st_size}:{st.st_mtime_ns}", rp
    return "none", "none", None


def load_chat(con, cfg: dict, vid: str, source: str, arg) -> int:
    con.execute("INSERT INTO chat_fts(chat_fts, rowid, text, author) "
                "SELECT 'delete', id, text, author FROM chat WHERE video_id=?", (vid,))
    con.execute("DELETE FROM chat WHERE video_id=?", (vid,))
    if source == "live":
        start, lo, hi = arg
        rows = [(vid, r["ts_ms"] / 1000 - start, r["user_id"], r["author"], r["text"], r["deleted"], r["kind"],
                 r["badges"], r["first_msg"], r["returning_chatter"], r["subscriber"], "live")
                for r in con.execute("SELECT * FROM live_msgs WHERE channel=? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
                                     (cfg["channel_name"], lo * 1000, hi * 1000))]
    elif source == "replay":
        rows = []
        opener = gzip.open if str(arg).endswith(".gz") else open
        with opener(arg, "rt", encoding="utf-8") as f:
            for line in f:
                m = json.loads(line)
                badges = m.get("badges") or ""
                sub = m.get("subscriber")
                rows.append((vid, m["t"], m.get("user_id"), m.get("author"), m.get("text"), int(m.get("deleted") or 0),
                             m.get("kind") or "msg", badges, m.get("first_msg"), m.get("returning_chatter"),
                             int("subscriber/" in badges) if sub is None else int(sub), "replay"))
    else:
        rows = []
    con.executemany("INSERT INTO chat(video_id, t, user_id, author, text, deleted, kind, badges, first_msg, "
                    "returning_chatter, subscriber, source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("INSERT INTO chat_fts(rowid, text, author) SELECT id, text, author FROM chat WHERE video_id=?", (vid,))
    return len(rows)


def build_one(con, cfg: dict, jp) -> tuple[bool, bool]:
    vid = jp.stem
    raw = jp.read_bytes()
    digest = hashlib.sha1(raw).hexdigest()
    audio = P(cfg, "audio")
    ip, mp = audio / f"{vid}.info.json", audio / f"{vid}.meta.json"
    info = json.loads(ip.read_text()) if ip.exists() else {}
    meta = json.loads(mp.read_text()) if mp.exists() else {}
    con.execute("INSERT OR IGNORE INTO videos(id) VALUES(?)", (vid,))
    row = con.execute("SELECT transcript_hash, chat_sig FROM videos WHERE id=?", (vid,)).fetchone()
    did_turns = did_chat = False
    doc = None
    if row["transcript_hash"] != digest:
        doc = json.loads(raw)
        n = load_turns(con, cfg, vid, doc)
        did_turns = True
        log.info("%s: %d turns", vid, n)
    duration = info.get("duration")
    if not duration:
        doc = doc or json.loads(raw)
        duration = doc["segments"][-1]["end"] if doc["segments"] else 0
    source, sig, arg = chat_plan(con, cfg, vid, float(duration))
    if row["chat_sig"] != sig:
        n = load_chat(con, cfg, vid, source, arg)
        did_chat = True
        log.info("%s: %d chat rows (%s)", vid, n, source)
    con.execute(
        "UPDATE videos SET platform=?, source_url=?, title=?, upload_date=?, twitch_vod_id=COALESCE(?, twitch_vod_id), "
        "duration=?, embed_type=?, embed_id=?, chat_source=?, transcript_hash=?, chat_sig=?, "
        "processed_at=CASE WHEN ? THEN ? ELSE processed_at END WHERE id=?",
        (meta.get("platform"), meta.get("source_url") or info.get("webpage_url"), info.get("title") or vid,
         info.get("upload_date"), meta.get("twitch_vod_id"), int(duration), meta.get("embed_type", "none"),
         meta.get("embed_id"), source, digest, sig, did_turns or did_chat, time.strftime("%Y-%m-%dT%H:%M:%S"), vid))
    return did_turns, did_chat


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild", action="store_true", help="drop derived rows (turns, chat, muted) and reload everything")
    ap.add_argument("--only")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    con = connect_archive(cfg)
    if a.rebuild:
        con.executescript("DELETE FROM turns; DELETE FROM chat; DELETE FROM muted; "
                          "UPDATE videos SET transcript_hash=NULL, chat_sig=NULL;")
        con.commit()
    changed = []
    for jp in sorted(P(cfg, "transcripts").glob("*.json")):
        if a.only and jp.stem != a.only:
            continue
        try:
            t, c = build_one(con, cfg, jp)
            con.commit()   # one short transaction per video so the logger is never blocked for long
            if t or c:
                changed.append(jp.stem)
        except Exception as e:
            con.rollback()
            log.exception("failed %s", jp.stem)
            log_failure(cfg, jp.stem, "build_db", e)
    if a.rebuild:
        con.execute("INSERT INTO turns_fts(turns_fts) VALUES('rebuild')")
        con.execute("INSERT INTO chat_fts(chat_fts) VALUES('rebuild')")
        con.commit()
    v, t, c = (con.execute(f"SELECT COUNT(*) FROM {x}").fetchone()[0] for x in ("videos", "turns", "chat"))
    print(f"reloaded {len(changed)} VOD(s): {' '.join(changed) or '-'}")
    print(f"archive.db: {v} videos, {t} turns, {c} chat rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
