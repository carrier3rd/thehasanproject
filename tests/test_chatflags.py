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
    (900012, "troll1", "", "BAD QUOTE he said the jews run things, what a clown", 0),   # second review rejects it
    (900004, "troll1", "", "BAD nazi stuff the mods removed", 1),          # deleted: never counts toward trophies
    (900005, "troll2", "", "BAD the jews again", 0),                       # 2 sure flags: below the minimum of 3
    (900006, "troll2", "", "BAD jews jews", 0),
    (900007, "troll2", "", "MAYBE jews, unclear", 0),                      # first pass not sure: doesn't count
    (900014, "troll3", "", "BAD jews one", 0),                             # 3 flags, but only 2 survive the review
    (900015, "troll3", "", "BAD jews two", 0),
    (900016, "troll3", "", "BAD QUOTE jews three", 0),
    (900008, "partnerguy", "partner/1", "BAD hitler had a point", 0),     # a streamer with 3 confirmed messages
    (900017, "partnerguy", "partner/1", "BAD jews did it", 0),
    (900018, "partnerguy", "partner/1", "BAD jews did it again", 0),
    (900009, "hasanabi", "broadcaster/1", "BAD nazi joke", 0),             # the broadcaster is never ranked
    (900010, "nice", "", "hello chat, union strong", 0),                   # no keyword: never sent to Claude
    (900013, "troll1", "", "=HYPERLINK(1) jews", 0),                       # a spreadsheet formula, unflagged
]


class FakeClaude:
    """`claude -p`. First pass: flags lines with BAD (sure) and MAYBE (not sure). Second review (items start with
    '### id'): confirms a message unless it contains QUOTE."""
    def __init__(self):
        self.calls, self.reviews = [], []

    def __call__(self, args, input=None, env=None, **kw):
        self.calls.append(input)
        if "### id " in input:
            self.reviews.append(input)
            verdicts = []
            for block in input.split("### id ")[1:]:
                mid = int(block.split("\n", 1)[0])
                msg = next(line for line in block.splitlines() if line.startswith("MESSAGE: "))
                verdicts.append({"id": mid, "confirmed": "QUOTE" not in msg, "category": "antisemitism", "why": "second look"})
            body = {"verdicts": verdicts}
        else:
            flags = []
            for line in input.splitlines():
                if "\t" not in line:
                    continue
                mid, text = line.split("\t", 1)
                if "BAD" in text or "MAYBE" in text:
                    flags.append({"id": int(mid), "category": "antisemitism", "why": "test", "sure": "BAD" in text})
            body = {"flags": flags}
        out = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(body),
               "structured_output": body, "stop_reason": "end_turn"}
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


def test_run_flags_reviews_and_ranks_the_week(flagged):
    cfg, fake = flagged
    assert chatflags.run(cfg) == "done" and len(fake.calls) == 2     # one first-pass chunk, one second-review chunk
    sent = "\n".join(fake.calls)
    assert "troll1" not in sent and "partnerguy" not in sent       # ids and text only, never usernames
    assert "hello chat, union strong" not in fake.calls[0]         # keyword-less chat is not sent to the first pass
    [review] = fake.reviews
    assert "MESSAGE: BAD jews again" in review and "MESSAGE: BAD the jews again" not in review   # troll2 cannot place
    con = sqlite3.connect(chatflags.db_path(cfg))
    assert con.execute("SELECT count(*) FROM flags").fetchone()[0] == 16
    assert con.execute("SELECT deleted FROM flags WHERE id=900004").fetchone()[0] == 1
    assert con.execute("SELECT count(*), sum(confirmed) FROM verified").fetchone() == (11, 9)
    assert chatflags.run(cfg) == "done" and len(fake.calls) == 2   # nothing new: no more calls
    held = chatflags.single_run(cfg)                               # a run in progress: a second one steps aside
    assert chatflags.run(cfg) == "busy"
    held.close()
    args = (chatflags.db_path(cfg), reel.public_db(cfg), WEEK, chatflags.settings(cfg))
    t = chatflags.trophies(*args)
    assert [(p["author"], p["count"]) for p in t["antisemitism"]] == [("troll1", 4), ("partnerguy", 3)]
    assert [p["author"] for p in t["hateful"]] == ["troll1", "partnerguy"]      # troll3: 2 confirmed, troll2: 2 flags
    assert [p["author"] for p in t["streamers"]] == ["partnerguy"] and t["streamers"][0]["partner"]
    assert all("hasanabi" not in json.dumps(t[k]) for k in ("antisemitism", "hateful", "streamers"))
    assert "QUOTE" not in json.dumps(t) and t["pending_review"] == 0 and t["min"] == 3
    assert t["antisemitism"][0]["messages"][0]["why"] == "second look"         # the reviewer's reason is shown
    con.execute("DELETE FROM verified")        # before the second review nobody places, and it says who is waiting
    con.commit()
    t = chatflags.trophies(*args)
    assert t["antisemitism"] == t["hateful"] == t["streamers"] == [] and t["pending_review"] == 3
    assert chatflags.trophies(chatflags.db_path(cfg), reel.public_db(cfg), "2026-11-08")["antisemitism"] == []
    with pytest.raises(ValueError):
        chatflags.week_days("2026-11-02")                           # a Monday


def test_a_reused_message_id_never_inherits_a_flag(flagged):
    """Chat reloads reuse row ids: the flag belongs to the message (stream, author, text), not the id."""
    cfg, fake = flagged
    chatflags.run(cfg)
    pub = sqlite3.connect(reel.public_db(cfg).execute("PRAGMA database_list").fetchone()[2])
    pub.execute("UPDATE chat SET author='innocent', text='hello jews, good stream' WHERE id=900001")
    pub.commit()
    pub.close()
    msgs = [{"id": 900001, "video_id": "v1001", "author": "innocent", "text": "hello jews, good stream"}]
    assert chatflags.flags_for(chatflags.db_path(cfg), msgs) == {}
    t = chatflags.trophies(chatflags.db_path(cfg), reel.public_db(cfg), WEEK, chatflags.settings(cfg))
    assert "innocent" not in json.dumps(t)
    [troll1] = [p for p in t["antisemitism"] if p["author"] == "troll1"]
    assert troll1["count"] == 3 and 900001 not in [m["id"] for m in troll1["messages"]]
    calls = len(fake.calls)
    assert chatflags.run(cfg) == "done" and len(fake.calls) == calls + 1   # the stream changed: the new text is checked


def test_site_shows_flags_trophies_and_exports_csv(flagged):
    from webcore import create_app
    cfg, _ = flagged
    chatflags.run(cfg)
    client = TestClient(create_app(cfg, "public"))
    msgs = {m["id"]: m for m in client.get("/api/videos/v1001/chat", params={"start": 0, "end": 60}).json()["messages"]}
    assert msgs[900001]["flag"]["category"] == "antisemitism" and "flag" not in msgs[900010]
    t = client.get(f"/api/trophies/{WEEK}").json()
    assert t["antisemitism"][0]["author"] == "troll1"
    for bad in ("not-a-week", "2026-02-30", "2026-11-02"):
        assert client.get(f"/api/trophies/{bad}").status_code == 404, bad
    r = client.get("/api/chat/author.csv", params={"author": "TROLL1"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("date,stream,time,author,message") and len(lines) == 8
    assert "'=HYPERLINK" in r.text and ",=HYPERLINK" not in r.text      # no formulas reach a spreadsheet
    assert "antisemitism" in r.text and "/watch.html?v=v1001" in r.text
    assert client.get("/api/chat/author.csv", params={"author": "nobody_here"}).status_code == 404
