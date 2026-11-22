"""Weekly reel: the week's most egregious things Hasan said, picked and explained by Claude, with clips and chat.

  reel.py auto            run from the nightly/hourly job: collect finished batches, build or refresh the latest
                          finished week (Sunday-Saturday) once it is due; cheap no-op otherwise
  reel.py build --week YYYY-MM-DD [--yes] [--wait]
                          one week by its Sunday; without --yes it only prints the cost estimate
  reel.py status          batches in flight and the reels on disk

Two passes, both through the Message Batches API (half price):
  1. screen: every transcript line of the week's streams, in ~12-minute windows (all speakers labelled, so Claude can
     tell Hasan from clips and guests), returns candidate moments where Hasan himself may have said something
     egregious;
  2. judge: the strongest candidates, each with 6 minutes of transcript and the chat at that time, get a final call:
     own view or reading/joke/condemnation, severity on a fixed rubric, context and nuance, the chat's reaction, and
     clip boundaries.
Results are cached per stream window and per candidate, so a late transcript only costs its own stream.
Reads public.db only (public speaker labels; chat without opt-outs or mod-deleted text) and writes reels/<week>.json,
which the site serves on reel.html.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from common import P, get_logger, load_config, make_dirs
from rhetoric import api_client, norm, read_jsonl

log = get_logger("reel")
DEFAULTS = {"model": "claude-opus-5-5", "screen_effort": "low", "judge_effort": "high",
            "window_s": 720, "overlap_s": 60, "screen_min_severity": 5, "max_judge": 30, "max_clips": 10,
            "min_severity": 5, "context_s": 180, "chat_max": 400, "clip_min_s": 15, "clip_max_s": 150,
            "build_after_hour": 10,          # local hour on Sunday after which the finished week is built
            "max_usd_per_week": 15.0,        # auto never submits more than this per week (estimate)
            "prices": {"claude-opus-5-5": [4.0, 20.0], "claude-sonnet-5-5": [2.0, 10.0]}}
CATEGORIES = ["antisemitism", "nazi_or_holocaust_comparison", "justifying_or_glorifying_violence",
              "dehumanization_or_bigotry", "targeting_individuals", "conspiracy", "anti_american",
              "misinformation", "other"]
STANCES = ["own_view", "joke_or_bait", "reading_or_quoting", "condemning", "unclear"]
WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

RUBRIC = """You review machine transcripts of HasanAbi's (Hasan Piker's) Twitch streams for a weekly reel of the most
egregious things HE said that week. The reel is read by people on all sides, so it must be fair, accurate and
consistent. Your job is judgement with nuance, not keyword matching.

Whose words count
- Only Hasan's own words. Lines are labelled: "Hasan" (his voice), "Guest", "Media" (videos and clips he plays),
  "Auto-captions" (older streams; speaker unknown, treat as Hasan only when the content makes it unmistakable).
- Hasan constantly reads articles, tweets, donations and chat messages aloud. Reading something is NOT his view,
  unless he then endorses it. Watch for cues: "X says", "this guy wrote", "look at this", quoting critics, mocking.
- Condemning or mocking a view is the opposite of holding it.

What is NOT egregious by itself
- Profanity, insults of politicians, harsh but mainstream political criticism.
- Criticism of Israeli or US government policy, including calling the war in Gaza a genocide (a position held by
  many scholars and rights groups). It becomes notable when it crosses into the categories below.

Categories (apply equally whatever group is targeted: Jews, Israelis, Muslims, Arabs, Americans, LGBTQ people,
any ethnicity or religion)
- antisemitism: tropes about Jews (control, dual loyalty applied to Jews, blood libel), holding Jews collectively
  responsible for Israel. Note where IHRA and the Jerusalem Declaration differ.
- nazi_or_holocaust_comparison: equating Israel, Zionists, Jews or other groups with Nazis; Holocaust distortion.
- justifying_or_glorifying_violence: praising or justifying attacks on civilians, terror, or armed groups; calls
  for or celebration of violence or death, including "jokes" that celebrate it.
- dehumanization_or_bigotry: describing a group as subhuman, vermin, inherently evil; slurs.
- targeting_individuals: accusing a named person of being a foreign agent, criminal or worse without evidence;
  directing an audience at a private person.
- conspiracy: unfounded conspiracy claims stated as fact.
- anti_american: contempt for America or Americans as such (not criticism of policy).
- misinformation: clearly false factual claims stated as fact.

Severity 1-10 (for what he actually said, in context)
- 1-3: harsh, crude or one-sided, but within mainstream political argument.
- 4-5: inflammatory; many people across the spectrum would find it offensive.
- 6-7: what major news outlets or civil-rights organisations would call extremist, bigoted or justifying violence.
- 8-10: explicit endorsement of violence against civilians, slurs, denial of atrocities, calls to harm people.
A joke lowers severity only when the target of the joke is clearly not the people harmed; a "joke" celebrating
violence or using a slur still counts. Mitigating context he gives (caveats, condemnations) lowers it.

Accuracy
- Transcripts are machine-made and can mishear words. If a key line looks garbled or the speaker label looks wrong,
  say so and lower your confidence; do not build a case on a likely mishearing.
- Quotes must be copied exactly from the lines you are given."""

SCREEN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["moments"],
    "properties": {"moments": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["start", "end", "stance", "category", "severity", "quote", "why"],
        "properties": {
            "start": {"type": "number", "description": "seconds, from the timestamp of the first line of the moment"},
            "end": {"type": "number", "description": "seconds, from the timestamp of the last line of the moment"},
            "stance": {"type": "string", "enum": STANCES},
            "category": {"type": "string", "enum": CATEGORIES},
            "severity": {"type": "integer", "minimum": 1, "maximum": 10},
            "quote": {"type": "string", "description": "exact words from one line"},
            "why": {"type": "string"}}}}}}

JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["keep", "stance", "severity", "confidence", "categories", "title", "summary", "why_notable",
                 "nuance", "chat_reaction", "chat_examples", "quote", "clip_start", "clip_end"],
    "properties": {
        "keep": {"type": "boolean", "description": "true if this belongs in a reel of his most egregious statements"},
        "stance": {"type": "string", "enum": STANCES},
        "severity": {"type": "integer", "minimum": 1, "maximum": 10},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}},
        "title": {"type": "string", "description": "neutral, factual, at most 80 characters, no editorialising"},
        "summary": {"type": "string", "description": "what he said, 1-2 sentences"},
        "why_notable": {"type": "string", "description": "why it ranks, tied to the rubric"},
        "nuance": {"type": "string", "description": "context, caveats he gave, what he did not say, transcript doubts"},
        "chat_reaction": {"type": "string", "description": "how chat responded: echoes, escalation, pushback; no usernames"},
        "chat_examples": {"type": "array", "items": {"type": "string"},
                          "description": "up to 4 chat messages copied exactly, the clearest echoes or pushback"},
        "quote": {"type": "string", "description": "his key words, copied exactly from one line"},
        "clip_start": {"type": "number", "description": "seconds; a line timestamp a little before the key words"},
        "clip_end": {"type": "number", "description": "seconds; after he finishes the point"}}}


# ---------------------------------------------------------------- helpers
def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("reel") or {})}


def root(cfg: dict) -> Path:
    return P(cfg, "archive_db").parent / "reel"


def reels_dir(cfg: dict) -> Path:
    paths = cfg.get("paths", {})
    p = Path(paths.get("reels", "reels"))
    return p if p.is_absolute() else Path(cfg["_root"]) / p


def h(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def fmt(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def week_bounds(week: str) -> tuple[str, str]:
    start = dt.date.fromisoformat(week)
    if start.weekday() != 6:
        raise SystemExit(f"{week} is not a Sunday (weeks run Sunday-Saturday)")
    return start.strftime("%Y%m%d"), (start + dt.timedelta(days=6)).strftime("%Y%m%d")


def last_finished_week(now: dt.datetime) -> str:
    """Sunday that starts the most recent Sunday-Saturday week that has fully ended."""
    days_back = (now.weekday() - 5) % 7 or 7          # to the last Saturday before today
    sat = now.date() - dt.timedelta(days=days_back)
    return (sat - dt.timedelta(days=6)).isoformat()


def public_db(cfg: dict) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{P(cfg, 'public_db')}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def week_streams(con, week: str) -> list[dict]:
    lo, hi = week_bounds(week)
    return [dict(r) for r in con.execute(
        "SELECT id, title, upload_date, duration, embed_type, embed_id, source_url FROM videos "
        "WHERE upload_date BETWEEN ? AND ? ORDER BY upload_date, id", (lo, hi))]


def turns_of(con, vid: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT start, end, speaker, text FROM turns WHERE video_id=? ORDER BY start", (vid,))]


def lines_text(turns: list[dict]) -> str:
    return "\n".join(f"[{t['start']:.0f}s {fmt(t['start'])}] {t['speaker']}: {t['text']}" for t in turns)


def windows(turns: list[dict], size: float, overlap: float) -> list[list[dict]]:
    if not turns:
        return []
    out, start, last = [], turns[0]["start"], turns[-1]["start"]
    while start <= last:
        w = [t for t in turns if start <= t["start"] < start + size]
        if w:
            out.append(w)
        start += size - overlap
    return out


def chat_window(con, vid: str, lo: float, hi: float, cap: int) -> list[dict]:
    rows = [dict(r) for r in con.execute(
        "SELECT t, text, kind, deleted FROM chat WHERE video_id=? AND t BETWEEN ? AND ? ORDER BY t, id", (vid, lo, hi))]
    rows = [r for r in rows if r["kind"] == "msg" and not r["deleted"] and (r["text"] or "").strip()]
    if len(rows) > cap:                    # keep the timeline: an even sample
        step = len(rows) / cap
        rows = [rows[int(i * step)] for i in range(cap)]
    return rows


# ---------------------------------------------------------------- cache + batches
def cache_get(cfg, kind: str, key: str):
    p = root(cfg) / kind / f"{key}.json"
    return json.loads(p.read_text()) if p.exists() else None


def cache_put(cfg, kind: str, key: str, value) -> None:
    p = root(cfg) / kind / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    tmp.replace(p)


def in_flight(cfg) -> set[str]:
    return {k for b in read_jsonl(root(cfg) / "batches.jsonl") if not b["collected"] for k in b["keys"]}


def submit(cfg, st: dict, kind: str, reqs: list[tuple[str, str, str, dict]], client) -> str:
    """reqs: (cache key, system, user prompt, schema)."""
    effort = st["screen_effort"] if kind == "screen" else st["judge_effort"]
    requests = [{"custom_id": f"{kind[0]}{i}-{key}", "params": {
        "model": st["model"], "max_tokens": 8192 if kind == "screen" else 16000,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}}}}
        for i, (key, system, user, schema) in enumerate(reqs)]
    batch = client.messages.batches.create(requests=requests)
    rec = {"batch_id": batch.id, "kind": kind, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "custom_ids": [r["custom_id"] for r in requests], "keys": [k for k, *_ in reqs], "collected": False}
    root(cfg).mkdir(parents=True, exist_ok=True)
    with open(root(cfg) / "batches.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    log.info("submitted %s batch %s (%d requests)", kind, batch.id, len(requests))
    return batch.id


def collect(cfg, client, wait: bool = False) -> int:
    path = root(cfg) / "batches.jsonl"
    batches = read_jsonl(path)
    if not any(not b["collected"] for b in batches):
        return 0
    done = 0
    for b in batches:
        if b["collected"]:
            continue
        while True:
            status = client.messages.batches.retrieve(b["batch_id"])
            if status.processing_status == "ended" or not wait:
                break
            time.sleep(60)
        if status.processing_status != "ended":
            continue
        keys = dict(zip(b["custom_ids"], b["keys"]))
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        for res in client.messages.batches.results(b["batch_id"]):
            key = keys[res.custom_id]
            value = {"status": res.result.type}
            if res.result.type == "succeeded":
                msg = res.result.message
                for k in usage:
                    usage[k] += getattr(msg.usage, k, 0) or 0
                if msg.stop_reason == "refusal":
                    value["status"] = "refused"
                elif msg.stop_reason == "max_tokens":
                    value["status"] = "truncated"
                else:
                    text = next((blk.text for blk in msg.content if blk.type == "text"), "")
                    try:
                        value["result"] = json.loads(text)
                    except json.JSONDecodeError:
                        value["status"] = "unparsed"
            if value["status"] != "succeeded":   # errored/expired/refused: cached too, so auto doesn't resubmit hourly
                log.warning("%s %s: %s", b["kind"], key, value["status"])
            cache_put(cfg, b["kind"], key, value)
            done += 1
        b["collected"], b["usage"] = True, usage
        log.info("collected %s batch %s: %s", b["kind"], b["batch_id"], usage)
    with open(path, "w", encoding="utf-8") as f:
        for b in batches:
            f.write(json.dumps(b) + "\n")
    return done


def estimate(st: dict, reqs: list[tuple[str, str, str, dict]], out_per_req: int) -> float:
    p_in, p_out = st["prices"].get(st["model"], [4.0, 20.0])
    if not reqs:
        return 0.0
    sys_tok = len(reqs[0][1]) // 4
    in_tok = sum(len(u) // 4 for _, _, u, _ in reqs) + sys_tok * (1 + 0.1 * len(reqs))
    return 0.5 * (in_tok * p_in + out_per_req * len(reqs) * p_out) / 1e6


# ---------------------------------------------------------------- pass 1: screen
def screen_requests(cfg, st, con, streams) -> tuple[list, list]:
    """-> (requests still needed, cached screen results with their window turns)."""
    need, have, flying = [], [], in_flight(cfg)
    for v in streams:
        turns = turns_of(con, v["id"])
        for w in windows(turns, st["window_s"], st["overlap_s"]):
            text = lines_text(w)
            key = h("screen", 1, st["model"], v["id"], text)
            got = cache_get(cfg, "screen", key)
            if got is not None:
                have.append((v, w, got))
            elif key not in flying:
                user = (f"Stream: {v['title']} ({v['upload_date']}). Transcript window "
                        f"{fmt(w[0]['start'])}-{fmt(w[-1]['end'])}.\n\nList every moment in this window where Hasan "
                        f"himself may have said something egregious under the rubric (severity 4 or more), with the "
                        f"stance you can tell from this window. Most windows have none: return an empty list then.\n\n"
                        f"{text}")
                need.append((key, RUBRIC, user, SCREEN_SCHEMA))
    return need, have


def candidates(st, screened) -> list[dict]:
    out = []
    for v, w, got in screened:
        if got.get("status") != "succeeded":
            continue
        text = norm(" ".join(t["text"] for t in w))
        for m in got["result"]["moments"]:
            if m["severity"] < st["screen_min_severity"] or m["stance"] in ("reading_or_quoting", "condemning"):
                continue
            if m["quote"] and norm(m["quote"]) not in text:
                continue                     # quote not in the window: likely invented, drop
            out.append({**m, "vid": v["id"]})
    out.sort(key=lambda m: (-m["severity"], m["vid"], m["start"]))
    kept = []
    for m in out:                            # one candidate per minute of a stream (windows overlap)
        if any(k["vid"] == m["vid"] and abs(k["start"] - m["start"]) < 60 for k in kept):
            continue
        kept.append(m)
    return kept[: st["max_judge"]]


# ---------------------------------------------------------------- pass 2: judge
def judge_input(st, con, v, m) -> tuple[str, list[dict], list[dict]]:
    turns = [t for t in turns_of(con, v["id"]) if m["start"] - st["context_s"] <= t["start"] <= m["end"] + st["context_s"]]
    chat = chat_window(con, v["id"], m["start"] - 10, m["end"] + 120, st["chat_max"])
    return lines_text(turns), turns, chat


def judge_requests(cfg, st, con, streams, cands) -> tuple[list, list]:
    by_id = {v["id"]: v for v in streams}
    need, have, flying = [], [], in_flight(cfg)
    for m in cands:
        v = by_id[m["vid"]]
        text, turns, chat = judge_input(st, con, v, m)
        chat_text = "\n".join(f"[{c['t']:.0f}s {fmt(c['t'])}] {c['text']}" for c in chat)
        key = h("judge", 1, st["model"], v["id"], m["start"], text, chat_text)
        got = cache_get(cfg, "judge", key)
        if got is not None:
            have.append((v, m, turns, chat, got))
        elif key not in flying:
            user = (f"Stream: {v['title']} ({v['upload_date']}).\nThe first pass flagged this moment: "
                    f"{fmt(m['start'])}-{fmt(m['end'])}, {m['category']}, stance {m['stance']}, severity {m['severity']}: "
                    f"\"{m['quote']}\" ({m['why']}).\n\nMake the final call with the full context below. Decide again "
                    f"whose words they are and whether he means them; score severity on the rubric; write the nuance a "
                    f"fair reader would need; describe the chat's reaction from the chat lines (no usernames); pick "
                    f"clip boundaries from line timestamps that show the point with enough lead-in.\n\n"
                    f"TRANSCRIPT ({fmt(turns[0]['start']) if turns else ''} onward):\n{text}\n\n"
                    f"CHAT AT THE TIME ({len(chat)} messages{', evenly sampled' if len(chat) >= st['chat_max'] else ''}):\n"
                    f"{chat_text or '(no chat)'}")
            need.append((key, RUBRIC, user, JUDGE_SCHEMA))
    return need, have


def clip_from(st, v, m, turns, chat, got) -> dict | None:
    if got.get("status") != "succeeded":
        return None
    r = got["result"]
    if not r["keep"] or r["severity"] < st["min_severity"] or r["stance"] in ("reading_or_quoting", "condemning"):
        return None
    ctx = norm(" ".join(t["text"] for t in turns))
    if r["quote"] and norm(r["quote"]) not in ctx:
        return None                          # never publish a quote that isn't in the transcript
    msgs = {norm(c["text"]) for c in chat}
    examples = [e for e in r["chat_examples"] if norm(e) in msgs][:4]
    lo, hi = turns[0]["start"], turns[-1]["end"]
    a = min(max(r["clip_start"], lo), hi)
    b = min(max(r["clip_end"], a + st["clip_min_s"]), a + st["clip_max_s"], hi + 5)
    lines = [{"t": t["start"], "speaker": t["speaker"], "text": t["text"]} for t in turns if a - 10 <= t["start"] <= b + 5]
    return {"video_id": v["id"], "stream_title": v["title"], "date": v["upload_date"], "embed_type": v["embed_type"],
            "embed_id": v["embed_id"], "source_url": v["source_url"], "start": round(a, 1), "end": round(b, 1),
            "severity": r["severity"], "confidence": r["confidence"], "stance": r["stance"],
            "categories": r["categories"], "title": r["title"], "summary": r["summary"],
            "why_notable": r["why_notable"], "nuance": r["nuance"], "chat_reaction": r["chat_reaction"],
            "chat_examples": examples, "quote": r["quote"], "lines": lines}


# ---------------------------------------------------------------- build
def build(cfg, week: str, client=None, yes: bool = False, wait: bool = False, budget: float | None = None,
          now: dt.datetime | None = None) -> str:
    """Advance one week as far as it can go. Returns 'written', 'waiting', 'estimate', 'over_budget' or 'empty'."""
    st = settings(cfg)
    con = public_db(cfg)
    streams = week_streams(con, week)
    if not streams:
        log.info("week %s: no streams in public.db", week)
        return "empty"
    while True:
        if client is not None:
            collect(cfg, client, wait=False)
        need, screened = screen_requests(cfg, st, con, streams)
        if need:
            usd = estimate(st, need, 400)
            print(f"week {week}: screening {len(need)} transcript windows with {st['model']}: about ${usd:.2f}")
            if not yes:
                return "estimate"
            if budget is not None and usd > budget:
                log.error("week %s: screening estimate $%.2f is over the $%.2f budget; not submitted", week, usd, budget)
                return "over_budget"
            submit(cfg, st, "screen", need, client or (client := api_client()))
        if need or _screen_pending(cfg, st, con, streams):
            if not wait:
                return "waiting"
            collect(cfg, client, wait=True)
            continue
        cands = candidates(st, screened)
        jneed, judged = judge_requests(cfg, st, con, streams, cands)
        if jneed:
            usd = estimate(st, jneed, 1500)
            print(f"week {week}: judging {len(jneed)} candidate moments: about ${usd:.2f}")
            if not yes:
                return "estimate"
            if budget is not None and usd > budget:
                log.error("week %s: judging estimate $%.2f is over the $%.2f budget; not submitted", week, usd, budget)
                return "over_budget"
            submit(cfg, st, "judge", jneed, client or (client := api_client()))
        if jneed or _judge_pending(cfg, st, con, streams, cands):
            if not wait:
                return "waiting"
            collect(cfg, client, wait=True)
            continue
        break
    clips = [c for c in (clip_from(st, *x) for x in judged) if c]
    clips.sort(key=lambda c: (-c["severity"], {"high": 0, "medium": 1, "low": 2}[c["confidence"]], c["date"], c["start"]))
    clips = clips[: st["max_clips"]]
    lo, hi = week_bounds(week)
    out = {"week": week, "week_end": (dt.date.fromisoformat(week) + dt.timedelta(days=6)).isoformat(),
           "generated_at": (now or dt.datetime.now()).isoformat(timespec="seconds"), "model": st["model"],
           "streams": [{"video_id": v["id"], "title": v["title"], "date": v["upload_date"]} for v in streams],
           "fingerprint": fingerprint(con, streams), "candidates_judged": len(judged), "clips": clips,
           "method": "Claude read every transcript line of the week's streams, flagged moments where Hasan himself "
                     "may have said something egregious, then re-judged the strongest with 6 minutes of context and "
                     "the chat at the time, against a fixed rubric (own words only; readings, condemnations and "
                     "mainstream criticism excluded)."}
    d = reels_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{week}.json.tmp"
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    tmp.replace(d / f"{week}.json")
    print(f"week {week}: wrote {len(clips)} clip(s) from {len(judged)} judged candidate(s) -> {d / (week + '.json')}")
    return "written"


def _screen_pending(cfg, st, con, streams) -> bool:
    flying = in_flight(cfg)
    if not flying:
        return False
    for v in streams:
        for w in windows(turns_of(con, v["id"]), st["window_s"], st["overlap_s"]):
            if h("screen", 1, st["model"], v["id"], lines_text(w)) in flying:
                return True
    return False


def _judge_pending(cfg, st, con, streams, cands) -> bool:
    flying = in_flight(cfg)
    if not flying:
        return False
    by_id = {v["id"]: v for v in streams}
    for m in cands:
        text, _, chat = judge_input(st, con, by_id[m["vid"]], m)
        chat_text = "\n".join(f"[{c['t']:.0f}s {fmt(c['t'])}] {c['text']}" for c in chat)
        if h("judge", 1, st["model"], m["vid"], m["start"], text, chat_text) in flying:
            return True
    return False


def fingerprint(con, streams) -> str:
    return h([(v["id"], con.execute("SELECT COUNT(*), COALESCE(MAX(end),0) FROM turns WHERE video_id=?",
                                    (v["id"],)).fetchone()[:]) for v in streams])


def auto(cfg, client=None, now: dt.datetime | None = None) -> str:
    """Hourly: build the latest finished week once it is due, and refresh it when its streams change."""
    st = settings(cfg)
    now = now or dt.datetime.now()
    week = last_finished_week(now)
    week_end = dt.date.fromisoformat(week) + dt.timedelta(days=6)
    due = dt.datetime.combine(week_end + dt.timedelta(days=1), dt.time(st["build_after_hour"]))
    pending = bool(in_flight(cfg))
    if now < due and not pending:
        return "not_due"
    path = reels_dir(cfg) / f"{week}.json"
    if path.exists() and not pending:
        con = public_db(cfg)
        old = json.loads(path.read_text())
        if old.get("fingerprint") == fingerprint(con, week_streams(con, week)):
            return "up_to_date"
    return build(cfg, week, client=client or api_client(), yes=True, budget=st["max_usd_per_week"], now=now)


def status(cfg) -> None:
    for b in read_jsonl(root(cfg) / "batches.jsonl")[-10:]:
        print(f"{b['created']} {b['kind']:6} {b['batch_id']} {len(b['keys'])} requests "
              f"{'collected' if b['collected'] else 'IN FLIGHT'} {b.get('usage', '')}")
    for p in sorted(reels_dir(cfg).glob("*.json")):
        r = json.loads(p.read_text())
        print(f"reel {r['week']}..{r['week_end']}: {len(r['clips'])} clips, {len(r['streams'])} streams, "
              f"generated {r['generated_at']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["auto", "build", "status"])
    ap.add_argument("--week", help="build: the Sunday that starts the week, YYYY-MM-DD (default: last finished week)")
    ap.add_argument("--yes", action="store_true", help="build: submit the batches (otherwise only estimate)")
    ap.add_argument("--wait", action="store_true", help="build: wait for the batches instead of returning")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    if a.cmd == "status":
        status(cfg)
        return 0
    if a.cmd == "auto":
        print("reel:", auto(cfg))
        return 0
    week = a.week or last_finished_week(dt.datetime.now())
    if not WEEK_RE.match(week):
        sys.exit("--week must be YYYY-MM-DD")
    client = api_client() if a.yes else None
    print("reel:", build(cfg, week, client=client, yes=a.yes, wait=a.wait))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
