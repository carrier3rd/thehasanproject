"""Quality checks: pilot report + labeling sheet (default), --score, --health."""
from __future__ import annotations

import argparse
import csv
import json
import random
import time
from collections import defaultdict

import numpy as np

from build_db import media_flags, merge_turns
from common import P, connect_archive, fmt_ts, load_config, make_dirs, read_tsv, watch_link

LABEL_FIELDS = ["model", "video_id", "start", "end", "link", "predicted_speaker", "text", "true_speaker", "transcript_ok"]
YES = {"y", "yes", "1", "true", "ok"}


def model_dirs(cfg: dict) -> dict[str, list]:
    base = P(cfg, "transcripts")
    out = {}
    for m in cfg.get("pilot_models", []):
        files = sorted((base / m).glob("*.json")) if (base / m).is_dir() else []
        if files:
            out[m] = files
    if not out:
        files = sorted(base.glob("*.json"))
        if files:
            out[cfg["whisper_model"]] = files
    return out


def recommend_threshold(scores: list[float]) -> float | None:
    """Midpoint of the widest gap between consecutive best-match scores in [0.3, 0.8]."""
    s = sorted(x for x in scores if 0.3 <= x <= 0.8)
    if len(s) < 2:
        return None
    gaps = [(b - a, (a + b) / 2) for a, b in zip(s, s[1:])]
    return round(max(gaps)[1], 2)


def report(cfg: dict) -> None:
    thr = cfg["speaker_match_threshold"]
    for model, files in model_dirs(cfg).items():
        print(f"\n=== {model}: {len(files)} VOD(s) ===")
        named, unknown = [], []
        for jp in files:
            doc = json.loads(jp.read_text())
            segs = doc["segments"]
            for key, score in doc["speaker_scores"].items():
                if score is not None:
                    (unknown if doc["speaker_map"][key].startswith("Unknown_") else named).append(score)
            total = sum(s["end"] - s["start"] for s in segs) or 1
            media = sum(s["end"] - s["start"] for s, f in zip(segs, media_flags(segs, cfg)) if f)
            mp = P(cfg, "muted") / f"{jp.stem}.json"
            spans = json.loads(mp.read_text()) if mp.exists() else []
            speakers = sorted({s["speaker"] for s in segs})
            print(f"{jp.stem}: media {100 * media / total:.0f}% of transcript time; "
                  f"muted {', '.join(f'{fmt_ts(a)}-{fmt_ts(b)}' for a, b in spans) or 'none'}; speakers {', '.join(speakers)}")
        edges = np.linspace(0, 1, 11)
        for label, vals in (("named", named), ("unknown", unknown)):
            hist = np.histogram(vals, bins=edges)[0] if vals else np.zeros(10, int)
            print(f"best-match scores, {label:<7} (n={len(vals)}): " +
                  " ".join(f"{edges[i]:.1f}:{hist[i]}" for i in range(10)))
        rec = recommend_threshold(named + unknown)
        print(f"current threshold {thr}; recommended {rec if rec is not None else 'n/a (too few speakers)'}")


def write_sheet(cfg: dict, n: int, force: bool) -> None:
    dest = P(cfg, "qa") / "qa_labels.tsv"
    if dest.exists() and not force:
        if any(r["true_speaker"] or r["transcript_ok"] for r in read_tsv(dest)):
            print(f"{dest} already has labels; not overwriting (use --force)")
            return
    rng = random.Random(4)
    rows = []
    for model, files in model_dirs(cfg).items():
        turns = []
        for jp in files:
            segs = json.loads(jp.read_text())["segments"]
            for t in merge_turns(segs, media_flags(segs, cfg), cfg["turn_merge_gap_seconds"], cfg.get("turn_max_seconds", 1e9)):
                if len(t["text"].split()) >= 4:
                    turns.append((jp.stem, t))
        for vid, t in rng.sample(turns, min(n, len(turns))):
            rows.append({"model": model, "video_id": vid, "start": round(t["start"], 1), "end": round(t["end"], 1),
                         "link": f"https://{cfg['site_domain']}/" + watch_link(vid, t["start"]),
                         "predicted_speaker": t["speaker"], "text": t["text"].replace("\t", " "),
                         "true_speaker": "", "transcript_ok": ""})
    with open(dest, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LABEL_FIELDS, delimiter="\t")
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {len(rows)} turns to {dest}; fill true_speaker and transcript_ok (y/n), then run qa.py --score")


def score(cfg: dict) -> int:
    rows = read_tsv(P(cfg, "qa") / "qa_labels.tsv")
    if not rows:
        print("qa/qa_labels.tsv not found; run qa.py first")
        return 1
    rtf = defaultdict(list)
    for r in read_tsv(P(cfg, "logs") / "process_times.tsv"):
        rtf[r["model"]].append(float(r["realtime_factor"]))
    by = defaultdict(list)
    for r in rows:
        by[r["model"]].append(r)
    print(f"{'model':<18}{'speaker acc':>14}{'transcript ok':>16}{'realtime x':>12}")
    for model, rs in by.items():
        sp = [r for r in rs if r["true_speaker"].strip()]
        ok = [r for r in rs if r["transcript_ok"].strip()]
        acc = sum(r["true_speaker"].strip().lower() == r["predicted_speaker"].lower() for r in sp)
        good = sum(r["transcript_ok"].strip().lower() in YES for r in ok)
        print(f"{model:<18}{f'{acc}/{len(sp)}' if sp else 'unlabeled':>14}{f'{good}/{len(ok)}' if ok else 'unlabeled':>16}"
              f"{(f'{np.mean(rtf[model]):.1f}' if rtf[model] else 'n/a'):>12}")
    return 0


def health(cfg: dict) -> int:
    con = connect_archive(cfg)
    now, week = time.time(), 7 * 86400
    since = int((now - week) * 1000)
    print("== chat logger, last 7 days ==")
    hb = P(cfg, "logs") / "chat_logger.heartbeat"
    age = now - int(hb.read_text()) if hb.exists() else None
    print("heartbeat:", f"{age / 60:.0f} min ago" + ("  <-- STALE" if age > 600 else "") if age is not None else "never written")
    sessions = con.execute("SELECT channel, start_ms, end_ms FROM sessions WHERE end_ms >= ? ORDER BY start_ms", (since,)).fetchall()
    for s in sessions:
        print(f"session {s['channel']} {time.strftime('%Y-%m-%d %H:%M', time.localtime(s['start_ms'] / 1000))} "
              f"{(s['end_ms'] - s['start_ms']) / 3.6e6:.1f} h")
    gaps = con.execute(
        "SELECT channel, prev, ts_ms FROM (SELECT channel, ts_ms, LAG(ts_ms) OVER (PARTITION BY channel ORDER BY ts_ms) prev "
        "FROM live_msgs WHERE ts_ms >= ?) WHERE ts_ms - prev > 600000 AND ts_ms - prev < ?",
        (since, cfg["session_gap_minutes"] * 60000)).fetchall()
    for g in gaps:
        print(f"GAP {g['channel']} {time.strftime('%Y-%m-%d %H:%M', time.localtime(g['prev'] / 1000))} "
              f"{(g['ts_ms'] - g['prev']) / 60000:.0f} min without messages mid-stream")
    print(f"{len(sessions)} session(s), {len(gaps)} gap(s) over 10 min")
    print("\n== failures ==")
    failed = read_tsv(P(cfg, "logs") / "failed.tsv")
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - week))
    recent = [f for f in failed if f["timestamp"][:19] >= cutoff]
    steps = defaultdict(int)
    for f in recent:
        steps[f["step"]] += 1
    print(f"failed.tsv: {len(failed)} total, {len(recent)} this week " + " ".join(f"{k}={v}" for k, v in steps.items()))
    print("\n== VODs without chat ==")
    rows = con.execute("SELECT id, upload_date FROM videos WHERE transcript_hash IS NOT NULL AND "
                       "(chat_source IS NULL OR chat_source='none') ORDER BY upload_date").fetchall()
    print(" ".join(r["id"] for r in rows) or "none")
    print("\n== low-confidence alignments ==")
    rows = con.execute("SELECT video_id, MIN(confidence) c FROM chat_anchors GROUP BY video_id HAVING c < 0.5").fetchall()
    print(" ".join(f"{r['video_id']}({r['c']:.2f})" for r in rows) or "none")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--turns", type=int, default=20, help="turns per model in the labeling sheet")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    if a.score:
        return score(cfg)
    if a.health:
        return health(cfg)
    report(cfg)
    write_sheet(cfg, a.turns, a.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
