"""Keeps the standing fact-check reports (reports/*.json: one topic across every stream) growing with every new stream.

    python pipeline/report_tags.py run       tag the stream fact-checks no run has tagged yet (claude -p, subscription)
    python pipeline/report_tags.py status    how many are tagged, and how many each report gained

The reports were made by hand once (their own items stay as they are). Every fact-check the weekly reel makes
afterwards (reels/<week>.json, one per claim of every stream) is shown to Claude with each report's title, scope and
sections, and Claude says which reports it belongs in, and under which section; most belong in none. The answers are
kept in reels/report_tags.json, which the site reads: a report then lists its own items plus the tagged stream
fact-checks dated after its last own item (webcore.load_report), so nothing is listed twice.
Cached per fact-check: a run with nothing new sends nothing. A usage limit stops the run; the next one resumes.
"""
from __future__ import annotations

import argparse
import json
import sys

import reel
from common import P, get_logger, load_config

log = get_logger("report_tags")
VERSION = 1
PER_REQUEST = 25
RUBRIC = """You sort fact-checked claims from HasanAbi's (Hasan Piker's) Twitch streams into standing reports on a public
archive. Each report collects his claims about one subject. You are given the reports (id, title, scope, sections)
and a numbered list of claims, each with the finding of its fact-check.

For each claim, list the reports it belongs in:
- A claim belongs in a report only when it is a claim ABOUT that report's subject: what those people or that party
  said, did, voted for, funded or are responsible for. Merely mentioning them in passing is not enough.
- Most claims belong in no report: return an empty list for them. A claim can belong in two reports when it is
  squarely about both.
- For each report it belongs in, pick the one section that fits best, by its key. Use the report's catch-all section
  when no other fits.
Judge only what the claim is about, not whether it is true."""


def tags_path(cfg) -> "Path":
    return reel.reels_dir(cfg) / "report_tags.json"


def reports_dir(cfg):
    return P(cfg, "reports") if "reports" in cfg.get("paths", {}) else None


def load_reports(cfg) -> list[dict]:
    d, out = reports_dir(cfg), []
    if d is None or not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("id") and r.get("sections"):
            out.append(r)
    return out


def stream_facts(cfg) -> list[dict]:
    out, d = [], reel.reels_dir(cfg)
    for p in sorted(d.glob("????-??-??.json")) if d.is_dir() else []:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out += [f for f in r.get("fact_checks") or [] if f.get("id") and f.get("claim")]
    return out


def load_tags(cfg) -> dict:
    try:
        t = json.loads(tags_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": VERSION, "reports": "", "tags": {}}
    return t if t.get("version") == VERSION else {"version": VERSION, "reports": "", "tags": {}}


def schema(reports: list[dict]) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["claims"], "properties": {"claims": {
        "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["n", "reports"],
                                   "properties": {"n": {"type": "integer"}, "reports": {
                                       "type": "array", "items": {
                                           "type": "object", "additionalProperties": False, "required": ["id", "section"],
                                           "properties": {"id": {"type": "string", "enum": [r["id"] for r in reports]},
                                                          "section": {"type": "string"}}}}}}}}}


def describe(reports: list[dict]) -> str:
    return "\n\n".join(
        f"Report id: {r['id']}\nTitle: {r['title']}\nScope: {r.get('subtitle', '')}\nSections:\n" +
        "\n".join(f"- {s['key']}: {s['title']}" for s in r["sections"]) for r in reports)


def run(cfg) -> str:
    """-> 'done', 'limit' or 'nothing' (no reports, or no stream fact-checks)."""
    reports, facts = load_reports(cfg), stream_facts(cfg)
    if not reports or not facts:
        return "nothing"
    st = {**reel.settings(cfg), "rtag_effort": "low"}
    desc = describe(reports)
    sig = reel.h("reports", VERSION, desc)             # reports added or re-sectioned: everything is tagged again
    state = load_tags(cfg)
    if state["reports"] != sig:
        state = {"version": VERSION, "reports": sig, "tags": {}}
    todo = [f for f in facts if f["id"] not in state["tags"]]
    sections = {r["id"]: {s["key"] for s in r["sections"]} for r in reports}
    sch, result = schema(reports), "done"
    for i in range(0, len(todo), PER_REQUEST):
        part = todo[i:i + PER_REQUEST]
        user = f"REPORTS\n\n{desc}\n\nCLAIMS\n\n" + "\n\n".join(
            f"{n}. Claim: {f['claim']}\n   Finding: {f.get('correction') or f.get('explanation') or ''}"
            for n, f in enumerate(part, 1))
        key = reel.h("rtag", VERSION, st["model"], sig, [f["id"] for f in part])
        if not reel.usable(reel.cache_get(cfg, "rtag", key)):
            try:
                reel.run_cli(cfg, st, "rtag", [(key, RUBRIC, user, sch)])
            except reel.UsageLimit as e:
                log.warning("usage limit (%s); the rest waits for the next run", e)
                result = "limit"
                break
        got = reel.cache_get(cfg, "rtag", key) or {}
        if got.get("status") != "succeeded":
            continue                                   # retried next run (reel.usable), then left untagged
        answers = {c.get("n"): c.get("reports") or [] for c in (got.get("result") or {}).get("claims", [])}
        for n, f in enumerate(part, 1):
            state["tags"][f["id"]] = {t["id"]: t["section"] for t in answers.get(n, [])
                                      if t.get("id") in sections and t.get("section") in sections[t["id"]]}
    path = tags_path(cfg)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    log.info("report tags: %d of %d stream fact-checks tagged (%s)", len(state["tags"]), len(facts), result)
    return result


def status(cfg) -> None:
    state, facts = load_tags(cfg), stream_facts(cfg)
    print(f"{len(state['tags'])} of {len(facts)} stream fact-checks tagged ({tags_path(cfg)})")
    for r in load_reports(cfg):
        print(f"  {r['id']}: {sum(1 for t in state['tags'].values() if r['id'] in t)} tagged for it")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "status"])
    a = ap.parse_args()
    cfg = load_config()
    if a.cmd == "status":
        status(cfg)
    else:
        print("report_tags:", run(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
