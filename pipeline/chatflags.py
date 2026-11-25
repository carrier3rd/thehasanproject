"""Archive-wide chat check: Claude flags hateful or abusive chat messages, saved for the site and later studies.

    python pipeline/chatflags.py status                 how many candidates are checked, flagged, left
    python pipeline/chatflags.py run [--max-requests N] check the next candidates (claude -p, the owner's subscription)
    python pipeline/chatflags.py week 2026-11-15        print that week's trophies

A keyword pass picks candidates first (the rhetoric study's watchlist and dogwhistles, any mention of Jews, and a
hate lexicon below): about 0.5% of chat. Claude judges only those, in chunks of `per_request`, with the same rubric
and categories as the weekly reel's chat check (reel.CHAT_RUBRIC). Every answer is cached; a usage limit stops the
run and the next run resumes. Results live in chatflags/chatflags.db (outside archive/, so the public site can read
it), keyed by the public chat message id. The site always joins them back to public.db, so opted-out users and
anything not published never show.

Trophies (the weekly page): per Sunday-Saturday week, chatters ranked by flagged messages the mods did NOT delete,
counting only flags Claude was sure of; at least `trophy_min` of them to place. The broadcaster and bots are left out.
Nazi/Holocaust comparisons are highlighted but don't count toward trophies (the site reports them as comparisons).
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import P, get_logger, load_config  # noqa: E402
import reel  # noqa: E402

log = get_logger("chatflags")
VERSION = 1
DEFAULTS = {"per_request": 300, "max_requests_per_run": 60, "trophy_min": 3, "trophy_top": 3,
            "exclude_authors": ["hasanabi", "fossabot", "nightbot", "streamelements", "moobot", "sery_bot",
                                "streamlabs", "wizebot", "pokemoncommunitygame", "soundalerts"]}
ANTISEMITISM = {"antisemitism"}
HATEFUL = {"slur", "antisemitism", "racism_or_bigotry", "dehumanizing", "violent_threat_or_glorification",
           "harassment_or_doxxing", "sexual_harassment"}            # not nazi_or_holocaust_comparison
HATE_LEXICON = re.compile(
    r"\b(n[i1!]gg+(?:a|er|uh|az|ers)s?|f[a@]gg?(?:ot|it|s)?s?|tr[a@]nn(?:y|ies)|retard(?:ed|s)?|ch[i1]nks?|sp[i1]cs?|"
    r"wetbacks?|k[i1]kes?|towelheads?|sandn[i1]gg\w*|subhuman|vermin|cockroach(?:es)?|"
    r"gas (?:the|them|all)|kill (?:all|every|them all)|should (?:all )?(?:die|be killed|hang)|"
    r"behead|hitler|nazi\w*|holocaust|1488|heil|zog|goyim|shekel\w*|globalists?)\b", re.I)
SCHEMA = """
CREATE TABLE IF NOT EXISTS scanned (video_id TEXT PRIMARY KEY, msgs INTEGER);
CREATE TABLE IF NOT EXISTS cands (id INTEGER PRIMARY KEY, video_id TEXT, day TEXT);
CREATE TABLE IF NOT EXISTS checked (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS flags (
  id INTEGER PRIMARY KEY, video_id TEXT, day TEXT, t REAL, author TEXT, category TEXT, why TEXT,
  sure INTEGER, deleted INTEGER, partner INTEGER, version INTEGER);
CREATE INDEX IF NOT EXISTS flags_day ON flags(day);
CREATE INDEX IF NOT EXISTS flags_video ON flags(video_id);
CREATE INDEX IF NOT EXISTS flags_author ON flags(lower(author));
"""


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("chatflags") or {})}


def db_path(cfg: dict) -> Path:
    paths = cfg.get("paths", {})
    return P(cfg, "chatflags") if "chatflags" in paths else Path(cfg["_root"]) / "chatflags" / "chatflags.db"


def open_db(cfg: dict) -> sqlite3.Connection:
    p = db_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    return con


def prefilter():
    import rhetoric
    pats = rhetoric.load_patterns()

    def hit(text: str) -> bool:
        if HATE_LEXICON.search(text):
            return True
        hits, topics = rhetoric.match(pats, text)
        return bool(hits) or "topic_jews" in topics
    return hit


def scan(con: sqlite3.Connection, pub: sqlite3.Connection) -> int:
    """Keyword pass over streams not scanned yet (or whose chat grew); candidate ids go in `cands`. -> new candidates."""
    hit, added = prefilter(), 0
    have = dict(con.execute("SELECT video_id, msgs FROM scanned"))
    for vid, day, n in pub.execute("SELECT v.id, v.upload_date, count(c.id) FROM videos v JOIN chat c ON c.video_id = v.id "
                                   "WHERE c.kind='msg' GROUP BY v.id ORDER BY v.upload_date"):
        if have.get(vid) == n:
            continue
        rows = [(r[0], vid, day) for r in pub.execute("SELECT id, text FROM chat WHERE video_id=? AND kind='msg'", (vid,))
                if r[1] and r[1].strip() != "[deleted by mods]" and hit(r[1])]
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO cands VALUES(?,?,?)", rows)
        added += con.total_changes - before
        con.execute("INSERT OR REPLACE INTO scanned VALUES(?,?)", (vid, n))
        con.commit()
    return added


def candidates(con: sqlite3.Connection, pub: sqlite3.Connection, limit: int) -> list[dict]:
    """The next unchecked candidates, oldest stream first, with their text from public.db."""
    ids = [r[0] for r in con.execute("SELECT id FROM cands WHERE id NOT IN (SELECT id FROM checked) "
                                     "ORDER BY day, video_id, id LIMIT ?", (limit,))]
    out = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in pub.execute(f"SELECT c.id, c.video_id, c.t, c.author, c.badges, c.text, c.deleted, v.upload_date "
                             f"FROM chat c JOIN videos v ON v.id = c.video_id WHERE c.id IN ({','.join('?' * len(chunk))})",
                             chunk):
            out[r["id"]] = dict(r)
    gone = [i for i in ids if i not in out]          # no longer published (opt-out, republish): nothing to check
    con.executemany("INSERT OR IGNORE INTO checked VALUES(?)", [(i,) for i in gone])
    con.commit()
    return [out[i] for i in ids if i in out]


def request_for(st: dict, part: list[dict]) -> tuple[str, str, str, dict]:
    text = "\n".join(f"{m['id']}\t{'[deleted by mods] ' if m['deleted'] else ''}{' '.join(m['text'].split())[:400]}"
                     for m in part)
    key = reel.h("chatflags", VERSION, st["model"], text)
    user = (f"Separate chat messages from HasanAbi's Twitch streams, picked by a keyword search (most will be fine; "
            f"flag only what the rubric covers).\n\nCHAT ({len(part)} messages):\n{text}")
    return key, reel.CHAT_RUBRIC, user, reel.CHAT_SCHEMA


def save(con: sqlite3.Connection, part: list[dict], got: dict) -> int:
    """Record one answered chunk: every message as checked, the flagged ones with their category."""
    by = {m["id"]: m for m in part}
    flags = reel.chat_flags([(set(by), got)])
    for f in flags:
        m = by[f["id"]]
        con.execute("INSERT OR REPLACE INTO flags VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (m["id"], m["video_id"], m["upload_date"], m["t"], m["author"], f["category"], f["why"],
                     int(f["sure"]), int(bool(m["deleted"])), int("partner" in (m["badges"] or "")), VERSION))
    con.executemany("INSERT OR IGNORE INTO checked VALUES(?)", [(i,) for i in by])
    con.commit()
    return len(flags)


def run(cfg: dict, max_requests: int | None = None) -> str:
    """Scan new streams, then send up to max_requests chunks to Claude. -> 'done', 'more' or 'limit'."""
    st = {**reel.settings(cfg), **settings(cfg)}
    con = open_db(cfg)
    pub = reel.public_db(cfg)
    added = scan(con, pub)
    n = max(50, int(st["per_request"]))
    limit = st["max_requests_per_run"] if max_requests is None else max_requests
    left = con.execute("SELECT count(*) FROM cands WHERE id NOT IN (SELECT id FROM checked)").fetchone()[0]
    log.info("%d new candidates; %d unchecked (%d chunks); this run does at most %d", added, left, -(-left // n), limit)
    flagged = answered = 0
    while answered < limit:
        todo = candidates(con, pub, n * min(10, limit - answered))   # up to 10 chunks per claude -p round
        if not todo:
            break
        parts = [todo[i:i + n] for i in range(0, len(todo), n)]
        reqs = [request_for(st, p) for p in parts]
        need = [r for r in reqs if not reel.usable(reel.cache_get(cfg, "chat", r[0]))]
        stop = None
        try:
            if need:
                reel.run_cli(cfg, st, "chat", need)
        except reel.UsageLimit as e:
            log.warning("usage limit (%s); the rest resume next run", e)
            stop = "limit"
        progress = 0
        for p, (key, *_rest) in zip(parts, reqs):
            got = reel.cache_get(cfg, "chat", key)
            if reel.usable(got):           # answered, refused, or failed twice: final either way
                flagged += save(con, p, got)
                answered += 1
                progress += 1
        if stop:
            log.info("answered %d chunks, %d messages flagged", answered, flagged)
            return stop
        if not progress:                   # every chunk failed once: they retry next run
            break
    log.info("answered %d chunks, %d messages flagged", answered, flagged)
    left = con.execute("SELECT count(*) FROM cands WHERE id NOT IN (SELECT id FROM checked)").fetchone()[0]
    return "more" if left else "done"


# ---------------------------------------------------------------- reading (used by the site too)
def week_days(week: str) -> tuple[str, str]:
    d = dt.date.fromisoformat(week)
    return d.strftime("%Y%m%d"), (d + dt.timedelta(days=6)).strftime("%Y%m%d")


def trophies(flags_db: Path, pub: sqlite3.Connection, week: str, st: dict | None = None) -> dict:
    """The week's podiums. Joined to public.db by message id, so only published messages (and authors) count."""
    st = {**DEFAULTS, **(st or {})}
    lo, hi = week_days(week)
    if not Path(flags_db).exists():
        return {"week": week, "antisemitism": [], "hateful": [], "streamers": [], "checked_through": None}
    fcon = sqlite3.connect(f"file:{flags_db}?mode=ro", uri=True)
    fcon.row_factory = sqlite3.Row
    try:
        flags = [dict(r) for r in fcon.execute("SELECT * FROM flags WHERE day BETWEEN ? AND ?", (lo, hi))]
        through = fcon.execute("SELECT max(day) FROM flags").fetchone()[0]
    finally:
        fcon.close()
    if not flags:
        return {"week": week, "antisemitism": [], "hateful": [], "streamers": [], "checked_through": through}
    ids = [f["id"] for f in flags]
    msgs = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in pub.execute(f"SELECT c.id, c.video_id, c.t, c.author, c.badges, c.text, c.deleted, v.title, v.upload_date "
                             f"FROM chat c JOIN videos v ON v.id = c.video_id WHERE c.id IN ({','.join('?' * len(chunk))})",
                             chunk):
            msgs[r["id"]] = dict(r)
    skip = {a.lower() for a in st["exclude_authors"]}
    rows = []
    for f in flags:
        m = msgs.get(f["id"])
        if not m or not m["author"] or m["author"].lower() in skip:
            continue
        rows.append({**f, "author": m["author"], "deleted": int(bool(m["deleted"])),
                     "partner": int("partner" in (m["badges"] or "")), "text": m["text"], "title": m["title"],
                     "date": m["upload_date"]})

    def podium(cats: set[str]) -> list[dict]:
        per = {}
        for r in rows:
            if r["category"] in cats and r["sure"] and not r["deleted"]:
                per.setdefault(r["author"].lower(), []).append(r)
        ranked = sorted((v for v in per.values() if len(v) >= st["trophy_min"]),
                        key=lambda v: (-len(v), min(x["date"] + f"{x['t']:012.1f}" for x in v)))
        return [person(v) for v in ranked[: st["trophy_top"]]]

    def person(v: list[dict]) -> dict:
        v = sorted(v, key=lambda x: (x["date"], x["t"]))
        cats = {}
        for x in v:
            cats[x["category"]] = cats.get(x["category"], 0) + 1
        return {"author": v[-1]["author"], "partner": any(x["partner"] for x in v), "count": len(v), "categories": cats,
                "messages": [{k: x[k] for k in ("id", "video_id", "t", "text", "category", "why", "title", "date")}
                             for x in v[:25]]}

    streamers = {}
    for r in rows:
        if r["partner"] and not r["deleted"]:
            streamers.setdefault(r["author"].lower(), []).append(r)
    return {"week": week, "antisemitism": podium(ANTISEMITISM), "hateful": podium(HATEFUL),
            "streamers": [person(v) for v in sorted(streamers.values(), key=lambda v: -len(v))],
            "checked_through": through, "min": st["trophy_min"]}


def flags_for(flags_db: Path, ids: list[int]) -> dict[int, dict]:
    """{message id: {category, why, sure}} for the given public chat ids (the chat panes' highlighting)."""
    if not ids or not Path(flags_db).exists():
        return {}
    con = sqlite3.connect(f"file:{flags_db}?mode=ro", uri=True)
    try:
        out = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            for r in con.execute(f"SELECT id, category, why, sure FROM flags WHERE id IN ({','.join('?' * len(chunk))})",
                                 chunk):
                out[r[0]] = {"category": r[1], "why": r[2], "sure": bool(r[3])}
        return out
    finally:
        con.close()


def status(cfg: dict) -> None:
    con = open_db(cfg)
    cands = con.execute("SELECT count(*) FROM cands").fetchone()[0]
    checked = con.execute("SELECT count(*) FROM checked").fetchone()[0]
    flagged = con.execute("SELECT count(*) FROM flags").fetchone()[0]
    streams = con.execute("SELECT count(*) FROM scanned").fetchone()[0]
    print(f"{streams} streams scanned, {cands:,} candidates, {checked:,} checked, {flagged:,} flagged ({db_path(cfg)})")
    for cat, n in con.execute("SELECT category, count(*) FROM flags GROUP BY category ORDER BY 2 DESC"):
        print(f"  {cat}: {n:,}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("--max-requests", type=int)
    w = sub.add_parser("week")
    w.add_argument("week")
    a = ap.parse_args()
    cfg = load_config()
    if a.cmd == "status":
        status(cfg)
    elif a.cmd == "run":
        print(run(cfg, a.max_requests))
    else:
        import json
        print(json.dumps(trophies(db_path(cfg), reel.public_db(cfg), a.week, settings(cfg)), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
