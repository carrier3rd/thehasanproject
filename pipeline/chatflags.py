"""Archive-wide chat check: Claude flags hateful or abusive chat messages, saved for the site and later studies.

    python pipeline/chatflags.py status                 how many candidates are checked, flagged, left
    python pipeline/chatflags.py run [--max-requests N] check the next candidates (claude -p, the owner's subscription)
    python pipeline/chatflags.py verify                 second, stricter review of messages that could count toward a trophy
    python pipeline/chatflags.py week 2026-11-15        print that week's trophies

A keyword pass picks candidates first (the rhetoric study's watchlist and dogwhistles, any mention of Jews, and a
hate lexicon below): about 0.5% of chat. Claude judges only those, in chunks of `per_request`, with the same rubric
and categories as the weekly reel's chat check (reel.CHAT_RUBRIC). Every answer is cached; a usage limit stops the
run and the next run resumes. Results live in chatflags/chatflags.db (outside archive/, so the public site can read
it), keyed by the public chat message id. The site always joins them back to public.db, so opted-out users and
anything not published never show.

Trophies (the weekly page): per Sunday-Saturday week, chatters ranked by flagged messages the mods did NOT delete,
counting only flags Claude was sure of AND a second, stricter Claude review confirmed (`verify`: the message in
context, told to reject quoting, sarcasm, criticism of states or ideologies, and anything doubtful); at least
`trophy_min` (3) confirmed messages to place. The broadcaster and bots are left out.
Nazi/Holocaust comparisons are highlighted but don't count toward trophies (the site reports them as comparisons).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import P, get_logger, load_config, try_lock  # noqa: E402
import reel  # noqa: E402

log = get_logger("chatflags")
VERSION = 2
DEFAULTS = {"per_request": 300, "max_requests_per_run": 60, "trophy_min": 3, "trophy_top": 3,
            "verify_per_request": 12, "verify_max_requests_per_run": 40, "verify_effort": "high",
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
CREATE TABLE IF NOT EXISTS scanned (video_id TEXT PRIMARY KEY, sig TEXT);
CREATE TABLE IF NOT EXISTS cands (fp TEXT PRIMARY KEY, id INTEGER, video_id TEXT, day TEXT);
CREATE TABLE IF NOT EXISTS checked (fp TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS failed (fp TEXT PRIMARY KEY, status TEXT);
CREATE TABLE IF NOT EXISTS flags (
  fp TEXT PRIMARY KEY, id INTEGER, video_id TEXT, day TEXT, t REAL, author TEXT, category TEXT, why TEXT,
  sure INTEGER, deleted INTEGER, partner INTEGER, version INTEGER);
CREATE INDEX IF NOT EXISTS flags_day ON flags(day);
CREATE TABLE IF NOT EXISTS verified (fp TEXT PRIMARY KEY, confirmed INTEGER, category TEXT, why TEXT, version INTEGER);
"""
# Flags are keyed by a fingerprint of the message itself (stream, author, text), never by the chat row id alone: row
# ids are reused when a stream's chat is reloaded and renumbered by build_db --rebuild, and a flag must never land on
# someone else's message. Every read checks the fingerprint against the message as published now.


def fingerprint(video_id: str, author: str | None, text: str | None) -> str:
    return hashlib.sha1(f"{video_id}\x1f{(author or '').lower()}\x1f{text or ''}".encode()).hexdigest()[:24]


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


def published(text: str | None) -> bool:
    return bool(text and text.strip() and text.strip() != "[deleted by mods]")


def scan(con: sqlite3.Connection, pub: sqlite3.Connection) -> int:
    """Keyword pass over streams not scanned yet or whose chat changed (count, ids or text); candidates go in
    `cands` by fingerprint. -> new candidates."""
    hit, added = prefilter(), 0
    have = dict(con.execute("SELECT video_id, sig FROM scanned"))
    for vid, day, *sig in pub.execute(
            "SELECT v.id, v.upload_date, count(c.id), min(c.id), max(c.id), total(length(c.text)), "
            "total(length(c.author)), total(c.id * length(c.text)) FROM videos v JOIN chat c ON c.video_id = v.id "
            "WHERE c.kind='msg' GROUP BY v.id ORDER BY v.upload_date"):
        sig = ":".join(str(x) for x in sig)      # changes when a reload renumbers or rewrites a stream's chat
        if have.get(vid) == sig:
            continue
        rows = [(fingerprint(vid, r[1], r[2]), r[0], vid, day)
                for r in pub.execute("SELECT id, author, text FROM chat WHERE video_id=? AND kind='msg'", (vid,))
                if published(r[2]) and hit(r[2])]
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO cands VALUES(?,?,?,?)", rows)
        added += con.total_changes - before
        con.executemany("UPDATE cands SET id=? WHERE fp=?", [(r[1], r[0]) for r in rows])   # where it lives now
        con.execute("INSERT OR REPLACE INTO scanned VALUES(?,?)", (vid, sig))
        con.commit()
    return added


UNCHECKED = "SELECT {} FROM cands WHERE fp NOT IN (SELECT fp FROM checked) AND fp NOT IN (SELECT fp FROM failed)"


def candidates(con: sqlite3.Connection, pub: sqlite3.Connection, limit: int) -> list[dict]:
    """The next unchecked candidates, oldest stream first, with their text as published now (fingerprint matched)."""
    want = dict(con.execute(UNCHECKED.format("id, fp") + " ORDER BY day, video_id, id LIMIT ?", (limit,)))
    ids = list(want)
    out = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in pub.execute(f"SELECT c.id, c.video_id, c.t, c.author, c.badges, c.text, c.deleted, v.upload_date "
                             f"FROM chat c JOIN videos v ON v.id = c.video_id WHERE c.id IN ({','.join('?' * len(chunk))})",
                             chunk):
            if fingerprint(r["video_id"], r["author"], r["text"]) == want[r["id"]]:
                out[r["id"]] = {**dict(r), "fp": want[r["id"]]}
    gone = [(want[i],) for i in ids if i not in out]   # unpublished, opted out, or moved (a rescan finds it again)
    con.executemany("INSERT OR IGNORE INTO checked VALUES(?)", gone)
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
    """Record one finished chunk. Answered: every message checked, the flagged ones saved. Refused or failed twice:
    its messages go in `failed` (counted by `status`, retried with `run --retry-failed`), never silently 'clean'."""
    if got.get("status") != "succeeded":
        con.executemany("INSERT OR REPLACE INTO failed VALUES(?,?)", [(m["fp"], got.get("status")) for m in part])
        con.commit()
        return 0
    by = {m["id"]: m for m in part}
    flags = reel.chat_flags([(set(by), got)])
    for f in flags:
        m = by[f["id"]]
        con.execute("INSERT OR REPLACE INTO flags VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (m["fp"], m["id"], m["video_id"], m["upload_date"], m["t"], m["author"], f["category"], f["why"],
                     int(f["sure"]), int(bool(m["deleted"])), int("partner" in (m["badges"] or "")), VERSION))
    con.executemany("INSERT OR IGNORE INTO checked VALUES(?)", [(m["fp"],) for m in part])
    con.commit()
    return len(flags)


def single_run(cfg: dict):
    """A lock held for the life of the process, so the nightly timer and a hand-started run never overlap (they
    would send the same chunks twice). -> the open lock file, or None when another run holds it."""
    f = open(db_path(cfg).parent / "run.lock", "w")
    if not try_lock(f):              # until Nov 26 2026 Windows had no lock here; chatflags now runs on the PC
        f.close()
        return None
    return f


def run(cfg: dict, max_requests: int | None = None, retry_failed: bool = False) -> str:
    """Scan new streams, then send up to max_requests chunks to Claude. -> 'done', 'more' or 'limit'.
    retry_failed: put refused/failed messages back in the queue, in chunks of 50 (a new request, not the cache)."""
    st = {**reel.settings(cfg), **settings(cfg)}
    con = open_db(cfg)
    lock = single_run(cfg)
    if lock is None:
        log.info("another chatflags run is in progress; nothing to do")
        return "busy"
    pub = reel.public_db(cfg)
    added = scan(con, pub)
    n = max(50, int(st["per_request"]))
    if retry_failed:
        con.execute("DELETE FROM failed")
        con.commit()
        n = 50
    limit = st["max_requests_per_run"] if max_requests is None else max_requests
    left = con.execute(UNCHECKED.format("count(*)")).fetchone()[0]
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
    more = bool(con.execute(UNCHECKED.format("count(*)")).fetchone()[0])
    con.close()
    reviewed = verify(cfg)                  # the second review of whatever could now count toward a trophy
    return "limit" if reviewed == "limit" else "more" if more or reviewed == "more" else "done"


# ---------------------------------------------------------------- second review (the trophy checker)
VERIFY_RUBRIC = """You are the second, stricter reviewer of Twitch chat messages from HasanAbi's (Hasan Piker's) stream.
A first pass flagged each message below as hateful or abusive. A message you confirm counts toward publicly naming
its author as one of the week's most hateful chatters, so a wrong confirmation harms a real person. Confirm only
what is unambiguous.

Each item gives: what was being said on stream around that moment, the chat just before it, the MESSAGE, and the
first-pass label.

Confirm (confirmed: true) only when, read in that context, the message is clearly the author's own hateful or abusive
statement in one of these categories:
- slur: a slur against a group (racial, ethnic, religious, homophobic, transphobic, ableist)
- antisemitism: hostility to Jews as Jews, antisemitic tropes or conspiracy, Holocaust denial or mockery
- racism_or_bigotry: hostility to people for their race, ethnicity, religion, nationality, gender, sexuality, disability
- dehumanizing: a group of people described as animals, vermin, disease, subhuman, or deserving to die
- violent_threat_or_glorification: threats, calls for violence or killing, celebrating deaths or attacks on civilians
- harassment_or_doxxing: abuse aimed at a specific person beyond insult, or personal information about someone
- sexual_harassment: sexual comments aimed at a specific person

Reject (confirmed: false) when any of these could be true:
- it quotes, repeats or paraphrases someone else in order to criticise, mock or report them (including the streamer,
  a video on stream, or another chatter)
- it is sarcasm or irony whose target is the hateful view itself
- its target is a government, state, army, party, ideology or policy rather than people for who they are; criticism
  of Israel or Zionism is not antisemitism, and comparisons of Israel to Nazi Germany are not counted here
- it is an ordinary insult, swearing, an emote, spam or copypasta whose meaning is unclear
- it is a joke with no target group, or you cannot tell who the target is
- you hesitate for any reason. When in doubt, reject.

For every item return its id, confirmed, the category that fits best (it may differ from the first pass), and one
short reason (no more than 15 words)."""
VERIFY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdicts"],
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "confirmed", "category", "why"],
        "properties": {"id": {"type": "integer"}, "confirmed": {"type": "boolean"},
                       "category": {"type": "string", "enum": reel.CHAT_CATEGORIES}, "why": {"type": "string"}}}}}}


def week_of(day: str) -> str:
    """The Sunday (YYYY-MM-DD) of the Sunday-Saturday week a YYYYMMDD stream day falls in."""
    d = dt.date(int(day[:4]), int(day[4:6]), int(day[6:8]))
    return (d - dt.timedelta(days=(d.weekday() + 1) % 7)).isoformat()


def eligible(flags: list[dict], pub: sqlite3.Connection, st: dict) -> list[dict]:
    """First-pass flags that could count toward a trophy: Claude was sure, the mods left the message up, it is
    still published as written, in a hate category, and not from the broadcaster or a bot."""
    skip = {a.lower() for a in st["exclude_authors"]}
    return [r for r in _published_rows(pub, [f for f in flags if f["sure"] and f["category"] in HATEFUL])
            if r["author"] and r["author"].lower() not in skip and not r["deleted"]]


def to_verify(con: sqlite3.Connection, pub: sqlite3.Connection, st: dict) -> list[dict]:
    """Messages still waiting for the second review: those of chatters who have at least trophy_min eligible flags
    in a week, so only chatters who could place are reviewed."""
    con.row_factory = sqlite3.Row
    flags = [dict(r) for r in con.execute("SELECT * FROM flags WHERE sure=1")]
    done = {r[0] for r in con.execute("SELECT fp FROM verified")}
    per = {}
    for r in eligible(flags, pub, st):
        per.setdefault((week_of(r["date"]), r["author"].lower()), []).append(r)
    out = [r for rows in per.values() if len(rows) >= st["trophy_min"] for r in rows if r["fp"] not in done]
    return sorted(out, key=lambda r: (r["date"], r["video_id"], r["t"]))


def context(pub: sqlite3.Connection, m: dict) -> tuple[str, str]:
    """(what was said on stream around the message, the chat just before it): text only, no usernames."""
    said = [f"{r['speaker']}: {' '.join((r['text'] or '').split())[:300]}" for r in pub.execute(
        "SELECT speaker, text FROM turns WHERE video_id=? AND start <= ? AND end >= ? ORDER BY start LIMIT 5",
        (m["video_id"], m["t"] + 5, m["t"] - 40))]
    before = [" ".join((r["text"] or "").split())[:200] for r in pub.execute(
        "SELECT text FROM chat WHERE video_id=? AND kind='msg' AND deleted=0 AND t < ? AND t >= ? ORDER BY t DESC LIMIT 6",
        (m["video_id"], m["t"], m["t"] - 45))]
    return "\n".join(said) or "(no transcript here)", "\n".join(f"- {x}" for x in reversed(before)) or "(none)"


def verify_request(st: dict, pub: sqlite3.Connection, part: list[dict]) -> tuple[str, str, str, dict]:
    items = []
    for m in part:
        said, before = context(pub, m)
        items.append(f"### id {m['id']}\nON STREAM AROUND THEN:\n{said}\nCHAT JUST BEFORE:\n{before}\n"
                     f"MESSAGE: {' '.join(m['text'].split())[:500]}\nFIRST PASS: {m['category']} ({m['why']})")
    text = "\n\n".join(items)
    return (reel.h("chatverify", VERSION, st["model"], text), VERIFY_RUBRIC,
            f"{len(part)} flagged messages to review.\n\n{text}", VERIFY_SCHEMA)


def verify(cfg: dict, max_requests: int | None = None) -> str:
    """The second review of every message that could count toward a trophy. -> 'done', 'more' or 'limit'."""
    st = {**reel.settings(cfg), **settings(cfg)}
    con = open_db(cfg)
    pub = reel.public_db(cfg)
    limit = st["verify_max_requests_per_run"] if max_requests is None else max_requests
    n = max(1, int(st["verify_per_request"]))
    todo = to_verify(con, pub, st)
    log.info("%d flagged messages await the second review (%d chunks); this run does at most %d", len(todo), -(-len(todo) // n), limit)
    parts = [todo[i:i + n] for i in range(0, len(todo), n)][:limit]
    reqs = [verify_request(st, pub, p) for p in parts]
    need = [r for r in reqs if not reel.usable(reel.cache_get(cfg, "verify", r[0]))]
    stop = None
    try:
        if need:
            reel.run_cli(cfg, st, "verify", need)
    except reel.UsageLimit as e:
        log.warning("usage limit (%s); the review resumes next run", e)
        stop = "limit"
    confirmed = reviewed = 0
    for p, (key, *_rest) in zip(parts, reqs):
        got = reel.cache_get(cfg, "verify", key)
        if not reel.usable(got):
            continue
        r = got.get("result") if got.get("status") == "succeeded" else None
        verdicts = {v.get("id"): v for v in (r or {}).get("verdicts", []) if isinstance(v, dict)} if isinstance(r, dict) else {}
        for m in p:                          # no verdict (refused, failed twice, or left out) never counts
            v = verdicts.get(m["id"])
            ok = bool(v and v.get("confirmed") is True and v.get("category") in HATEFUL)
            con.execute("INSERT OR REPLACE INTO verified VALUES(?,?,?,?,?)",
                        (m["fp"], int(ok), (v or {}).get("category") or m["category"],
                         str((v or {}).get("why") or f"not reviewed ({got.get('status')})")[:160], VERSION))
            confirmed += ok
            reviewed += 1
    con.commit()
    log.info("second review: %d messages reviewed, %d confirmed", reviewed, confirmed)
    return stop or ("more" if len(todo) > reviewed else "done")


# ---------------------------------------------------------------- reading (used by the site too)
def week_days(week: str) -> tuple[str, str]:
    """YYYYMMDD bounds of a Sunday-Saturday week. ValueError unless `week` is a real date that is a Sunday."""
    d = dt.date.fromisoformat(week)
    if d.weekday() != 6:
        raise ValueError("weeks start on a Sunday")
    return d.strftime("%Y%m%d"), (d + dt.timedelta(days=6)).strftime("%Y%m%d")


def _published_rows(pub: sqlite3.Connection, flags: list[dict]) -> list[dict]:
    """Each flag joined to its message as published now; dropped unless the fingerprint still matches."""
    by_id = {f["id"]: f for f in flags}
    ids, out = list(by_id), []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in pub.execute(f"SELECT c.id, c.video_id, c.t, c.author, c.badges, c.text, c.deleted, v.title, v.upload_date "
                             f"FROM chat c JOIN videos v ON v.id = c.video_id WHERE c.id IN ({','.join('?' * len(chunk))})",
                             chunk):
            f = by_id[r["id"]]
            if fingerprint(r["video_id"], r["author"], r["text"]) != f["fp"]:
                continue
            out.append({**f, "author": r["author"], "deleted": int(bool(r["deleted"])), "t": r["t"],
                        "partner": int("partner" in (r["badges"] or "")), "text": r["text"], "title": r["title"],
                        "date": r["upload_date"]})
    return out


def trophies(flags_db: Path, pub: sqlite3.Connection, week: str, st: dict | None = None) -> dict:
    """The week's podiums: messages the first pass was sure of AND the second review confirmed, that the mods left
    up, in the hate categories, at least trophy_min per chatter. Streamers (partner badge) are listed by the same
    rules. pending_review: chatters who could place but still have messages awaiting the second review."""
    st = {**DEFAULTS, **(st or {})}
    lo, hi = week_days(week)
    empty = {"week": week, "antisemitism": [], "hateful": [], "streamers": [], "checked_through": None,
             "min": st["trophy_min"], "pending_review": 0}
    if not Path(flags_db).exists():
        return empty
    fcon = sqlite3.connect(f"file:{flags_db}?mode=ro", uri=True)
    fcon.row_factory = sqlite3.Row
    try:
        flags = [dict(r) for r in fcon.execute("SELECT * FROM flags WHERE day BETWEEN ? AND ?", (lo, hi))]
        empty["checked_through"] = fcon.execute("SELECT max(day) FROM flags").fetchone()[0]
        has = fcon.execute("SELECT 1 FROM sqlite_master WHERE name='verified'").fetchone()
        verdicts = {r["fp"]: dict(r) for r in fcon.execute("SELECT * FROM verified")} if has else {}
    finally:
        fcon.close()
    could = eligible(flags, pub, st)
    per = {}
    for r in could:
        per.setdefault(r["author"].lower(), []).append(r)
    empty["pending_review"] = sum(1 for v in per.values() if len(v) >= st["trophy_min"]
                                  and any(x["fp"] not in verdicts for x in v))
    rows = [{**r, "category": verdicts[r["fp"]]["category"], "why": verdicts[r["fp"]]["why"]}
            for r in could if verdicts.get(r["fp"], {}).get("confirmed")]       # confirmed by the second review

    def ranked(cats: set[str], only_partners: bool = False) -> list[list[dict]]:
        per = {}
        for r in rows:
            if r["category"] in cats and (r["partner"] or not only_partners):
                per.setdefault(r["author"].lower(), []).append(r)
        return sorted((v for v in per.values() if len(v) >= st["trophy_min"]),
                      key=lambda v: (-len(v), min(x["date"] + f"{x['t']:012.1f}" for x in v)))

    def person(v: list[dict]) -> dict:
        v = sorted(v, key=lambda x: (x["date"], x["t"]))
        cats = {}
        for x in v:
            cats[x["category"]] = cats.get(x["category"], 0) + 1
        return {"author": v[-1]["author"], "partner": any(x["partner"] for x in v), "count": len(v), "categories": cats,
                "messages": [{k: x[k] for k in ("id", "video_id", "t", "text", "category", "why", "title", "date")}
                             for x in v[:25]]}

    return {**empty, "antisemitism": [person(v) for v in ranked(ANTISEMITISM)[: st["trophy_top"]]],
            "hateful": [person(v) for v in ranked(HATEFUL)[: st["trophy_top"]]],
            "streamers": [person(v) for v in ranked(HATEFUL, only_partners=True)]}


def flags_for(flags_db: Path, msgs: list[dict]) -> dict[int, dict]:
    """{message id: {category, why, sure}} for chat rows (each with id, video_id, author, text), matched by
    fingerprint so a reused row id can never pick up another message's flag. Unpublished text never matches."""
    if not msgs or not Path(flags_db).exists():
        return {}
    fps = {}
    for m in msgs:
        if published(m.get("text")):
            fps.setdefault(fingerprint(m["video_id"], m.get("author"), m.get("text")), []).append(m["id"])
    keys = list(fps)
    con = sqlite3.connect(f"file:{flags_db}?mode=ro", uri=True)
    try:
        out = {}
        for i in range(0, len(keys), 900):
            chunk = keys[i:i + 900]
            for r in con.execute(f"SELECT fp, category, why, sure FROM flags WHERE fp IN ({','.join('?' * len(chunk))})",
                                 chunk):
                for mid in fps[r[0]]:
                    out[mid] = {"category": r[1], "why": r[2], "sure": bool(r[3])}
        return out
    finally:
        con.close()


def status(cfg: dict) -> None:
    con = open_db(cfg)
    cands = con.execute("SELECT count(*) FROM cands").fetchone()[0]
    checked = con.execute("SELECT count(*) FROM checked").fetchone()[0]
    failed = con.execute("SELECT count(*) FROM failed").fetchone()[0]
    rev = con.execute("SELECT count(*), coalesce(sum(confirmed), 0) FROM verified").fetchone()
    flagged = con.execute("SELECT count(*) FROM flags").fetchone()[0]
    streams = con.execute("SELECT count(*) FROM scanned").fetchone()[0]
    print(f"{streams} streams scanned, {cands:,} candidates, {checked:,} checked, {flagged:,} flagged, "
          f"{failed:,} in refused/failed chunks (retry: run --retry-failed) ({db_path(cfg)})")
    print(f"second review: {rev[0]:,} messages reviewed, {rev[1]:,} confirmed")
    for cat, n in con.execute("SELECT category, count(*) FROM flags GROUP BY category ORDER BY 2 DESC"):
        print(f"  {cat}: {n:,}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("--max-requests", type=int)
    r.add_argument("--retry-failed", action="store_true", help="re-queue refused/failed chunks in chunks of 50")
    v = sub.add_parser("verify")
    v.add_argument("--max-requests", type=int)
    w = sub.add_parser("week")
    w.add_argument("week")
    a = ap.parse_args()
    cfg = load_config()
    if a.cmd == "status":
        status(cfg)
    elif a.cmd == "verify":
        print(verify(cfg, a.max_requests))
    elif a.cmd == "run":
        print(run(cfg, a.max_requests, a.retry_failed))
    else:
        import json
        print(json.dumps(trophies(db_path(cfg), reel.public_db(cfg), a.week, settings(cfg)), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
