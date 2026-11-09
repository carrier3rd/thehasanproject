"""archive.db -> public.db: allowlisted columns, public speaker labels, chat minus opt-outs, aggregates."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from collections import defaultdict

from common import SPEC_VERSION, Anchors, P, cfg_file, connect_archive, get_logger, load_config

log = get_logger("publish")

PUBLIC_SCHEMA = """
CREATE TABLE videos(
  id TEXT PRIMARY KEY, title TEXT, upload_date TEXT, duration INTEGER,
  embed_type TEXT, embed_id TEXT, source_url TEXT, has_chat_stats INTEGER);
CREATE TABLE turns(
  id INTEGER PRIMARY KEY, video_id TEXT, start REAL, end REAL,
  speaker TEXT, low_confidence INTEGER, is_media INTEGER, text TEXT);
CREATE INDEX idx_turns_vid ON turns(video_id, start);
CREATE VIRTUAL TABLE turns_fts USING fts5(text, speaker, content='turns', content_rowid='id');
CREATE TABLE muted(video_id TEXT, start REAL, end REAL);
CREATE TABLE chat_minutes(
  video_id TEXT, minute INTEGER,
  msgs INTEGER, distinct_chatters INTEGER,
  first_msg_share REAL, top_terms TEXT,
  PRIMARY KEY(video_id, minute));
CREATE TABLE speakers(name TEXT PRIMARY KEY);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE chat(
  id INTEGER PRIMARY KEY, video_id TEXT, t REAL,
  author TEXT, badges TEXT, first_msg INTEGER, subscriber INTEGER,
  text TEXT, deleted INTEGER, kind TEXT);
CREATE INDEX idx_chat_vid_t ON chat(video_id, t);
CREATE INDEX idx_chat_author ON chat(author COLLATE NOCASE);
CREATE VIRTUAL TABLE chat_fts USING fts5(text, author, content='chat', content_rowid='id');
"""
EXPECTED_COLUMNS = {
    "videos": ["id", "title", "upload_date", "duration", "embed_type", "embed_id", "source_url", "has_chat_stats"],
    "turns": ["id", "video_id", "start", "end", "speaker", "low_confidence", "is_media", "text"],
    "muted": ["video_id", "start", "end"],
    "chat_minutes": ["video_id", "minute", "msgs", "distinct_chatters", "first_msg_share", "top_terms"],
    "speakers": ["name"],
    "meta": ["key", "value"],
    "chat": ["id", "video_id", "t", "author", "badges", "first_msg", "subscriber", "text", "deleted", "kind"],
}
FORBIDDEN_COLUMNS = {"user_id", "msg_id", "room_id", "speaker_raw", "speaker_score"}

_URL = re.compile(r"https?://\S+|www\.\S+")
_MENTION = re.compile(r"@\w+")
_STOP = set("""a an and are as at be but by for from had has have he her his i if in is it its me my no not of on or
our she so that the their them they this to us was we were what who will with you your im its dont just like do did
can all one out up get got about been would there when how why than then too very""".split())


def tokens(text: str) -> set[str]:
    text = _MENTION.sub(" ", _URL.sub(" ", text.lower()))
    out = set()
    for tok in text.split():
        tok = tok.strip(".,!?;:\"'()[]{}<>*~`")
        if 2 <= len(tok) <= 30 and tok not in _STOP:
            out.add(tok)
    return out


def read_optout(cfg: dict) -> set[str]:
    path = cfg_file(cfg, "optout_file")
    if not path.exists():
        return set()
    return {line.split("#")[0].strip().lower() for line in path.read_text().splitlines()} - {""}


def resolve_optout(arc: sqlite3.Connection, entries: set[str]) -> tuple[set[str], set[str]]:
    """-> (excluded user_ids, excluded lowercase names). A listed name excludes every user_id that
    ever used it, and every other name those user_ids used, so renames stay excluded."""
    if not entries:
        return set(), set()
    arc.execute("CREATE TEMP TABLE IF NOT EXISTS _oo(v TEXT PRIMARY KEY)")
    arc.execute("DELETE FROM _oo")
    arc.executemany("INSERT OR IGNORE INTO _oo VALUES(?)", [(e,) for e in entries])
    ids = set(entries)
    for table in ("chat", "live_msgs"):
        ids |= {r[0] for r in arc.execute(
            f"SELECT DISTINCT user_id FROM {table} WHERE user_id IS NOT NULL AND lower(author) IN (SELECT v FROM _oo)")}
    arc.execute("CREATE TEMP TABLE IF NOT EXISTS _ooid(v TEXT PRIMARY KEY)")
    arc.execute("DELETE FROM _ooid")
    arc.executemany("INSERT OR IGNORE INTO _ooid VALUES(?)", [(i,) for i in ids])
    names = set(entries)
    for table in ("chat", "live_msgs"):
        names |= {r[0] for r in arc.execute(
            f"SELECT DISTINCT lower(author) FROM {table} WHERE author IS NOT NULL AND user_id IN (SELECT v FROM _ooid)")}
    return ids, names


def public_speaker(name: str, is_media: int, allowed: set[str]) -> str:
    if name in allowed:
        return name
    return "Media" if is_media else "Guest"


def publish_video(arc, pub, cfg: dict, v, allowed: set[str], ex_ids: set[str], ex_names: set[str]) -> None:
    vid = v["id"]
    thr, margin = cfg["speaker_match_threshold"], cfg["low_confidence_margin"]
    for t in arc.execute("SELECT * FROM turns WHERE video_id=? ORDER BY start", (vid,)):
        score = t["speaker_score"]
        low = int(score is not None and abs(score - thr) < margin)
        pub.execute("INSERT INTO turns(id, video_id, start, end, speaker, low_confidence, is_media, text) VALUES(?,?,?,?,?,?,?,?)",
                    (t["id"], vid, t["start"], t["end"], public_speaker(t["speaker"], t["is_media"], allowed),
                     low, t["is_media"], t["text"]))
    pub.executemany("INSERT INTO muted VALUES(?,?,?)",
                    arc.execute("SELECT video_id, start, end FROM muted WHERE video_id=?", (vid,)).fetchall())

    anchors = Anchors.load(arc, vid)
    duration = float(v["duration"] or 0)
    placeholder = cfg["deleted_placeholder"]
    show_deleted = bool(cfg["publish_deleted_text"])
    authors = {r[0] for r in arc.execute("SELECT DISTINCT lower(author) FROM chat WHERE video_id=? AND author IS NOT NULL", (vid,))}
    minutes = defaultdict(lambda: {"msgs": 0, "first": 0, "who": set(), "terms": defaultdict(set)})
    rows = []
    for c in arc.execute("SELECT * FROM chat WHERE video_id=? ORDER BY t", (vid,)):
        if (c["user_id"] and c["user_id"] in ex_ids) or (c["author"] or "").lower() in ex_names:
            continue
        t = anchors.to_transcript(c["t"])
        if t is None or t < 0 or t > duration + 1:
            continue   # falls in a cut, or outside the published audio
        text = c["text"] if (show_deleted or not c["deleted"]) else placeholder
        rows.append((c["id"], vid, round(t, 3), c["author"], c["badges"], c["first_msg"], c["subscriber"],
                     text, c["deleted"], c["kind"]))
        if c["kind"] == "msg":
            m = minutes[int(t // 60)]
            who = c["user_id"] or c["author"]
            m["msgs"] += 1
            m["first"] += 1 if c["first_msg"] else 0
            m["who"].add(who)
            if not c["deleted"]:
                for tok in tokens(c["text"] or ""):
                    m["terms"][tok].add(who)
    pub.executemany("INSERT INTO chat(id, video_id, t, author, badges, first_msg, subscriber, text, deleted, kind) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    k, top = cfg["publish_min_distinct_chatters"], cfg["publish_top_terms"]
    for minute, m in minutes.items():
        terms = sorted(((term, len(users)) for term, users in m["terms"].items()
                        if len(users) >= k and term not in authors), key=lambda x: (-x[1], x[0]))[:top]
        pub.execute("INSERT INTO chat_minutes VALUES(?,?,?,?,?,?)",
                    (vid, minute, m["msgs"], len(m["who"]), round(m["first"] / m["msgs"], 4), json.dumps(terms)))
    pub.execute("INSERT INTO videos VALUES(?,?,?,?,?,?,?,?)",
                (vid, v["title"], v["upload_date"], v["duration"], v["embed_type"], v["embed_id"], v["source_url"],
                 int(bool(minutes))))


def check(pub: sqlite3.Connection, ex_names: set[str]) -> list[str]:
    """Opt-out + schema test. Any returned problem fails the publish."""
    problems = []
    for table, cols in EXPECTED_COLUMNS.items():
        got = [r[1] for r in pub.execute(f"PRAGMA table_info({table})")]
        if got != cols:
            problems.append(f"schema: {table} has columns {got}, expected {cols}")
    for (name,) in pub.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        bad = {r[1] for r in pub.execute(f"PRAGMA table_info('{name}')")} & FORBIDDEN_COLUMNS
        if bad:
            problems.append(f"schema: {name} exposes {sorted(bad)}")
    extra = {r[0] for r in pub.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%\\_fts%' ESCAPE '\\'")} - set(EXPECTED_COLUMNS)
    if extra:
        problems.append(f"schema: unexpected tables {sorted(extra)}")
    if ex_names:
        hits = [a for (a,) in pub.execute("SELECT DISTINCT lower(author) FROM chat WHERE author IS NOT NULL") if a in ex_names]
        if hits:
            problems.append(f"opt-out: {len(hits)} opted-out author(s) present in public chat")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()
    cfg = load_config()
    arc = connect_archive(cfg)
    dest = P(cfg, "public_db")
    tmp = dest.with_name("public.tmp.db")
    for p in (tmp, tmp.with_name(tmp.name + "-journal")):
        if p.exists():
            p.unlink()
    pub = sqlite3.connect(tmp)
    pub.executescript(PUBLIC_SCHEMA)
    ex_ids, ex_names = resolve_optout(arc, read_optout(cfg))
    allowed = set(cfg["public_speakers"])
    videos = arc.execute("SELECT * FROM videos WHERE transcript_hash IS NOT NULL ORDER BY id").fetchall()
    for v in videos:
        publish_video(arc, pub, cfg, v, allowed, ex_ids, ex_names)
    pub.execute("INSERT INTO turns_fts(turns_fts) VALUES('rebuild')")
    pub.execute("INSERT INTO chat_fts(chat_fts) VALUES('rebuild')")
    pub.execute("INSERT INTO speakers SELECT DISTINCT speaker FROM turns")
    counts = {t: pub.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("videos", "turns", "chat", "chat_minutes")}
    meta = {"published_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "spec_version": SPEC_VERSION,
            "publish_deleted_text": str(int(bool(cfg["publish_deleted_text"]))),
            "optout_excluded_names": str(len(ex_names)), **{f"count_{k}": str(n) for k, n in counts.items()}}
    pub.executemany("INSERT INTO meta VALUES(?,?)", meta.items())
    pub.commit()
    # second, independent derivation of opted-out names for the test
    _, verify_names = resolve_optout(arc, read_optout(cfg))
    problems = check(pub, verify_names | ex_names)
    pub.close()
    if problems:
        for p in problems:
            log.error("PUBLISH FAILED: %s", p)
        tmp.unlink()
        return 1
    os.replace(tmp, dest)
    size = dest.stat().st_size / 1e6
    print(f"public.db: {size:.1f} MB, " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    print(f"opt-out test passed ({len(ex_names)} name(s), {len(ex_ids)} id(s) excluded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
