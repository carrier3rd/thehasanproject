"""The archive-wide chat check (chatflags.py): keyword scan, claude -p chunks, saved flags, weekly trophies and the
site endpoints that use them. Claude is faked; nothing leaves the machine."""
import json
import sqlite3
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

import chatflags
import reel

WEEK = "2026-11-01"          # v1001 is dated 2026-11-01
EXTRA = [  # id, author, badges, text, deleted
    (900001, "troll1", "", "the jews run the banks BAD", 0),
    (900002, "troll1", "", "BAD jews control the media", 0),
    (900003, "troll1", "", "holocaust never happened BAD", 0),
    (900011, "troll1", "", "BAD jews again", 0),
    (900012, "troll1", "", "BAD jews once more", 0),
    (900004, "troll1", "", "BAD nazi stuff the mods removed", 1),          # deleted: never counts toward trophies
    (900005, "troll2", "", "BAD the jews again", 0),                       # 2 sure flags: below the minimum of 5
    (900006, "troll2", "", "BAD jews jews", 0),
    (900007, "troll2", "", "MAYBE jews, unclear", 0),                      # Claude not sure: doesn't count
    (900008, "partnerguy", "partner/1", "BAD hitler had a point", 0),     # a streamer: listed under streamers
    (900009, "hasanabi", "broadcaster/1", "BAD nazi joke", 0),             # the broadcaster is never ranked
    (900010, "nice", "", "hello chat, union strong", 0),                   # no keyword: never sent to Claude
]


class FakeClaude:
    """`claude -p` for the chat check: flags lines with BAD (sure) and MAYBE (not sure)."""
    def __init__(self):
        self.calls = []

    def __call__(self, args, input=None, env=None, **kw):
        self.calls.append(input)
        flags = []
        for line in input.splitlines():
            if "\t" not in line:
                continue
            mid, text = line.split("\t", 1)
            if "BAD" in text or "MAYBE" in text:
                flags.append({"id": int(mid), "category": "antisemitism", "why": "test", "sure": "BAD" in text})
        out = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps({"flags": flags}),
               "structured_output": {"flags": flags}, "stop_reason": "end_turn"}
        return NS(returncode=0, stdout=json.dumps(out) + "\n", stderr="")


@pytest.fixture()
def flagged(project, tmp_path, monkeypatch):
    import shutil
    from common import load_config
    root, _ = project
    alt = tmp_path / "proj"
    shutil.copytree(root, alt)
    cfg = load_config(alt / "config.yaml")
    cfg["reel"] = {**(cfg.get("reel") or {}), "backend": "subscription", "cli_workers": 1}
    pub = sqlite3.connect(alt / "public.db")
    pub.executemany("INSERT INTO chat(id, video_id, t, author, badges, first_msg, subscriber, text, deleted, kind) "
                    "VALUES(?, 'v1001', ?, ?, ?, 0, 0, ?, ?, 'msg')",
                    [(i, 20 + n, a, b, t, d) for n, (i, a, b, t, d) in enumerate(EXTRA)])
    pub.commit()
    pub.close()
    fake = FakeClaude()
    monkeypatch.setattr("subprocess.run", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-reach-claude")
    return cfg, fake


def test_run_flags_candidates_once_and_ranks_the_week(flagged):
    cfg, fake = flagged
    assert chatflags.run(cfg) == "done"
    sent = "\n".join(fake.calls)
    assert "troll1" not in sent and "hello chat" not in sent       # ids and text only; keyword-less chat not sent
    con = sqlite3.connect(chatflags.db_path(cfg))
    assert con.execute("SELECT count(*) FROM flags").fetchone()[0] == 11
    assert con.execute("SELECT deleted FROM flags WHERE id=900004").fetchone()[0] == 1
    assert chatflags.run(cfg) == "done" and len(fake.calls) == 1   # nothing new: no second call
    t = chatflags.trophies(chatflags.db_path(cfg), reel.public_db(cfg), WEEK, chatflags.settings(cfg))
    assert [p["author"] for p in t["antisemitism"]] == ["troll1"] and t["antisemitism"][0]["count"] == 5
    assert [p["author"] for p in t["hateful"]] == ["troll1"]
    assert [p["author"] for p in t["streamers"]] == ["partnerguy"] and t["streamers"][0]["partner"]
    assert all("hasanabi" not in json.dumps(t[k]) for k in ("antisemitism", "hateful", "streamers"))
    assert chatflags.trophies(chatflags.db_path(cfg), reel.public_db(cfg), "2026-11-08")["antisemitism"] == []


def test_site_shows_flags_trophies_and_exports_csv(flagged):
    from webcore import create_app
    cfg, _ = flagged
    chatflags.run(cfg)
    client = TestClient(create_app(cfg, "public"))
    msgs = {m["id"]: m for m in client.get("/api/videos/v1001/chat", params={"start": 0, "end": 60}).json()["messages"]}
    assert msgs[900001]["flag"]["category"] == "antisemitism" and "flag" not in msgs[900010]
    t = client.get(f"/api/trophies/{WEEK}").json()
    assert t["antisemitism"][0]["author"] == "troll1"
    assert client.get("/api/trophies/not-a-week").status_code == 404
    r = client.get("/api/chat/author.csv", params={"author": "TROLL1"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("date,stream,time,author,message") and len(lines) == 7
    assert "antisemitism" in r.text and "/watch.html?v=v1001" in r.text
    assert client.get("/api/chat/author.csv", params={"author": "nobody_here"}).status_code == 404
