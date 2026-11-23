import datetime as dt
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

import reel

WEEK = "2026-11-01"          # Sunday; the fixture's v1001 is dated 2026-11-01


class FakeBatches:
    """Answers like Claude would, by rule. Screen: flags the rent-control line (plus one invented quote).
    Judge: keeps it, with one real and one invented chat example."""
    def __init__(self, refuse=False):
        self.created, self.refuse, self.store = [], refuse, {}

    def create(self, requests):
        bid = f"b{len(self.created)}"
        self.created.append(requests)
        self.store[bid] = requests
        return NS(id=bid)

    def retrieve(self, bid):
        return NS(processing_status="ended")

    def results(self, bid):
        for r in self.store[bid]:
            user = r["params"]["messages"][0]["content"]
            if r["custom_id"].startswith("f"):
                yield self.fact(r)
                continue
            if r["custom_id"].startswith("s"):
                moments = []
                if "rent control" in user:
                    moments.append({"start": 6.5, "end": 12, "stance": "own_view", "category": "other",
                                    "severity": 6, "quote": "the housing market and rent control", "why": "test"})
                    moments.append({"start": 300, "end": 310, "stance": "own_view", "category": "other",
                                    "severity": 9, "quote": "words that were never said", "why": "invented"})
                claims = []
                if "rent control" in user:
                    claims = [{"start": 6.5, "quote": "the housing market and rent control", "topic": "economy",
                               "claim": "Rent control is today's topic", "importance": 4},
                              {"start": 1, "quote": "Welcome back everybody", "topic": "other",
                               "claim": "He welcomed viewers back", "importance": 3},
                              {"start": 300, "quote": "Okay chat that was the glacier video", "topic": "science_and_health",
                               "claim": "Glaciers are retreating", "importance": 5},
                              {"start": 2, "quote": "never said this", "topic": "other", "claim": "x", "importance": 5},
                              {"start": 5, "quote": "Welcome back", "topic": "other", "claim": "dup", "importance": 1}]
                body = {"moments": moments, "claims": claims}
            else:
                body = {"keep": True, "stance": "own_view", "severity": 7, "confidence": "high",
                        "categories": ["misinformation"], "title": "Rent control segment", "summary": "s",
                        "why_notable": "w", "nuance": "n", "chat_reaction": "c",
                        "chat_examples": ["rent is too damn high https://example.com/x", "a message nobody sent"],
                        "quote": "housing market and rent control", "clip_start": 0, "clip_end": 9999,
                        "segment_reason": "the whole opening"}
            stop = "refusal" if self.refuse and r["custom_id"].startswith("j") else "end_turn"
            msg = NS(stop_reason=stop, content=[NS(type="text", text=json.dumps(body))],
                     usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))
            yield NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=msg))


    def fact(self, r):
        user = r["params"]["messages"][0]["content"]
        assert r["params"]["tools"][0]["type"] == "web_search_20260209" and "format" not in r["params"]["output_config"]
        found = [NS(type="web_search_result", url="https://example.org/real", title="Real")]
        if "Glaciers" in user:      # a false verdict citing only a source the search never returned: must be dropped
            body = {"verdict": "false", "claim": "c", "correction": "x", "explanation": "e", "confidence": "high",
                    "sources": [{"url": "https://invented.example/none", "title": "made up"}]}
        elif "welcomed" in user:
            body = {"verdict": "not_checkable", "claim": "c", "correction": "", "explanation": "greeting",
                    "confidence": "high", "sources": []}
        else:
            body = {"verdict": "misleading", "claim": "Rent control is the topic", "correction": "Partly.",
                    "explanation": "Checked.", "confidence": "medium",
                    "sources": [{"url": "https://example.org/real", "title": "Real"},
                                {"url": "https://invented.example/x", "title": "not searched"}]}
        content = [NS(type="server_tool_use"), NS(type="web_search_tool_result", content=found),
                   NS(type="text", text="I searched. Here is my verdict:\n" + json.dumps(body))]
        msg = NS(stop_reason="end_turn", content=content,
                 usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))
        return NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=msg))


class FakeClient:
    def __init__(self, **kw):
        self.messages = NS(batches=FakeBatches(**kw))


@pytest.fixture()
def reel_project(project, tmp_path):
    import shutil
    from common import load_config
    root, _ = project
    alt = tmp_path / "proj"
    shutil.copytree(root, alt)
    return alt, load_config(alt / "config.yaml")


def test_last_finished_week():
    sunday = dt.datetime(2026, 11, 22, 9)          # a Sunday: last week is Nov 15-21
    assert reel.last_finished_week(sunday) == "2026-11-15"
    assert reel.last_finished_week(dt.datetime(2026, 11, 21, 23)) == "2026-11-08"   # Saturday: week not over
    assert reel.last_finished_week(dt.datetime(2026, 11, 17, 12)) == "2026-11-08"
    with pytest.raises(SystemExit):
        reel.week_bounds("2026-11-02")             # a Monday


def test_build_estimates_then_screens_judges_and_writes(reel_project):
    root, cfg = reel_project
    assert reel.build(cfg, WEEK, yes=False) == "estimate"
    client = FakeClient()
    assert reel.build(cfg, WEEK, client=client, yes=True) == "waiting"          # screen batch submitted
    assert len(client.messages.batches.created) == 1
    assert reel.build(cfg, WEEK, client=client, yes=True) == "waiting"          # collected; judge + fact batches
    assert len(client.messages.batches.created) == 3
    judge_reqs = client.messages.batches.created[1]
    fact_reqs = client.messages.batches.created[2]
    assert len(fact_reqs) == 3        # invented quote dropped, near-duplicate merged
    assert len(judge_reqs) == 1                                                  # invented-quote moment dropped
    assert "rent is too damn high" in judge_reqs[0]["params"]["messages"][0]["content"]   # chat went to Claude
    assert reel.build(cfg, WEEK, client=client, yes=True) == "written"
    out = json.loads((root / "reels" / f"{WEEK}.json").read_text())
    assert out["week_end"] == "2026-11-07" and [s["video_id"] for s in out["streams"]] == ["v1001"]
    [c] = out["clips"]
    assert c["video_id"] == "v1001" and c["severity"] == 7 and c["embed_type"] == "twitch"
    assert c["chat_examples"] == ["rent is too damn high https://example.com/x"]  # invented chat line dropped
    assert c["start"] >= 0 and c["end"] - c["start"] <= 900                       # long segment kept, capped at 15 min
    assert c["end"] - c["start"] > 150 and c["segment_reason"] == "the whole opening"
    [f] = out["fact_checks"]                       # not_checkable and the unsourced 'false' are not published
    assert f["verdict"] == "misleading" and f["topic"] == "economy" and f["t"] == 6.5
    assert [x["url"] for x in f["sources"]] == ["https://example.org/real"]       # unsearched URL dropped
    assert out["fact_summary"]["checked"] == 1 and out["fact_summary"]["by_verdict"]["misleading"] == 1
    assert any("rent control" in l["text"] for l in c["lines"])
    # nothing changed: rebuilding uses the cache, no new batches
    assert reel.build(cfg, WEEK, client=client, yes=True) == "written"
    assert len(client.messages.batches.created) == 3


def test_build_wait_runs_through_and_refusals_are_skipped(reel_project):
    root, cfg = reel_project
    client = FakeClient(refuse=True)
    assert reel.build(cfg, WEEK, client=client, yes=True, wait=True) == "written"
    out = json.loads((root / "reels" / f"{WEEK}.json").read_text())
    assert out["clips"] == [] and out["candidates_judged"] == 1
    # a refused moment is cached as a failure, so the next run doesn't resubmit it
    assert reel.build(cfg, WEEK, client=client, yes=True) == "written"
    assert len(client.messages.batches.created) == 3


def test_build_respects_budget_and_empty_weeks(reel_project):
    root, cfg = reel_project
    assert reel.build(cfg, WEEK, client=FakeClient(), yes=True, budget=0.0) == "over_budget"
    assert reel.build(cfg, "2026-11-15", client=FakeClient(), yes=True) == "empty"


def test_auto_waits_for_sunday_then_refreshes_only_on_change(reel_project):
    root, cfg = reel_project
    client = FakeClient()
    saturday = dt.datetime(2026, 11, 7, 23)        # week Nov 1-7 not over; Oct 25-31 has no streams
    assert reel.auto(cfg, client=client, now=saturday) == "up_to_date"
    early_sunday = dt.datetime(2026, 11, 8, 7)     # before build_after_hour (10): last week's reel stands
    assert reel.auto(cfg, client=client, now=early_sunday) == "up_to_date"
    assert client.messages.batches.created == []
    sunday = dt.datetime(2026, 11, 8, 11)
    results = [reel.auto(cfg, client=client, now=sunday) for _ in range(4)]
    assert results == ["2026-11-01: waiting", "2026-11-01: waiting", "2026-11-01: written", "up_to_date"]
    assert (root / "reels" / f"{WEEK}.json").exists()


def test_auto_backfills_earlier_weeks_within_limits(reel_project):
    root, cfg = reel_project
    assert reel.backfill_weeks(reel.settings(cfg), "2026-11-01") == []          # off unless backfill_from is set
    st = {**reel.settings(cfg), "backfill_from": "2026-10-15", "max_backfill_weeks": 12}
    assert reel.backfill_weeks(st, "2026-11-01") == ["2026-10-25", "2026-10-18", "2026-10-11"]   # rounded to Sunday
    assert reel.backfill_weeks({**st, "max_backfill_weeks": 1}, "2026-11-01") == ["2026-10-25"]
    cfg["reel"] = {**(cfg.get("reel") or {}), "backfill_from": "2026-10-11"}
    client, sunday = FakeClient(), dt.datetime(2026, 11, 8, 11)
    results = [reel.auto(cfg, client=client, now=sunday) for _ in range(8)]
    assert results[2] == "2026-11-01: written"                                   # the latest week first
    assert "2026-10-18: written" in results and results[-1] == "up_to_date"     # then the week with ytABCDEFGH1
    assert not (root / "reels" / "2026-10-25.json").exists()                    # no streams: nothing built


def test_reel_api(reel_project):
    root, cfg = reel_project
    reel.build(cfg, WEEK, client=FakeClient(), yes=True, wait=True)
    from webcore import create_app
    c = TestClient(create_app(cfg, "public"))
    weeks = c.get("/api/reels").json()["weeks"]
    assert weeks == [{"week": WEEK, "week_end": "2026-11-07", "clips": 1, "streams": 1,
                      "generated_at": weeks[0]["generated_at"]}]
    r = c.get(f"/api/reels/{WEEK}").json()
    assert r["clips"][0]["title"] == "Rent control segment"
    for bad in ("2026-11-02", "..%2fconfig.yaml", "%2e%2e%2f..%2fconfig", "nope"):
        assert c.get(f"/api/reels/{bad}").status_code == 404
    assert c.get("/reel.html").status_code == 200
    fc = c.get("/api/factchecks").json()
    assert fc["total"]["misleading"] == 1 and fc["by_topic"]["economy"]["misleading"] == 1
    assert fc["weeks"] == [{"week": WEEK, "false": 0, "misleading": 1, "unsupported": 0, "mostly_accurate": 0,
                            "accurate": 0}]


def test_last_json():
    assert reel.last_json('text {"a": 1} more {"b": {"c": 2}}') == {"b": {"c": 2}}
    assert reel.last_json('{"a": "brace } inside"}') == {"a": "brace } inside"}
    assert reel.last_json("no json here") is None


def test_quote_matching_ignores_punctuation_but_not_words():
    text = reel.norm("Welcome back, everybody! Today we're talking about the housing market — and rent control.")
    assert reel.quoted("welcome back everybody", text)
    assert reel.quoted("Today we’re talking about the housing market and rent control", text)   # curly ', no dash
    assert reel.quoted('"the housing market, and rent control."', text)
    assert not reel.quoted("the housing market and rent strikes", text)
    assert not reel.quoted("", text) and not reel.quoted("...", text)
    assert not reel.quoted("rent", reel.norm("current events"))          # whole words only


def test_diagnostics_and_rebuild_on_new_version(reel_project):
    root, cfg = reel_project
    client = FakeClient()
    reel.build(cfg, WEEK, client=client, yes=True, wait=True)
    path = root / "reels" / f"{WEEK}.json"
    out = json.loads(path.read_text())
    d = out["diagnostics"]
    assert out["version"] == reel.VERSION
    assert d["screen_windows"] == 1 and d["screen_status"] == {"succeeded": 1}
    assert d["moments"] == 2 and d["moments_quote_mismatch"] == 1 and d["examples_quote_mismatch"] == ["words that were never said"]
    assert d["claims"] == 5 and d["claims_low_importance"] == 1 and d["claims_quote_mismatch"] == 1
    assert d["judged_kept"] == 1 and d["fact_not_checkable"] == 1 and d["fact_no_found_source"] == 1
    # a reel made by older code is rebuilt by auto, from cache: no new batches
    out["version"] = 2
    path.write_text(json.dumps(out))
    n = len(client.messages.batches.created)
    assert reel.auto(cfg, client=client, now=dt.datetime(2026, 11, 8, 11)) == "2026-11-01: written"
    assert len(client.messages.batches.created) == n
    assert reel.auto(cfg, client=client, now=dt.datetime(2026, 11, 8, 12)) == "up_to_date"


def test_failed_answers_are_retried_once(reel_project):
    root, cfg = reel_project

    class Flaky(FakeBatches):
        def results(self, bid):
            for r in self.store[bid]:
                yield NS(custom_id=r["custom_id"], result=NS(type="errored"))
    c = FakeClient(); c.messages.batches = Flaky()
    assert reel.build(cfg, WEEK, client=c, yes=True) == "waiting"     # submitted
    assert reel.build(cfg, WEEK, client=c, yes=True) == "waiting"     # errored -> resubmitted once
    assert reel.build(cfg, WEEK, client=c, yes=True) == "written"     # errored again: give up, write what there is
    assert len(c.messages.batches.created) == 2
    out = json.loads((root / "reels" / f"{WEEK}.json").read_text())
    assert out["diagnostics"]["screen_status"] == {"errored": 1}
