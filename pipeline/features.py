"""Featured content for the public site: Spotlight (topic timelines against Reddit), Receipts, Chat vs Hasan, the quiz.

  status    what has been done and what is waiting
  topics    pass A: for each 2-hour part of a stream, Claude lists the stories Hasan talked about, roughly how long,
            the positions he stated (exact quotes) and a few lines that stand on their own. Newest streams first.
  stories   pass B: groups those topic labels, and the week's top Reddit posts, into stories with search keywords
  reddit    free, local: counts Reddit posts and comments that match each story, per subreddit and day
  receipts  pass C: per story, Claude looks for pairs of his positions that conflict; pass D (high effort, with the
            transcript around both quotes) confirms or rejects each pair. Confirmed pairs wait for the owner: only
            pairs approved with `receipts approve ID` (or on the admin site) are published.
  chatvs    free candidate minutes (pushback words in chat, mass deletions), then Claude reads the transcript and chat
  publish   writes features/*.json for the public site
  run       all of it in order, each Claude step capped per run (the nightly timer, features.timer)

Every Claude answer goes through `claude -p` on the owner's subscription (reel.run_cli) and is cached under
archive/reel/<kind>/, so a usage limit only pauses the work; the next run carries on. Working state (story list,
review queue, Reddit counts) lives in archive/features/, which the public site never reads. Only features/ (the
`features` path) is public, and it holds no Reddit usernames, no chat usernames and no unapproved receipts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import reel
from common import P, get_logger, load_config, make_dirs

log = get_logger("features")
VERSION = 1
DEFAULTS = {
    "topic_model": "claude-sonnet-5-5", "topics_effort": "low", "part_s": 7200, "topics_max_requests": 80,
    "stories_effort": "medium", "stories_batch": 250, "stories_max_requests": 20,
    "receipts_effort": "high", "rverify_effort": "high", "receipts_max_requests": 15, "rverify_max_requests": 30,
    "receipt_min_positions": 4, "receipt_min_days": 7, "receipt_context_s": 120,
    "chatvs_effort": "medium", "chatvs_max_requests": 40, "chatvs_min_pushback": 12, "chatvs_ratio": 2.5,
    "chatvs_deleted_min": 30, "chatvs_per_stream": 4,
    "reddit_dir": "", "reddit_top_posts": 8,
    "story_min_minutes": 8, "story_min_reddit": 30,
    "cli_workers": 2, "part_min_lines": 30,
    "tags_effort": "low", "tags_batch": 60, "tags_max_requests": 10,
    "orbit": [], "orbit_min_reddit": 20, "orbit_story_min_reddit": 8, "outsiders": [],
}
KINDS = ["news_politics", "israel_palestine", "war_and_foreign_policy", "streamers_and_internet", "culture_and_entertainment",
         "games", "sports", "science_tech_health", "personal_or_stream", "other"]
# Communities, for the Spotlight coverage bars. Groupings are a reading aid; every subreddit is also shown by name.
GROUPS = {
    "Streamer communities": ["LivestreamFail", "Hasan_Piker", "Destiny", "Asmongold", "atrioc", "h3h3productions", "LeftoversH3"],
    "Left": ["dsa", "TankieTheDeprogram", "Palestine"],
    "Liberal & center": ["neoliberal", "thebulwark", "thedavidpakmanshow", "samharris"],
    "Right": ["Conservative"],
    "Debate & mixed": ["PoliticalCompassMemes", "IsraelPalestine", "DecodingTheGurus"],
    "News": ["news", "politics", "worldnews", "ukraine"],
    "Pop culture": ["Fauxmoi", "youtubedrama"],
}
GROUP_OF = {s.lower(): g for g, subs in GROUPS.items() for s in subs}
REDDIT_FILE = re.compile(r"^r_(?P<sub>[A-Za-z0-9_]+)_(?P<kind>posts|comments)_(?P<lo>\d{4}-\d{2}-\d{2})_to_(?P<hi>\d{4}-\d{2}-\d{2})\.zip$")


def settings(cfg: dict) -> dict:
    return {**reel.settings(cfg), **DEFAULTS, **(cfg.get("features") or {})}


def work_dir(cfg: dict) -> Path:
    p = P(cfg, "archive_db").parent / "features"
    p.mkdir(parents=True, exist_ok=True)
    return p


def out_dir(cfg: dict) -> Path:
    paths = cfg.get("paths", {})
    p = P(cfg, "features") if "features" in paths else Path(cfg["_root"]) / "features"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def save_json(p: Path, value) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(p)


def day(upload_date: str) -> str:
    return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"


def clean_quote(q: str) -> bool:
    """Lines fit to show on their own: not a slur or hate keyword, no links or @mentions, a sensible length."""
    import chatflags
    words = len(q.split())
    return 6 <= words <= 45 and not chatflags.HATE_LEXICON.search(q) and "http" not in q and "@" not in q


def ask(cfg, st: dict, kind: str, reqs: list, cap: int) -> tuple[int, str | None]:
    """Answer the uncached requests (at most `cap`), via claude -p. Returns (answered, 'limit' or None)."""
    need = [r for r in reqs if not reel.usable(reel.cache_get(cfg, kind, r[0]))][:max(0, cap)]
    if not need:
        return 0, None
    sub = {**st, "model": st["topic_model"] if kind in ("topics", "tags") else st["model"], "cli_workers": st["cli_workers"]}
    try:
        return reel.run_cli(cfg, sub, kind, need), None
    except reel.UsageLimit as e:
        log.warning("%s: Claude usage limit (%s); the rest waits for the next run", kind, e)
        return 0, "limit"


def answer(cfg, kind: str, key: str):
    got = reel.cache_get(cfg, kind, key)
    return got.get("result") if got and got.get("status") == "succeeded" else None


# ---------------------------------------------------------------- pass A: topics per stream part
TOPIC_RUBRIC = """You index machine transcripts of HasanAbi's (Hasan Piker's) Twitch streams for a public archive read by
people from every side and from many online communities (streamer fans, political subreddits, Destiny's community).
The index must be neutral, specific and accurate.

For the part of the stream you are given, list each story or subject he spent real time on (about 2 minutes or more).
- label: specific and recognisable, the way a headline or a Reddit thread would name it: "Zohran Mamdani's NYC mayoral
  win", "Government shutdown over ACA subsidies", "xQc and Hasan argue about gambling sponsors". Never generic ("US
  politics", "news", "chatting"). Name the event, person or controversy.
- kind: the closest category. Use personal_or_stream for his own life, the stream itself, food, chatting with chat.
- start, end: seconds, taken from the [123s ...] timestamps of the first and last lines about it.
- minutes: about how many minutes of this part went to it (he can come back to a story several times).
- people: the named people central to it (at most 5).
- positions: up to 4 things HE asserted as his own view on it (not what a video, article, tweet, guest or chat said,
  and not sarcasm or a bit). position = one neutral, self-contained sentence of what he claimed or argued. quote = his
  exact words, copied from ONE line labelled Hasan. Only include positions he plainly holds. Not positions:
  * his analysis of what someone else did or decided (explaining or praising a politician's decision is commentary on
    that decision, not his own policy view, unless he states it as his own);
  * running his channel: moderating chat, banning someone, telling chat not to post something, following Twitch rules
    or the law on stream. That is public-facing conduct, not a view.
- quotables: up to 2 lines of his that would stand on their own for a reader (clear, memorable, about the story; not
  a slur, not a private person's details). quote copied exactly from one line labelled Hasan.

Lines are labelled Hasan, Guest, Media (videos he plays) or Auto-captions (older streams, speaker unknown: never quote
these as his). Media lines are shortened. The transcript is machine-made and can mishear; do not build anything on a
garbled line. Copy quotes exactly."""

TOPIC_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["topics"],
    "properties": {"topics": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["label", "kind", "start", "end", "minutes", "people", "positions", "quotables"],
        "properties": {
            "label": {"type": "string"}, "kind": {"type": "string", "enum": KINDS},
            "start": {"type": "number"}, "end": {"type": "number"}, "minutes": {"type": "number"},
            "people": {"type": "array", "items": {"type": "string"}},
            "positions": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                     "required": ["position", "quote"],
                                                     "properties": {"position": {"type": "string"}, "quote": {"type": "string"}}}},
            "quotables": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                     "required": ["quote"], "properties": {"quote": {"type": "string"}}}}}}}}}


def part_text(turns: list[dict]) -> str:
    out = []
    for t in turns:
        text = t["text"] if t["speaker"] != "Media" else (t["text"][:160] + ("…" if len(t["text"]) > 160 else ""))
        out.append(f"[{t['start']:.0f}s {reel.fmt(t['start'])}] {t['speaker']}: {text}")
    return "\n".join(out)


def stream_parts(con, vid: str, size: float, min_lines: int = 30) -> list[list[dict]]:
    turns = reel.turns_of(con, vid)
    parts: dict[int, list[dict]] = defaultdict(list)
    for t in turns:
        parts[int(t["start"] // size)].append(t)
    return [parts[k] for k in sorted(parts) if sum(1 for t in parts[k] if t["speaker"] in ("Hasan", "Auto-captions")) >= min_lines]


def topic_requests(st: dict, con) -> list[tuple]:
    """(key, system, user, schema, meta) for every part of every stream, newest stream first."""
    out = []
    for v in con.execute("SELECT id, title, upload_date FROM videos ORDER BY upload_date DESC, id"):
        for i, part in enumerate(stream_parts(con, v["id"], st["part_s"], st["part_min_lines"])):
            text = part_text(part)
            user = f"Stream: \"{v['title']}\" ({day(v['upload_date'])})\n\n{text}"
            key = reel.h("topics", VERSION, st["topic_model"], v["id"], i, text)
            out.append((key, TOPIC_RUBRIC, user, TOPIC_SCHEMA, {"video_id": v["id"], "date": day(v["upload_date"]),
                                                                  "part": i, "turns": part}))
    return out


def find_line(quote: str, turns: list[dict], speakers=("Hasan",)) -> dict | None:
    q = reel.norm(quote)
    if not q.strip():
        return None
    for t in turns:
        if t["speaker"] in speakers and q in reel.norm(t["text"]):
            return t
    return None


def topic_entries(cfg, st: dict, con) -> list[dict]:
    """Every answered part's topics, with quotes checked against the transcript (unmatched quotes are dropped)."""
    out = []
    for key, _, _, _, meta in topic_requests(st, con):
        got = answer(cfg, "topics", key)
        if not got:
            continue
        turns = meta["turns"]
        lo, hi = turns[0]["start"], turns[-1]["end"]
        for n, tp in enumerate(got.get("topics") or []):
            label = " ".join(str(tp.get("label") or "").split())[:120]
            if not label:
                continue
            positions, quotables = [], []
            for p in tp.get("positions") or []:
                line = find_line(p.get("quote", ""), turns)
                if line:
                    positions.append({"position": p.get("position", "")[:300], "quote": p["quote"].strip(), "t": line["start"]})
            for q in tp.get("quotables") or []:
                line = find_line(q.get("quote", ""), turns)
                if line and clean_quote(q["quote"]):
                    quotables.append({"quote": q["quote"].strip(), "t": line["start"]})
            start = min(max(float(tp.get("start") or lo), lo), hi)
            out.append({"id": f"{key}-{n}", "video_id": meta["video_id"], "date": meta["date"], "label": label,
                        "kind": tp.get("kind") if tp.get("kind") in KINDS else "other", "start": start,
                        "minutes": round(min(max(float(tp.get("minutes") or 0), 0), st["part_s"] / 60), 1),
                        "people": [str(x)[:60] for x in (tp.get("people") or [])][:5],
                        "positions": positions, "quotables": quotables})
    return out


# ---------------------------------------------------------------- pass B: stories
STORY_RUBRIC = """You keep the story list for a neutral public archive of HasanAbi's streams, which is compared with
what Reddit communities were discussing at the same time.

You get the current story list and a batch of new items: topic labels from his streams ("stream") and titles of top
Reddit posts ("reddit"), each with a date. Put every item in a story.
- A story is one specific news event, controversy, person in the news or ongoing issue that people would recognise,
  e.g. "Government shutdown (Oct-Nov 2026)", "Zohran Mamdani's mayoral campaign", "Kick streamer controversy".
  Use an existing story when the item is about the same thing; otherwise create a new one.
- Use story "none" for items that are not a story: chatting, his personal life, generic subjects, memes, one-off jokes,
  Reddit posts that are only a clip of a streamer with no wider subject, and anything too vague to place.
- New stories: key (n1, n2, ...), a neutral title (at most 70 characters, no judgement words), kind, a one-sentence
  neutral summary, and 2-6 keywords: names or phrases that a Reddit title or comment about this story would contain
  and that rarely appear otherwise ("Mamdani", "government shutdown"). Never a common word or a first name alone.
  Keywords are matched case-insensitively as whole words."""

STORY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["assign", "new"],
    "properties": {
        "assign": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["item", "story"],
                                              "properties": {"item": {"type": "integer"}, "story": {"type": "string"}}}},
        "new": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                           "required": ["key", "title", "kind", "summary", "keywords"],
                                           "properties": {"key": {"type": "string"}, "title": {"type": "string"},
                                                          "kind": {"type": "string", "enum": KINDS},
                                                          "summary": {"type": "string"},
                                                          "keywords": {"type": "array", "items": {"type": "string"}}}}}}}


def label_key(source: str, text: str) -> str:
    return reel.h(source, " ".join(text.lower().split()))


def stories_state(cfg) -> dict:
    return load_json(work_dir(cfg) / "stories.json", {"stories": {}, "assigned": {}})


def story_items(entries: list[dict], reddit_titles: list[dict]) -> list[dict]:
    """Unique items to place, oldest first: stream topic labels and top Reddit titles."""
    seen, items = set(), []
    for e in sorted(entries, key=lambda e: e["date"]):
        k = label_key("stream", e["label"])
        if k not in seen:
            seen.add(k)
            items.append({"key": k, "source": "stream", "date": e["date"], "text": e["label"]})
    for r in sorted(reddit_titles, key=lambda r: r["date"]):
        k = label_key("reddit", r["title"])
        if k not in seen:
            seen.add(k)
            items.append({"key": k, "source": "reddit", "date": r["date"], "text": f"r/{r['sub']}: {r['title']}"})
    return sorted(items, key=lambda i: i["date"])


def story_request(st: dict, state: dict, batch: list[dict]) -> tuple:
    known = "\n".join(f"{sid} | {s['title']} | {', '.join(s['keywords'])}" for sid, s in state["stories"].items()) or "(none yet)"
    items = "\n".join(f"{i} | {it['date']} | {it['source']} | {it['text']}" for i, it in enumerate(batch))
    user = f"Current stories (id | title | keywords):\n{known}\n\nNew items (item | date | source | text):\n{items}"
    return (reel.h("stories", VERSION, st["model"], user), STORY_RUBRIC, user, STORY_SCHEMA)


def good_keywords(words) -> list[str]:
    out = []
    for w in words or []:
        w = " ".join(str(w).split())
        if len(w) >= 4 and not w.isdigit() and w.lower() not in {"trump", "biden", "israel", "gaza", "america", "democrats",
                                                                  "republicans", "twitch", "reddit", "news", "war"}:
            out.append(w[:60])
    return out[:6]


def run_stories(cfg, st: dict, entries: list[dict], reddit_titles: list[dict], cap: int) -> tuple[int, str | None]:
    """Places new items in batches; each batch sees the stories made by the ones before it (so one request at a time)."""
    state = stories_state(cfg)
    todo = [it for it in story_items(entries, reddit_titles) if it["key"] not in state["assigned"]]
    done, why = 0, None
    while todo and done < cap:
        batch = todo[:st["stories_batch"]]
        req = story_request(st, state, batch)
        n, why = ask(cfg, st, "stories", [req], 1)
        got = answer(cfg, "stories", req[0])
        if got is None:
            if why or not reel.usable(reel.cache_get(cfg, "stories", req[0])):
                break
            for it in batch:            # refused or failed twice: leave these items out rather than loop on them
                state["assigned"][it["key"]] = "none"
        else:
            new = {}
            for s in got.get("new") or []:
                kw = good_keywords(s.get("keywords"))
                title = " ".join(str(s.get("title") or "").split())[:90]
                if not title or not kw:
                    continue
                sid = reel.h("story", title.lower())[:10]
                state["stories"].setdefault(sid, {"title": title, "kind": s.get("kind") if s.get("kind") in KINDS else "other",
                                                  "summary": str(s.get("summary") or "")[:300], "keywords": kw})
                new[str(s.get("key"))] = sid
            placed = {}
            for a in got.get("assign") or []:
                i, sid = a.get("item"), str(a.get("story"))
                if isinstance(i, int) and 0 <= i < len(batch):
                    placed[i] = new.get(sid) or (sid if sid in state["stories"] else "none")
            for i, it in enumerate(batch):
                state["assigned"][it["key"]] = placed.get(i, "none")
        save_json(work_dir(cfg) / "stories.json", state)
        todo = todo[len(batch):]
        done += 1
        if why:
            break
    return done, why


# ---------------------------------------------------------------- blindspot tags: orbit, proximity, who did what
EVENT_TYPES = ["attacks_on_civilians", "war_or_invasion", "crackdown_on_protest_or_dissent", "censorship_or_press_freedom",
               "elections_and_democracy", "human_rights_abuses", "terrorism", "other"]
BLOCS = {"us_and_allies": "US & allies", "not_us_allied": "Not allied with the US"}
TAG_RUBRIC = """You tag stories for a neutral public archive that compares how much attention HasanAbi (Hasan Piker) gave
different stories. For each story give:
- world_event: true for real-world political, military or human-rights events; false for internet or streamer drama,
  culture, games, sports and his own life.
- event_type: the closest type (other if none fits).
- actor: the country, government or armed group mainly responsible for the act at the centre of the story; empty if none.
- actor_bloc: us_and_allies (the US, Israel, the UK, EU and NATO states, Ukraine, Taiwan, Japan, South Korea,
  Australia, and US-aligned states such as Saudi Arabia or the UAE), not_us_allied (any other country, government or
  armed group: Russia, China, Iran, North Korea, Venezuela, Cuba, Hamas, Hezbollah, the Houthis, the RSF, juntas and
  so on), or neither (no state or armed group is the actor).
- target: who was harmed or affected, in a few words.
Record only who did what. Do not judge the act or the story."""
TAG_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["tags"],
    "properties": {"tags": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "world_event", "event_type", "actor", "actor_bloc", "target"],
        "properties": {"id": {"type": "integer"}, "world_event": {"type": "boolean"},
                       "event_type": {"type": "string", "enum": EVENT_TYPES}, "actor": {"type": "string"},
                       "actor_bloc": {"type": "string", "enum": [*BLOCS, "neither"]}, "target": {"type": "string"}}}}}}


def run_tags(cfg, st: dict, cap: int) -> tuple[int, str | None]:
    """Who did what, for each story without tags yet (Sonnet, low effort, many stories per request)."""
    state = stories_state(cfg)
    tags = state.setdefault("tags", {})
    todo = [sid for sid in state["stories"] if sid not in tags]
    done, why = 0, None
    while todo and done < cap and not why:
        batch = todo[:st["tags_batch"]]
        user = "Stories (id | title | summary):\n" + "\n".join(
            f"{i} | {state['stories'][sid]['title']} | {state['stories'][sid]['summary']}" for i, sid in enumerate(batch))
        req = (reel.h("tags", VERSION, st["topic_model"], TAG_RUBRIC, user), TAG_RUBRIC, user, TAG_SCHEMA)
        _, why = ask(cfg, st, "tags", [req], 1)
        got = answer(cfg, "tags", req[0])
        if got is None and not reel.usable(reel.cache_get(cfg, "tags", req[0])):
            break
        by_id = {t.get("id"): t for t in (got or {}).get("tags") or []}
        for i, sid in enumerate(batch):
            t = by_id.get(i) or {}
            tags[sid] = {"world_event": bool(t.get("world_event")),
                         "event_type": t.get("event_type") if t.get("event_type") in EVENT_TYPES else "other",
                         "actor": str(t.get("actor") or "")[:60],
                         "actor_bloc": t.get("actor_bloc") if t.get("actor_bloc") in BLOCS else "neither",
                         "target": str(t.get("target") or "")[:80]}
        save_json(work_dir(cfg) / "stories.json", state)
        todo = todo[len(batch):]
        done += 1
    return done, why


def orbit_list(st: dict) -> list[dict]:
    out = []
    for o in st.get("orbit") or []:
        if o.get("name") and o.get("group") in ("orbit", "proximity", "adversary"):
            out.append({"name": str(o["name"]), "group": o["group"], "tie": o.get("tie", ""),
                        "variants": [" ".join(str(v).split()) for v in o.get("variants") or [] if len(str(v).strip()) >= 3]})
    return out


def orbit_matcher(st: dict):
    by = {v.lower(): o for o in orbit_list(st) for v in o["variants"]}
    if not by:
        return None, by
    rx = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(k) for k in sorted(by, key=len, reverse=True)) + r")(?!\w)", re.I)
    return rx, by


def hasan_mentions(con, stories: dict) -> dict[str, list[tuple]]:
    """Every line of Hasan's that contains one of a story's keywords: (date, video_id, t, text). A blindspot must
    count these, so a story he did mention in passing is never shown as one he ignored."""
    rx, by_kw = keyword_matcher(stories)
    out: dict[str, list[tuple]] = defaultdict(list)
    if rx is None:
        return out
    for r in con.execute("SELECT t.video_id, t.start, t.text, v.upload_date FROM turns t JOIN videos v ON v.id = t.video_id "
                         "WHERE t.speaker = 'Hasan' ORDER BY v.upload_date, t.start"):
        for sid in matches(rx, by_kw, r[2]):
            out[sid].append((day(r[3]), r[0], r[1], r[2][:200]))
    return out


# ---------------------------------------------------------------- Reddit: counts per story, subreddit and day
def reddit_files(st: dict) -> list[tuple[Path, dict]]:
    """Posts and comments files from the Reddit pulls, with the days each one is used for. Overlapping pulls of the
    same subreddit and kind count once: each day goes to the largest file that covers it."""
    root = Path(st["reddit_dir"]).expanduser() if st["reddit_dir"] else None
    if not root or not root.is_dir():
        return []
    found = []
    for p in root.glob("*/*/r_*.zip"):
        if p.parts[-3] in ("histories", "repull") or p.parts[-3].startswith("bb_"):
            continue
        m = REDDIT_FILE.match(p.name)
        if m and m["sub"].lower() in GROUP_OF:
            found.append((p, m.groupdict()))
    found.sort(key=lambda x: -x[0].stat().st_size)
    owner: dict[tuple, Path] = {}
    out = []
    for p, m in found:
        d0, d1 = dt.date.fromisoformat(m["lo"]), dt.date.fromisoformat(m["hi"])
        days = []
        while d0 <= d1:
            k = (m["sub"].lower(), m["kind"], d0.isoformat())
            if k not in owner:
                owner[k] = p
                days.append(d0.isoformat())
            d0 += dt.timedelta(days=1)
        if days:
            out.append((p, {**m, "days": days}))
    return out


def iter_reddit(p: Path):
    with zipfile.ZipFile(p) as z:
        for name in z.namelist():
            with z.open(name) as f:
                for line in f:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue


def keyword_matcher(stories: dict, only: set[str] | None = None):
    by_kw: dict[str, set[str]] = defaultdict(set)
    for sid, s in stories.items():
        if only is None or sid in only:
            for k in s["keywords"]:
                by_kw[k.lower()].add(sid)
    if not by_kw:
        return None, by_kw
    rx = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in sorted(by_kw, key=len, reverse=True)) + r")\b", re.I)
    return rx, by_kw


def matches(rx, by_kw, text: str) -> set[str]:
    if not text or rx is None:
        return set()
    return {sid for m in rx.finditer(text) for sid in by_kw.get(m.group(0).lower(), ())}


def scan_reddit(cfg, st: dict, stories: dict) -> dict:
    """Per file (cached by size, date and keywords): totals per day, and per story the matching posts and comments per
    day plus its top posts. Only posts that were not removed are counted or shown."""
    cache_dir = work_dir(cfg) / "reddit"
    files = reddit_files(st)
    for p, m in files:
        stat = p.stat()
        ck = cache_dir / (reel.h(str(p), stat.st_size, int(stat.st_mtime), m["days"]) + ".json")
        c = load_json(ck, None) or {"sub": m["sub"], "kind": m["kind"], "totals": None, "stories": {}, "kw": {}}
        sig = {sid: reel.h(s["keywords"]) for sid, s in stories.items()}
        stale = {sid for sid in stories if c["kw"].get(sid) != sig[sid]}
        if c["totals"] is not None and not stale:
            continue
        rx, by_kw = keyword_matcher(stories, stale)
        totals: Counter = Counter()
        res = {sid: {"days": Counter(), "top": []} for sid in stale}
        days = set(m["days"])
        for d in iter_reddit(p):
            try:
                when = dt.datetime.fromtimestamp(int(float(d.get("created_utc") or 0)), dt.timezone.utc).date().isoformat()
            except (TypeError, ValueError, OverflowError):
                continue
            if when not in days:
                continue
            if m["kind"] == "posts":
                if d.get("removed_by_category") or d.get("over_18"):
                    continue
                text = f"{d.get('title') or ''}\n{(d.get('selftext') or '')[:3000]}"
            else:
                body = d.get("body") or ""
                if body in ("[removed]", "[deleted]"):
                    continue
                text = body[:3000]
            totals[when] += 1
            for sid in matches(rx, by_kw, text):
                res[sid]["days"][when] += 1
                if m["kind"] == "posts":
                    res[sid]["top"].append({"title": (d.get("title") or "")[:300], "score": int(d.get("score") or 0),
                                            "comments": int(d.get("num_comments") or 0), "date": when,
                                            "url": "https://www.reddit.com" + (d.get("permalink") or "")})
        c["totals"] = c["totals"] or dict(totals)
        for sid, r in res.items():
            top = sorted(r["top"], key=lambda x: -x["score"])[:5]
            c["stories"][sid] = {"days": dict(r["days"]), "top": top}
            c["kw"][sid] = sig[sid]
        save_json(ck, c)
    merged = {"totals": defaultdict(Counter), "stories": defaultdict(lambda: {"days": defaultdict(Counter), "top": []})}
    for p, m in files:
        stat = p.stat()
        c = load_json(cache_dir / (reel.h(str(p), stat.st_size, int(stat.st_mtime), m["days"]) + ".json"), None)
        if not c:
            continue
        sub = m["sub"]
        for d, n in (c["totals"] or {}).items():
            merged["totals"][sub][d] += n
        for sid, r in c["stories"].items():
            if sid not in stories:
                continue
            for d, n in r["days"].items():
                merged["stories"][sid]["days"][sub][d] += n
            merged["stories"][sid]["top"] += [{**x, "sub": sub} for x in r["top"]]
    return merged


def reddit_top_titles(st: dict) -> list[dict]:
    """The highest-scoring posts per subreddit and week: what each community was talking about, for the story list."""
    best: dict[tuple, list] = defaultdict(list)
    for p, m in reddit_files(st):
        if m["kind"] != "posts":
            continue
        days = set(m["days"])
        for d in iter_reddit(p):
            if d.get("removed_by_category") or d.get("over_18") or d.get("stickied"):
                continue
            try:
                when = dt.datetime.fromtimestamp(int(float(d.get("created_utc") or 0)), dt.timezone.utc).date()
            except (TypeError, ValueError, OverflowError):
                continue
            if when.isoformat() not in days:
                continue
            week = (when - dt.timedelta(days=when.weekday())).isoformat()
            best[(m["sub"], week)].append((int(d.get("score") or 0), d.get("title") or "", when.isoformat()))
    out = []
    for (sub, _), posts in best.items():
        for score, title, when in sorted(posts, reverse=True)[:st["reddit_top_posts"]]:
            if len(title.split()) >= 3:
                out.append({"sub": sub, "title": title[:200], "date": when, "score": score})
    return out


# ---------------------------------------------------------------- passes C and D: receipts
RECEIPT_RUBRIC = """You look for "receipts" for a neutral public archive of HasanAbi's (Hasan Piker's) streams: pairs of
moments, on different days, where his own stated positions on the same question genuinely conflict. People from his
own community and from communities critical of him both read these, so be strict and fair. A wrong receipt is far
worse than a missed one.

Count a pair only when
- both are his own view (not reading, quoting, a bit or sarcasm), on the SAME question, and
- a fair viewer would agree they cannot both be his position at once.
Not a receipt: a change that follows new facts or events (say what changed), nuance or a caveat added, different
subjects that only sound alike, conditional statements ("if X then Y"), tone changes, or holding two compatible views.
Also not a receipt:
- public-facing conduct set against a view. A streamer moderating his chat or keeping to platform rules (banning calls
  for violence, refusing to endorse something on stream) does not contradict what he thinks; he can hold a private
  view he cannot say on stream. Only compare things he SAID as his view with other things he said as his view.
- his analysis of someone else's decisions (a politician, a government) set against his own position. Describing,
  explaining or praising what another person did is commentary on them, not a stance of his own.

kind: reversal (he moved from one position to the other), contradiction (they clash without a clear direction), or
double_standard (the same act judged oppositely depending on who did it).
question: the shared question in neutral words. strength 1-5 (5 = plainly opposite). Return an empty list if none."""

RECEIPT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["pairs"],
    "properties": {"pairs": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["a", "b", "kind", "question", "strength", "why"],
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"},
                       "kind": {"type": "string", "enum": ["reversal", "contradiction", "double_standard"]},
                       "question": {"type": "string"}, "strength": {"type": "integer"}, "why": {"type": "string"}}}}}}

RVERIFY_RUBRIC = """You double-check a possible "receipt" for a neutral public archive of HasanAbi's (Hasan Piker's)
streams: two moments, on different days, where his stated positions may conflict. You get the transcript around
each quote. A person will approve or reject it after you, but your check comes first: when in doubt, reject.

Confirm only if, read in context, both are his own sincere view (not reading something out, quoting, steelmanning, a
joke or a bit), both answer the same question, and they genuinely conflict. Reject if either side is really him
running his channel (moderating chat, banning someone, keeping to Twitch rules or the law on stream) or his analysis
of what someone else decided; neither is a position of his own. Reject if the context shows he was
quoting, joking or answering a different question, or if the later view follows new facts he cites (then say so).
Machine transcripts mishear: reject if a key line looks garbled.

If confirmed, write it for readers who do not know him: title (neutral, at most 80 characters, no mockery, e.g.
"On whether to vote for Harris"), then_said and now_said (what he said each time, one or two plain sentences each),
and context: anything fair readers should know (what happened in between, caveats he gave). Never call it a lie."""

RVERIFY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["confirmed", "why", "title", "then_said", "now_said", "context", "confidence"],
    "properties": {"confirmed": {"type": "boolean"}, "why": {"type": "string"}, "title": {"type": "string"},
                   "then_said": {"type": "string"}, "now_said": {"type": "string"}, "context": {"type": "string"},
                   "confidence": {"type": "string", "enum": ["low", "medium", "high"]}}}


def story_of(state: dict, e: dict) -> str:
    return state["assigned"].get(label_key("stream", e["label"]), "none")


def positions_by_story(state: dict, entries: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        sid = story_of(state, e)
        if sid == "none":
            continue
        for p in e["positions"]:
            out[sid].append({**p, "video_id": e["video_id"], "date": e["date"],
                             "pid": reel.h(e["video_id"], round(p["t"]), p["quote"])[:12]})
    return out


def receipt_requests(st: dict, state: dict, entries: list[dict]) -> list[tuple]:
    out = []
    for sid, ps in positions_by_story(state, entries).items():
        ps = sorted({p["pid"]: p for p in ps}.values(), key=lambda p: (p["date"], p["t"]))
        dates = sorted({p["date"] for p in ps})
        if len(ps) < st["receipt_min_positions"] or len(dates) < 2 or \
                (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days < st["receipt_min_days"]:
            continue
        if len(ps) > 80:                     # an even spread across the whole period
            ps = [ps[int(i * len(ps) / 80)] for i in range(80)]
        s = state["stories"][sid]
        lines = "\n".join(f"{i} | {p['date']} | {p['position']} | \"{p['quote']}\"" for i, p in enumerate(ps))
        user = f"Story: {s['title']} ({s['summary']})\n\nHis stated positions (n | date | position | his words):\n{lines}"
        out.append((reel.h("receipts", VERSION, st["model"], RECEIPT_RUBRIC, user), RECEIPT_RUBRIC, user, RECEIPT_SCHEMA,
                    {"story": sid, "positions": ps}))
    return out


def context_text(con, vid: str, t: float, span: float) -> str:
    rows = [dict(r) for r in con.execute("SELECT start, end, speaker, text FROM turns WHERE video_id=? AND start BETWEEN ? AND ? "
                                         "ORDER BY start", (vid, t - span, t + span))]
    return part_text(rows)


def rverify_request(st: dict, con, story: dict, pa: dict, pb: dict, pair: dict) -> tuple:
    a_ctx = context_text(con, pa["video_id"], pa["t"], st["receipt_context_s"])
    b_ctx = context_text(con, pb["video_id"], pb["t"], st["receipt_context_s"])
    user = (f"Story: {story['title']}\nQuestion: {pair['question']}\nWhy it was suggested: {pair['why']}\n\n"
            f"FIRST, {pa['date']}: \"{pa['quote']}\"\n{a_ctx}\n\nSECOND, {pb['date']}: \"{pb['quote']}\"\n{b_ctx}")
    return (reel.h("rverify", VERSION, st["model"], RVERIFY_RUBRIC, user), RVERIFY_RUBRIC, user, RVERIFY_SCHEMA)


def queue_path(cfg) -> Path:
    return work_dir(cfg) / "receipts.json"


def run_receipts(cfg, st: dict, con, entries: list[dict]) -> str | None:
    state = stories_state(cfg)
    reqs = receipt_requests(st, state, entries)
    _, why = ask(cfg, st, "receipts", [r[:4] for r in reqs], st["receipts_max_requests"])
    queue = load_json(queue_path(cfg), {})
    pending = []
    for r in reqs:
        got = answer(cfg, "receipts", r[0])
        if not got:
            continue
        ps, sid = r[4]["positions"], r[4]["story"]
        for pair in got.get("pairs") or []:
            a, b = pair.get("a"), pair.get("b")
            if not (isinstance(a, int) and isinstance(b, int) and 0 <= a < len(ps) and 0 <= b < len(ps)) or a == b:
                continue
            if int(pair.get("strength") or 0) < 3:
                continue
            pa, pb = sorted((ps[a], ps[b]), key=lambda p: (p["date"], p["t"]))
            if pa["date"] == pb["date"]:
                continue
            rid = reel.h("receipt", pa["pid"], pb["pid"])[:12]
            if rid in queue:
                continue
            pending.append((rid, sid, pa, pb, pair, rverify_request(st, con, state["stories"][sid], pa, pb, pair)))
    _, why2 = ask(cfg, st, "rverify", [p[5] for p in pending], st["rverify_max_requests"])
    for rid, sid, pa, pb, pair, req in pending:
        got = answer(cfg, "rverify", req[0])
        if got is None:
            continue
        side = lambda p: {k: p[k] for k in ("video_id", "date", "t", "quote", "position")}
        queue[rid] = {"id": rid, "story": sid, "story_title": state["stories"][sid]["title"], "kind": pair["kind"],
                      "question": pair["question"], "strength": pair["strength"], "a": side(pa), "b": side(pb),
                      "confirmed": bool(got.get("confirmed")), "check": got.get("why", ""),
                      "title": got.get("title", "")[:100], "then_said": got.get("then_said", ""),
                      "now_said": got.get("now_said", ""), "context": got.get("context", ""),
                      "confidence": got.get("confidence", "low"),
                      "status": "pending" if got.get("confirmed") else "rejected_by_check",
                      "found": dt.date.today().isoformat()}
    save_json(queue_path(cfg), queue)
    return why or why2


def decide(cfg, rid: str, decision: str, note: str = "") -> dict:
    """The owner's call on a receipt: approved ones are published, rejected ones never are."""
    if decision not in ("approved", "rejected", "pending"):
        raise ValueError("decision must be approved, rejected or pending")
    queue = load_json(queue_path(cfg), {})
    if rid not in queue:
        raise KeyError(rid)
    queue[rid].update(status=decision, note=note[:500], decided=dt.datetime.now().isoformat(timespec="seconds"))
    save_json(queue_path(cfg), queue)
    publish_receipts(cfg, queue)
    return queue[rid]


# ---------------------------------------------------------------- Chat vs Hasan
PUSHBACK = re.compile(
    r"\b(wrong|not true|that'?s false|false|no shot|bad take|l take|terrible take|cringe|disagree|hypocrit\w*|copium|"
    r"delusional|lying|liar|lies|fact ?check|source\?|clueless|yikes|bro what|hello\?+|what\?{2,}|huh\?+|"
    r"ratio|embarrassing|dishonest|misinfo\w*|cap)\b|\?{3,}", re.I)

# Identity-politics language in chat, and chat going after people outside the left
IDPOL = re.compile(
    r"\b(woke|problematic|transphob\w*|homophob\w*|racis[tm]\w*|misogyn\w*|ableis[tm]\w*|sexis[tm]\w*|bigot\w*|cis|"
    r"privilege[d]?|pronouns?|white (?:people|men|women|guys?)|colonizers?|settlers?|terfs?|islamophob\w*|"
    r"anti-?black|whiteness|microaggression\w*|harmful|yikes|cancel+ed|apologi[sz]e|do better)\b", re.I)
OUTSIDER_CHAT = re.compile(
    r"\b(libs?|liberals?|neolibs?|shitlibs?|centrists?|never ?trump\w*|republicans?|ex-?republicans?|grift\w*|sell-?outs?|"
    r"neocons?|warmongers?|war criminals?|fascists?|chuds?|rightoids?|bulwark|traitors?|ghouls?|sold out|platforming|"
    r"why (?:is|are) (?:he|you|they) (?:here|on)|dont trust|don't trust|cringe|gross|ew+)\b", re.I)
DEFAULT_OUTSIDERS = ["Bulwark", "Tim Miller", "Sarah Longwell", "JVL", "Jonathan V. Last", "Charlie Sykes", "Never Trump",
                     "Never Trumpers", "Liz Cheney", "Adam Kinzinger", "Pakman", "Ezra Klein", "Douthat", "centrists",
                     "moderates", "liberals", "Democratic establishment"]

CHATVS_RUBRIC = """You review moments from HasanAbi's (Hasan Piker's) Twitch streams for a neutral public archive's
"Chat vs Hasan" section: political moments where his own chat and he were not on the same page. You get the
transcript leading up to the moment and a sample of the chat during it (usernames removed).

Decide what happened. The archive mostly wants the first two:
- id_politics: chat is dug into identity politics (calling something or someone problematic, racist, transphobic,
  "do better", demands to apologise, purity tests) and Hasan wants nothing to do with it: he brushes it off, argues
  against it, or steers back to class or material politics.
- courting_outsider: Hasan is engaging or warming to someone outside the left that he sees as a possible ally (for
  example The Bulwark's Tim Miller or Sarah Longwell, ex-Republicans, Never Trumpers, centrists, liberal pundits or
  Democrats), while chat attacks that person or him for platforming them.
- pushback: a real share of chat disagreed with, doubted or mocked a political point HE made.
- split: chat was clearly divided about a political point he made.
- other: anything else. Video games (GTA and the like), entertainment, food, his life, hype, spam, donations, raids,
  mods sweeping spam, chat agreeing with him (including backing him when he bans or scolds a chatter), or chat
  reacting to a video rather than to him.
keep = true only for the first four, when a reader would find the moment worth a look.

If kept: title (neutral, at most 80 characters), who (for courting_outsider: the person or group he was engaging;
otherwise empty), hasan_said (what he said or argued, one or two plain sentences), chat_said (what chat said back,
one or two sentences, describing the mix fairly), quote (his key words, copied exactly from one line labelled
Hasan; empty if none fits), examples (up to 4 chat messages copied exactly from the sample, the clearest ones; never
slurs, threats or personal details), clip_start and clip_end (seconds, from line timestamps)."""

CHATVS_KINDS = ["id_politics", "courting_outsider", "pushback", "split", "other"]
CHATVS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["keep", "kind", "title", "who", "hasan_said", "chat_said", "quote", "examples", "clip_start", "clip_end"],
    "properties": {"keep": {"type": "boolean"}, "kind": {"type": "string", "enum": CHATVS_KINDS},
                   "title": {"type": "string"}, "who": {"type": "string"}, "hasan_said": {"type": "string"},
                   "chat_said": {"type": "string"}, "quote": {"type": "string"},
                   "examples": {"type": "array", "items": {"type": "string"}},
                   "clip_start": {"type": "number"}, "clip_end": {"type": "number"}}}


def outsider_rx(st: dict):
    names = [n for n in (st.get("outsiders") or DEFAULT_OUTSIDERS) if len(n.strip()) >= 3]
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?!\w)", re.I)


def off_topic_spans(entries: list[dict]) -> dict[str, list[tuple[float, float]]]:
    """Per stream, the stretches the topic index says were games or his own life (not politics)."""
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for e in entries:
        if e["kind"] in ("games", "personal_or_stream", "sports", "culture_and_entertainment"):
            out[e["video_id"]].append((e["start"], e["start"] + max(e["minutes"], 1) * 60))
    return out


def chat_candidates(cfg, st: dict, con, entries: list[dict] | None = None) -> list[dict]:
    """Minutes worth a look, per stream, cached:
    - outsider: Hasan is talking about someone outside the left (st['outsiders']) and chat turns hostile;
    - idpol: identity-politics language in chat far above that stream's usual;
    - pushback: pushback words far above that stream's usual.
    Stretches the topic index marks as games or his personal life are skipped."""
    cache = load_json(work_dir(cfg) / "chatvs_minutes.json", {})
    orx = outsider_rx(st)
    skip = off_topic_spans(entries or [])
    out = []
    for v in con.execute("SELECT id, upload_date FROM videos ORDER BY upload_date DESC"):
        vid = v["id"]
        sig = "v2:" + str(con.execute("SELECT count(*) || ':' || coalesce(max(id), 0) FROM chat WHERE video_id=?", (vid,)).fetchone()[0])
        if cache.get(vid, {}).get("sig") != sig:
            mins: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])      # msgs, pushback, deleted, idpol, outsider
            for t, text, deleted, kind in con.execute("SELECT t, text, deleted, kind FROM chat WHERE video_id=?", (vid,)):
                if kind != "msg":
                    continue
                m = mins[int(t // 60)]
                m[0] += 1
                m[2] += 1 if deleted else 0
                if not deleted and text:
                    m[1] += 1 if PUSHBACK.search(text) else 0
                    m[3] += 1 if IDPOL.search(text) else 0
                    m[4] += 1 if OUTSIDER_CHAT.search(text) else 0
            cache[vid] = {"sig": sig, "minutes": {str(k): x for k, x in mins.items()}}
        mins = {int(k): x for k, x in cache[vid]["minutes"].items()}
        if not mins:
            continue
        talk = {int(r[0] // 60) for r in con.execute("SELECT start, text FROM turns WHERE video_id=? AND speaker='Hasan'", (vid,))
                if orx.search(r[1] or "")}
        near_talk = {k + d for k in talk for d in range(-1, 4)}
        busy = [x for x in mins.values() if x[0] >= 20]
        med = lambda i: (sorted(x[i] / x[0] for x in busy) or [0])[len(busy) // 2] if busy else 0
        base = {"push": med(1), "idpol": med(3), "out": med(4)}
        spans = skip.get(vid, [])
        found = []
        for k, (n, push, deleted, idp, outs) in mins.items():
            if n < 20 or any(a <= k * 60 <= b for a, b in spans):
                continue
            row = {"video_id": vid, "date": day(v["upload_date"]), "minute": k, "msgs": n, "pushback": push,
                   "share": round(push / n, 3), "base": round(base["push"], 3), "deleted": deleted,
                   "idpol": idp, "outsider": outs}
            hostile = (outs + push) / n
            if k in near_talk and outs + push >= 8 and hostile >= max(2 * (base["out"] + base["push"]), 0.08):
                found.append({**row, "why": "outsider", "score": 10 + hostile / max(base["out"] + base["push"], 0.01)})
            elif idp >= 8 and idp / n >= max(st["chatvs_ratio"] * base["idpol"], 0.05):
                found.append({**row, "why": "idpol", "score": 5 + (idp / n) / max(base["idpol"], 0.01)})
            elif push >= st["chatvs_min_pushback"] and push / n >= max(base["push"] * st["chatvs_ratio"], 0.06):
                found.append({**row, "why": "pushback", "score": (push / n) / max(base["push"], 0.01)})
        found.sort(key=lambda c: -c["score"])
        picked = []
        for c in found:                      # one per 5 minutes; the strongest few per stream
            if all(abs(c["minute"] - p["minute"]) > 5 for p in picked):
                picked.append(c)
            if len(picked) >= st["chatvs_per_stream"]:
                break
        out += picked
    save_json(work_dir(cfg) / "chatvs_minutes.json", cache)
    return sorted(out, key=lambda c: -c["score"])      # outsider moments first, then identity politics, then pushback


def chatvs_request(st: dict, con, c: dict) -> tuple:
    lo, hi = c["minute"] * 60, c["minute"] * 60 + 60
    turns = [dict(r) for r in con.execute("SELECT start, end, speaker, text FROM turns WHERE video_id=? AND start BETWEEN ? AND ? "
                                          "ORDER BY start", (c["video_id"], lo - 240, hi + 30))]
    chat = [dict(r) for r in con.execute(
        "SELECT t, author, text FROM chat WHERE video_id=? AND t BETWEEN ? AND ? AND kind='msg' AND NOT deleted "
        "AND coalesce(text, '') != '' ORDER BY t, id", (c["video_id"], lo - 20, hi + 40))]
    if len(chat) > 250:
        chat = [chat[int(i * len(chat) / 250)] for i in range(250)]
    lines = "\n".join(f"[{m['t']:.0f}s] {m['text']}" for m in chat)
    user = (f"Stream date {c['date']}. Chat in this minute: {c['msgs']} messages, {c['pushback']} with pushback words "
            f"({c['share']:.0%} of messages; this stream's usual is {c['base']:.0%}), {c.get('idpol', 0)} with "
            f"identity-politics words, {c.get('outsider', 0)} attacking outsiders; {c['deleted']} deleted by mods. "
            f"Flagged because: {c['why']}.\n\n"
            f"TRANSCRIPT\n{part_text(turns)}\n\nCHAT SAMPLE\n{lines}")
    return (reel.h("chatvs", VERSION, st["model"], CHATVS_RUBRIC, user), CHATVS_RUBRIC, user, CHATVS_SCHEMA,
            {"chat": chat, "turns": turns})          # the rubric is in the key: a stricter rubric re-judges every moment


def chatvs_items(cfg, st: dict, con, cands: list[dict], run_claude: bool = True) -> tuple[list[dict], str | None]:
    reqs = [(c, chatvs_request(st, con, c)) for c in cands]
    why = None
    if run_claude:
        _, why = ask(cfg, st, "chatvs", [r[:4] for _, r in reqs], st["chatvs_max_requests"])
    import chatflags
    flagged = flagged_fps(cfg)
    out = []
    for c, r in reqs:
        got = answer(cfg, "chatvs", r[0])
        if not got or not got.get("keep") or got.get("kind") not in CHATVS_KINDS[:4]:
            continue
        by_text: dict[str, list[dict]] = defaultdict(list)
        for m in r[4]["chat"]:
            by_text[m["text"].strip()].append(m)
        examples = [e.strip() for e in got.get("examples") or [] if e.strip() in by_text and clean_example(e) and
                    not any(chatflags.fingerprint(c["video_id"], m["author"], m["text"]) in flagged for m in by_text[e.strip()])][:4]
        line = find_line(got.get("quote", ""), r[4]["turns"])
        lo = c["minute"] * 60
        start = float(got.get("clip_start") or lo - 60)
        start = min(max(start, lo - 300), lo + 60)
        out.append({"id": reel.h("chatvs", c["video_id"], c["minute"])[:10], "video_id": c["video_id"], "date": c["date"],
                    "kind": got["kind"], "title": got.get("title", "")[:100], "who": str(got.get("who") or "")[:80],
                    "hasan_said": got.get("hasan_said", ""),
                    "chat_said": got.get("chat_said", ""), "quote": got["quote"].strip() if line else "",
                    "quote_t": line["start"] if line else None, "examples": examples, "start": start, "minute": c["minute"],
                    "stats": {k: c.get(k, 0) for k in ("msgs", "pushback", "share", "base", "deleted", "idpol", "outsider")}})
    return out, why


def clean_example(text: str) -> bool:
    import chatflags
    return 2 <= len(text) <= 200 and not chatflags.HATE_LEXICON.search(text) and "http" not in text and "@" not in text


def flagged_fps(cfg) -> set[str]:
    """Fingerprints of chat lines the chat check flagged (sure or not): never shown as examples or in the quiz."""
    import chatflags
    p = chatflags.db_path(cfg)
    if not p.exists():
        return set()
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        rows = con.execute("SELECT fp FROM flags").fetchall()
        con.close()
    except sqlite3.Error:
        return set()
    return {r[0] for r in rows}


def single_run(cfg):
    """A process-long lock so the nightly timer and a hand-started run never overlap. None when another run holds it."""
    f = open(work_dir(cfg) / "run.lock", "w")
    try:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        return f
    except OSError:
        f.close()
        return None
    return f


# ---------------------------------------------------------------- publish
def publish_receipts(cfg, queue: dict | None = None) -> int:
    queue = queue if queue is not None else load_json(queue_path(cfg), {})
    items = [{k: r[k] for k in ("id", "story", "story_title", "kind", "question", "a", "b", "title", "then_said",
                                "now_said", "context")} for r in queue.values() if r.get("status") == "approved"]
    items.sort(key=lambda r: r["b"]["date"], reverse=True)
    save_json(out_dir(cfg) / "receipts.json", {"generated": dt.datetime.now().isoformat(timespec="seconds"), "receipts": items})
    return len(items)


def stream_days(con) -> dict[str, float]:
    """Hours streamed per day (the archive's streams), so 'he did not talk about it' only counts days he was live."""
    out: dict[str, float] = defaultdict(float)
    for r in con.execute("SELECT upload_date, duration FROM videos"):
        out[day(r["upload_date"])] += (r["duration"] or 0) / 3600
    return dict(out)


POLITICAL = ("news_politics", "israel_palestine", "war_and_foreign_policy")
COMMUNITY = ("Hasan_Piker", "LivestreamFail", "Destiny")      # his own subreddit and the two that follow him most


def build_stories(cfg, st: dict, con, entries: list[dict], reddit: dict | None) -> tuple[list[dict], dict]:
    state = stories_state(cfg)
    live = stream_days(con)
    orx, oby = orbit_matcher(st)
    said = hasan_mentions(con, state["stories"])
    by_story: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        sid = story_of(state, e)
        if sid != "none" and sid in state["stories"]:
            by_story[sid].append(e)
    covered: dict[str, set] = defaultdict(set)      # day -> subreddits with data
    totals = reddit["totals"] if reddit else {}
    for sub, days in totals.items():
        for d in days:
            covered[d].add(sub)
    summaries, details = [], {}
    for sid, s in state["stories"].items():
        es = by_story.get(sid, [])
        mins: Counter = Counter()
        for e in es:
            mins[e["date"]] += e["minutes"]
        rs = (reddit or {}).get("stories", {}).get(sid) if reddit else None
        rday: Counter = Counter()
        by_sub: dict[str, int] = {}
        share_sub: dict[str, float] = {}
        if rs:
            for sub, days in rs["days"].items():
                by_sub[sub] = sum(days.values())
                for d, k in days.items():
                    rday[d] += k
        reddit_n = sum(by_sub.values())
        hasan_min = round(sum(mins.values()), 1)
        # the story itself only: a name in some Reddit title that matched its keywords is not enough
        # (a r/dsa post that mentions Dolly Parton does not make her death a DSA story)
        blob = " \n ".join([s["title"], s["summary"], *s["keywords"], *(e["label"] for e in es)])
        who: dict[str, list[str]] = {}
        if orx is not None:
            for m in orx.finditer(blob):
                o = oby[m.group(0).lower()]
                if o["name"] not in who.setdefault(o["group"], []):
                    who[o["group"]].append(o["name"])
        floor = st["orbit_story_min_reddit"] if who.get("orbit") or who.get("proximity") else st["story_min_reddit"]
        if hasan_min < st["story_min_minutes"] and reddit_n < floor:
            continue
        days_all = sorted(set(mins) | set(rday))
        first, last = days_all[0], days_all[-1]
        # How much of each community's talk it was: mentions per 1,000 posts and comments over the story's window
        # (first to last day it came up anywhere), counting only days that subreddit has data for
        window = lambda sub: sum(n for d, n in totals.get(sub, {}).items() if first <= d <= last)
        for sub in totals:
            den = window(sub)
            if den:
                share_sub[sub] = by_sub.get(sub, 0) / den
        groups = {}
        for g, subs in GROUPS.items():
            den = sum(window(sub) for sub in subs if sub in totals)
            if den:
                num = sum(by_sub.get(sub, 0) for sub in subs)
                groups[g] = {"mentions": num, "per_1000": round(1000 * num / den, 2)}
        # Badges compare like with like: only days that have both Reddit data and a stream
        both = [d for d in days_all if d in covered and d in live]
        hasan_on_both = sum(mins[d] for d in both)
        reddit_on_both = sum(rday[d] for d in both)
        mentions = said.get(sid, [])
        said_on_both = sum(1 for m in mentions if m[0] in both)
        community = {sub: by_sub[sub] for sub in COMMUNITY if by_sub.get(sub)}
        # under 3 minutes and at most 2 passing mentions on the days compared, and never a story he covered at length
        # some other time (the Reddit data has gaps; a story he spent hours on is not a blindspot)
        quiet = hasan_on_both < 3 and said_on_both <= 2 and hasan_min < 10 and len(mentions) <= 6
        badge = None
        if who.get("orbit") and quiet and both and reddit_on_both >= st["orbit_min_reddit"]:
            badge = "close_to_home"
        elif who.get("proximity") and quiet and both and reddit_on_both >= st["orbit_min_reddit"]:
            badge = "proximity_gap"
        elif reddit_on_both >= 200 and quiet and len(both) >= 3 and s["kind"] in POLITICAL:
            badge = "blindspot"           # only political and world stories: a celebrity death he skipped tells nobody anything
        elif hasan_on_both >= 60 and reddit_on_both < 30 and len(both) >= 2:
            badge = "stream_focus"
        tags = state.get("tags", {}).get(sid)
        quotes = sorted((dict(q, video_id=e["video_id"], date=e["date"]) for e in es for q in e["quotables"]),
                        key=lambda q: q["date"], reverse=True)
        positions = sorted((dict(p, video_id=e["video_id"], date=e["date"]) for e in es for p in e["positions"]),
                           key=lambda p: (p["date"], p["t"]))
        top = sorted((rs or {}).get("top", []), key=lambda x: -x["score"])
        tops, seen = [], set()
        for x in top:
            if x["url"] not in seen and clean_example(x["title"][:200]):
                seen.add(x["url"])
                tops.append({**x, "group": GROUP_OF.get(x["sub"].lower(), "Other")})
            if len(tops) >= 12:
                break
        summaries.append({"id": sid, "title": s["title"], "kind": s["kind"], "summary": s["summary"], "first": first,
                          "last": last, "hasan_minutes": hasan_min, "streams": len({e["video_id"] for e in es}),
                          "hasan_days": {d: round(m, 1) for d, m in sorted(mins.items())},
                          "reddit_mentions": reddit_n, "groups": groups, "badge": badge,
                          "quote": quotes[0] if quotes else None, "who": who, "said": len(mentions),
                          "community": community, "tags": tags,
                          "on_both": {"days": len(both), "hasan_minutes": round(hasan_on_both, 1), "said": said_on_both,
                                      "reddit": reddit_on_both}})
        details[sid] = {"id": sid, "title": s["title"], "kind": s["kind"], "summary": s["summary"],
                        "keywords": s["keywords"], "hasan_days": {d: round(m, 1) for d, m in sorted(mins.items())},
                        "reddit_days": dict(sorted(rday.items())), "by_sub": dict(sorted(by_sub.items(), key=lambda x: -x[1])),
                        "per_1000_sub": {k: round(1000 * v, 2) for k, v in sorted(share_sub.items(), key=lambda x: -x[1])},
                        "groups": groups, "badge": badge, "who": who, "community": community, "tags": tags,
                        "said": [{"date": m[0], "video_id": m[1], "t": m[2], "text": m[3]} for m in mentions[-25:]],
                        "said_total": len(mentions),
                        "segments": sorted(({"video_id": e["video_id"], "date": e["date"], "t": e["start"],
                                             "minutes": e["minutes"], "label": e["label"]} for e in es),
                                           key=lambda x: (x["date"], x["t"])),
                        "quotes": quotes[:12], "positions": positions[-30:], "reddit_top": tops,
                        "all_positions": positions}
    summaries.sort(key=lambda s: (s["last"], s["hasan_minutes"] + s["reddit_mentions"] / 20), reverse=True)
    return summaries, details


def build_quiz(cfg, stories: list[dict], details: dict, chatvs: list[dict], receipts: list[dict]) -> dict:
    who, more, first = [], [], []
    for sid, d in details.items():
        for q in d["quotes"][:3]:
            who.append({"text": q["quote"], "answer": "hasan", "date": q["date"], "story": d["title"],
                        "link": f"watch.html?v={q['video_id']}&t={int(q['t'])}"})
        for x in d["reddit_top"][:2]:
            if 6 <= len(x["title"].split()) <= 40:
                who.append({"text": x["title"], "answer": "reddit", "date": x["date"], "story": d["title"],
                            "source": f"r/{x['sub']}", "link": x["url"]})
    for c in chatvs:
        for e in c["examples"]:
            if len(e.split()) >= 5:
                who.append({"text": e, "answer": "chat", "date": c["date"], "story": c["title"],
                            "link": f"watch.html?v={c['video_id']}&t={int(c['start'])}"})
    big = [s for s in stories if s["hasan_minutes"] >= 10]
    big.sort(key=lambda s: s["hasan_minutes"])
    for i in range(len(big)):
        for j in range(i + 1, len(big)):
            a, b = big[i], big[j]
            if b["hasan_minutes"] >= 2 * a["hasan_minutes"] and len(more) < 400:
                more.append({"a": {"id": a["id"], "title": a["title"], "minutes": a["hasan_minutes"]},
                             "b": {"id": b["id"], "title": b["title"], "minutes": b["hasan_minutes"]}})
    for r in receipts:
        first.append({"id": r["id"], "title": r["title"], "a": r["a"], "b": r["b"]})
    return {"who": who, "more": more, "first": first}


def attention(stories: list[dict]) -> dict:
    """Selective attention: for each type of event, how many stories with each side as the actor he covered (3+
    minutes, or said its keywords 5+ times), and his total minutes. Counts, never just percentages."""
    out: dict[str, dict] = {}
    for s in stories:
        t = s.get("tags") or {}
        if not t.get("world_event") or t.get("event_type") == "other" or t.get("actor_bloc") not in BLOCS:
            continue
        cell = out.setdefault(t["event_type"], {}).setdefault(t["actor_bloc"], {"stories": 0, "covered": 0, "minutes": 0.0, "ids": []})
        cell["stories"] += 1
        cell["covered"] += 1 if s["hasan_minutes"] >= 3 or s["said"] >= 5 else 0
        cell["minutes"] = round(cell["minutes"] + s["hasan_minutes"], 1)
        cell["ids"].append(s["id"])
    return out


def publish(cfg, st: dict, con, entries: list[dict], reddit: dict | None, chatvs: list[dict]) -> dict:
    od = out_dir(cfg)
    stories, details = build_stories(cfg, st, con, entries, reddit)
    sd = od / "stories"
    sd.mkdir(exist_ok=True)
    for sid, d in details.items():
        save_json(sd / f"{sid}.json", {k: v for k, v in d.items() if k != "all_positions"})
    for p in sd.glob("*.json"):
        if p.stem not in details:
            p.unlink()
    weeks: dict[str, Counter] = defaultdict(Counter)
    for s in stories:
        for d, m in s["hasan_days"].items():
            wk = dt.date.fromisoformat(d)
            weeks[(wk - dt.timedelta(days=(wk.weekday() + 1) % 7)).isoformat()][s["id"]] += m
    timeline = [{"week": w, "top": [{"id": sid, "minutes": round(m, 1)} for sid, m in c.most_common(8)]}
                for w, c in sorted(weeks.items())]
    covered = sorted({d for days in (reddit or {}).get("totals", {}).values() for d in days})
    now = dt.datetime.now().isoformat(timespec="seconds")
    save_json(od / "stories.json", {"generated": now, "stories": stories, "timeline": timeline,
                                    "reddit_days": covered, "groups": GROUPS, "stream_days": stream_days(con),
                                    "attention": attention(stories), "blocs": BLOCS,
                                    "orbit": [{k: o[k] for k in ("name", "group", "tie")} for o in orbit_list(st)]})
    # "Where he stood": every position he stated on a story, in order (documented, not judged; no approval needed:
    # each line is his exact words with a link, and the one-sentence summary is labelled as Claude's)
    timelines = []
    for sid, d in details.items():
        ps = d["all_positions"]
        if len(ps) >= 3 and len({p["date"] for p in ps}) >= 2:
            timelines.append({"story": sid, "title": d["title"], "kind": d["kind"], "first": ps[0]["date"],
                              "last": ps[-1]["date"], "positions": ps[-40:]})
    timelines.sort(key=lambda t: (t["last"], len(t["positions"])), reverse=True)
    save_json(od / "timelines.json", {"generated": now, "timelines": timelines})
    chatvs = sorted(chatvs, key=lambda c: c["date"], reverse=True)
    save_json(od / "chatvs.json", {"generated": now, "moments": chatvs})
    n = publish_receipts(cfg)
    receipts = load_json(od / "receipts.json", {}).get("receipts", [])
    save_json(od / "quiz.json", {"generated": now, **build_quiz(cfg, stories, details, chatvs, receipts)})
    return {"stories": len(stories), "chatvs": len(chatvs), "receipts": n}


# ---------------------------------------------------------------- run
def run(cfg, steps: set[str] | None = None) -> dict:
    st = settings(cfg)
    lock = single_run(cfg)
    if lock is None:
        return {"stopped": "busy: another features run is going"}
    con = reel.public_db(cfg)
    report, limit = {}, None
    try:
        steps = steps or {"topics", "stories", "reddit", "receipts", "chatvs", "publish"}
        if "topics" in steps:
            reqs = topic_requests(st, con)
            n, limit = ask(cfg, st, "topics", [r[:4] for r in reqs], st["topics_max_requests"])
            report["topics"] = {"answered": n, "parts": len(reqs),
                                "done": sum(1 for r in reqs if reel.usable(reel.cache_get(cfg, "topics", r[0])))}
        entries = topic_entries(cfg, st, con)
        titles = reddit_top_titles(st) if ("stories" in steps and not limit) else []
        if "stories" in steps and not limit:
            n, limit = run_stories(cfg, st, entries, titles, st["stories_max_requests"])
            report["stories"] = {"batches": n, "stories": len(stories_state(cfg)["stories"])}
        if "stories" in steps and not limit:
            n, limit = run_tags(cfg, st, st["tags_max_requests"])
            report["tags"] = {"batches": n}
        reddit = scan_reddit(cfg, st, stories_state(cfg)["stories"]) if "reddit" in steps or "publish" in steps else None
        if "receipts" in steps and not limit:
            limit = run_receipts(cfg, st, con, entries)
            q = load_json(queue_path(cfg), {})
            report["receipts"] = Counter(r["status"] for r in q.values())
        cands = chat_candidates(cfg, st, con, entries) if ("chatvs" in steps or "publish" in steps) else []
        chatvs, why = chatvs_items(cfg, st, con, cands, run_claude="chatvs" in steps and not limit)
        limit = limit or why
        report["chatvs"] = {"candidates": len(cands), "kept": len(chatvs)}
        if "publish" in steps:
            report["published"] = publish(cfg, st, con, entries, reddit, chatvs)
    finally:
        con.close()
        lock.close()
    report["stopped"] = "usage limit" if limit else None
    return report


def status(cfg) -> None:
    st = settings(cfg)
    con = reel.public_db(cfg)
    try:
        reqs = topic_requests(st, con)
        done = sum(1 for r in reqs if reel.usable(reel.cache_get(cfg, "topics", r[0])))
        print(f"topics: {done}/{len(reqs)} stream parts indexed")
        state = stories_state(cfg)
        print(f"stories: {len(state['stories'])}, {len(state['assigned'])} labels and Reddit titles placed")
        print(f"reddit files: {len(reddit_files(st))} ({st['reddit_dir'] or 'features.reddit_dir not set'})")
        q = load_json(queue_path(cfg), {})
        print("receipts:", dict(Counter(r["status"] for r in q.values())) or "none yet")
        od = out_dir(cfg)
        for name in ("stories", "chatvs", "receipts", "quiz"):
            p = od / f"{name}.json"
            print(f"published {name}.json: {p.stat().st_size // 1024} KB" if p.exists() else f"published {name}.json: not yet")
    finally:
        con.close()


def review(cfg) -> None:
    q = load_json(queue_path(cfg), {})
    for r in sorted(q.values(), key=lambda r: r["found"]):
        if r["status"] != "pending":
            continue
        print(f"\n[{r['id']}] {r['title']}  ({r['kind']}, check confidence {r['confidence']})")
        print(f"  {r['a']['date']}: \"{r['a']['quote']}\"\n  {r['b']['date']}: \"{r['b']['quote']}\"")
        print(f"  then: {r['then_said']}\n  now:  {r['now_said']}\n  context: {r['context']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("run")
    for name in ("topics", "stories", "reddit", "chatvs", "publish"):
        sub.add_parser(name)
    rp = sub.add_parser("receipts")
    rp.add_argument("action", nargs="?", default="find", choices=["find", "review", "approve", "reject", "reset"])
    rp.add_argument("ids", nargs="*")
    rp.add_argument("--note", default="")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    if a.cmd == "status":
        return status(cfg)
    if a.cmd == "receipts" and a.action != "find":
        if a.action == "review":
            return review(cfg)
        for rid in a.ids:
            r = decide(cfg, rid, {"approve": "approved", "reject": "rejected", "reset": "pending"}[a.action], a.note)
            print(rid, r["status"], r["title"])
        return
    steps = {"run": None, "topics": {"topics"}, "stories": {"stories"}, "reddit": {"reddit"},
             "receipts": {"receipts"}, "chatvs": {"chatvs"}, "publish": {"publish"}}[a.cmd]
    print(json.dumps(run(cfg, steps), indent=1, default=dict))


if __name__ == "__main__":
    sys.exit(main())
