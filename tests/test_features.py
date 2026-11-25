"""Featured content (features.py): topics, stories against Reddit, receipts and their review queue, Chat vs Hasan, the
quiz, and the site endpoints. Claude is faked; nothing leaves the machine."""
import json
import re
import sqlite3
import zipfile
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

import features
import reel

EARLY = "Rent control is the only real answer to the housing crisis in this country"      # yt video, 2026-10-20
LATE = "Honestly rent control does not work and we should just build way more housing"     # v1001, 2026-11-01
NAZI = "This rent control landlord is acting like a total nazi about the whole thing"     # never quotable


class FakeClaude:
    def __init__(self):
        self.calls, self.limit = [], False

    def __call__(self, args, input=None, env=None, **kw):
        system = args[args.index("--system-prompt") + 1]
        self.calls.append((system[:40], input))
        if self.limit:
            out = {"type": "result", "subtype": "error", "is_error": True, "result": "Claude usage limit reached"}
            return NS(returncode=0, stdout=json.dumps(out) + "\n", stderr="")
        if system == features.TOPIC_RUBRIC:
            lines = [l.split("Hasan: ", 1)[1] for l in input.splitlines() if " Hasan: " in l and "rent control" in l.lower()]
            body = {"topics": [{"label": "Rent control debate", "kind": "news_politics", "start": 0, "end": 60, "minutes": 12,
                                "people": [], "positions": [{"position": "He takes a view on rent control.", "quote": q}
                                                            for q in lines if q != NAZI],
                                "quotables": [{"quote": q} for q in lines]}]} if lines else {"topics": []}
        elif system == features.STORY_RUBRIC:
            items = [l.split(" | ", 3) for l in input.split("New items (item | date | source | text):\n", 1)[1].splitlines()]
            body = {"assign": [{"item": int(i), "story": "n1" if "rent" in text.lower() else "none"} for i, _, _, text in items],
                    "new": [{"key": "n1", "title": "Rent control fight", "kind": "news_politics",
                             "summary": "Arguments over rent control.", "keywords": ["rent control", "war"]}]}
        elif system == features.RECEIPT_RUBRIC:
            rows = [l.split(" | ") for l in input.splitlines() if re.match(r"^\d+ \| ", l)]
            a = next(int(r[0]) for r in rows if EARLY in r[3])
            b = next(int(r[0]) for r in rows if LATE in r[3])
            body = {"pairs": [{"a": a, "b": b, "kind": "reversal", "question": "Does rent control work?", "strength": 4,
                               "why": "opposite answers"}]}
        elif system == features.RVERIFY_RUBRIC:
            body = {"confirmed": True, "why": "both sincere", "title": "On whether rent control works",
                    "then_said": "He called it the only answer.", "now_said": "He said it does not work.",
                    "context": "", "confidence": "high"}
        elif system == features.CHATVS_RUBRIC:
            sample = input.split("CHAT SAMPLE\n", 1)[1].splitlines()
            texts = [l.split("] ", 1)[1] for l in sample if "] " in l]
            body = {"keep": True, "kind": "pushback", "title": "Chat disagrees about rent", "hasan_said": "He made a point.",
                    "chat_said": "Chat said he was wrong.", "quote": "not a real line", "examples": texts[:2] + ["made up message"],
                    "clip_start": 60, "clip_end": 180}
        else:
            raise AssertionError("unexpected prompt: " + system[:60])
        out = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(body),
               "structured_output": body, "stop_reason": "end_turn"}
        return NS(returncode=0, stdout=json.dumps(out) + "\n", stderr="")


def reddit_zip(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.stem + ".jsonl", "".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture()
def feat(project, tmp_path, monkeypatch):
    import shutil
    from common import load_config
    root, _ = project
    alt = tmp_path / "proj"
    shutil.copytree(root, alt)
    pub = sqlite3.connect(alt / "public.db")
    pub.executemany("INSERT INTO turns(video_id, start, end, speaker, low_confidence, is_media, text) VALUES(?,?,?,?,0,0,?)",
                    [("ytABCDEFGH1", 40, 46, "Hasan", EARLY), ("v1001", 200, 206, "Hasan", LATE),
                     ("v1001", 210, 216, "Hasan", NAZI)])
    chat = [("v1001", 60 + i * 0.5, f"user{i}", "nah that's just wrong and you know it" if i % 2 else
             "that's not true lol, look it up") for i in range(40)]
    chat += [("v1001", 300 + i * 2, f"calm{i}", "good stream today") for i in range(90)]      # the stream's usual minutes
    chat += [("v1001", 63, "hater", "you are wrong you nazi")]                   # hateful: never an example
    pub.executemany("INSERT INTO chat(video_id, t, author, badges, first_msg, subscriber, text, deleted, kind) "
                    "VALUES(?,?,?,'',0,0,?,0,'msg')", chat)
    pub.commit()
    pub.close()
    rd = tmp_path / "reddit_pulls"
    day = 1793500000                         # 2026-11-01 (UTC)
    reddit_zip(rd / "wk" / "2026-10-19_to_2026-11-02" / "r_LivestreamFail_posts_2026-10-19_to_2026-11-02.zip",
               [{"id": "p1", "title": "Hasan flips on rent control, chat goes wild about it", "selftext": "", "score": 900,
                 "num_comments": 40, "permalink": "/r/LivestreamFail/comments/p1/x/", "created_utc": day, "author": "redditor_one"},
                {"id": "p2", "title": "Unrelated clip of a streamer falling off a chair", "selftext": "", "score": 50,
                 "num_comments": 2, "permalink": "/r/LivestreamFail/comments/p2/y/", "created_utc": day, "author": "redditor_two"},
                {"id": "p3", "title": "Removed post about rent control", "selftext": "", "score": 5000, "num_comments": 1,
                 "permalink": "/r/x/", "created_utc": day, "removed_by_category": "moderator", "author": "redditor_three"}])
    reddit_zip(rd / "wk" / "2026-10-19_to_2026-11-02" / "r_Destiny_comments_2026-10-19_to_2026-11-02.zip",
               [{"id": f"c{i}", "body": "rent control is bad policy" if i < 3 else "anyway", "created_utc": day,
                 "author": "dgger"} for i in range(10)])
    cfg = load_config(alt / "config.yaml")
    cfg["reel"] = {**(cfg.get("reel") or {}), "backend": "subscription", "cli_workers": 1}
    cfg["features"] = {"reddit_dir": str(rd), "part_min_lines": 1, "receipt_min_positions": 2, "story_min_minutes": 1,
                       "chatvs_min_pushback": 5, "chatvs_per_stream": 2, "cli_workers": 1}
    fake = FakeClaude()
    monkeypatch.setattr("subprocess.run", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-reach-claude")
    return cfg, fake


def published(cfg, name):
    return json.loads((features.out_dir(cfg) / f"{name}.json").read_text())


def test_run_builds_every_feature(feat):
    cfg, fake = feat
    report = features.run(cfg)
    assert report["stopped"] is None
    kinds = [c[0] for c in fake.calls]
    assert any(k == features.TOPIC_RUBRIC[:40] for k in kinds) and any(k == features.RVERIFY_RUBRIC[:40] for k in kinds)
    # stories: the keyword "war" is too generic and is dropped; removed Reddit posts never count or show
    state = features.stories_state(cfg)
    [(sid, story)] = state["stories"].items()
    assert story["keywords"] == ["rent control"]
    [s] = published(cfg, "stories")["stories"]
    assert s["hasan_minutes"] == 24 and s["streams"] == 2 and s["reddit_mentions"] == 4      # 1 post + 3 comments
    assert set(s["groups"]) == {"Streamer communities"}
    detail = json.loads((features.out_dir(cfg) / "stories" / f"{sid}.json").read_text())
    assert [p["title"] for p in detail["reddit_top"]] == ["Hasan flips on rent control, chat goes wild about it"]
    assert all(NAZI not in q["quote"] for q in detail["quotes"])                              # hate words: never quoted
    # receipts wait for the owner: nothing public until approved
    q = features.load_json(features.queue_path(cfg), {})
    [r] = q.values()
    assert r["status"] == "pending" and r["a"]["quote"] == EARLY and r["b"]["quote"] == LATE
    assert published(cfg, "receipts")["receipts"] == []
    features.decide(cfg, r["id"], "approved")
    assert [x["title"] for x in published(cfg, "receipts")["receipts"]] == ["On whether rent control works"]
    features.decide(cfg, r["id"], "rejected")
    assert published(cfg, "receipts")["receipts"] == []
    with pytest.raises(ValueError):
        features.decide(cfg, r["id"], "maybe")
    # chat vs hasan: examples are real sampled lines; made-up or hateful ones and unmatched quotes are dropped
    [m] = published(cfg, "chatvs")["moments"]
    assert m["kind"] == "pushback" and m["examples"] and "made up message" not in m["examples"] and m["quote"] == ""
    assert all("nazi" not in e for e in m["examples"]) and m["stats"]["pushback"] >= 20
    quiz = published(cfg, "quiz")
    assert {w["answer"] for w in quiz["who"]} == {"hasan", "chat", "reddit"}
    # nothing public carries a username
    blob = "".join(p.read_text() for p in features.out_dir(cfg).rglob("*.json"))
    for name in ("redditor_one", "redditor_two", "dgger", "user1", "calm1", "hater", "redditor_three", "Removed post"):
        assert name not in blob
    # a second run with nothing new asks Claude nothing
    n = len(fake.calls)
    features.run(cfg)
    assert len(fake.calls) == n


def test_usage_limit_pauses_and_busy_steps_aside(feat):
    cfg, fake = feat
    fake.limit = True
    assert features.run(cfg)["stopped"] == "usage limit"
    held = features.single_run(cfg)
    if held is not None and hasattr(__import__("os"), "fork"):       # POSIX: a run in progress makes a second one wait
        assert "busy" in features.run(cfg)["stopped"]
    held.close()
    fake.limit = False
    assert features.run(cfg)["stopped"] is None


def test_site_serves_features_and_admin_review(feat):
    from webcore import create_app
    cfg, _ = feat
    features.run(cfg)
    client = TestClient(create_app(cfg, "public"))
    assert client.get("/api/features/stories").status_code == 200
    assert client.get("/api/features/quiz").json()["who"]
    assert client.get("/api/features/secrets").status_code == 404
    sid = client.get("/api/features/stories").json()["stories"][0]["id"]
    assert client.get(f"/api/features/story/{sid}").json()["id"] == sid
    assert client.get("/api/features/story/..%2f..%2fconfig").status_code == 404
    assert client.get("/api/review/receipts").status_code == 404 or "receipts" not in client.get("/api/review/receipts").text
    assert client.post("/api/review/receipts/x", json={"decision": "approved"}).status_code in (404, 405)
    admin = TestClient(create_app(cfg, "admin"))
    [r] = admin.get("/api/review/receipts").json()["receipts"]
    assert admin.post(f"/api/review/receipts/{r['id']}", json={"decision": "approved"}).json()["status"] == "approved"
    assert admin.post("/api/review/receipts/nope", json={"decision": "approved"}).status_code == 404
    assert admin.post(f"/api/review/receipts/{r['id']}", json={"decision": "yes"}).status_code == 400
    assert client.get("/api/features/receipts").json()["receipts"][0]["id"] == r["id"]
    # share links: preview tags for Reddit/Discord/X, then on to the page; unknown or malformed ids are 404s
    page = client.get(f"/s/story/{sid}")
    assert page.status_code == 200 and 'og:title' in page.text and f"url=/spotlight.html?story={sid}" in page.text
    assert f"url=/receipts.html?r={r['id']}" in client.get(f"/s/receipt/{r['id']}").text
    mid = client.get("/api/features/chatvs").json()["moments"][0]["id"]
    assert "Chat vs Hasan:" in client.get(f"/s/moment/{mid}").text
    for bad in ("/s/story/ffffffffff", "/s/receipt/zz", "/s/nope/abcdef12", "/s/moment/<script>"):
        assert client.get(bad).status_code == 404
    # where he stood: every position on the story, in order, each with his exact words
    [t] = client.get("/api/features/timelines").json()["timelines"]
    assert [p["quote"] for p in t["positions"]][0] == EARLY and LATE in [p["quote"] for p in t["positions"]]
    assert "all_positions" not in client.get(f"/api/features/story/{sid}").text
    for page in ("spotlight.html", "receipts.html", "chatvs.html", "quiz.html", "home.html"):
        assert client.get("/" + page).status_code == 200
    assert client.get("/review.html").status_code == 404                     # the review page is admin only
    assert admin.get("/review.html").status_code == 200
