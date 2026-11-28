"""VOD chat replay -> data/chat/<id>.jsonl with {t, user_id, author, text, badges}.

Since about Nov 20 2026 Twitch answers TwitchDownloaderCLI's parallel downloads with only the first pages of each
thread's share of the VOD, and the CLI still reports success (upstream issue lay295/TwitchDownloader#1685): with the
default 4 threads a replay came back with ~5,400 of ~150,000 messages. So the CLI runs with one thread
(twitch_downloader_threads), and every replay is compared with the live chat log for the same stream: one with
less than replay_min_share of the live log's messages is replaced by the live log's messages, with a warning.
  chat_replay.py --redo VIDEO_ID   download one replay again (the old file is kept as <id>.jsonl.short.bak)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from common import P, connect_archive, get_logger, load_config, log_failure, make_dirs

log = get_logger("chat_replay")
REPLAY_MIN_SHARE = 0.5        # a replay with fewer messages than this share of the live log is not trusted
LIVE_MIN = 1000               # below this the live log is too thin to judge a replay by


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
        threads = str(cfg.get("twitch_downloader_threads", 1))   # more than 1 loses most of the chat (see top)
        r = subprocess.run([cfg["twitch_downloader_cli"], "chatdownload", "--id", vod_id, "--threads", threads,
                            "-o", str(out)], capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "TwitchDownloaderCLI failed")
        return convert_twitchdownloader(json.loads(out.read_text(), strict=False))  # chat bodies can hold raw control characters


def via_chat_downloader(vod_id: str) -> list[dict]:
    from chat_downloader import ChatDownloader
    rows = []
    # Bounded retries (Nov 27 2026): with the defaults, a Twitch error ('data' missing from its answer) was retried with
    # doubling waits and an "or press Enter" prompt, so the hourly run sat in this step for over an hour, never publishing.
    for m in ChatDownloader().get_chat(f"https://www.twitch.tv/videos/{vod_id}", max_attempts=3, retry_timeout=10,
                                      interruptible_retry=False, inactivity_timeout=300):
        who = m.get("author") or {}
        badges = ",".join(f"{b.get('name')}/{b.get('version', '')}" for b in (who.get("badges") or []))
        rows.append({"t": float(m.get("time_in_seconds", 0)), "user_id": str(who.get("id") or ""),
                     "author": who.get("name") or who.get("display_name") or "",
                     "text": m.get("message") or "", "badges": badges})
    rows.sort(key=lambda r: r["t"])
    return rows


def stream_window(con, cfg: dict, vid: str) -> tuple[float, float] | None:
    """(start unix, duration s) of a VOD's stream, from archive.db or its info/meta files; None if unknown."""
    audio = P(cfg, "audio")
    try:
        meta = json.loads((audio / f"{vid}.meta.json").read_text())
    except (OSError, ValueError):
        meta = {}
    ip = audio / f"{vid}.info.json"
    info = json.loads(ip.read_text()) if ip.exists() else {}
    row = con.execute("SELECT stream_start_unix, duration FROM videos WHERE id=?", (vid,)).fetchone()
    start = (row["stream_start_unix"] if row else None) or meta.get("stream_start_unix") or \
        (info.get("timestamp") if meta.get("platform") == "twitch" else None) or meta.get("twitch_timestamp")
    dur = info.get("duration") or (row["duration"] if row else None)
    return (float(start), float(dur)) if start and dur else None


def live_rows(con, cfg: dict, start: float, dur: float) -> list[dict]:
    """The live logger's messages for the stream, in the replay format (t = seconds since the stream started, which is
    the Twitch VOD's own clock). Mod-deleted messages are left out, as a replay never has them."""
    rows = []
    for r in con.execute("SELECT ts_ms, user_id, author, text, badges FROM live_msgs WHERE channel=? AND kind='msg' "
                         "AND COALESCE(deleted,0)=0 AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
                         (cfg["channel_name"], (start - 5) * 1000, (start + dur + 5) * 1000)):
        rows.append({"t": round(max(r["ts_ms"] / 1000 - start, 0.0), 3), "user_id": str(r["user_id"] or ""),
                     "author": r["author"] or "", "text": r["text"] or "", "badges": r["badges"] or ""})
    return rows


def checked_rows(con, cfg: dict, vid: str, rows: list[dict]) -> tuple[list[dict], str]:
    """-> (rows to keep, source). A replay far smaller than the live log is replaced by the live log."""
    win = stream_window(con, cfg, vid)
    if not win:
        return rows, "replay"
    live = live_rows(con, cfg, *win)
    if len(live) >= LIVE_MIN and len(rows) < cfg.get("replay_min_share", REPLAY_MIN_SHARE) * len(live):
        log.warning("%s: the replay has %d messages but the live log has %d; using the live log", vid, len(rows), len(live))
        return live, "live"
    return rows, "replay"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only")
    ap.add_argument("--redo", metavar="VIDEO_ID", help="download this replay again; the old one is kept as .short.bak")
    a = ap.parse_args()
    if a.redo:
        a.only = a.redo
    cfg = load_config()
    make_dirs(cfg)
    done = none = 0
    con = connect_archive(cfg)
    for meta_path in sorted(P(cfg, "audio").glob("*.meta.json")):
        vid = meta_path.name[:-len(".meta.json")]
        dest = P(cfg, "chat") / f"{vid}.jsonl"
        if a.only and vid != a.only:
            continue
        if dest.exists():
            if a.redo != vid:
                continue
            dest.replace(dest.with_name(dest.name + ".short.bak"))
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
            rows, source = checked_rows(con, cfg, vid, rows)
            tmp = dest.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:   # Windows' default encoding can't hold chat
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            tmp.replace(dest)
            done += 1
            log.info("%s: %d %s messages", vid, len(rows), "replay" if source == "replay" else "live-log")
        except Exception as e:
            log.error("%s: no chat replay: %s", vid, e)
            log_failure(cfg, vid, "chat_replay", e)
            bak = dest.with_name(dest.name + ".short.bak")
            if a.redo == vid and bak.exists() and not dest.exists():
                bak.replace(dest)                # a failed redo leaves the old replay where it was
    print(f"chat replay: {done} downloaded, {none} VOD(s) with no Twitch VOD id")


if __name__ == "__main__":
    main()
