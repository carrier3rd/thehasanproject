"""List VODs from every configured source -> logs/inventory.tsv."""
from __future__ import annotations

import argparse
import json
import subprocess
import time

from common import P, get_logger, load_config, log_failure, make_dirs, write_tsv

FIELDS = ["id", "source", "platform", "url", "title", "date", "duration", "is_live", "timestamp"]
log = get_logger("inventory")


def entry_row(e: dict, source: str) -> dict | None:
    vid = e.get("id")
    if not vid:
        return None
    url = e.get("url") or e.get("webpage_url") or ""
    extractor = (e.get("ie_key") or e.get("extractor_key") or e.get("extractor") or "").lower()
    platform = "twitch" if "twitch" in extractor or "twitch.tv" in url or "twitch.tv" in source else "youtube"
    ts = e.get("timestamp") or e.get("release_timestamp")
    date = e.get("upload_date") or (time.strftime("%Y%m%d", time.gmtime(ts)) if ts else "")
    live = e.get("is_live") or e.get("live_status") in ("is_live", "is_upcoming", "post_live")
    return {"id": vid, "source": source, "platform": platform, "url": url,
            "title": (e.get("title") or "").replace("\t", " ").replace("\n", " "),
            "date": date, "duration": int(e["duration"]) if e.get("duration") else "",
            "is_live": 1 if live else 0, "timestamp": int(ts) if ts else ""}


def list_source(source: str) -> list[dict]:
    out = subprocess.run(["yt-dlp", "--flat-playlist", "--dump-json", "--ignore-errors", source],
                         capture_output=True, text=True)
    rows = []
    for line in out.stdout.splitlines():
        try:
            row = entry_row(json.loads(line), source)
        except json.JSONDecodeError:
            continue
        if row:
            rows.append(row)
    if not rows and out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-400:] or "yt-dlp failed")
    return rows


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    cfg = load_config()
    make_dirs(cfg)
    rows, seen = [], set()
    for source in cfg["sources"]:
        try:
            got = list_source(source)
        except Exception as e:
            log.error("source failed: %s: %s", source, e)
            log_failure(cfg, source, "inventory", e)
            continue
        hours = sum(int(r["duration"] or 0) for r in got) / 3600
        print(f"{source}: {len(got)} VODs, {hours:.0f} h, {sum(r['is_live'] for r in got)} live")
        for r in got:
            if r["id"] not in seen:
                seen.add(r["id"])
                rows.append(r)
    if not rows:
        log.error("no VODs found; keeping the previous inventory")
        return 1
    write_tsv(P(cfg, "logs") / "inventory.tsv", rows, FIELDS)
    print(f"total: {len(rows)} VODs -> logs/inventory.tsv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
