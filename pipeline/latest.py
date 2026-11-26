"""The home page's "Latest stream" section: a headline, the time split and a segment timeline for the newest stream.

Reads the public transcript (public.db, so only public speaker labels), asks Claude for the segments through
`claude -p` on the owner's Claude subscription (no API bill), then checks the answer before writing it:
  - every segment starts at a real transcript line (the model picks a line number, the time comes from the line);
  - every phrase in quotation marks must appear word for word in what Hasan said during that segment, otherwise
    the sentence holding it is dropped.
Writes features/streams/<video>.json (cached by a hash of the transcript) and features/latest.json (the newest).

  python pipeline/latest.py              newest stream, only if its transcript changed
  python pipeline/latest.py --video ID   that stream
  python pipeline/latest.py --force      ask again even if cached
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import P, get_logger, load_config  # noqa: E402

DEFAULTS = {"model": "claude-opus-5-5", "effort": "high", "claude_bin": "claude", "timeout_s": 1800,
            "max_line_chars": 700}
CATEGORIES = ["Politics", "News", "Drama", "Gaming", "Chat", "Other"]

SYSTEM = """You write the "Latest stream" section of thehasanproject, a neutral, searchable archive of HasanAbi's \
Twitch streams. You get the stream's full transcript, one numbered line per turn: "[N] h:mm:ss Speaker: text". \
"Hasan" is the streamer, "Media" is a clip or video he is playing or reacting to (or game audio), "Guest" is anyone \
else talking. The text is automatic speech-to-text, so names can be misspelled: fix a name only when you are sure.

Return:
- headline: what the stream was about, as a plain list of its biggest subjects, at most 90 characters. Factual and \
dry, no puns, no jokes, no editorializing, no contrasts like "X hours on A, zero on B". Example: "The H3 fallout, a \
MAGA mom, an Army blast in Iowa, and three hours of GTA VI".
- topics: 3 to 6 subjects that took the most time, each with a short name (2 to 4 words, e.g. "GTA VI", "MAGA-parent \
talk", "H3 & xQc drama") and a note of at most 6 words (e.g. "end of chapter 3, start of 4").
- segments: 6 to 12 segments in order, covering the stream's main parts (skip short asides). For each:
  line: the number of the transcript line where the segment starts (Hasan introducing it, or the clip starting);
  title: at most 6 words; topic: one of your topic names, or "Other"; category: one of %s;
  summary: one or two sentences of what Hasan actually said or argued. You may quote Hasan, but only words he \
said in that segment, copied exactly from the transcript, inside straight double quotes. Never quote Media or Guest \
lines as Hasan's.
Describe only what the transcript shows. Do not add outside facts, do not judge whether his claims are true, and do \
not guess at anything the transcript doesn't say.
The summaries are about Hasan. Attribute every claim about another person or group to him ("he said xQc mocked \
him", not "xQc mocked him"). Name public figures (politicians, journalists, streamers, podcasters), but never name a \
private person such as a guest's relative or a caller: refer to them by role ("their MAGA mom") and don't attribute \
specific opinions to them.""" % ", ".join(CATEGORIES)

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["headline", "topics", "segments"],
    "properties": {
        "headline": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["name", "note"],
                   "properties": {"name": {"type": "string"}, "note": {"type": "string"}}}},
        "segments": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                     "required": ["line", "title", "topic", "category", "summary"],
                     "properties": {"line": {"type": "integer"}, "title": {"type": "string"}, "topic": {"type": "string"},
                                    "category": {"type": "string", "enum": CATEGORIES}, "summary": {"type": "string"}}}},
    },
}

QUOTE_RE = re.compile(r'"([^"]+)"|“([^”]+)”')
SENT_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z"“])')


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.lower().replace("’", "'")).split())


def ts(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def load(pub: sqlite3.Connection, vid: str) -> tuple[dict, list[tuple]]:
    v = pub.execute("SELECT id, title, upload_date, duration FROM videos WHERE id=?", (vid,)).fetchone()
    if not v:
        raise SystemExit(f"{vid}: not in public.db")
    turns = pub.execute("SELECT start, end, speaker, text FROM turns WHERE video_id=? ORDER BY start", (vid,)).fetchall()
    return {"video_id": v[0], "title": v[1], "upload_date": v[2], "duration": v[3]}, turns


def prompt(turns: list[tuple], st: dict) -> str:
    cut = st["max_line_chars"]
    return "\n".join(f"[{i}] {ts(s)} {sp}: {t if len(t) <= cut else t[:cut] + '…'}"
                     for i, (s, _e, sp, t) in enumerate(turns))


def ask(text: str, st: dict) -> dict:
    """One `claude -p` call on the subscription; returns the structured answer."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                        "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_MODEL")}
    env["PATH"] = str(Path.home() / ".local" / "bin") + os.pathsep + env.get("PATH", "")
    args = [st["claude_bin"], "-p", "--model", st["model"], "--effort", st["effort"], "--output-format", "json",
            "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
            "--tools", "", "--system-prompt", SYSTEM, "--json-schema", json.dumps(SCHEMA)]
    p = subprocess.run(args, input=text, capture_output=True, text=True, encoding="utf-8", env=env,
                       cwd=tempfile.gettempdir(), timeout=st["timeout_s"])
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude exited {p.returncode}: {(p.stderr or p.stdout)[:300]}")
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise RuntimeError(f"claude returned no answer: {str(out.get('result'))[:300]}")
    return out["structured_output"]


def check(meta: dict, turns: list[tuple], ans: dict) -> tuple[dict, list[str]]:
    """Keep only what the transcript supports. Returns the section and a list of what was dropped."""
    dropped = []
    segs = []
    for s in sorted(ans.get("segments", []), key=lambda s: s.get("line", -1)):
        i = s.get("line")
        if not isinstance(i, int) or not 0 <= i < len(turns):
            dropped.append(f"segment {s.get('title')!r}: line {i} is not in the transcript")
            continue
        if segs and turns[i][0] <= segs[-1]["t"]:
            dropped.append(f"segment {s.get('title')!r}: starts at or before the previous one")
            continue
        segs.append({"i": i, "t": round(turns[i][0]), "title": s["title"].strip(), "topic": s["topic"].strip(),
                     "category": s["category"] if s["category"] in CATEGORIES else "Other", "summary": s["summary"]})
    for k, s in enumerate(segs):
        s["end"] = round(segs[k + 1]["t"]) if k + 1 < len(segs) else int(meta["duration"] or turns[-1][1])
        said = norm(" ".join(t[3] for t in turns if s["t"] - 5 <= t[0] < s["end"] + 5 and t[2] == "Hasan"))
        kept = []
        for sent in SENT_RE.split(s["summary"].strip()):
            quotes = [a or b for a, b in QUOTE_RE.findall(sent)]
            bad = [q for q in quotes if not norm(q) or norm(q) not in said]
            if (sent.count('"') % 2) or (sent.count("“") != sent.count("”")):   # a quote split across sentences
                bad = bad or [sent]
            if bad:
                dropped.append(f"{ts(s['t'])} {s['title']!r}: quote not found in Hasan's words: {bad[0][:80]!r}")
            else:
                kept.append(sent)
        s["summary"] = " ".join(kept)
    names = {t["name"].strip() for t in ans.get("topics", [])}
    hours: dict[str, float] = {}
    for s in segs:
        topic = s["topic"] if s["topic"] in names else "Other"
        s["topic"] = topic
        hours[topic] = hours.get(topic, 0) + (s["end"] - s["t"]) / 3600
    topics = sorted(({"name": t["name"].strip(), "note": t["note"].strip(), "hours": round(hours.get(t["name"].strip(), 0), 1)}
                     for t in ans.get("topics", [])), key=lambda t: -t["hours"])
    topics = [t for t in topics if t["hours"] >= 0.1][:4]
    for s in segs:
        s.pop("i", None)
    headline = " ".join(str(ans.get("headline", "")).split())[:110]
    return {**meta, "headline": headline, "topics": topics, "segments": segs}, dropped


def build(cfg: dict, vid: str | None, force: bool) -> int:
    log = get_logger("latest", cfg)
    st = {**DEFAULTS, **(cfg.get("latest") or {})}
    pub = sqlite3.connect(f"file:{P(cfg, 'public_db')}?mode=ro", uri=True)
    if vid is None:
        row = pub.execute("SELECT id FROM videos WHERE upload_date IS NOT NULL ORDER BY upload_date DESC, id DESC LIMIT 1").fetchone()
        if not row:
            print("no streams published yet")
            return 0
        vid = row[0]
    meta, turns = load(pub, vid)
    if not turns:
        print(f"{vid}: no transcript yet")
        return 0
    text = prompt(turns, st)
    sig = hashlib.sha256((st["model"] + SYSTEM + text).encode()).hexdigest()[:16]
    out_dir = P(cfg, "features")
    cache = out_dir / "streams" / f"{vid}.json"
    section = None
    if cache.exists() and not force:
        old = json.loads(cache.read_text(encoding="utf-8"))
        if old.get("sig") == sig:
            section = old
    if section is None:
        t0 = time.time()
        log.info("%s: asking %s for the segments (%d transcript lines)", vid, st["model"], len(turns))
        ans = ask(text, st)
        section, dropped = check(meta, turns, ans)
        for d in dropped:
            log.warning("%s: dropped %s", vid, d)
        if len(section["segments"]) < 3 or not section["headline"]:
            log.error("%s: too little survived the checks (%d segments); not writing", vid, len(section["segments"]))
            return 1
        section.update(sig=sig, model=st["model"], generated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       dropped=len(dropped))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(section, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("%s: %d segments, %d topics, %d dropped, %.0f s", vid, len(section["segments"]),
                 len(section["topics"]), len(dropped), time.time() - t0)
    newest = pub.execute("SELECT id FROM videos WHERE upload_date IS NOT NULL ORDER BY upload_date DESC, id DESC LIMIT 1").fetchone()[0]
    if vid == newest:
        tmp = out_dir / "latest.json.tmp"
        tmp.write_text(json.dumps(section, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, out_dir / "latest.json")
        print(f"latest.json: {vid} {section['upload_date']}: {section['headline']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    return build(load_config(), a.video, a.force)


if __name__ == "__main__":
    raise SystemExit(main())
