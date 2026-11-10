"""Builds a small synthetic project (2 VODs, live + replay chat) in a temp dir."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))

START = 1793500000  # stream start of the Twitch VOD (unix)


def seg(start, end, speaker, text, raw=None, score=0.8):
    return {"start": start, "end": end, "speaker_raw": raw or f"c000:{speaker}", "speaker": speaker, "score": score,
            "text": text, "words": []}


def transcript(vid, segments):
    scores = {s["speaker_raw"]: s["score"] for s in segments}
    return {"video_id": vid, "model": "large-v3", "chunks": [[0, 600]],
            "speaker_map": {s["speaker_raw"]: s["speaker"] for s in segments}, "speaker_scores": scores,
            "unknown_clusters": {}, "segments": segments}


def run(root, script, *args, check=True):
    env = dict(os.environ, VOD_ARCHIVE_CONFIG=str(root / "config.yaml"))
    r = subprocess.run([sys.executable, str(REPO / "pipeline" / script), *args], env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"{script} failed:\n{r.stdout}\n{r.stderr}")
    return r


def build_project(root: Path) -> dict:
    cfg = yaml.safe_load((REPO / "config.yaml").read_text())
    cfg.update(channel_name="teststreamer", log_channels=["teststreamer"], site_domain="localhost", site_password_required=False,
               contact_email="archive@example.org", rate_limit_per_minute=1000)
    (root / "config.yaml").write_text(yaml.safe_dump(cfg))
    from common import connect_archive, load_config, make_dirs, P
    cfg = load_config(root / "config.yaml")
    make_dirs(cfg)
    audio, tr, chat = P(cfg, "audio"), P(cfg, "transcripts"), P(cfg, "chat")

    # --- VOD 1: Twitch copy with live-logged chat ---
    (audio / "v1001.info.json").write_text(json.dumps(
        {"title": "Solo stream <b>news</b>", "upload_date": "20261101", "duration": 600, "timestamp": START}))
    (audio / "v1001.meta.json").write_text(json.dumps(
        {"platform": "twitch", "source_url": "https://www.twitch.tv/videos/1001", "embed_type": "twitch",
         "embed_id": "v1001", "twitch_vod_id": "1001"}))
    segs = [seg(1, 6, "Hasan", "Welcome back everybody to the broadcast."),
            seg(6.5, 12, "Hasan", "Today we are talking about the housing market and rent control."),
            seg(14, 17, "Unknown_1", "Thanks for having me.", score=0.31),
            seg(18, 24, "Hasan", "Borderline label right here.", raw="c000:SPEAKER_09", score=0.57),
            seg(130, 150, "Unknown_2", "This documentary narration goes on for a long while about glaciers.", score=0.2),
            seg(152, 170, "Unknown_2", "Glaciers are retreating at an unprecedented rate.", score=0.2),
            seg(300, 310, "Hasan", "Okay chat that was the glacier video.")]
    (tr / "v1001.json").write_text(json.dumps(transcript("v1001", segs)))
    (P(cfg, "muted") / "v1001.json").write_text(json.dumps([[400.0, 430.0]]))

    con = connect_archive(cfg)
    def live(t, uid, author, text, **kw):
        con.execute("INSERT INTO live_msgs(ts_ms, channel, room_id, msg_id, user_id, author, text, badges, first_msg, "
                    "returning_chatter, subscriber, deleted, kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int((START + t) * 1000), "teststreamer", "99", f"m{t}", uid, author, text, kw.get("badges", ""),
                     kw.get("first", 0), 0, kw.get("sub", 0), kw.get("deleted", 0), kw.get("kind", "msg")))
    live(5, "1", "alice", "hello chat", first=1)
    live(8, "2", "bob", "rent is too damn high https://example.com/x", sub=1, badges="subscriber/12")
    live(9, "3", "carol_old", "message from before my rename")
    live(20, "3", "carol_new", "message after my rename")
    live(21, "4", "dave", "something the mods removed", deleted=1)
    live(22, "4", "dave", "timeout 600s", kind="mod_action")
    for i, name in enumerate(["u10", "u11", "u12", "u13", "u14"]):
        live(61 + i, str(10 + i), name, "KEKW @alice lol")
    live(70, "1", "alice", "KEKW secretword")
    live(3000, "1", "alice", "after the stream ended")
    con.execute("INSERT INTO sessions VALUES('teststreamer', ?, ?)", (START * 1000, (START + 600) * 1000))
    con.commit()
    con.close()

    # --- VOD 2: YouTube re-upload with replay chat and a mid-stream cut (120 s removed at t=300) ---
    (audio / "ytABCDEFGH1.info.json").write_text(json.dumps(
        {"title": "Guest interview", "upload_date": "20261020", "duration": 500}))
    (audio / "ytABCDEFGH1.meta.json").write_text(json.dumps(
        {"platform": "youtube", "source_url": "https://www.youtube.com/watch?v=ytABCDEFGH1", "embed_type": "youtube",
         "embed_id": "ytABCDEFGH1", "twitch_vod_id": "2002"}))
    segs = [seg(2, 9, "Hasan", "We have a guest today to discuss union organizing."),
            seg(10, 13, "Unknown_1", "Happy to be here.", score=0.3),
            seg(320, 328, "Hasan", "And we are back after the break.")]
    (tr / "ytABCDEFGH1.json").write_text(json.dumps(transcript("ytABCDEFGH1", segs)))
    rows = [{"t": 4, "user_id": "1", "author": "alice", "text": "union strong", "badges": ""},
            {"t": 350, "user_id": "2", "author": "bob", "text": "said during the cut", "badges": "subscriber/3"},
            {"t": 445, "user_id": "2", "author": "bob", "text": "welcome back", "badges": "subscriber/3"}]
    (chat / "ytABCDEFGH1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return cfg


@pytest.fixture(scope="session")
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("proj")
    cfg = build_project(root)
    os.environ["VOD_ARCHIVE_CONFIG"] = str(root / "config.yaml")
    run(root, "align_chat.py")                       # Twitch VOD -> same_source; YouTube -> manual placeholder
    run(root, "align_chat.py", "--set", "ytABCDEFGH1", "0", "0")
    run(root, "align_chat.py", "--set", "ytABCDEFGH1", "300", "120")
    (root / "optout.txt").write_text("# opt-outs\ncarol_old\n")
    run(root, "build_db.py")
    run(root, "publish.py")
    return root, cfg
