import json
import socket
import sqlite3
import subprocess
import threading
import time

import numpy as np
import pytest

from conftest import START, run


# ---------- process.py (GPU-free parts) ----------

def test_chunk_bounds_and_stitch_dedupes_overlap():
    import process as p
    assert p.chunk_bounds(100, 3600, 10) == [(0.0, 100)]
    bounds = p.chunk_bounds(7300, 3600, 10)
    assert bounds == [(0.0, 3610.0), (3600.0, 7210.0), (7200.0, 7300)]
    w = lambda word, s, e, spk: {"word": word, "start": s, "end": e, "speaker": spk}
    # the same three words fall in the overlap zone of both chunks (global 3601..3609)
    c0 = [{"start": 3590, "end": 3609, "speaker": "SPEAKER_00", "text": "x",
           "words": [w("before", 3590, 3591, "SPEAKER_00"), w("one", 3601, 3602, "SPEAKER_00"),
                     w("two", 3604.6, 3605.2, "SPEAKER_00"), w("three", 3608, 3609, "SPEAKER_00")]}]
    c1 = [{"start": 1, "end": 20, "speaker": "SPEAKER_01", "text": "x",
           "words": [w("one", 1, 2, "SPEAKER_01"), w("two", 4.6, 5.2, "SPEAKER_01"), w("three", 8, 9, "SPEAKER_01"),
                     w("after", 19, 20, "SPEAKER_01")]}]
    out = p.stitch([c0, c1, []], bounds, 10)
    words = [x["word"] for s in out for x in s["words"]]
    assert words == ["before", "one", "two", "three", "after"]      # no duplicates, nothing lost
    assert [s["speaker_raw"] for s in out] == ["c000:SPEAKER_00", "c001:SPEAKER_01"]
    assert out[0]["text"] == "before one two" and out[1]["text"] == "three after"


def test_split_by_speaker_and_missing_word_times():
    import process as p
    seg = {"start": 0, "end": 5, "speaker": "A", "text": "hi 42 there",
           "words": [{"word": "hi", "start": 0, "end": 1, "speaker": "A"}, {"word": "42"},
                     {"word": "there", "start": 3, "end": 4, "speaker": "B"}]}
    parts = p.split_by_speaker(seg)
    assert [(x["speaker"], x["text"]) for x in parts] == [("A", "hi 42"), ("B", "there")]


def test_unknown_guest_gets_one_label_across_chunks():
    import process as p
    rng = np.random.default_rng(0)
    hasan, guest, other = (p.l2(rng.normal(size=64)) for _ in range(3))
    noisy = lambda v: p.l2(v + 0.05 * rng.normal(size=64))
    embs = {"c000:S0": noisy(hasan), "c000:S1": noisy(guest), "c001:S0": noisy(guest),
            "c001:S1": noisy(hasan), "c001:S2": noisy(other)}
    smap, scores, clusters = p.name_speakers(embs, {"Hasan": hasan}, 0.55, 0.60)
    assert smap["c000:S0"] == smap["c001:S1"] == "Hasan"
    assert smap["c000:S1"] == smap["c001:S0"] == "Unknown_1"
    assert smap["c001:S2"] == "Unknown_2"
    assert clusters["Unknown_1"] == ["c000:S1", "c001:S0"]
    assert scores["c000:S0"] > 0.9


def test_cleanup_muted_and_repeats():
    import process as p
    s = lambda a, b, t: {"start": a, "end": b, "text": t, "words": []}
    segs = [s(0, 2, "hello"), s(10, 12, "inside mute"), s(20, 21, "Thank you."), s(21, 22, "Thank you."),
            s(22, 23, "thank you"), s(30, 31, "bye"), s(32, 33, "bye")]
    out = p.drop_repeats(p.drop_muted(segs, [[9, 13]]), 3)
    assert [x["text"] for x in out] == ["hello", "bye", "bye"]


def test_rename_only_rewrites_only_changed(project, tmp_path):
    import process as p
    from common import P
    root, cfg = project
    d = P(cfg, "transcripts") / "pilot-test"
    d.mkdir()
    rng = np.random.default_rng(1)
    hasan = p.l2(rng.normal(size=32))
    np.save(P(cfg, "voiceprints") / "Hasan.npy", hasan)
    embs = {"c000:S0": p.l2(hasan + 0.02 * rng.normal(size=32)), "c000:S1": p.l2(rng.normal(size=32))}
    segs = [{"start": 0, "end": 1, "speaker_raw": "c000:S0", "text": "a", "words": []},
            {"start": 2, "end": 3, "speaker_raw": "c000:S1", "text": "b", "words": []}]
    p.save_embs(d / "x.emb.npz", embs)
    p.write_json(d / "x.json", p.finish("x", "m", [(0, 3)], segs, embs, dict(cfg, speaker_match_threshold=0.999999)))
    assert json.loads((d / "x.json").read_text())["segments"][0]["speaker"].startswith("Unknown_")
    r = run(root, "process.py", "--rename-only", "--model", "pilot-test")
    assert "renamed speakers in 1" in r.stdout
    assert [s["speaker"] for s in json.loads((d / "x.json").read_text())["segments"]] == ["Hasan", "Unknown_1"]
    assert "renamed speakers in 0" in run(root, "process.py", "--rename-only", "--model", "pilot-test").stdout
    (P(cfg, "voiceprints") / "Hasan.npy").unlink()


# ---------- download planning ----------

def test_download_plan_prefers_twitch_and_skips_live():
    import download as d
    cfg = {"dedupe_date_days": 1, "dedupe_duration_tolerance": 0.02, "skip_if_ended_within_hours": 2}
    now = 1_800_000_000
    rows = [
        {"id": "v1", "platform": "twitch", "date": "20261101", "duration": "10000", "is_live": "0", "timestamp": str(now - 90000), "url": ""},
        {"id": "yt1", "platform": "youtube", "date": "20261102", "duration": "9900", "is_live": "0", "timestamp": "", "url": ""},
        {"id": "yt2", "platform": "youtube", "date": "20260101", "duration": "5000", "is_live": "0", "timestamp": "", "url": ""},
        {"id": "v2", "platform": "twitch", "date": "20261109", "duration": "3600", "is_live": "0", "timestamp": str(now - 3600 - 600), "url": ""},
        {"id": "v3", "platform": "twitch", "date": "20261109", "duration": "", "is_live": "1", "timestamp": str(now - 100), "url": ""},
    ]
    todo, twins = d.plan(rows, cfg, have=set())
    assert twins["yt1"]["id"] == "v1"
    assert [r["id"] for r in todo] == ["v1", "yt2", "v2", "v3"]            # yt1 waits while the Twitch copy exists
    assert [r["id"] for r in todo if d.is_recent_or_live(r, cfg, now)] == ["v2", "v3"]
    assert d.meta_for(rows[1], twins["yt1"])["twitch_vod_id"] == "1"
    todo, _ = d.plan(rows, cfg, have={"yt1"})
    assert "v1" not in [r["id"] for r in todo]                             # already hold the YouTube copy


# ---------- muted.py ----------

@pytest.mark.parametrize("ext,codec", [("wav", []), ("opus", ["-c:a", "libopus", "-b:a", "64k"])])
def test_muted_span_detected_within_5s(tmp_path, ext, codec):
    import muted
    path = tmp_path / f"a.{ext}"
    src = "anoisesrc=d=30:c=pink:a=0.3,volume=1[a];anullsrc=r=48000:d=25[b];anoisesrc=d=20:c=pink:a=0.3[c];[a][b][c]concat=n=3:v=0:a=1"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src, "-ar", "48000", "-ac", "1", *codec, str(path)], check=True)
    spans = muted.detect(path, {"mute_noise_db": -70, "mute_min_seconds": 20})
    assert len(spans) == 1
    assert abs(spans[0][0] - 30) < 5 and abs(spans[0][1] - 55) < 5
    assert muted.from_metadata({"muted_segments": [{"offset": 10, "duration": 30}]}) == [[10.0, 40.0]]


# ---------- align_chat.py ----------

def speechlike(seconds, seed):
    rng = np.random.default_rng(seed)
    n = seconds * 100
    env = np.convolve(rng.normal(size=n + 200), np.ones(40) / 40, mode="valid")[:n]
    return (4 + 3 * env + 0.05 * rng.normal(size=n)).astype(np.float32)


def test_anchor_map_finds_trim_and_mid_stream_cut():
    import align_chat as a
    from common import Anchors
    cfg = {"chat_anchor_window_seconds": 60, "chat_anchor_every_minutes": 15, "chat_anchor_max_jump_seconds": 2.0,
           "chat_anchor_bisect_seconds": 30}
    twitch = speechlike(2 * 3600, 3)
    # re-upload: first 95 s trimmed, and 400 s cut out starting at twitch time 3000 s
    yt = np.concatenate([twitch[95 * 100:3000 * 100], twitch[3400 * 100:]])
    yt = yt + 0.1 * np.random.default_rng(9).normal(size=len(yt)).astype(np.float32)   # re-encode noise
    anchors = a.build_anchors(yt, twitch, cfg)
    assert all(c >= 0.5 for _, _, c in anchors), anchors
    m = Anchors([(t, o) for t, o, _ in anchors])
    cut_at = 3000 - 95                                    # transcript time of the cut
    for t in (10, 1500, cut_at - 40, cut_at + 40, 5000, 6500):   # spot checks across the stream
        truth = t + 95 if t < cut_at else t + 495
        assert abs(m.to_chat(t) - truth) <= 2.0, (t, m.to_chat(t), truth, anchors)
    assert m.to_transcript(3200) is None                  # chat sent during the cut maps nowhere
    assert abs(m.to_transcript(3500) - (3500 - 495)) <= 2.0


def test_anchor_same_audio_is_single_zero_anchor():
    import align_chat as a
    cfg = {"chat_anchor_window_seconds": 60, "chat_anchor_every_minutes": 15, "chat_anchor_max_jump_seconds": 2.0,
           "chat_anchor_bisect_seconds": 30}
    x = speechlike(3600, 5)
    assert [(t, o) for t, o, _ in a.build_anchors(x, x, cfg)] == [(0.0, 0.0)]


def test_envelope_decodes_audio(tmp_path):
    import align_chat as a
    path = tmp_path / "t.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=3", str(path)], check=True)
    assert abs(len(a.envelope(path)) - 300) <= 2


# ---------- chat_logger.py ----------

IRC = [
    ":tmi.twitch.tv 001 justinfan1 :Welcome",
    "@badge-info=subscriber/14;badges=subscriber/12,premium/1;first-msg=1;id=abc-1;returning-chatter=0;room-id=99;"
    "subscriber=1;tmi-sent-ts=1793500001000;user-id=42;display-name=Al\\sice :alice!alice@alice.tmi.twitch.tv PRIVMSG #teststreamer :hello : world",
    "@first-msg=0;id=abc-2;room-id=99;subscriber=0;tmi-sent-ts=1793500002000;user-id=43 :bob!bob@bob.tmi.twitch.tv PRIVMSG #teststreamer :\x01ACTION waves\x01",
    "PING :tmi.twitch.tv",
    "@login=bob;room-id=99;target-msg-id=abc-2;tmi-sent-ts=1793500003000 :tmi.twitch.tv CLEARMSG #teststreamer :waves",
    "@ban-duration=600;room-id=99;target-user-id=43;tmi-sent-ts=1793500004000 :tmi.twitch.tv CLEARCHAT #teststreamer :bob",
    "@room-id=99;target-user-id=44;tmi-sent-ts=1793500005000 :tmi.twitch.tv CLEARCHAT #teststreamer :eve",
    "@room-id=99;tmi-sent-ts=1793500006000 :tmi.twitch.tv CLEARCHAT #teststreamer",
    "garbage line that should not crash anything",
]


def test_logger_against_fake_irc_server(tmp_path):
    import chat_logger as cl
    from common import load_config
    import yaml
    from conftest import REPO
    cfg = yaml.safe_load((REPO / "config.yaml").read_text())
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cfg.update(irc_host="127.0.0.1", irc_port=srv.getsockname()[1], log_channels=["teststreamer"], logger_batch_seconds=0.1)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    cfg = load_config(tmp_path / "config.yaml")
    received = []

    def serve():
        conn, _ = srv.accept()
        conn.settimeout(3)
        for line in IRC:
            conn.sendall((line + "\r\n").encode())
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                received.append(data.decode())
                if "PONG" in "".join(received):
                    break
        except OSError:
            pass
        time.sleep(0.5)
        conn.close()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    store = cl.Store(cfg)
    with pytest.raises(ConnectionError):          # server closes -> the caller's reconnect loop takes over
        cl.run_once(cfg, store, time.monotonic() + 10)
    th.join()
    sent = "".join(received)
    assert "CAP REQ :twitch.tv/tags twitch.tv/commands" in sent and "JOIN #teststreamer" in sent
    assert "NICK justinfan" in sent and "PONG :tmi.twitch.tv" in sent
    con = sqlite3.connect(tmp_path / "archive.db")
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM live_msgs ORDER BY ts_ms").fetchall()
    assert [(r["author"], r["text"], r["kind"], r["deleted"]) for r in rows] == [
        ("alice", "hello : world", "msg", 0), ("bob", "/me waves", "msg", 1),
        ("bob", "timeout 600s", "mod_action", 0), ("eve", "ban", "mod_action", 0)]
    a = rows[0]
    assert (a["ts_ms"], a["room_id"], a["msg_id"], a["user_id"], a["badges"], a["first_msg"], a["returning_chatter"], a["subscriber"]) == \
        (1793500001000, "99", "abc-1", "42", "subscriber/12,premium/1", 1, 0, 1)
    assert con.execute("SELECT channel, start_ms, end_ms FROM sessions").fetchall()[0][:] == ("teststreamer", 1793500001000, 1793500002000)
    # a message 31 minutes later opens a new session; one 5 minutes after that extends it
    store.handle(cl.parse_line("@tmi-sent-ts=1793501862000;user-id=1;id=z :a!a@a PRIVMSG #teststreamer :later"))
    store.handle(cl.parse_line("@tmi-sent-ts=1793502162000;user-id=1;id=y :a!a@a PRIVMSG #teststreamer :later2"))
    store.maybe_commit(force=True)
    assert con.execute("SELECT start_ms, end_ms FROM sessions ORDER BY start_ms").fetchall()[1][:] == (1793501862000, 1793502162000)


# ---------- build_db.py + publish.py ----------

def dbs(project):
    root, _ = project
    arc, pub = sqlite3.connect(root / "archive.db"), sqlite3.connect(root / "public.db")
    arc.row_factory = pub.row_factory = sqlite3.Row
    return arc, pub


def test_build_db_turns_media_and_chat(project):
    arc, _ = dbs(project)
    turns = arc.execute("SELECT * FROM turns WHERE video_id='v1001' ORDER BY start").fetchall()
    assert turns[0]["text"] == "Welcome back everybody to the broadcast. Today we are talking about the housing market and rent control."
    assert [(t["speaker"], t["is_media"]) for t in turns] == [
        ("Hasan", 0), ("Unknown_1", 0), ("Hasan", 0), ("Unknown_2", 1), ("Unknown_2", 1), ("Hasan", 0)]
    v = arc.execute("SELECT * FROM videos WHERE id='v1001'").fetchone()
    assert (v["chat_source"], v["stream_start_unix"], v["stream_start_source"], v["embed_id"]) == ("live", START, "twitch_info", "v1001")
    chat = arc.execute("SELECT * FROM chat WHERE video_id='v1001' ORDER BY t").fetchall()
    assert len(chat) == 12 and chat[0]["t"] == 5 and chat[0]["source"] == "live"       # post-stream message excluded
    y = arc.execute("SELECT chat_source, stream_start_unix FROM videos WHERE id='ytABCDEFGH1'").fetchone()
    assert tuple(y) == ("replay", None)
    assert arc.execute("SELECT COUNT(*) FROM turns_fts WHERE turns_fts MATCH 'glaciers'").fetchone()[0] == 2
    assert arc.execute("SELECT start, end FROM muted").fetchall()[0][:] == (400.0, 430.0)


def test_build_db_reloads_exactly_the_changed_vod(project):
    root, cfg = project
    from common import P
    assert "reloaded 0 VOD(s)" in run(root, "build_db.py").stdout
    jp = P(cfg, "transcripts") / "ytABCDEFGH1.json"
    doc = json.loads(jp.read_text())
    doc["segments"][0]["text"] += " Solidarity."
    jp.write_text(json.dumps(doc))
    out = run(root, "build_db.py").stdout
    assert "reloaded 1 VOD(s): ytABCDEFGH1" in out and "2 videos" in out
    arc, _ = dbs(project)
    assert arc.execute("SELECT COUNT(*) FROM turns_fts WHERE turns_fts MATCH 'solidarity'").fetchone()[0] == 1
    assert arc.execute("SELECT COUNT(*) FROM turns WHERE video_id='ytABCDEFGH1'").fetchone()[0] == 3   # no duplicates
    run(root, "publish.py")


def test_public_db_speakers_and_schema(project):
    _, pub = dbs(project)
    turns = pub.execute("SELECT speaker, low_confidence, is_media FROM turns WHERE video_id='v1001' ORDER BY start").fetchall()
    assert [tuple(t) for t in turns] == [("Hasan", 0, 0), ("Guest", 0, 0), ("Hasan", 1, 0), ("Media", 0, 1), ("Media", 0, 1), ("Hasan", 0, 0)]
    assert {r[0] for r in pub.execute("SELECT name FROM speakers")} == {"Hasan", "Guest", "Media"}
    schema = " ".join(r[0] or "" for r in pub.execute("SELECT sql FROM sqlite_master")).lower()
    for banned in ("user_id", "msg_id", "speaker_raw", "speaker_score", "unknown_", "live_msgs"):
        assert banned not in schema
    assert pub.execute("SELECT COUNT(*) FROM turns WHERE speaker LIKE 'Unknown%'").fetchone()[0] == 0
    assert pub.execute("SELECT value FROM meta WHERE key='spec_version'").fetchone()[0] == "4"


def test_public_chat_optout_deleted_and_cut(project):
    _, pub = dbs(project)
    chat = pub.execute("SELECT * FROM chat WHERE video_id='v1001' ORDER BY t").fetchall()
    authors = {c["author"] for c in chat}
    assert "carol_old" not in authors and "carol_new" not in authors            # rename stays excluded via user_id
    removed = [c for c in chat if c["deleted"]]
    assert [c["text"] for c in removed] == ["[deleted by mods]"]
    assert [c["text"] for c in chat if c["kind"] == "mod_action"] == ["timeout 600s"]
    dump = " ".join(str(v) for row in pub.execute("SELECT * FROM chat") for v in row)
    assert "something the mods removed" not in dump and "rename" not in dump
    yt = pub.execute("SELECT t, text FROM chat WHERE video_id='ytABCDEFGH1' ORDER BY t").fetchall()
    assert [tuple(r) for r in yt] == [(4.0, "union strong"), (325.0, "welcome back")]   # cut message dropped, offset applied


def test_chat_minutes_k_anonymity(project):
    _, pub = dbs(project)
    rows = {r["minute"]: r for r in pub.execute("SELECT * FROM chat_minutes WHERE video_id='v1001'")}
    assert rows[0]["msgs"] == 3 and rows[0]["distinct_chatters"] == 3 and json.loads(rows[0]["top_terms"]) == []
    assert rows[0]["first_msg_share"] == pytest.approx(1 / 3, abs=1e-3)
    assert json.loads(rows[1]["top_terms"]) == [["kekw", 6], ["lol", 5]]        # 'secretword' (1 chatter) and @alice never appear
    assert pub.execute("SELECT has_chat_stats FROM videos WHERE id='v1001'").fetchone()[0] == 1


def test_publish_fails_closed_when_optout_check_trips(project):
    import publish
    _, pub = dbs(project)
    assert publish.check(pub, {"nobody"}) == []
    assert any("opt-out" in p for p in publish.check(pub, {"alice"}))
    bad = sqlite3.connect(":memory:")
    bad.executescript(publish.PUBLIC_SCHEMA + "ALTER TABLE chat ADD COLUMN user_id TEXT;")
    assert any("user_id" in p for p in publish.check(bad, set()))


def test_publish_deleted_text_flag_and_id_optout(project, tmp_path):
    """publish_deleted_text: true shows the text; a numeric user_id in optout.txt excludes all of that user's names."""
    import shutil, yaml
    root, _ = project
    alt = tmp_path / "alt"
    shutil.copytree(root, alt, ignore=shutil.ignore_patterns("public*.db"))
    c = yaml.safe_load((alt / "config.yaml").read_text())
    c["publish_deleted_text"] = True
    (alt / "config.yaml").write_text(yaml.safe_dump(c))
    (alt / "optout.txt").write_text("4\n")
    run(alt, "publish.py")
    pub = sqlite3.connect(alt / "public.db")
    authors = {r[0] for r in pub.execute("SELECT author FROM chat")}
    assert "dave" not in authors and "carol_new" in authors
    (alt / "optout.txt").write_text("")
    run(alt, "publish.py")
    pub = sqlite3.connect(alt / "public.db")
    assert pub.execute("SELECT text FROM chat WHERE deleted=1").fetchone()[0] == "something the mods removed"


def test_qa_report_sheet_and_health(project):
    root, cfg = project
    out = run(root, "qa.py").stdout
    assert "media" in out and "recommended" in out and "00:06:40-00:07:10" in out
    sheet = (root / "qa" / "qa_labels.tsv").read_text().splitlines()
    assert sheet[0].split("\t")[-2:] == ["true_speaker", "transcript_ok"] and len(sheet) > 3
    assert "unlabeled" in run(root, "qa.py", "--score").stdout
    health = run(root, "qa.py", "--health").stdout
    assert "low-confidence alignments" in health and "VODs without chat" in health
