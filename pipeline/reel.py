"""Weekly reel: the week's most egregious things Hasan said, picked and explained by Claude, with clips and chat.

  reel.py auto            run from the nightly/hourly job: collect finished batches, build or refresh the latest
                          finished week (Sunday-Saturday) once it is due; cheap no-op otherwise
  reel.py weekly          the subscription run (reel-weekly.timer): the latest week, then one earlier week, every
                          Claude call through `claude -p` on the owner's Claude login (no API bill)
  reel.py build --week YYYY-MM-DD [--yes] [--wait]
                          one week by its Sunday; without --yes it only prints what it would send
  reel.py status          batches in flight and the reels on disk

Three passes, through `claude -p` on the owner's Claude subscription (backend "subscription", the default) or, only
when the config sets reel.backend: api, through the Message Batches API (half price):
  1. screen: every transcript line of the week's streams, in ~12-minute windows (all speakers labelled, so Claude can
     tell Hasan from clips and guests), returns candidate moments where Hasan himself may have said something
     egregious;
  2. judge: the strongest candidates, each with 20 minutes of transcript and the chat at that time, get a final call:
     own view or reading/joke/condemnation, severity on a fixed rubric, context and nuance, the chat's reaction, and
     a segment as long as the point needs (15 seconds to 15 minutes);
  3. fact-check: pass 1 also lists the checkable factual claims he states as fact; the most consequential are checked
     with web search, as of the day he said them, and get a verdict with sources.
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
from rhetoric import api_client, read_jsonl

log = get_logger("reel")
DEFAULTS = {"model": "claude-opus-5-5", "screen_effort": "low", "judge_effort": "high",
            "window_s": 720, "overlap_s": 60, "screen_min_severity": 5, "max_judge": 30, "max_clips": 10,
            "min_severity": 5, "context_s": 600, "chat_max": 400, "clip_min_s": 15, "clip_max_s": 900,
            "fact_effort": "medium", "max_facts": 40, "fact_min_importance": 3, "fact_search_uses": 5,
            "fact_context_s": 90, "search_usd": 0.01,   # web search: $10 per 1,000 searches
            "build_after_hour": 10,          # local hour on Sunday after which the finished week is built
            "max_usd_per_week": 15.0,        # auto never submits more than this per week (estimate)
            "prices": {"claude-opus-5-5": [4.0, 20.0], "claude-sonnet-5-5": [2.0, 10.0]},
            # "subscription" (default): every Claude call goes through `claude -p` on the owner's Claude login, run by
            # `reel.py weekly` (deploy/laptop/user/reel-weekly.timer), never billed to the API. "api": the paid
            # Message Batches API, from the hourly `reel.py auto`; only when the config asks for it.
            "backend": "subscription", "claude_bin": "claude", "cli_workers": 2, "cli_timeout_s": 1200,
            "cli_backfill_per_run": 1,      # earlier weeks filled in per weekly run, so usage is spread out
            "chat_effort": "low", "chat_per_request": 400, "verify_effort": "high"}   # chat check: messages per request
CATEGORIES = ["antisemitism", "nazi_or_holocaust_comparison", "justifying_or_glorifying_violence",
              "dehumanization_or_bigotry", "targeting_individuals", "conspiracy", "anti_american",
              "misinformation", "other"]
STANCES = ["own_view", "joke_or_bait", "reading_or_quoting", "condemning", "unclear"]
WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERSION = 6          # bump when selection logic changes: auto rebuilds reels made by an older version (from cache)
_NONWORD = re.compile(r"[^a-z0-9]+")


def norm(s: str) -> str:
    """Words only: lowercase, punctuation and quote marks dropped. Claude often fixes punctuation or apostrophes when it
    quotes; the words must still match exactly."""
    return " " + " ".join(_NONWORD.sub(" ", (s or "").lower().replace("\u2019", "'").replace("'", "")).split()) + " "


def quoted(q: str, text_norm: str) -> bool:
    q = norm(q)
    return q.strip() != "" and q in text_norm
TOPICS = ["israel_palestine", "us_politics", "elections_and_polls", "economy", "war_and_foreign_policy",
          "other_countries", "media_and_public_figures", "himself_and_his_critics", "history", "science_and_health",
          "other"]
VERDICTS = ["accurate", "mostly_accurate", "misleading", "false", "unsupported", "not_checkable"]

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
    "type": "object", "additionalProperties": False, "required": ["moments", "claims"],
    "properties": {"moments": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["start", "end", "stance", "category", "severity", "quote", "why"],
        "properties": {
            "start": {"type": "number", "description": "seconds, from the timestamp of the first line of the moment"},
            "end": {"type": "number", "description": "seconds, from the timestamp of the last line of the moment"},
            "stance": {"type": "string", "enum": STANCES},
            "category": {"type": "string", "enum": CATEGORIES},
            "severity": {"type": "integer", "description": "1 to 10 on the rubric"},
            "quote": {"type": "string", "description": "exact words from one line"},
            "why": {"type": "string"}}}},
        "claims": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["start", "quote", "claim", "topic", "importance"],
            "properties": {
                "start": {"type": "number", "description": "seconds, from the line's timestamp"},
                "quote": {"type": "string", "description": "exact words from one Hasan line"},
                "claim": {"type": "string", "description": "the factual claim, restated neutrally and self-contained"},
                "topic": {"type": "string", "enum": TOPICS},
                "importance": {"type": "integer",
                               "description": "1 to 5: 5 = a central factual claim about events, numbers or people; 1 = trivial"}}}}}}

JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["keep", "stance", "severity", "confidence", "categories", "title", "summary", "why_notable",
                 "nuance", "chat_reaction", "chat_examples", "quote", "clip_start", "clip_end", "segment_reason"],
    "properties": {
        "keep": {"type": "boolean", "description": "true if this belongs in a reel of his most egregious statements"},
        "stance": {"type": "string", "enum": STANCES},
        "severity": {"type": "integer", "description": "1 to 10 on the rubric"},
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
        "clip_start": {"type": "number", "description": "seconds; a line timestamp where the segment should start"},
        "clip_end": {"type": "number", "description": "seconds; where it should end"},
        "segment_reason": {"type": "string", "description": "why this length: one line, a short exchange, or a whole argument"}}}

FACT_RUBRIC = """You fact-check factual claims HasanAbi (Hasan Piker) made on his Twitch stream, for a weekly breakdown
read by people on all sides. Be fair, precise and sourced.

- Judge the claim as of the date he said it, with the information available then. Note later developments
  separately.
- Check the claim he actually made, read charitably: the plain meaning a reasonable listener would take, not a
  stretched one. Rounding and loose wording are fine if the substance holds.
- Use web search. Prefer primary sources (official data, court records, original reporting, the person's own words)
  and reputable outlets across the spectrum. Do not rely on a single partisan source.
- Opinions, value judgements, predictions, rhetorical hyperbole and jokes are not factual claims: mark them
  not_checkable.
- You cannot know whether he believed it. Never call a claim a lie; assess only accuracy.
- The transcript is machine-made and may mishear words. If the claim depends on a likely mishearing, say so and mark
  it not_checkable.

Verdicts: accurate | mostly_accurate (true with minor errors) | misleading (technically true or partly true but gives
a false impression, or omits context that changes the meaning) | false | unsupported (no good evidence either way, or
the evidence is contested) | not_checkable.

Finish your answer with one JSON object and nothing after it:
{"verdict": "...", "claim": "the claim as you understood it", "correction": "what is accurate, in one or two
sentences (empty if accurate)", "explanation": "how you checked and what you found", "confidence": "low|medium|high",
"sources": [{"url": "...", "title": "..."}]}
Use only URLs that your searches returned."""


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


RETRY = ("errored", "expired", "truncated", "paused", "unparsed")


def usable(got) -> bool:
    """A cached answer counts unless it is a retryable failure on its first attempt (refusals are never retried)."""
    return got is not None and not (got.get("status") in RETRY and got.get("attempts", 1) < 2)


def in_flight(cfg) -> set[str]:
    return {k for b in read_jsonl(root(cfg) / "batches.jsonl") if not b["collected"] for k in b["keys"]}


def submit(cfg, st: dict, kind: str, reqs: list[tuple[str, str, str, dict | None]], client) -> str:
    """reqs: (cache key, system, user prompt, JSON schema or None)."""
    effort = st[f"{kind}_effort"]
    requests = []
    for i, (key, system, user, schema) in enumerate(reqs):
        params = {"model": st["model"], "max_tokens": 16000,
                  "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                  "messages": [{"role": "user", "content": user}], "output_config": {"effort": effort}}
        if schema is not None:
            params["output_config"]["format"] = {"type": "json_schema", "schema": schema}
        if kind == "fact":   # web search; citations can't be combined with a JSON schema, so the answer ends in JSON
            params["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": st["fact_search_uses"]}]
        requests.append({"custom_id": f"{kind[0]}{i}-{key}", "params": params})
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
                elif msg.stop_reason in ("max_tokens", "pause_turn"):
                    value["status"] = "truncated" if msg.stop_reason == "max_tokens" else "paused"
                elif b["kind"] == "fact":
                    text = "".join(blk.text for blk in msg.content if blk.type == "text")
                    urls = []
                    for blk in msg.content:          # URLs its searches actually returned
                        if blk.type == "web_search_tool_result" and isinstance(blk.content, list):
                            urls += [getattr(x, "url", None) for x in blk.content]
                    value["result"], value["urls"] = last_json(text), [u for u in urls if u]
                    if value["result"] is None:
                        value["status"] = "unparsed"
                else:
                    text = next((blk.text for blk in msg.content if blk.type == "text"), "")
                    try:
                        value["result"] = json.loads(text)
                    except json.JSONDecodeError:
                        value["status"] = "unparsed"
            if value["status"] != "succeeded":   # cached too: retried once if retryable, never resubmitted hourly
                prev = cache_get(cfg, b["kind"], key) or {}
                value["attempts"] = prev.get("attempts", 0) + 1
                log.warning("%s %s: %s (attempt %d)", b["kind"], key, value["status"], value["attempts"])
            cache_put(cfg, b["kind"], key, tidy(value))
            done += 1
        b["collected"], b["usage"] = True, usage
        log.info("collected %s batch %s: %s", b["kind"], b["batch_id"], usage)
    with open(path, "w", encoding="utf-8") as f:
        for b in batches:
            f.write(json.dumps(b) + "\n")
    return done


def _clamp(x, lo: int, hi: int):
    return max(lo, min(hi, int(x))) if isinstance(x, (int, float)) and not isinstance(x, bool) else lo


def tidy(value: dict) -> dict:
    """Structured outputs can't carry integer minimum/maximum, so the scales are held here: severity 1-10,
    importance 1-5."""
    r = value.get("result")
    if not isinstance(r, dict):
        return value
    for m in r.get("moments", []) if isinstance(r.get("moments"), list) else []:
        if isinstance(m, dict) and "severity" in m:
            m["severity"] = _clamp(m["severity"], 1, 10)
    for c in r.get("claims", []) if isinstance(r.get("claims"), list) else []:
        if isinstance(c, dict) and "importance" in c:
            c["importance"] = _clamp(c["importance"], 1, 5)
    if "severity" in r and "keep" in r:
        r["severity"] = _clamp(r["severity"], 1, 10)
    return value


# ---------------------------------------------------------------- subscription backend (claude -p)
class UsageLimit(Exception):
    """The Claude login hit its usage limit (or is logged out): stop, keep what is cached, resume next run."""


URL_RE = re.compile(r"https?://[^\s\"'<>\]\)\\]+")
LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|out of extra usage|not logged in|/login|"
                      r"invalid api key|authentication", re.I)


def cli_env() -> dict:
    """The environment for `claude -p`, without any API key: with one set, Claude Code would bill the API instead of
    the owner's subscription (rhetoric.api_client loads .env into os.environ)."""
    import os
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                        "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_MODEL")}
    env["PATH"] = str(Path.home() / ".local" / "bin") + ":" + env.get("PATH", "/usr/bin:/bin")
    return env


def cli_args(st: dict, kind: str, system: str, schema: dict | None) -> list[str]:
    effort = st[f"{kind}_effort"]      # screen, judge, fact, chat, verify here; features.py adds its own kinds
    args = [st["claude_bin"], "-p", "--model", st["model"], "--effort", effort,
            "--output-format", "stream-json", "--verbose", "--no-session-persistence", "--strict-mcp-config",
            "--disable-slash-commands", "--setting-sources", "", "--system-prompt", system]
    if kind == "fact":   # web search only; the answer ends in JSON (as with the API)
        args += ["--tools", "WebSearch", "--allowedTools", "WebSearch", "--max-turns", str(st["fact_search_uses"] + 3)]
    else:
        args += ["--tools", ""]
    if schema is not None:
        args += ["--json-schema", json.dumps(schema)]
    return args


def cli_value(kind: str, lines: list[str], schema: dict | None) -> dict:
    """The cache value for one `claude -p` run, in the same shape `collect` writes for a batch result."""
    final, urls = None, []
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            final = ev
        elif ev.get("type") == "user":      # tool results: the URLs the web searches actually returned
            for blk in (ev.get("message") or {}).get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    urls += URL_RE.findall(json.dumps(blk.get("content"), ensure_ascii=False))
    if final is None:
        return {"status": "errored"}
    text = str(final.get("result") or "")
    if final.get("is_error") or final.get("subtype") not in ("success", None):
        if LIMIT_RE.search(text) or final.get("api_error_status") in (401, 403, 429):
            raise UsageLimit(text[:200] or str(final.get("api_error_status")))
        return {"status": "truncated" if "max_turns" in str(final.get("subtype")) else "errored"}
    if final.get("stop_reason") == "refusal":
        return {"status": "refused"}
    value = {"status": "succeeded"}
    if kind == "fact":
        value["result"], value["urls"] = last_json(text), list(dict.fromkeys(u.rstrip(".,;") for u in urls))
    elif schema is not None and isinstance(final.get("structured_output"), dict):
        value["result"] = final["structured_output"]
    else:
        try:
            value["result"] = json.loads(text)
        except json.JSONDecodeError:
            value["result"] = None
    if value["result"] is None:
        value["status"] = "unparsed"
    return value


def run_cli(cfg, st: dict, kind: str, reqs: list[tuple[str, str, str, dict | None]]) -> int:
    """Answer requests one `claude -p` call each (a few at a time) and cache them like batch results.
    Raises UsageLimit when the login is out of usage; everything answered so far stays cached."""
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    env = cli_env()

    def one(req) -> tuple[str, dict]:
        key, system, user, schema = req
        try:
            p = subprocess.run(cli_args(st, kind, system, schema), input=user, capture_output=True, text=True,
                               env=env, cwd="/tmp", timeout=st["cli_timeout_s"])
        except subprocess.TimeoutExpired:
            return key, {"status": "errored"}
        if p.returncode != 0 and not p.stdout.strip():
            if LIMIT_RE.search(p.stderr):
                raise UsageLimit(p.stderr.strip()[:200])
            log.warning("%s %s: claude exited %d: %s", kind, key, p.returncode, p.stderr.strip()[:200])
            return key, {"status": "errored"}
        return key, cli_value(kind, p.stdout.splitlines(), schema)

    done, limit = 0, None
    with ThreadPoolExecutor(max_workers=max(1, st["cli_workers"])) as pool:
        futures = [pool.submit(one, r) for r in reqs]
        for fut in futures:
            if fut.cancelled():                    # dropped after a usage limit; asked again next run
                continue
            try:
                key, value = fut.result()
            except UsageLimit as e:
                limit = limit or e
                for f in futures:
                    f.cancel()
                continue
            if value["status"] != "succeeded":
                prev = cache_get(cfg, kind, key) or {}
                value["attempts"] = prev.get("attempts", 0) + 1
                log.warning("%s %s: %s (attempt %d)", kind, key, value["status"], value["attempts"])
            cache_put(cfg, kind, key, tidy(value))
            done += 1
    log.info("subscription: answered %d/%d %s requests", done, len(reqs), kind)
    if limit:
        raise limit
    return done


def estimate(st: dict, reqs: list[tuple[str, str, str, dict | None]], out_per_req: int, extra_in: int = 0,
             extra_usd: float = 0.0) -> float:
    p_in, p_out = st["prices"].get(st["model"], [4.0, 20.0])
    if not reqs:
        return 0.0
    sys_tok = len(reqs[0][1]) // 4
    in_tok = sum(len(u) // 4 + extra_in for _, _, u, _ in reqs) + sys_tok * (1 + 0.1 * len(reqs))
    return 0.5 * (in_tok * p_in + out_per_req * len(reqs) * p_out) / 1e6 + extra_usd * len(reqs)


# ---------------------------------------------------------------- pass 1: screen
def screen_requests(cfg, st, con, streams) -> tuple[list, list, bool]:
    """-> (requests still needed, cached results with their window turns, whether some are still in flight)."""
    need, have, flying, waiting = [], [], in_flight(cfg), False
    for v in streams:
        turns = turns_of(con, v["id"])
        for w in windows(turns, st["window_s"], st["overlap_s"]):
            text = lines_text(w)
            key = h("screen", 2, st["model"], v["id"], text)
            got = cache_get(cfg, "screen", key)
            if usable(got):
                have.append((v, w, got))
            elif key in flying:
                waiting = True
            else:
                user = (f"Stream: {v['title']} ({v['upload_date']}). Transcript window "
                        f"{fmt(w[0]['start'])}-{fmt(w[-1]['end'])}.\n\n"
                        f"1. moments: every moment in this window where Hasan himself may have said something egregious "
                        f"under the rubric (severity 4 or more), with the stance you can tell from this window. Most "
                        f"windows have none: return an empty list then.\n"
                        f"2. claims: the checkable factual claims Hasan himself states as fact in this window (events, "
                        f"numbers, what someone said or did, history, science). Not opinions, predictions, hyperbole, "
                        f"jokes, or things he is reading aloud. Rate how consequential each is. Often there are none.\n\n"
                        f"{text}")
                need.append((key, RUBRIC, user, SCREEN_SCHEMA))
    return need, have, waiting


def candidates(st, screened) -> list[dict]:
    out = []
    for v, w, got in screened:
        if got.get("status") != "succeeded":
            continue
        text = norm(" ".join(t["text"] for t in w))
        for m in got["result"]["moments"]:
            if m["severity"] < st["screen_min_severity"] or m["stance"] in ("reading_or_quoting", "condemning"):
                continue
            if m["quote"] and not quoted(m["quote"], text):
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
    chat = chat_window(con, v["id"], m["start"] - 120, m["end"] + 480, st["chat_max"])
    return lines_text(turns), turns, chat


def judge_requests(cfg, st, con, streams, cands) -> tuple[list, list, bool]:
    by_id = {v["id"]: v for v in streams}
    need, have, flying, waiting = [], [], in_flight(cfg), False
    for m in cands:
        v = by_id[m["vid"]]
        text, turns, chat = judge_input(st, con, v, m)
        chat_text = "\n".join(f"[{c['t']:.0f}s {fmt(c['t'])}] {c['text']}" for c in chat)
        key = h("judge", 2, st["model"], v["id"], m["start"], text, chat_text)
        got = cache_get(cfg, "judge", key)
        if usable(got):
            have.append((v, m, turns, chat, got))
        elif key in flying:
            waiting = True
        else:
            user = (f"Stream: {v['title']} ({v['upload_date']}).\nThe first pass flagged this moment: "
                    f"{fmt(m['start'])}-{fmt(m['end'])}, {m['category']}, stance {m['stance']}, severity {m['severity']}: "
                    f"\"{m['quote']}\" ({m['why']}).\n\nMake the final call with the full context below. Decide again "
                    f"whose words they are and whether he means them; score severity on the rubric; write the nuance a "
                    f"fair reader would need; describe the chat's reaction from the chat lines (no usernames).\n"
                    f"Then choose the segment for the reel from line timestamps. Make it as long as the point needs and "
                    f"no longer: 15-30 seconds for a one-liner, a few minutes for an exchange, up to 15 minutes when he "
                    f"builds an argument and the full run is what a viewer needs to judge it fairly. Include enough "
                    f"lead-in to show what he is responding to, and the caveats he gives if any.\n\n"
                    f"TRANSCRIPT ({fmt(turns[0]['start']) if turns else ''} onward):\n{text}\n\n"
                    f"CHAT AT THE TIME ({len(chat)} messages{', evenly sampled' if len(chat) >= st['chat_max'] else ''}):\n"
                    f"{chat_text or '(no chat)'}")
            need.append((key, RUBRIC, user, JUDGE_SCHEMA))
    return need, have, waiting


STOP = set("""about after again also and any are because been before being but can could did does doing dont down
from going gonna have having here just know like make more most much need never not now only other over really right
said same should some still such than that the their them then there these they thing think this those very want was
were what when where which while who will with would yeah you your youre theyre thats whats""".split())


EMOTES = set("""kekw kekl lul omegalul pog pogu poggers pogchamp icant cooked based toobased yep nope nodders aware monka
monkas monkaw monkaomega sadge peepohappy kappa lol lmao lmfao rofl xd catjam hmm hmmm classic noted copium hopium clueless
hesright fricc ripbozo letsgo gigachad chad smorc dnmsit glorpslam wokege glorp ez widepeepohappy pepega pepelaugh
omegaluliguess kekwait susge shrug libbers bedge waiting""".split())
_MENTION_EMOTE = re.compile(r"@\w+|\bhas[A-Z]\w*")


def burst_key(text: str) -> str:
    """Message text reduced to its real words: @mentions, the channel's has* emotes and common Twitch emotes dropped.
    A copy-paste burst needs 3+ such words, so emote spam and '@someone lol' don't count."""
    words = [w for w in norm(_MENTION_EMOTE.sub(" ", text or "")).split() if w not in EMOTES and len(w) >= 2]
    return " ".join(words) if len(words) >= 3 else ""


def chat_metrics(con, vid: str, start: float, end: float, quote: str, lines: list[dict]) -> dict:
    """How chat responded around a clip, from public chat (counts only; no usernames leave this function).
    rate_*: messages per minute 5 min before / during / 5 min after; echo: share of messages using his distinctive
    words (from the quote and his lines in the clip) vs the 5 minutes before; first_msg share vs the whole stream;
    bursts: identical 3+-word messages from 5+ different accounts within 60 s (copy-paste / coordinated posting)."""
    rows = [dict(r) for r in con.execute(
        "SELECT t, author, text, first_msg FROM chat WHERE video_id=? AND kind='msg' AND deleted=0 AND t BETWEEN ? AND ? "
        "ORDER BY t", (vid, start - 300, end + 300))]
    words = set()
    for t in [quote] + [l["text"] for l in lines if l["speaker"] in ("Hasan", "Auto-captions")]:
        words |= {w for w in norm(t).split() if len(w) >= 5 and w not in STOP}
    before = [r for r in rows if r["t"] < start]
    during = [r for r in rows if start <= r["t"] <= end]
    after = [r for r in rows if r["t"] > end]
    resp = during + after
    rate = lambda rs, secs: round(len(rs) / max(secs, 1) * 60, 1)
    echo = lambda rs: (sum(1 for r in rs if words & set(norm(r["text"]).split())) / len(rs)) if rs and words else 0.0
    base_first = con.execute("SELECT AVG(COALESCE(first_msg,0)) FROM chat WHERE video_id=? AND kind='msg'", (vid,)).fetchone()[0] or 0
    first = (sum(1 for r in resp if r["first_msg"]) / len(resp)) if resp else 0.0
    groups: dict[str, list[dict]] = {}
    for r in resp:
        key = burst_key(r["text"])
        if key:
            groups.setdefault(key, []).append(r)
    bursts = []
    for key, rs in groups.items():
        rs.sort(key=lambda r: r["t"])
        i = 0
        for j in range(len(rs)):                      # widest 60 s window with the most distinct accounts
            while rs[j]["t"] - rs[i]["t"] > 60:
                i += 1
            authors = {r["author"] for r in rs[i:j + 1]}
            if len(authors) >= 5:
                bursts.append({"text": rs[j]["text"][:200], "accounts": len({r["author"] for r in rs}), "messages": len(rs)})
                break
    bursts.sort(key=lambda b: -b["accounts"])
    return {"rate_before": rate(before, min(300, start)), "rate_during": rate(during, end - start),
            "rate_after": rate(after, 300), "echo_share": round(echo(resp), 3), "echo_share_before": round(echo(before), 3),
            "first_msg_share": round(first, 3), "first_msg_share_stream": round(base_first, 3),
            "bursts": len(bursts), "burst_accounts": sum(b["accounts"] for b in bursts), "burst_examples": bursts[:3]}


def clip_from(st, v, m, turns, chat, got, con=None) -> dict | None:
    if got.get("status") != "succeeded":
        return None
    r = got["result"]
    if not r["keep"] or r["severity"] < st["min_severity"] or r["stance"] in ("reading_or_quoting", "condemning"):
        return None
    ctx = norm(" ".join(t["text"] for t in turns))
    if r["quote"] and not quoted(r["quote"], ctx):
        return None                          # never publish a quote that isn't in the transcript
    msgs = {norm(c["text"]) for c in chat}
    examples = [e for e in r["chat_examples"] if norm(e) in msgs][:4]
    lo, hi = turns[0]["start"], turns[-1]["end"]
    a = min(max(r["clip_start"], lo), hi)
    b = min(max(r["clip_end"], a + st["clip_min_s"]), a + st["clip_max_s"], hi + 5)
    lines = [{"t": t["start"], "speaker": t["speaker"], "text": t["text"]} for t in turns if a - 10 <= t["start"] <= b + 5]
    metrics = chat_metrics(con, v["id"], a, b, r["quote"], lines) if con is not None else None
    return {"id": f"c-{v['id']}-{int(a)}", "video_id": v["id"], "stream_title": v["title"], "date": v["upload_date"],
            "embed_type": v["embed_type"],
            "embed_id": v["embed_id"], "source_url": v["source_url"], "start": round(a, 1), "end": round(b, 1),
            "severity": r["severity"], "confidence": r["confidence"], "stance": r["stance"],
            "categories": r["categories"], "title": r["title"], "summary": r["summary"],
            "why_notable": r["why_notable"], "nuance": r["nuance"], "chat_reaction": r["chat_reaction"],
            "chat_examples": examples, "quote": r["quote"], "segment_reason": r.get("segment_reason", ""),
            "chat_metrics": metrics,
            "lines": lines}


# ---------------------------------------------------------------- pass 3: fact-check
def claims_from(st, screened) -> list[dict]:
    out = []
    for v, w, got in screened:
        if got.get("status") != "succeeded":
            continue
        text = norm(" ".join(t["text"] for t in w if t["speaker"] in ("Hasan", "Auto-captions")))
        for c in got["result"].get("claims", []):
            if c["importance"] < st["fact_min_importance"] or not quoted(c["quote"], text):
                continue
            out.append({**c, "vid": v["id"]})
    out.sort(key=lambda c: (-c["importance"], c["vid"], c["start"]))
    kept = []
    for c in out:                            # windows overlap; the same claim is often listed twice
        if any(k["vid"] == c["vid"] and norm(k["quote"]) == norm(c["quote"]) for k in kept):
            continue
        kept.append(c)
    return kept[: st["max_facts"]]


def fact_requests(cfg, st, con, streams, claims) -> tuple[list, list, bool]:
    by_id = {v["id"]: v for v in streams}
    need, have, flying, waiting = [], [], in_flight(cfg), False
    for c in claims:
        v = by_id[c["vid"]]
        turns = [t for t in turns_of(con, v["id"]) if abs(t["start"] - c["start"]) <= st["fact_context_s"]]
        text = lines_text(turns)
        key = h("fact", 1, st["model"], v["id"], c["start"], c["claim"], text)
        got = cache_get(cfg, "fact", key)
        if usable(got):
            have.append((v, c, turns, got))
        elif key in flying:
            waiting = True
        else:
            user = (f"Stream: {v['title']}, {v['upload_date'][:4]}-{v['upload_date'][4:6]}-{v['upload_date'][6:]}.\n"
                    f"Claim to check (as restated by a first pass): {c['claim']}\n"
                    f"His words: \"{c['quote']}\" at {fmt(c['start'])}.\n\nTranscript around it:\n{text}")
            need.append((key, FACT_RUBRIC, user, None))
    return need, have, waiting


def fact_from(v, c, turns, got) -> dict | None:
    if got.get("status") != "succeeded" or not isinstance(got.get("result"), dict):
        return None
    r = got["result"]
    if r.get("verdict") not in VERDICTS or r["verdict"] == "not_checkable":
        return None
    seen = set(got.get("urls", []))
    sources = [s for s in r.get("sources", []) if isinstance(s, dict) and s.get("url") in seen][:5]
    if r["verdict"] != "accurate" and not sources:
        return None                          # never publish a negative verdict without a source it actually found
    return {"id": f"f-{v['id']}-{int(c['start'])}", "video_id": v["id"], "stream_title": v["title"],
            "date": v["upload_date"], "t": c["start"],
            "quote": c["quote"], "claim": r.get("claim") or c["claim"], "topic": c["topic"], "verdict": r["verdict"],
            "correction": r.get("correction", ""), "explanation": r.get("explanation", ""),
            "confidence": r.get("confidence", "medium"), "sources": sources}


def last_json(text: str):
    """The last top-level {...} object in a text (the fact-check answer ends with one). Uses the JSON decoder, so
    braces inside strings don't confuse it; nested objects are skipped over, not returned."""
    dec, found, i = json.JSONDecoder(), None, text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        i = text.find("{", end)
    return found


# ---------------------------------------------------------------- pass 4: chat check
CHAT_CATEGORIES = ["slur", "antisemitism", "nazi_or_holocaust_comparison", "racism_or_bigotry", "dehumanizing",
                   "violent_threat_or_glorification", "harassment_or_doxxing", "sexual_harassment"]
CHAT_RUBRIC = """You review Twitch chat messages from HasanAbi's (Hasan Piker's) stream for a research site, and flag the
hateful or abusive ones so readers can find them. Each line is `id<TAB>message`; a line starting
with [deleted by mods] was removed by the channel's moderators.

Flag a message only if it clearly is one of:
- slur: a slur against a group (racial, ethnic, religious, homophobic, transphobic, ableist), including obvious misspellings
- antisemitism: hostility to Jews as Jews, antisemitic tropes or conspiracy (Jewish control, blood libel, "the Jews" did
  X), Holocaust denial or mockery, calling Jews generally Nazis. Criticism of Israel or Zionism as a political ideology is
  NOT antisemitism by itself
- nazi_or_holocaust_comparison: likening Israel, Zionists or Israelis to Nazis, or Gaza to the Holocaust (death camps,
  "the new Nazis", "Zionism is Nazism"). The site reports these as comparisons, without calling them antisemitic; use
  antisemitism instead when the message targets Jews as Jews
- racism_or_bigotry: hostility to people for their race, ethnicity, religion (incl. Islamophobia), nationality, gender,
  sexuality or disability
- dehumanizing: describing a group of people as animals, vermin, disease, subhuman, or deserving to die
- violent_threat_or_glorification: threats, calls for violence or killing, celebrating deaths or attacks on civilians
- harassment_or_doxxing: abuse aimed at a specific person (a chatter, a guest, a public figure) beyond insult, or
  personal information about someone
- sexual_harassment: sexual comments aimed at a specific person

Do NOT flag: ordinary insults, swearing, emotes, spam, jokes without a target group, strong political opinions, or
messages quoting something only to criticise it. When unsure, don't flag. Use only ids from the list.
For each flag give the category, a few words on why (no more than 12 words), and sure: true when the message is
unambiguous on its own, false when it depends on context you can't see (sarcasm, quoting someone, unclear target)."""
CHAT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["flags"],
    "properties": {"flags": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "category", "why", "sure"],
        "properties": {"id": {"type": "integer"}, "category": {"type": "string", "enum": CHAT_CATEGORIES},
                       "why": {"type": "string"}, "sure": {"type": "boolean"}}}}}}


def clip_chat(con, vid: str, start: float, end: float) -> list[dict]:
    """The chat the weekly page shows next to a clip (10 s before to 60 s after), with message ids. Mod-deleted
    messages are included when their text was published."""
    return [dict(r) for r in con.execute(
        "SELECT id, t, text, deleted FROM chat WHERE video_id=? AND t BETWEEN ? AND ? AND kind='msg' ORDER BY t, id",
        (vid, max(start - 10, 0), end + 60)) if (r["text"] or "").strip() and r["text"].strip() != "[deleted by mods]"]


def chat_requests(cfg, st, con, clips) -> tuple[list, dict, bool]:
    """-> (requests still needed, {clip id: [cached answers with their message ids]}, whether some are in flight)."""
    need, have, flying, waiting = [], {}, in_flight(cfg), False
    n = max(50, int(st["chat_per_request"]))
    for c in clips:
        msgs = clip_chat(con, c["video_id"], c["start"], c["end"])
        have[c["id"]] = []
        for i in range(0, len(msgs), n):
            part = msgs[i:i + n]
            text = "\n".join(f"{m['id']}\t{'[deleted by mods] ' if m['deleted'] else ''}"
                             f"{' '.join(m['text'].split())[:400]}" for m in part)
            key = h("chat", 1, st["model"], c["video_id"], c["start"], text)
            got = cache_get(cfg, "chat", key)
            if usable(got):
                have[c["id"]].append(({m["id"] for m in part}, got))
            elif key in flying:
                waiting = True
            else:
                need.append((key, CHAT_RUBRIC, f"Stream: {c['stream_title']} ({c['date']}). Clip: {c['title']}.\n\n"
                                                f"CHAT ({len(part)} messages):\n{text}", CHAT_SCHEMA))
    return need, have, waiting


def chat_flags(answers: list) -> list[dict]:
    out = {}
    for ids, got in answers:
        r = got.get("result") if got.get("status") == "succeeded" else None
        for f in (r or {}).get("flags", []) if isinstance(r, dict) else []:
            if isinstance(f, dict) and f.get("id") in ids and f.get("category") in CHAT_CATEGORIES:
                out[f["id"]] = {"id": f["id"], "category": f["category"], "why": str(f.get("why", ""))[:120],
                                "sure": f.get("sure") is not False}
    return sorted(out.values(), key=lambda x: x["id"])


def diagnostics(st, screened, judged, checked) -> dict:
    """Counts of what each pass produced and why things were dropped (no private data: Hasan's public words only)."""
    from collections import Counter
    d = {"screen_windows": len(screened), "screen_status": dict(Counter(g.get("status") for *_, g in screened)),
         "moments": 0, "moments_below_threshold": 0, "moments_reading_or_condemning": 0, "moments_quote_mismatch": 0,
         "claims": 0, "claims_low_importance": 0, "claims_quote_mismatch": 0,
         "judge_status": dict(Counter(g.get("status") for *_, g in judged)), "judged_kept": 0,
         "judged_rejected": 0, "judged_below_min": 0, "judged_reading_or_condemning": 0, "judged_quote_mismatch": 0,
         "fact_status": dict(Counter(g.get("status") for *_, g in checked)), "fact_not_checkable": 0,
         "fact_no_found_source": 0, "examples_quote_mismatch": []}
    for v, w, got in screened:
        if got.get("status") != "succeeded":
            continue
        text = norm(" ".join(t["text"] for t in w))
        htext = norm(" ".join(t["text"] for t in w if t["speaker"] in ("Hasan", "Auto-captions")))
        for m in got["result"].get("moments", []):
            d["moments"] += 1
            if m["severity"] < st["screen_min_severity"]:
                d["moments_below_threshold"] += 1
            elif m["stance"] in ("reading_or_quoting", "condemning"):
                d["moments_reading_or_condemning"] += 1
            elif m["quote"] and not quoted(m["quote"], text):
                d["moments_quote_mismatch"] += 1
                if len(d["examples_quote_mismatch"]) < 5:
                    d["examples_quote_mismatch"].append(m["quote"][:200])
        for c in got["result"].get("claims", []):
            d["claims"] += 1
            if c["importance"] < st["fact_min_importance"]:
                d["claims_low_importance"] += 1
            elif not quoted(c["quote"], htext):
                d["claims_quote_mismatch"] += 1
    for v, m, turns, chat, got in judged:
        if got.get("status") != "succeeded":
            continue
        r = got["result"]
        if not r["keep"]:
            d["judged_rejected"] += 1
        elif r["severity"] < st["min_severity"]:
            d["judged_below_min"] += 1
        elif r["stance"] in ("reading_or_quoting", "condemning"):
            d["judged_reading_or_condemning"] += 1
        elif r["quote"] and not quoted(r["quote"], norm(" ".join(t["text"] for t in turns))):
            d["judged_quote_mismatch"] += 1
        else:
            d["judged_kept"] += 1
    for v, c, turns, got in checked:
        r = got.get("result") if got.get("status") == "succeeded" else None
        if not isinstance(r, dict):
            continue
        if r.get("verdict") == "not_checkable":
            d["fact_not_checkable"] += 1
        elif r.get("verdict") != "accurate" and not [x for x in r.get("sources", []) if isinstance(x, dict)
                                                       and x.get("url") in set(got.get("urls", []))]:
            d["fact_no_found_source"] += 1
    return d


def summarise(facts: list[dict]) -> dict:
    by_verdict = {v: 0 for v in VERDICTS if v != "not_checkable"}
    by_topic = {}
    for f in facts:
        by_verdict[f["verdict"]] += 1
        t = by_topic.setdefault(f["topic"], {v: 0 for v in by_verdict})
        t[f["verdict"]] += 1
    return {"checked": len(facts), "by_verdict": by_verdict, "by_topic": by_topic}


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

    def send(batches: list[tuple[str, list, float]]) -> str | None:
        """batches: (kind, requests, estimated usd). Returns a status to stop with, or None after submitting."""
        nonlocal client
        todo = [(k, r, u) for k, r, u in batches if r]
        if not todo:
            return None
        usd = sum(u for *_, u in todo)
        if st["backend"] != "api":           # the owner's Claude subscription: no API bill
            print(f"week {week}: " + "; ".join(f"{k} {len(r)} requests" for k, r, _ in todo) +
                  f" with {st['model']} through claude -p (subscription)")
            if not yes:
                return "estimate"
            for kind, reqs, _ in todo:
                try:
                    run_cli(cfg, st, kind, reqs)
                except UsageLimit as e:
                    log.warning("week %s: subscription usage limit (%s); resuming next run", week, e)
                    return "limit"
            return None
        print(f"week {week}: " + "; ".join(f"{k} {len(r)} requests" for k, r, _ in todo) +
              f" with {st['model']}: about ${usd:.2f}")
        if not yes:
            return "estimate"
        if budget is not None and usd > budget:
            log.error("week %s: estimate $%.2f is over the $%.2f budget; not submitted", week, usd, budget)
            return "over_budget"
        client = client or api_client()
        for kind, reqs, _ in todo:
            submit(cfg, st, kind, reqs, client)
        return None

    while True:
        if client is not None:
            collect(cfg, client, wait=False)
        need, screened, waiting = screen_requests(cfg, st, con, streams)
        stop = send([("screen", need, estimate(st, need, 600))])
        if stop:
            return stop
        if need and st["backend"] != "api":   # answered and cached just now: read them back
            continue
        if need or waiting:
            if not wait:
                return "waiting"
            collect(cfg, client, wait=True)
            continue
        jneed, judged, jwait = judge_requests(cfg, st, con, streams, candidates(st, screened))
        fneed, checked, fwait = fact_requests(cfg, st, con, streams, claims_from(st, screened))
        stop = send([("judge", jneed, estimate(st, jneed, 2000)),
                     ("fact", fneed, estimate(st, fneed, 2500, extra_in=20000,
                                              extra_usd=st["search_usd"] * st["fact_search_uses"]))])
        if stop:
            return stop
        if (jneed or fneed) and st["backend"] != "api":
            continue
        if jneed or fneed or jwait or fwait:
            if not wait:
                return "waiting"
            collect(cfg, client, wait=True)
            continue
        break
    clips = [c for c in (clip_from(st, *x, con=con) for x in judged) if c]
    clips.sort(key=lambda c: (-c["severity"], {"high": 0, "medium": 1, "low": 2}[c["confidence"]], c["date"], c["start"]))
    clips = clips[: st["max_clips"]]
    while True:                              # pass 4: flag hateful or abusive chat next to each chosen clip
        if client is not None:
            collect(cfg, client, wait=False)
        cneed, flagged, cwait = chat_requests(cfg, st, con, clips)
        stop = send([("chat", cneed, estimate(st, cneed, 600))])
        if stop:
            return stop
        if cneed and st["backend"] != "api":
            continue
        if cneed or cwait:
            if not wait:
                return "waiting"
            collect(cfg, client, wait=True)
            continue
        break
    for c in clips:
        c["chat_flags"] = chat_flags(flagged.get(c["id"], []))
    facts = [f for f in (fact_from(*x) for x in checked) if f]
    order = {"false": 0, "misleading": 1, "unsupported": 2, "mostly_accurate": 3, "accurate": 4}
    facts.sort(key=lambda f: (order[f["verdict"]], f["date"], f["t"]))
    out = {"week": week, "week_end": (dt.date.fromisoformat(week) + dt.timedelta(days=6)).isoformat(),
           "generated_at": (now or dt.datetime.now()).isoformat(timespec="seconds"), "model": st["model"],
           "streams": [{"video_id": v["id"], "title": v["title"], "date": v["upload_date"]} for v in streams],
           "fingerprint": fingerprint(con, streams), "candidates_judged": len(judged), "clips": clips,
           "fact_checks": facts, "fact_summary": summarise(facts), "version": VERSION,
           "diagnostics": diagnostics(st, screened, judged, checked),
           "method": "Claude read every transcript line of the week's streams, flagged moments where Hasan himself "
                     "may have said something egregious, then re-judged the strongest with 20 minutes of context and "
                     "the chat at the time, against a fixed rubric (own words only; readings, condemnations and "
                     "mainstream criticism excluded), choosing each segment's length. The same first pass listed the "
                     "factual claims he stated as fact; the most consequential were checked with web search as of the "
                     "day he said them. Verdicts assess accuracy only: nobody can tell from a transcript whether he "
                     "believed a claim. Chat next to each clip was read by Claude, which flagged slurs, "
                     "hate, threats and harassment for highlighting."}
    d = reels_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{week}.json.tmp"
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    tmp.replace(d / f"{week}.json")
    print(f"week {week}: wrote {len(clips)} clip(s) from {len(judged)} judged, {len(facts)} fact-check(s) -> "
          f"{d / (week + '.json')}")
    return "written"


def fingerprint(con, streams) -> str:
    return h([(v["id"], con.execute("SELECT COUNT(*), COALESCE(MAX(end),0) FROM turns WHERE video_id=?",
                                    (v["id"],)).fetchone()[:]) for v in streams])


def needs_work(cfg, con, week: str) -> bool:
    path = reels_dir(cfg) / f"{week}.json"
    if not path.exists():
        return bool(week_streams(con, week))
    old = json.loads(path.read_text())
    return old.get("fingerprint") != fingerprint(con, week_streams(con, week)) or old.get("version") != VERSION


def backfill_weeks(st: dict, latest: str) -> list[str]:
    """Earlier weeks to fill in, newest first: from `backfill_from` (rounded back to its Sunday), at most
    `max_backfill_weeks`. Empty unless backfill_from is set, so a big import of old streams never costs by surprise."""
    if not st.get("backfill_from"):
        return []
    first = dt.date.fromisoformat(str(st["backfill_from"]))
    first -= dt.timedelta(days=(first.weekday() + 1) % 7)
    out, w = [], dt.date.fromisoformat(latest) - dt.timedelta(days=7)
    while w >= first and len(out) < st.get("max_backfill_weeks", 12):
        out.append(w.isoformat())
        w -= dt.timedelta(days=7)
    return out


def auto(cfg, client=None, now: dt.datetime | None = None, subscription_run: bool = False) -> str:
    """Hourly: build the latest finished week once it is due (and refresh it when its streams change); when it is up
    to date, fill in earlier weeks one at a time (backfill_from). One week is worked on at a time.
    With the subscription backend the hourly call does nothing: `reel.py weekly` (its own timer) does the work, so a
    run of a few hours never holds up the hourly pipeline."""
    st = settings(cfg)
    if st["backend"] != "api" and not subscription_run:
        return "subscription backend: built by reel.py weekly"
    now = now or dt.datetime.now()
    state_path = root(cfg) / "auto.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}

    def work(week: str) -> str:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"week": week}))
        api = st["backend"] == "api"
        result = build(cfg, week, client=(client or api_client()) if api else None, yes=True,
                       budget=st["max_usd_per_week"] if api else None, now=now)
        if result not in ("waiting", "limit"):
            state_path.unlink(missing_ok=True)
        return f"{week}: {result}"

    if state.get("week"):                    # finish what is in flight first
        return work(state["week"])
    latest = last_finished_week(now)
    week_end = dt.date.fromisoformat(latest) + dt.timedelta(days=6)
    due = dt.datetime.combine(week_end + dt.timedelta(days=1), dt.time(st["build_after_hour"]))
    con = public_db(cfg)
    if now < due:
        latest = (dt.date.fromisoformat(latest) - dt.timedelta(days=7)).isoformat()   # last week's reel stands
    if needs_work(cfg, con, latest):
        return work(latest)
    for week in backfill_weeks(st, latest):
        if needs_work(cfg, con, week):
            return work(week)
    return "up_to_date"


def weekly(cfg, now: dt.datetime | None = None) -> list[str]:
    """The subscription run: the latest week, then at most `cli_backfill_per_run` earlier weeks."""
    st = settings(cfg)
    out = []
    for _ in range(1 + int(st["cli_backfill_per_run"])):
        r = auto(cfg, now=now, subscription_run=True)
        out.append(r)
        if not r.endswith(": written"):
            break
    return out


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
    ap.add_argument("cmd", choices=["auto", "weekly", "build", "status"])
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
    if a.cmd == "weekly":
        print("reel:", "; ".join(weekly(cfg)))
        return 0
    week = a.week or last_finished_week(dt.datetime.now())
    if not WEEK_RE.match(week):
        sys.exit("--week must be YYYY-MM-DD")
    client = api_client() if a.yes and settings(cfg)["backend"] == "api" else None
    print("reel:", build(cfg, week, client=client, yes=a.yes, wait=a.wait))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
