import json
import sys

import numpy as np
import pytest
from fastapi.testclient import TestClient


class FakeEmbedder:
    """Bag-of-words hashing embedder: deterministic, no model download."""
    def _vec(self, text):
        v = np.zeros(64, dtype=np.float32)
        for tok in text.lower().replace(":", " ").replace(".", " ").split():
            v[hash(tok) % 64] += 1
        n = np.linalg.norm(v)
        return v / n if n else v

    def docs(self, texts):
        return np.stack([self._vec(t) for t in texts])

    def query(self, q):
        return self._vec(q)


@pytest.fixture(scope="module")
def apps(project):
    root, cfg = project
    import embed
    from webcore import create_app
    stats = embed.build(cfg, FakeEmbedder().docs)
    assert stats["windows"] > 0 and stats["embedded"] == 2
    again = embed.build(cfg, FakeEmbedder().docs)
    assert again["kept"] == 2 and again["embedded"] == 0 and again["windows"] == stats["windows"]
    pub, adm = create_app(cfg, "public"), create_app(cfg, "admin")
    pub.state.embedder = adm.state.embedder = FakeEmbedder()
    return TestClient(pub), TestClient(adm)


def test_transcript_search(apps):
    pub, _ = apps
    r = pub.get("/api/search", params={"q": "housing"})
    assert r.headers["x-robots-tag"].startswith("noindex")
    hit = r.json()["hits"][0]
    assert "[[housing]]" in hit["snippet"] and hit["speaker"] == "Hasan" and hit["video_id"] == "v1001" and hit["start"] == 1
    assert "speaker_raw" not in hit and "speaker_score" not in hit
    assert pub.get("/api/search", params={"q": "glaciers"}).json()["hits"] == []                    # media hidden by default
    media = pub.get("/api/search", params={"q": "glaciers", "include_media": 1}).json()["hits"]
    assert len(media) == 2 and media[0]["speaker"] == "Media"
    assert pub.get("/api/search", params={"q": "guest", "speaker": "Guest"}).json()["hits"] == []
    assert len(pub.get("/api/search", params={"q": "guest", "from": "2026-10-01", "to": "20261025"}).json()["hits"]) == 1
    assert pub.get("/api/search", params={"q": "guest", "from": "2026-10-25"}).json()["hits"] == []
    # FTS syntax errors fall back to a quoted phrase instead of a 500
    for bad in ['"unbalanced', "rent AND", "housing market)", "NEAR(", "*"]:
        assert pub.get("/api/search", params={"q": bad}).status_code == 200
    assert len(pub.get("/api/search", params={"q": '"housing market'}).json()["hits"]) == 1


def test_videos_and_watch_data(apps):
    pub, _ = apps
    r = pub.get("/api/videos").json()
    assert [v["id"] for v in r["videos"]] == ["v1001", "ytABCDEFGH1"] and r["total"] == 2
    assert [v["id"] for v in pub.get("/api/videos", params={"to": "20261025"}).json()["videos"]] == ["ytABCDEFGH1"]
    d = pub.get("/api/videos/v1001").json()
    assert d["video"]["embed_type"] == "twitch" and d["muted"] == [[400.0, 430.0]] and len(d["turns"]) == 6
    assert "stream_start_unix" not in d["video"] and "twitch_vod_id" not in d["video"]
    assert d["turns"][2]["low_confidence"] == 1
    assert pub.get("/api/videos/nope").status_code == 404
    stats = pub.get("/api/videos/v1001/chat_stats").json()["minutes"]
    assert stats[1]["minute"] == 1 and stats[1]["top_terms"][0] == ["kekw", 6]
    assert pub.get("/api/speakers").json()["speakers"] == ["Guest", "Hasan", "Media"]


def test_public_chat(apps):
    pub, _ = apps
    msgs = pub.get("/api/videos/v1001/chat", params={"start": 0, "end": 60}).json()["messages"]
    assert [m["author"] for m in msgs] == ["alice", "bob", "dave", "dave"]
    assert all("user_id" not in m for m in msgs)
    assert msgs[2]["text"] == "[deleted by mods]" and msgs[2]["deleted"] == 1 and msgs[3]["kind"] == "mod_action"
    assert msgs[0]["first_msg"] == 1
    hits = pub.get("/api/chat/search", params={"q": "rent"}).json()["hits"]
    assert len(hits) == 1 and hits[0]["author"] == "bob" and "[[rent]]" in hits[0]["snippet"] and hits[0]["t"] == 8
    by_author = pub.get("/api/chat/search", params={"author": "ALICE"}).json()["hits"]
    assert {h["video_id"] for h in by_author} == {"v1001", "ytABCDEFGH1"} and len(by_author) == 3
    assert pub.get("/api/chat/search", params={"author": "carol_new"}).json()["hits"] == []
    assert pub.get("/api/chat/search", params={"q": "removed"}).json()["hits"] == []                # deleted text not searchable
    assert len(pub.get("/api/chat/search", params={"author": "alice", "first_msg": 1}).json()["hits"]) == 1
    # user_id is ignored on the public side
    assert pub.get("/api/chat/search", params={"user_id": "1"}).json()["hits"] == []
    assert pub.get("/api/export").status_code == 404


def test_search_timeline_sort_and_video_filter(apps):
    pub, adm = apps
    t = pub.get("/api/search/timeline", params={"q": "glaciers", "include_media": 1}).json()
    by = {v["video_id"]: v for v in t["videos"]}
    assert by["v1001"]["stream"] == 2 and t["totals"]["stream"] == 2
    assert [v["upload_date"] for v in t["videos"]] == sorted(v["upload_date"] for v in t["videos"])   # oldest first
    assert all(v["stream"] == 0 for k, v in by.items() if k != "v1001")                              # zero rows kept
    assert pub.get("/api/search/timeline", params={"q": "glaciers"}).json()["totals"]["stream"] == 0  # media hidden by default
    assert pub.get("/api/search/timeline", params={"q": "rent"}).json()["totals"]["chat"] == 1
    assert pub.get("/api/search/timeline", params={"q": "removed"}).json()["totals"]["chat"] == 0     # deleted text stays hidden
    who = pub.get("/api/search/timeline", params={"author": "alice"}).json()
    assert who["totals"] == {"stream": 0, "chat": 3}
    assert pub.get("/api/search/timeline", params={"user_id": "1"}).json()["totals"] == {"stream": 0, "chat": 0}  # public: no ids
    assert pub.get("/api/search/timeline", params={"q": "*"}).status_code == 200
    # time sorts and the one-stream filter
    hits = pub.get("/api/search", params={"q": "glaciers", "include_media": 1, "sort": "oldest"}).json()["hits"]
    assert [h["start"] for h in hits] == sorted(h["start"] for h in hits)
    assert pub.get("/api/search", params={"q": "glaciers", "include_media": 1, "video": "nope"}).json()["hits"] == []
    assert len(pub.get("/api/search", params={"q": "glaciers", "include_media": 1, "video": "v1001"}).json()["hits"]) == 2
    alice = pub.get("/api/chat/search", params={"author": "alice", "sort": "oldest"}).json()["hits"]
    assert len(alice) == 3 and alice[0]["upload_date"] <= alice[-1]["upload_date"]
    assert {h["video_id"] for h in pub.get("/api/chat/search", params={"author": "alice", "video": "v1001"}).json()["hits"]} == {"v1001"}
    assert pub.get("/api/search", params={"q": "housing", "sort": "drop table"}).status_code == 200   # unknown sort = relevance
    assert adm.get("/api/search/timeline", params={"user_id": "3"}).json()["totals"]["chat"] >= 1


def test_semantic_and_hybrid(apps):
    pub, _ = apps
    for mode in ("semantic", "hybrid"):
        hits = pub.get("/api/semantic", params={"q": "union organizing guest", "mode": mode}).json()["hits"]
        assert hits[0]["video_id"] == "ytABCDEFGH1" and hits[0]["start"] == 0
        assert hits[0]["turns"][0]["speaker"] == "Hasan"
    text = json.dumps(pub.get("/api/semantic", params={"q": "glaciers retreating"}).json())
    assert "Unknown_" not in text and "retreating" not in text          # media excluded from the index


def test_admin_app(apps):
    _, adm = apps
    assert adm.get("/api/config").json()["mode"] == "admin"
    hit = adm.get("/api/search", params={"q": "glaciers", "include_media": 1}).json()["hits"][0]
    assert hit["speaker"] == "Unknown_2" and hit["speaker_raw"] == "c000:Unknown_2"
    msgs = adm.get("/api/videos/v1001/chat", params={"start": 0, "end": 60}).json()["messages"]
    assert [m["author"] for m in msgs] == ["alice", "bob", "carol_old", "carol_new", "dave", "dave"]
    assert msgs[4]["text"] == "something the mods removed" and msgs[4]["user_id"] == "4"
    # re-upload with a mid-stream cut: transcript range 310-330 maps to chat clock 430-450
    cut = adm.get("/api/videos/ytABCDEFGH1/chat", params={"start": 310, "end": 330}).json()["messages"]
    assert [(m["t"], m["text"]) for m in cut] == [(325.0, "welcome back")]
    assert adm.get("/api/videos/ytABCDEFGH1/chat", params={"start": 200, "end": 310}).json()["messages"] == []
    by_id = adm.get("/api/chat/search", params={"user_id": "3"}).json()["hits"]
    assert {h["author"] for h in by_id} == {"carol_old", "carol_new"}
    in_cut = adm.get("/api/chat/search", params={"q": "cut"}).json()["hits"][0]
    assert in_cut["t"] is None and in_cut["chat_t"] == 350
    lines = [json.loads(x) for x in adm.get("/api/export", params={"video_id": "v1001"}).text.splitlines()]
    assert len(lines) == 12 and lines[0]["ts"] == 1793500005 and lines[0]["user_id"] == "1"
    assert adm.get("/api/videos/v1001/chat_stats").json()["minutes"][0]["msgs"] == 5


def test_static_files_and_traversal(apps):
    pub, adm = apps
    for page in ("/", "/index.html", "/videos.html", "/watch.html", "/about.html", "/common.js", "/style.css"):
        assert pub.get(page).status_code == 200, page
    assert pub.get("/chat.html").status_code == 404 and adm.get("/chat.html").status_code == 200
    for path in ("/../app.py", "/%2e%2e/app.py", "/..%2f..%2farchive.db", "/static/../../config.yaml"):
        assert pub.get(path).status_code == 404, path


def test_rate_limit(project):
    root, cfg = project
    from webcore import create_app
    client = TestClient(create_app(dict(cfg, rate_limit_per_minute=3), "public"))
    codes = [client.get("/api/search", params={"q": "x"}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert client.get("/api/videos").status_code == 200           # browsing is not limited


def test_site_password_and_auto_domain(project, monkeypatch):
    root, cfg = project
    from webcore import create_app
    locked = dict(cfg, site_password_required=True, site_domain="auto")
    monkeypatch.delenv("SITE_PASSWORD", raising=False)
    assert TestClient(create_app(locked, "public")).get("/").status_code == 503          # fails closed
    monkeypatch.setenv("SITE_USER", "viewer")
    monkeypatch.setenv("SITE_PASSWORD", "s3cret pass")
    client = TestClient(create_app(locked, "public"), base_url="https://laptop.tail1234.ts.net")
    r = client.get("/api/videos")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert client.get("/api/videos", auth=("viewer", "wrong")).status_code == 401
    assert client.get("/index.html", auth=("viewer", "s3cret pass")).status_code == 200
    assert client.get("/api/config", auth=("viewer", "s3cret pass")).json()["site_domain"] == "laptop.tail1234.ts.net"
    assert TestClient(create_app(locked, "admin")).get("/api/config").status_code == 200  # admin is local-only, no password
