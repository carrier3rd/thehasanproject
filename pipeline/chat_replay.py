"""VOD chat replay -> data/chat/<id>.jsonl with {t, user_id, author, text, badges}."""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from common import P, get_logger, load_config, log_failure, make_dirs

log = get_logger("chat_replay")


def convert_twitchdownloader(doc: dict) -> list[dict]:
    rows = []
    for c in doc.get("comments", []):
        who, msg = c.get("commenter") or {}, c.get("message") or {}
        badges = ",".join(f"{b.get('_id')}/{b.get('version')}" for b in (msg.get("user_badges") or []))
        rows.append({"t": float(c.get("content_offset_seconds", 0)), "user_id": str(who.get("_id") or ""),
                     "author": who.get("name") or who.get("display_name") or "",
                     "text": msg.get("body") or "", "badges": badges})
    rows.sort(key=lambda r: r["t"])
    return rows


def via_twitchdownloader(vod_id: str, cfg: dict) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "chat.json"
        r = subprocess.run([cfg["twitch_downloader_cli"], "chatdownload", "--id", vod_id, "-o", str(out)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "TwitchDownloaderCLI failed")
        return convert_twitchdownloader(json.loads(out.read_text()))


def via_chat_downloader(vod_id: str) -> list[dict]:
    from chat_downloader import ChatDownloader
    rows = []
    for m in ChatDownloader().get_chat(f"https://www.twitch.tv/videos/{vod_id}"):
        who = m.get("author") or {}
        badges = ",".join(f"{b.get('name')}/{b.get('version', '')}" for b in (who.get("badges") or []))
        rows.append({"t": float(m.get("time_in_seconds", 0)), "user_id": str(who.get("id") or ""),
                     "author": who.get("name") or who.get("display_name") or "",
                     "text": m.get("message") or "", "badges": badges})
    rows.sort(key=lambda r: r["t"])
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only")
    a = ap.parse_args()
    cfg = load_config()
    make_dirs(cfg)
    done = none = 0
    for meta_path in sorted(P(cfg, "audio").glob("*.meta.json")):
        vid = meta_path.name[:-len(".meta.json")]
        dest = P(cfg, "chat") / f"{vid}.jsonl"
        if (a.only and vid != a.only) or dest.exists():
            continue
        vod_id = json.loads(meta_path.read_text()).get("twitch_vod_id")
        if not vod_id:
            none += 1
            continue
        try:
            try:
                rows = via_twitchdownloader(vod_id, cfg)
            except Exception as e:
                log.warning("%s: TwitchDownloaderCLI failed (%s); trying chat-downloader", vid, e)
                rows = via_chat_downloader(vod_id)
            tmp = dest.with_suffix(".tmp")
            with open(tmp, "w") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            tmp.replace(dest)
            done += 1
            log.info("%s: %d replay messages", vid, len(rows))
        except Exception as e:
            log.error("%s: no chat replay: %s", vid, e)
            log_failure(cfg, vid, "chat_replay", e)
    print(f"chat replay: {done} downloaded, {none} VOD(s) with no Twitch VOD id")


if __name__ == "__main__":
    main()
