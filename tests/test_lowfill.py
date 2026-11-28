"""The lower-importance backfill (reel.py lowfill) never holds up the week in progress, works newest stream first
within a budget and rests after a usage limit; chat replays far smaller than the live log are replaced by it
(Nov 27 2026)."""
import datetime as dt
import json
import sqlite3

import chat_replay
import reel


def streams():
    return [{"id": "older", "title": "s", "upload_date": "20261110"},
            {"id": "old", "title": "s", "upload_date": "20261120"},
            {"id": "new", "title": "s", "upload_date": "20261127"}]


def claim(vid, imp, start=10.0):
    return {"vid": vid, "importance": imp, "start": start, "claim": f"{vid}-{imp}-{start}", "quote": "q", "topic": "t"}


def no_cache(monkeypatch):
    monkeypatch.setattr(reel, "turns_of", lambda con, vid: [])
    monkeypatch.setattr(reel, "cache_get", lambda cfg, kind, key: None)
    monkeypatch.setattr(reel, "in_flight", lambda cfg: set())


def test_backfill_claims_are_asked_only_within_the_budget_newest_stream_first(monkeypatch):
    no_cache(monkeypatch)
    st = reel.settings({})
    claims = [claim("older", 1), claim("old", 2), claim("old", 4, 20.0), claim("new", 2)]
    need, have, waiting = reel.fact_requests({}, st, None, streams(), claims)
    assert len(need) == 2 and not waiting               # the regular ones only: imp 4, and imp 2 from fact_low_since on
    st["_low_budget"] = [1]
    need, _, waiting = reel.fact_requests({}, st, None, streams(), claims)
    assert len(need) == 3 and not waiting and st["_low_budget"] == [0]
    assert "old-2-10.0" in need[-1][2] and "older" not in need[-1][2]   # the newer of the two older streams


def test_backfill_is_off_without_a_start_date():
    st = reel.settings({"reel": {"fact_low_backfill_from": ""}})
    assert reel.checked_importance(st, {"upload_date": "20261120"}) == st["fact_min_importance"]
    st = reel.settings({})
    assert reel.checked_importance(st, {"upload_date": "20261120"}) == 1
    assert reel.is_backfill(st, {"upload_date": "20261120"}, {"importance": 2})
    assert not reel.is_backfill(st, {"upload_date": "20261127"}, {"importance": 2})
    assert not reel.is_backfill(st, {"upload_date": "20261120"}, {"importance": 3})


def fake_lowfill(monkeypatch, tmp_path, pending, result="written", new_work=False):
    calls = []
    monkeypatch.setattr(reel, "reels_dir", lambda cfg: tmp_path)
    monkeypatch.setattr(reel, "public_db", lambda cfg: None)
    monkeypatch.setattr(reel, "lowfill_weeks", lambda cfg, now: sorted(pending, reverse=True))
    monkeypatch.setattr(reel, "low_pending", lambda cfg, st, con, w: (pending[w], 0, True))
    monkeypatch.setattr(reel, "regular_pending", lambda cfg, st, con, w: new_work)

    def build(cfg, week, yes=False, now=None, low_budget=None, **kw):
        calls.append((week, low_budget[0]))
        take = min(low_budget[0], pending[week])
        if result == "written":
            pending[week] -= take
            low_budget[0] -= take
        return result
    monkeypatch.setattr(reel, "build", build)
    return calls


def test_lowfill_stops_at_its_cap_newest_week_first(monkeypatch, tmp_path):
    pending = {"2026-11-15": 30, "2026-11-22": 5}
    calls = fake_lowfill(monkeypatch, tmp_path, pending)
    out = reel.lowfill({}, now=dt.datetime(2026, 11, 27, 20), max_claims=12)
    assert calls[0][0] == "2026-11-22" and pending == {"2026-11-15": 23, "2026-11-22": 0}
    assert out.startswith("ok") and json.loads((tmp_path / "lowfill.json").read_text())["sent_last_run"] == 12


def test_lowfill_rests_after_a_usage_limit(monkeypatch, tmp_path):
    pending = {"2026-11-22": 5}
    fake_lowfill(monkeypatch, tmp_path, pending, result="limit")
    assert reel.lowfill({}, now=dt.datetime.now()).startswith("usage limit")
    state = json.loads((tmp_path / "lowfill.json").read_text())
    assert state["limit_until"] and state["remaining"] == 5
    calls = fake_lowfill(monkeypatch, tmp_path, pending)
    assert reel.lowfill({}, now=dt.datetime.now()).startswith("resting") and not calls


def test_lowfill_yields_to_new_work_in_the_week_in_progress(monkeypatch, tmp_path):
    calls = fake_lowfill(monkeypatch, tmp_path, {"2026-11-22": 5}, new_work=True)
    assert "goes first" in reel.lowfill({}, now=dt.datetime(2026, 11, 27, 20)) and not calls


def live_db(n):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE live_msgs(ts_ms, channel, user_id, author, text, badges, kind, deleted)")
    con.executemany("INSERT INTO live_msgs VALUES(?,?,?,?,?,?,?,?)",
                    [(1_000_000 + i * 50, "chan", "u", "a", f"m{i}", "", "msg", 0) for i in range(n)] +
                    [(1_000_010, "chan", "u", "a", "gone", "", "msg", 1), (1_000_020, "other", "u", "a", "x", "", "msg", 0)])
    return con


def test_a_short_replay_is_replaced_by_the_live_log(monkeypatch):
    monkeypatch.setattr(chat_replay, "stream_window", lambda con, cfg, vid: (1000.0, 100.0))
    cfg = {"channel_name": "chan"}
    con = live_db(1500)
    rows, source = chat_replay.checked_rows(con, cfg, "v1", [{"t": 1.0}] * 10)
    assert source == "live" and len(rows) == 1500
    assert rows[0] == {"t": 0.0, "user_id": "u", "author": "a", "text": "m0", "badges": ""} and rows[-1]["t"] == 74.95
    full = [{"t": 1.0}] * 1400
    assert chat_replay.checked_rows(con, cfg, "v1", full) == (full, "replay")
    assert chat_replay.checked_rows(live_db(200), cfg, "v1", [{"t": 1.0}] * 10)[1] == "replay"   # live too thin to judge
