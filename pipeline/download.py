"""Download audio + info.json for inventoried VODs; skips live/recent VODs and duplicate copies."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import time

from common import P, get_logger, load_config, log_failure, make_dirs, read_tsv

log = get_logger("download")


def _date(s: str):
    try:
        return dt.datetime.strptime(s, "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def find_twins(rows: list[dict], cfg: dict) -> dict[str, dict]:
    """Map YouTube id -> Twitch row for the same stream (date +/- N days, duration within tolerance)."""
    days, tol = cfg["dedupe_date_days"], cfg["dedupe_duration_tolerance"]
    twitch = [r for r in rows if r["platform"] == "twitch" and r["duration"] and _date(r["date"])]
    twins, used = {}, set()
    for r in rows:
        if r["platform"] != "youtube" or not r["duration"] or not _date(r["date"]):
            continue
        d, dur = _date(r["date"]), float(r["duration"])
        best = None
        for t in twitch:
            if t["id"] in used:
                continue
            if abs((_date(t["date"]) - d).days) > days:
                continue
            rel = abs(float(t["duration"]) - dur) / max(float(t["duration"]), 1)
            if rel <= tol and (best is None or rel < best[0]):
                best = (rel, t)
        if best:
            twins[r["id"]] = best[1]
            used.add(best[1]["id"])
    return twins


def is_recent_or_live(row: dict, cfg: dict, now: float | None = None) -> bool:
    if str(row.get("is_live")) == "1":
        return True
    now = now or time.time()
    if row.get("timestamp") and row.get("duration"):
        end = float(row["timestamp"]) + float(row["duration"])
        return now - end < cfg["skip_if_ended_within_hours"] * 3600
    if row.get("timestamp") and not row.get("duration"):
        return True  # started but no duration yet: still live
    return False


def meta_for(row: dict, twin: dict | None) -> dict:
    if row["platform"] == "twitch":
        num = row["id"].lstrip("v")
        return {"platform": "twitch", "source_url": row["url"] or f"https://www.twitch.tv/videos/{num}",
                "embed_type": "twitch", "embed_id": f"v{num}", "twitch_vod_id": num,
                "twitch_timestamp": row.get("timestamp") or None}
    return {"platform": "youtube", "source_url": row["url"] or f"https://www.youtube.com/watch?v={row['id']}",
            "embed_type": "youtube", "embed_id": row["id"],
            "twitch_vod_id": twin["id"].lstrip("v") if twin else None,
            "twitch_timestamp": (twin.get("timestamp") or None) if twin else None}


def plan(rows: list[dict], cfg: dict, have: set[str]) -> tuple[list[dict], dict[str, dict]]:
    """Rows to download, in inventory order. A YouTube copy is skipped while its Twitch twin is listed."""
    twins = find_twins(rows, cfg)
    todo = []
    for r in rows:
        if r["id"] in have:
            continue
        if r["platform"] == "youtube" and r["id"] in twins and twins[r["id"]]["id"] not in have:
            continue  # prefer the Twitch copy while it exists
        if r["platform"] == "twitch":
            yt = next((y for y, t in twins.items() if t["id"] == r["id"]), None)
            if yt in have:
                continue  # already have the YouTube copy of this stream
        todo.append(r)
    return todo, twins


def fetch(row: dict, cfg: dict) -> None:
    audio = P(cfg, "audio")
    lo, hi = cfg["download_sleep"]
    cmd = ["yt-dlp", "-x", "--audio-format", cfg["audio_format"], "--audio-quality", str(cfg["audio_quality"]),
           "--download-archive", str(P(cfg, "logs") / "download_archive.txt"),
           "--write-info-json", "--ignore-errors", "--no-playlist",
           "--match-filter", "!is_live & live_status!=is_live & live_status!=post_live",
           "--sleep-interval", str(lo), "--max-sleep-interval", str(hi),
           "-o", str(audio / f"{row['id']}.%(ext)s")]
    if row["platform"] == "twitch":
        cmd += ["-f", "audio_only/bestaudio/worst"]
    else:
        cmd += ["-f", "bestaudio/best"]
    url = row["url"] or (f"https://www.twitch.tv/videos/{row['id'].lstrip('v')}" if row["platform"] == "twitch"
                         else f"https://www.youtube.com/watch?v={row['id']}")
    out = subprocess.run(cmd + [url], capture_output=True, text=True)
    if not (audio / f"{row['id']}.{cfg['audio_format']}").exists():
        raise RuntimeError((out.stderr or out.stdout).strip()[-400:] or "no audio produced (skipped as live?)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only")
    ap.add_argument("--yes", action="store_true", help="allow more than 5 downloads in one run")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    audio = P(cfg, "audio")
    rows = read_tsv(P(cfg, "logs") / "inventory.tsv")
    if not rows:
        log.error("logs/inventory.tsv is empty; run inventory.py first")
        return 1
    have = {p.stem for p in audio.glob(f"*.{cfg['audio_format']}")}
    todo, twins = plan(rows, cfg, have)
    if a.only:
        todo = [r for r in rows if r["id"] == a.only and r["id"] not in have]
    skipped = [r for r in todo if is_recent_or_live(r, cfg)]
    todo = [r for r in todo if not is_recent_or_live(r, cfg)]
    for r in skipped:
        log.info("skip %s: live or ended within %sh (not archived)", r["id"], cfg["skip_if_ended_within_hours"])
    if a.limit:
        todo = todo[:a.limit]
    if len(todo) > 5 and not a.yes:
        print(f"{len(todo)} VODs pending. Downloading more than 5 needs --yes (or use --limit).")
        return 2
    ok = 0
    for r in todo:
        try:
            fetch(r, cfg)
            meta = meta_for(r, twins.get(r["id"]))
            (audio / f"{r['id']}.meta.json").write_text(json.dumps(meta))
            ok += 1
            log.info("downloaded %s %s", r["id"], r["title"][:60])
        except Exception as e:
            log.error("failed %s: %s", r["id"], e)
            log_failure(cfg, r["id"], "download", e)
    # keep pairing info fresh for copies we already hold
    for r in rows:
        mp = audio / f"{r['id']}.meta.json"
        if r["id"] in have and (not mp.exists() or (r["id"] in twins and not json.loads(mp.read_text()).get("twitch_vod_id"))):
            mp.write_text(json.dumps(meta_for(r, twins.get(r["id"]))))
    print(f"downloaded {ok}/{len(todo)}; skipped {len(skipped)} live/recent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
