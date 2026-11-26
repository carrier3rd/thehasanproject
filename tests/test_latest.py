"""latest.py: the home page's latest-stream section. Claude is faked; what's tested is that only what the transcript
supports reaches the page."""
import json
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

import latest

TURNS = [(0, 10, "Hasan", "Good morning everybody, happy Thanksgiving Eve."),
         (600, 610, "Media", "Officials said the detonation exceeded expectations."),
         (620, 630, "Hasan", "The problem should have been dealt with safely a very long time ago."),
         (1800, 1810, "Hasan", "We are back to Grand Theft Auto, ladies and gentlemen."),
         (1900, 1910, "Guest", "You took too long on the roof, man.")]
META = {"video_id": "v9", "title": "t", "upload_date": "20261125", "duration": 3600}


def answer(**over):
    a = {"headline": "An Army blast and GTA VI",
         "topics": [{"name": "Iowa blast", "note": "Army detonation"}, {"name": "GTA VI", "note": "chapter 4"}],
         "segments": [
             {"line": 0, "title": "Opening", "topic": "Other", "category": "Chat", "summary": "He opened the stream."},
             {"line": 1, "title": "Iowa", "topic": "Iowa blast", "category": "News",
              "summary": 'He said "the problem should have been dealt with safely". He said "the Army lied about everything".'},
             {"line": 3, "title": "GTA", "topic": "GTA VI", "category": "Gaming",
              "summary": 'He said "you took too long on the roof". He went back to the game.'}]}
    a.update(over)
    return a


def test_quotes_must_be_hasans_own_words_in_that_segment():
    sec, dropped = latest.check(META, TURNS, answer())
    iowa, gta = sec["segments"][1], sec["segments"][2]
    assert iowa["summary"] == 'He said "the problem should have been dealt with safely".'        # real quote kept
    assert "lied" not in iowa["summary"]                                                            # invented quote gone
    assert gta["summary"] == "He went back to the game."                                            # a Guest's words aren't Hasan's
    assert len(dropped) == 2


def test_times_come_from_transcript_lines_and_bad_lines_are_dropped():
    a = answer()
    a["segments"].append({"line": 99, "title": "Ghost", "topic": "GTA VI", "category": "Gaming", "summary": "x"})
    sec, dropped = latest.check(META, TURNS, a)
    assert [s["t"] for s in sec["segments"]] == [0, 600, 1800]
    assert [s["end"] for s in sec["segments"]] == [600, 1800, 3600]
    assert any("line 99" in d for d in dropped)
    hours = {t["name"]: t["hours"] for t in sec["topics"]}
    assert hours == {"GTA VI": 0.5, "Iowa blast": 0.3}


def test_a_quote_split_across_sentences_is_not_trusted():
    a = answer()
    a["segments"][0]["summary"] = 'He said "happy Thanksgiving. Eve" and left. Then he talked.'
    sec, _ = latest.check(META, TURNS, a)
    assert '"' not in sec["segments"][0]["summary"]


def test_build_writes_latest_and_reuses_the_cache(project, tmp_path, monkeypatch):
    from common import load_config
    from webcore import create_app
    root, _ = project
    alt = tmp_path / "proj"
    shutil.copytree(root, alt)
    cfg = load_config(alt / "config.yaml")
    pub = sqlite3.connect(alt / cfg["paths"]["public_db"])
    vid = pub.execute("SELECT id FROM videos ORDER BY upload_date DESC, id DESC LIMIT 1").fetchone()[0]
    n = pub.execute("SELECT COUNT(*) FROM turns WHERE video_id=?", (vid,)).fetchone()[0]
    pub.close()
    calls = []

    def fake_ask(text, st):
        calls.append(text)
        return {"headline": "A test stream", "topics": [{"name": "Test", "note": "n"}],
                "segments": [{"line": i, "title": f"s{i}", "topic": "Test", "category": "Chat", "summary": "ok."}
                             for i in range(min(n, 3))]}

    monkeypatch.setattr(latest, "ask", fake_ask)
    if n < 3:
        pytest.skip("the synthetic newest stream has fewer than 3 turns")
    assert latest.build(cfg, None, False) == 0
    out = json.loads((alt / "features" / "latest.json").read_text(encoding="utf-8"))
    assert out["video_id"] == vid and out["headline"] == "A test stream" and len(out["segments"]) == 3
    assert latest.build(cfg, None, False) == 0 and len(calls) == 1          # unchanged transcript: Claude not asked again
    client = TestClient(create_app(cfg, "public"))
    assert client.get("/api/features/latest").json()["video_id"] == vid
