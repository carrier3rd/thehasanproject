"""Download audio + info.json for inventoried VODs; skips live/recent VODs and duplicate copies."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import time
from pathlib import Path

from common import P, get_logger, load_config, log_failure, make_dirs, read_tsv, write_tsv

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


def video_url(row: dict) -> str:
    if row.get("url"):
        return row["url"]
    if row["platform"] == "twitch":
        return f"https://www.twitch.tv/videos/{row['id'].lstrip('v')}"
    return f"https://www.youtube.com/watch?v={row['id']}"


def refresh_meta(row: dict, runner=subprocess.run) -> dict:
    """Fill start time, duration, date and live state from the video's own page (the playlist listing
    often lacks them, e.g. Twitch). Raises if the lookup fails, so an unknown VOD is never downloaded blind."""
    if row.get("timestamp") and row.get("date"):
        return row
    out = runner(["yt-dlp", "-J", "--skip-download", "--no-playlist", video_url(row)], capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError((out.stderr or "metadata lookup failed").strip()[-300:])
    info = json.loads(out.stdout)
    ts = info.get("timestamp") or info.get("release_timestamp")
    live = info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live")
    row = dict(row)
    if ts:
        row["timestamp"] = int(ts)
    if info.get("duration"):
        row["duration"] = int(info["duration"])
    row["date"] = info.get("upload_date") or (time.strftime("%Y%m%d", time.gmtime(ts)) if ts else row.get("date", ""))
    row["is_live"] = 1 if live else 0
    return row


KEEP_SUFFIXES = (".info.json", ".meta.json", ".incomplete")


def leftovers(audio: Path, vod_id: str, fmt: str) -> list[Path]:
    """Intermediate files yt-dlp leaves when a download or the audio conversion stops part way."""
    return [f for f in audio.glob(f"{vod_id}.*")
            if f.name != f"{vod_id}.{fmt}" and not f.name.endswith(KEEP_SUFFIXES)]


def probe_seconds(path: Path) -> float | None:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def check_complete(audio: Path, row: dict, fmt: str, probe=probe_seconds) -> str | None:
    """Why the audio file is not a finished download, or None when it is.
    A conversion that is cut off still leaves a playable but short file, so existence alone proves nothing."""
    f = audio / f"{row['id']}.{fmt}"
    if not f.exists():
        return "no audio produced (skipped as live?)"
    left = leftovers(audio, row["id"], fmt)
    if left:
        return "conversion did not finish (leftover " + ", ".join(x.name for x in left) + ")"
    want = float(row.get("duration") or 0)
    got = probe(f) if want else None
    if want and got is not None and got < want * 0.98:
        return f"audio is {got / 3600:.2f} h but the VOD is {want / 3600:.2f} h"
    return None


def fetch(row: dict, cfg: dict) -> None:
    audio = P(cfg, "audio")
    lo, hi = cfg["download_sleep"]
    cmd = ["yt-dlp", "-x", "--audio-format", cfg["audio_format"], "--audio-quality", str(cfg["audio_quality"]),
           "--download-archive", str(P(cfg, "logs") / "download_archive.txt"),
           "--write-info-json", "--ignore-errors", "--no-playlist",
           "--match-filter", "!is_live & live_status!=is_live & live_status!=post_live",
           "--sleep-interval", str(lo), "--max-sleep-interval", str(hi),
           # fetch several HLS pieces at once; one at a time is the main reason Twitch downloads crawl
           "--concurrent-fragments", str(cfg.get("download_fragments", 4)),
           "-o", str(audio / f"{row['id']}.%(ext)s")]
    if row["platform"] == "twitch":
        cmd += ["-f", "audio_only/bestaudio/worst"]
    else:
        cmd += ["-f", "bestaudio/best"]
    url = video_url(row)
    out = subprocess.run(cmd + [url], capture_output=True, text=True)
    problem = check_complete(audio, row, cfg["audio_format"])
    if problem:
        f = audio / f"{row['id']}.{cfg['audio_format']}"
        if f.exists():   # set the short file aside so the next run converts again
            f.rename(f.with_name(f.name + ".incomplete"))
        raise RuntimeError(f"{problem}: " + ((out.stderr or out.stdout).strip()[-300:]))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", nargs="+", metavar="ID", help="these VOD ids only (space or comma separated)")
    ap.add_argument("--yes", action="store_true", help="allow more than 5 downloads in one run")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    audio = P(cfg, "audio")
    rows = read_tsv(P(cfg, "logs") / "inventory.tsv")
    if not rows:
        log.error("logs/inventory.tsv is empty; run inventory.py first")
        return 1
    fmt = cfg["audio_format"]
    # an audio file with yt-dlp leftovers next to it is a conversion that stopped part way: fetch it again
    have = {p.stem for p in audio.glob(f"*.{fmt}") if not leftovers(audio, p.stem, fmt)}
    todo, twins = plan(rows, cfg, have)
    if a.only:
        wanted = [x for part in a.only for x in part.split(",") if x]
        unknown = [w for w in wanted if w not in {r["id"] for r in rows}]
        if unknown:
            print(f"not in logs/inventory.tsv: {' '.join(unknown)}")
            return 1
        todo = [r for r in rows if r["id"] in wanted and r["id"] not in have]
    if a.limit:
        todo = todo[:a.limit]
    if len(todo) > 5 and not a.yes:
        print(f"{len(todo)} VODs pending. Downloading more than 5 needs --yes (or use --limit).")
        return 2
    # look up real start times / live state before deciding (and keep them in the inventory)
    checked, skipped, by_id = [], [], {r["id"]: i for i, r in enumerate(rows)}
    for r in todo:
        try:
            r = refresh_meta(r)
            rows[by_id[r["id"]]] = r
        except Exception as e:
            log.error("skip %s: could not read its details (%s)", r["id"], e)
            log_failure(cfg, r["id"], "download", f"metadata: {e}")
            continue
        (skipped if is_recent_or_live(r, cfg) else checked).append(r)
    write_tsv(P(cfg, "logs") / "inventory.tsv", rows, list(rows[0].keys()))
    todo = checked
    for r in skipped:
        log.info("skip %s: live or ended within %sh (not archived)", r["id"], cfg["skip_if_ended_within_hours"])
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
