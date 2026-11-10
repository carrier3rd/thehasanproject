"""Rhetoric study, chat part (design: research/README.md; predictions: research/rhetoric-predictions.md).

  scan     find candidate messages with the research/ patterns, plus a random sample of the rest (free, local)
  code     send candidates + sample to Claude as one Message Batch (prints the cost estimate; needs --yes to spend)
  collect  fetch finished batches into coded.jsonl
  report   aggregate report (archive/rhetoric/report.md, no account names) and the private per-account file
           (archive/rhetoric/accounts.tsv, names; never commit or publish it)

Units: each VOD with a chat replay (data/chat/<id>.jsonl), and each live-logged session (live_msgs, which keeps
messages mods deleted). Everything is written under archive/rhetoric/, which the public site cannot read.
Console output never contains account names, so it can be pasted into a session.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from common import ROOT, P, connect_archive, get_logger, load_config

log = get_logger("rhetoric")
RESEARCH = ROOT / "research"
CATEGORIES = ["antisemitism", "holocaust_distortion", "israel", "violence", "finkelstein", "russia", "anti_us",
              "anti_liberal"]
STANCES = ["endorse", "quote_or_report", "condemn", "joke_or_bait", "unclear"]
DEFAULTS = {"model": "claude-opus-5-5", "effort": "low", "context_messages": 10, "sample_size": 500, "seed": 1,
            "max_requests": 5000, "transcript_window_s": 30,
            # USD per million tokens (input, output), standard rates as of 2026-11; batches bill half
            "prices": {"claude-opus-5-5": [4.0, 20.0], "claude-sonnet-5-5": [2.0, 10.0], "claude-haiku-5-5": [0.10, 0.50]},
            "est_output_tokens": 400}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("rhetoric") or {})}


def out_root(cfg: dict) -> Path:
    return P(cfg, "archive_db").parent / "rhetoric"


# ---------------------------------------------------------------- patterns
class Pattern:
    __slots__ = ("id", "category", "tier", "ihra", "jda", "rx", "needs_context", "meaning")

    def __init__(self, id, category, tier, ihra, jda, pattern, needs_context=False, meaning=""):
        self.id, self.category, self.tier, self.ihra, self.jda = id, category, tier, ihra, jda
        self.rx, self.needs_context, self.meaning = re.compile(pattern, re.I), needs_context, meaning


def load_patterns(research: Path = RESEARCH) -> list[Pattern]:
    pats = []
    with open(research / "watchlist.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pats.append(Pattern(r["id"], r["category"], r["tier"], r["ihra"], r["jda"], r["pattern"], meaning=r["meaning"]))
    with open(research / "dogwhistles_allen.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pats.append(Pattern(r["id"], "antisemitism", "dogwhistle", "context", "context", r["pattern"],
                                r["needs_context"] == "yes", f"{r['dogwhistle']}: {r['covert_meaning']}"))
    with open(research / "kremlin_narratives.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pats.append(Pattern("kn_" + r["id"], "russia", "narrative", "n/a", "n/a", r["pattern"],
                                meaning=f"{r['claim']} (rated {r['rating']})"))
    return pats


def match(pats: list[Pattern], text: str) -> tuple[list[str], set[str]]:
    """-> (candidate pattern ids, topic ids)."""
    topics = {p.id for p in pats if p.tier == "topic" and p.rx.search(text)}
    on_topic = bool(topics & {"topic_jews", "topic_israel"})
    hits = [p.id for p in pats if p.tier not in ("topic", "mention") and (on_topic or not p.needs_context)
            and p.rx.search(text)]
    return hits, topics


# ---------------------------------------------------------------- chat units
def units(cfg: dict) -> dict[str, dict]:
    """unit id -> {"kind": "replay"|"live", ...}."""
    out = {}
    for path in sorted(P(cfg, "chat").glob("*.jsonl")):
        out[path.stem] = {"kind": "replay", "path": path, "vid": path.stem}
    con = connect_archive(cfg)
    for s in con.execute("SELECT start_ms, end_ms FROM sessions WHERE channel=? ORDER BY start_ms", (cfg["channel_name"],)):
        uid = "live-" + time.strftime("%Y%m%d-%H%M", time.gmtime(s["start_ms"] / 1000))
        out[uid] = {"kind": "live", "start_ms": s["start_ms"], "end_ms": s["end_ms"]}
    con.close()
    return out


def messages(cfg: dict, u: dict) -> list[dict]:
    rows = []
    if u["kind"] == "replay":
        with open(u["path"], encoding="utf-8") as f:
            for line in f:
                m = json.loads(line)
                rows.append({"t": float(m["t"]), "user_id": m.get("user_id") or m.get("author") or "",
                             "author": m.get("author") or "", "text": m.get("text") or "", "deleted": 0})
    else:
        con = connect_archive(cfg)
        for r in con.execute("SELECT ts_ms, user_id, author, text, deleted FROM live_msgs WHERE channel=? AND kind='msg' "
                             "AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms", (cfg["channel_name"], u["start_ms"], u["end_ms"])):
            rows.append({"t": (r["ts_ms"] - u["start_ms"]) / 1000, "user_id": r["user_id"] or r["author"] or "",
                         "author": r["author"] or "", "text": r["text"] or "", "deleted": int(r["deleted"] or 0)})
        con.close()
    return [r for r in rows if r["text"].strip()]


def transcript_segments(cfg: dict, vid: str | None) -> list[dict]:
    if not vid:
        return []
    for path in sorted(P(cfg, "transcripts").glob(f"**/{vid}.json")):
        return json.loads(path.read_text())["segments"]
    return []


def stream_text(segs: list[dict], t: float, window: float) -> str:
    near = [s for s in segs if s["end"] >= t - window and s["start"] <= t + 5]
    return " ".join(f"[{s['speaker']}] {s['text']}" for s in near)[-1500:]


def context(rows: list[dict], i: int, n: int) -> tuple[list[list[str]], str]:
    """Previous n messages with per-item aliases (no account names leave the laptop)."""
    alias: dict[str, str] = {}
    def a(uid):
        return alias.setdefault(uid, f"u{len(alias) + 1}")
    target = a(rows[i]["user_id"])
    return [[a(r["user_id"]), r["text"][:300]] for r in rows[max(0, i - n):i]], target


# ---------------------------------------------------------------- scan
def scan(cfg: dict, only: list[str] | None = None, research: Path = RESEARCH) -> dict:
    st, pats = settings(cfg), load_patterns(research)
    summary = {}
    for uid, u in units(cfg).items():
        if only and uid not in only:
            continue
        rows = messages(cfg, u)
        if not rows:
            continue
        segs = transcript_segments(cfg, u.get("vid"))
        hits_by_id, topic_counts, cand = Counter(), Counter(), []
        rest = []
        for i, r in enumerate(rows):
            hits, topics = match(pats, r["text"])
            topic_counts.update(topics)
            if hits:
                hits_by_id.update(hits)
                cand.append(i)
            else:
                rest.append(i)
        rng = random.Random(f"{st['seed']}:{uid}")
        sample = sorted(rng.sample(rest, min(st["sample_size"], len(rest))))
        d = out_root(cfg) / uid
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "candidates.jsonl", "w", encoding="utf-8") as f:
            for kind, idx in (("candidate", cand), ("sample", sample)):
                for i in idx:
                    r = rows[i]
                    ctx, target = context(rows, i, st["context_messages"])
                    f.write(json.dumps({"n": i, "kind": kind, "t": r["t"], "user_id": r["user_id"], "author": r["author"],
                                        "text": r["text"], "deleted": r["deleted"],
                                        "hits": match(pats, r["text"])[0], "alias": target, "context": ctx,
                                        "stream": stream_text(segs, r["t"], st["transcript_window_s"]) if segs else ""},
                                       ensure_ascii=False) + "\n")
        cats = Counter(p.category for p in pats for _ in range(hits_by_id.get(p.id, 0)))
        stats = {"unit": uid, "kind": u["kind"], "messages": len(rows), "chatters": len({r["user_id"] for r in rows}),
                 "deleted": sum(r["deleted"] for r in rows), "candidates": len(cand), "sample": len(sample),
                 "non_candidates": len(rest), "transcript": bool(segs), "topic_messages": dict(topic_counts),
                 "hits_by_pattern": dict(hits_by_id.most_common()), "hits_by_category": dict(cats)}
        (d / "stats.json").write_text(json.dumps(stats, indent=1))
        summary[uid] = stats
        print(f"{uid}: {len(rows)} messages, {stats['chatters']} chatters, {len(cand)} candidates "
              f"({100 * len(cand) / len(rows):.2f}%), sample {len(sample)}")
    return summary


# ---------------------------------------------------------------- code
SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}},
        "stance": {"type": "string", "enum": STANCES + ["not_applicable"]},
        "ihra_antisemitic": {"type": "boolean"},
        "jda_antisemitic": {"type": "boolean"},
        "us_target": {"type": "string", "enum": ["administration", "agency", "company", "system", "none"]},
        "pattern_ids": {"type": "array", "items": {"type": "string"}},
        "quote": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["categories", "stance", "ihra_antisemitic", "jda_antisemitic", "us_target", "pattern_ids", "quote", "reason"],
    "additionalProperties": False,
}


def system_prompt(pats: list[Pattern]) -> str:
    lines = [f"- {p.id} [{p.category}/{p.tier}]: {p.meaning}" for p in pats if p.tier not in ("topic", "mention")]
    return f"""You code Twitch chat messages from HasanAbi's streams for a research study of extremist, antisemitic,
Holocaust-distorting, anti-American, anti-liberal and Russia-apologist rhetoric. You will see one target message, the
chat messages just before it (authors replaced by aliases u1, u2, ...), and, when available, what was being said on
stream around that moment. Judge only the target message, using the context to read it.

Categories (choose all that apply, or none):
- antisemitism: hostility to Jews as Jews: slurs, Nazi codes, calls for violence, conspiracy tropes (Jewish control,
  dual loyalty, blood libel, Khazars, Soros/Rothschild/"globalist" used about Jews), collective blame of Jews for Israel.
- holocaust_distortion: denial, minimisation, "6 gorillion", casting it as good, accusing Jews of "using" it,
  Holocaust inversion (Gaza as the Holocaust, Israelis as Nazis).
- israel: contested Israel-related speech: Israel-Nazi comparisons, "Zio", denying Israel's right to exist,
  "from the river to the sea", "globalize the intifada", "AIPAC owns Congress".
- violence: praising or justifying violence against civilians or armed groups (Oct 7, Hamas, Hezbollah, Houthis).
- finkelstein: Norman Finkelstein's specific claims (Holocaust industry; Oct 7 as heroic resistance or a camp breakout;
  denial of the Oct 7 sexual violence).
- russia: Mearsheimer-style "the West's fault", "Putin has no imperial ambitions", Ukraine as a pawn, or a documented
  false Kremlin narrative.
- anti_us: the US as a system framed as the world's worst evil or terrorist, "deserved 9/11", "death to America",
  US decline/hegemony/colour-revolution themes. Criticism of a specific US policy or official is NOT anti_us.
- anti_liberal: contempt for liberals or liberal democracy ("libs", "liberals are worse than fascists", "uniparty",
  "electoralism", "liberals get the bullet").

Stance of the target message toward the flagged content:
- endorse: the author asserts, agrees with, or promotes it (sincerely, even if crudely or with emotes).
- quote_or_report: repeating or describing what someone else said, without endorsing.
- condemn: criticising it.
- joke_or_bait: obvious irony, copypasta, trolling the streamer or chat, with no sincere claim.
- unclear: cannot tell.
- not_applicable: no category applies.

Definitions of antisemitism, judged separately:
- ihra_antisemitic: antisemitic under the IHRA Working Definition and its 11 examples (which include Israel-Nazi
  comparisons, denying Jewish self-determination, double standards for Israel, collective blame).
- jda_antisemitic: antisemitic under the Jerusalem Declaration (hostility to Jews as Jews; it does NOT treat
  evidence-based criticism of Israel, apartheid or settler-colonial comparisons, BDS, or harsh anti-Zionist speech as
  antisemitic on their face, but does treat classic tropes applied to Israel, collective blame and dual-loyalty claims
  as antisemitic).
Both are false unless the stance is endorse.

us_target: for US-related content, what is aimed at (administration = a named administration/official/politician;
agency = an agency or institution; company; system = the US as a whole, its empire or capitalism); else none.
pattern_ids: which of the reference patterns below actually apply (may be empty; may include ones the keyword search
missed). quote: the exact words of the target message that carry the flag, copied verbatim (empty if none).
reason: one short sentence.

Twitch conventions: emote names (KEKW, OMEGALUL, Clueless, Aware, ...) signal tone; "Clueless" usually means
sarcasm. Many chatters quote or mock the streamer's opponents. Do not flag a message only because it mentions Jews,
Israel, Zionism, Russia or the US.

Reference patterns:
""" + "\n".join(lines)


def user_prompt(item: dict) -> str:
    ctx = "\n".join(f"{a}: {t}" for a, t in item["context"]) or "(none)"
    parts = [f"Chat just before (oldest first):\n{ctx}"]
    if item.get("stream"):
        parts.append(f"On stream around this moment (speaker labels may be Unknown_n):\n{item['stream']}")
    parts.append(f"TARGET MESSAGE ({item['alias']}{', later deleted by moderators' if item['deleted'] else ''}):\n"
                 f"{item['text'][:1000]}")
    if item["hits"]:
        parts.append(f"Keyword search matched: {', '.join(item['hits'])}")
    return "\n\n".join(parts)


def pending_items(cfg: dict, only: list[str] | None, kind: str | None = None) -> list[tuple[str, dict]]:
    out = []
    for d in sorted(p for p in out_root(cfg).glob("*") if (p / "candidates.jsonl").exists()):
        if only and d.name not in only:
            continue
        done = set()
        if (d / "coded.jsonl").exists():
            done = {json.loads(l)["n"] for l in open(d / "coded.jsonl", encoding="utf-8")}
        submitted = set()
        for b in read_jsonl(out_root(cfg) / "batches.jsonl"):
            if not b.get("collected"):
                submitted |= {n for u, n in b["items"] if u == d.name}
        for line in open(d / "candidates.jsonl", encoding="utf-8"):
            it = json.loads(line)
            if it["n"] not in done and it["n"] not in submitted and (not kind or it["kind"] == kind):
                out.append((d.name, it))
    return out


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in open(path, encoding="utf-8")] if path.exists() else []


def estimate(st: dict, system: str, items: list[tuple[str, dict]]) -> tuple[float, int]:
    p_in, p_out = st["prices"].get(st["model"], [None, None])
    sys_tok = len(system) // 4
    in_tok = sum(len(user_prompt(it)) // 4 for _, it in items)
    out_tok = st["est_output_tokens"] * len(items)
    if p_in is None:
        return float("nan"), in_tok + out_tok
    # half price for batches; the shared system prompt is cached after the first requests (reads bill ~0.1x)
    usd = 0.5 * ((in_tok + sys_tok * (1 + 0.1 * len(items))) * p_in + out_tok * p_out) / 1e6
    return usd, in_tok + sys_tok * len(items) + out_tok


def code(cfg: dict, only=None, yes=False, client=None, research: Path = RESEARCH, kind=None) -> str | None:
    st = settings(cfg)
    items = pending_items(cfg, only, kind)
    if st.get("sample_per_unit") and kind != "candidate":   # trim the random sample per unit, keeping the seeded order
        keep, seen = [], Counter()
        for uid, it in items:
            if it["kind"] == "sample":
                seen[uid] += 1
                if seen[uid] > st["sample_per_unit"]:
                    continue
            keep.append((uid, it))
        items = keep
    items = items[: st["max_requests"]]
    if not items:
        print("nothing to code (run scan first, or everything is coded or submitted)")
        return None
    system = system_prompt(load_patterns(research))
    usd, tok = estimate(st, system, items)
    print(f"{len(items)} messages to code with {st['model']} (effort {st['effort']}), batch API: "
          f"about {tok / 1e6:.1f}M tokens, roughly ${usd:.2f}")
    if not yes:
        print("Not submitted. Re-run with --yes to spend that.")
        return None
    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    requests, ids = [], []
    for k, (uid, it) in enumerate(items):
        cid = f"{re.sub(r'[^A-Za-z0-9_-]', '_', uid)[:40]}-{it['n']}"
        ids.append([uid, it["n"]])
        requests.append({"custom_id": cid, "params": {
            "model": st["model"], "max_tokens": 4096,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user_prompt(it)}],
            "output_config": {"effort": st["effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
        }})
    batch = client.messages.batches.create(requests=requests)
    rec = {"batch_id": batch.id, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": st["model"],
           "items": ids, "custom_ids": [r["custom_id"] for r in requests], "collected": False}
    out_root(cfg).mkdir(parents=True, exist_ok=True)
    with open(out_root(cfg) / "batches.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"submitted batch {batch.id}; run `rhetoric.py collect` in an hour or so")
    return batch.id


# ---------------------------------------------------------------- collect
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def collect(cfg: dict, client=None, wait=False) -> int:
    path = out_root(cfg) / "batches.jsonl"
    batches = read_jsonl(path)
    if not any(not b["collected"] for b in batches):
        print("no batches waiting")
        return 0
    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    total = 0
    for b in batches:
        if b["collected"]:
            continue
        while True:
            status = client.messages.batches.retrieve(b["batch_id"])
            if status.processing_status == "ended" or not wait:
                break
            time.sleep(60)
        if status.processing_status != "ended":
            print(f"batch {b['batch_id']}: {status.processing_status}, not finished yet")
            continue
        by_cid = dict(zip(b["custom_ids"], b["items"]))
        items = {}
        for uid in {u for u, _ in b["items"]}:
            for line in open(out_root(cfg) / uid / "candidates.jsonl", encoding="utf-8"):
                it = json.loads(line)
                items[(uid, it["n"])] = it
        out = defaultdict(list)
        usage = Counter()
        for res in client.messages.batches.results(b["batch_id"]):
            uid, n = by_cid[res.custom_id]
            it = items[(uid, n)]
            rec = {"n": n, "kind": it["kind"], "status": res.result.type}
            if res.result.type == "succeeded":
                msg = res.result.message
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                    usage[k] += getattr(msg.usage, k, 0) or 0
                if msg.stop_reason == "refusal":
                    rec["status"] = "refused"
                else:
                    text = next((blk.text for blk in msg.content if blk.type == "text"), "")
                    try:
                        code_ = json.loads(text)
                        rec.update(code_)
                        rec["quote_ok"] = (not code_["quote"]) or norm(code_["quote"]) in norm(it["text"])
                    except (json.JSONDecodeError, KeyError):
                        rec["status"] = "unparsed"
            out[uid].append(rec)
        for uid, recs in out.items():
            with open(out_root(cfg) / uid / "coded.jsonl", "a", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            total += len(recs)
        b["collected"], b["usage"] = True, dict(usage)
        print(f"batch {b['batch_id']}: {sum(len(v) for v in out.values())} results; usage {dict(usage)}")
    with open(path, "w", encoding="utf-8") as f:
        for b in batches:
            f.write(json.dumps(b) + "\n")
    return total


# ---------------------------------------------------------------- report
ANTISEMITISM_CATS = {"antisemitism", "holocaust_distortion"}


def report(cfg: dict) -> str:
    root = out_root(cfg)
    lines = ["# Rhetoric study: chat report", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M')}. Counts are messages coded `endorse` by Claude. Aggregates only;",
             "per-account detail is in accounts.tsv (private).", ""]
    accounts = defaultdict(lambda: Counter())
    names = defaultdict(set)
    pooled = Counter()
    for d in sorted(p for p in root.glob("*") if (p / "stats.json").exists()):
        st = json.loads((d / "stats.json").read_text())
        cands = {json.loads(l)["n"]: json.loads(l) for l in open(d / "candidates.jsonl", encoding="utf-8")}
        coded = [c for c in read_jsonl(d / "coded.jsonl") if c["n"] in cands]
        ok = [c for c in coded if c["status"] == "succeeded"]
        cand_codes = [c for c in ok if c["kind"] == "candidate"]
        samp_codes = [c for c in ok if c["kind"] == "sample"]
        endorsed = [c for c in cand_codes if c["stance"] == "endorse" and c["categories"]]
        n_msg, n_chat = st["messages"], st["chatters"]
        lines += [f"## {st['unit']} ({st['kind']})", "",
                  f"- {n_msg:,} messages from {n_chat:,} chatters; {st['deleted']:,} deleted by mods; "
                  f"transcript context: {'yes' if st['transcript'] else 'no'}",
                  f"- {st['candidates']:,} keyword candidates ({100 * st['candidates'] / n_msg:.2f}%); "
                  f"coded {len(cand_codes):,}; sample {len(samp_codes):,} of {st['sample']:,}"]
        bad = Counter(c["status"] for c in coded if c["status"] != "succeeded")
        if bad:
            lines.append(f"- not coded: {dict(bad)}")
        if coded:
            q = [c for c in ok if c.get("quote")]
            lines.append(f"- quotes found verbatim in the message: {sum(c['quote_ok'] for c in q)}/{len(q)}")
        lines += ["", "| category | endorsed msgs | % of messages | chatters | % of chatters |", "|---|---|---|---|---|"]
        for cat in CATEGORIES:
            rows = [c for c in endorsed if cat in c["categories"]]
            users = {cands[c["n"]]["user_id"] for c in rows}
            pooled[cat] += len(rows)
            lines.append(f"| {cat} | {len(rows)} | {100 * len(rows) / n_msg:.3f} | {len(users)} | "
                         f"{100 * len(users) / n_chat:.2f} |")
        ihra = [c for c in endorsed if c["ihra_antisemitic"]]
        jda = [c for c in endorsed if c["jda_antisemitic"]]
        topic = max(st["topic_messages"].get("topic_jews", 0), st["topic_messages"].get("topic_israel", 0))
        lines += ["", f"- antisemitic, IHRA: {len(ihra)} messages ({100 * len(ihra) / n_msg:.3f}%), "
                      f"JDA: {len(jda)} ({100 * len(jda) / n_msg:.3f}%)"
                      + (f"; per 100 messages on Jews/Israel (at least {topic:,}): IHRA {100 * len(ihra) / topic:.2f}, "
                         f"JDA {100 * len(jda) / topic:.2f}" if topic else "")]
        stance = Counter(c["stance"] for c in cand_codes)
        lines.append(f"- stances among candidates: {dict(stance)}")
        flagged_users = Counter(cands[c["n"]]["user_id"] for c in endorsed if set(c["categories"]) & ANTISEMITISM_CATS
                                or c["ihra_antisemitic"])
        if flagged_users:
            counts = sorted(flagged_users.values(), reverse=True)
            top = lambda frac: sum(counts[: max(1, round(len(counts) * frac))]) / sum(counts)
            lines.append(f"- concentration (antisemitism/Holocaust/IHRA): {len(counts)} accounts; "
                         f"top 10% wrote {100 * top(0.10):.0f}%, top 1% wrote {100 * top(0.01):.0f}% of those messages")
        if st["kind"] == "live" and st["deleted"]:
            dele = [c for c in endorsed if cands[c["n"]]["deleted"]]
            kept = len(endorsed) - len(dele)
            lines.append(f"- endorsed-flag rate: deleted messages {100 * len(dele) / st['deleted']:.2f}%, "
                         f"kept {100 * kept / max(1, n_msg - st['deleted']):.3f}%")
        if samp_codes:
            miss = [c for c in samp_codes if c["stance"] == "endorse" and c["categories"]]
            rate = len(miss) / len(samp_codes)
            lines.append(f"- missed by keywords (random sample): {len(miss)}/{len(samp_codes)} endorsed "
                         f"({100 * rate:.2f}%), so about {rate * st['non_candidates']:,.0f} more messages in this stream")
        lines.append("")
        for c in endorsed:
            it = cands[c["n"]]
            names[it["user_id"]].add(it["author"])
            for cat in c["categories"]:
                accounts[it["user_id"]][cat] += 1
            accounts[it["user_id"]]["ihra"] += int(c["ihra_antisemitic"])
            accounts[it["user_id"]]["jda"] += int(c["jda_antisemitic"])
            accounts[it["user_id"]]["deleted"] += it["deleted"]
            accounts[it["user_id"]]["streams:" + d.name] = 1
    lines += ["## Pooled endorsed messages by category", ""] + [f"- {k}: {pooled[k]}" for k in CATEGORIES]
    text = "\n".join(lines) + "\n"
    (root / "report.md").write_text(text)
    with open(root / "accounts.tsv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["user_id", "names", "total", *CATEGORIES, "ihra", "jda", "deleted", "streams"])
        for u, c in sorted(accounts.items(), key=lambda kv: -sum(kv[1][k] for k in CATEGORIES)):
            w.writerow([u, "|".join(sorted(names[u])), sum(c[k] for k in CATEGORIES), *(c[k] for k in CATEGORIES),
                        c["ihra"], c["jda"], c["deleted"], sum(1 for k in c if k.startswith("streams:"))])
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["scan", "code", "collect", "report"])
    ap.add_argument("--unit", action="append", help="only this VOD id / live-YYYYmmdd-HHMM unit (repeatable)")
    ap.add_argument("--yes", action="store_true", help="code: actually submit the batch (spends money)")
    ap.add_argument("--wait", action="store_true", help="collect: wait until batches finish")
    ap.add_argument("--model", help="code: model id (default rhetoric.model in config.yaml, else claude-opus-5-5)")
    ap.add_argument("--max", type=int, help="code: at most this many messages in this batch")
    ap.add_argument("--kind", choices=["candidate", "sample"], help="code: only keyword candidates, or only the random sample")
    ap.add_argument("--sample", type=int, help="code: code at most this many sample messages per stream")
    a = ap.parse_args()
    cfg = load_config()
    over = {k: v for k, v in (("model", a.model), ("max_requests", a.max), ("sample_per_unit", a.sample)) if v}
    if over:
        cfg["rhetoric"] = {**(cfg.get("rhetoric") or {}), **over}
    if a.step == "scan":
        scan(cfg, a.unit)
    elif a.step == "code":
        code(cfg, a.unit, a.yes, kind=a.kind)
    elif a.step == "collect":
        collect(cfg, wait=a.wait)
    else:
        print(report(cfg))
        print(f"(per-account file: {out_root(cfg) / 'accounts.tsv'}; private, never commit or publish)")


if __name__ == "__main__":
    sys.exit(main())
