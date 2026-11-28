"""Fact-check ids are unique (two claims of one stream can start in the same second), and claims rated 1-2 for
importance are checked from fact_low_since on and carry their importance to the site (Nov 27 2026)."""
import json

import feedback
import reel


def fact(t, claim, quote="q", vid="v1"):
    return {"id": f"f-{vid}-{int(t)}", "video_id": vid, "t": t, "claim": claim, "quote": quote, "verdict": "false"}


def test_repeated_ids_get_a_stable_suffix_and_the_first_keeps_the_plain_id():
    a, b, c = fact(100.7, "zeta"), fact(100.2, "alpha"), fact(250, "other")
    out = feedback.unique_ids([a, b, c], "fact")
    assert [f["id"] for f in out] == ["f-v1-100-2", "f-v1-100", "f-v1-250"]   # earlier second keeps the plain id
    again = feedback.unique_ids([fact(100.2, "alpha"), fact(100.7, "zeta"), fact(250, "other")], "fact")
    assert sorted(f["id"] for f in again) == sorted(f["id"] for f in out)       # same result in any order
    assert len({f["id"] for f in out}) == 3


def test_served_reels_never_repeat_an_id(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "corrections", lambda cfg, week: {"f-v1-100-2": {"action": "withdraw", "text": "t"}})
    r = {"week": "2026-11-22", "clips": [], "fact_checks": [fact(100.2, "alpha"), fact(100.7, "zeta"), fact(5, "x")]}
    out = feedback.apply_corrections({}, r)
    assert [f["id"] for f in out["fact_checks"]] == ["f-v1-100", "f-v1-5"] and out["withdrawn"] == 1


def test_low_importance_claims_are_checked_only_from_the_cutoff():
    st = reel.settings({})
    assert reel.checked_importance(st, {"upload_date": "20261126"}) == 3
    assert reel.checked_importance(st, {"upload_date": st["fact_low_since"]}) == 1
    v = {"id": "v1", "title": "s", "upload_date": "20261127"}
    got = {"status": "succeeded", "urls": ["https://e.x/a"],
           "result": {"verdict": "false", "claim": "c", "sources": [{"url": "https://e.x/a", "title": "a"}]}}
    f = reel.fact_from(v, {"start": 12.0, "quote": "q", "claim": "c", "topic": "economy", "importance": 2}, [], got)
    assert f["importance"] == 2 and f["id"] == "f-v1-12"


def test_report_tags_retag_a_claim_whose_id_used_to_be_shared(monkeypatch, tmp_path):
    import report_tags
    facts = [fact(100.2, "alpha"), fact(100.7, "zeta"), fact(5, "x")]
    feedback.unique_ids(facts, "fact")
    monkeypatch.setattr(report_tags, "stream_facts", lambda cfg: facts)
    monkeypatch.setattr(report_tags, "load_reports", lambda cfg: [{"id": "r", "title": "R", "sections": [{"key": "a", "title": "A"}]}])
    path = tmp_path / "report_tags.json"
    monkeypatch.setattr(report_tags, "tags_path", lambda cfg: path)
    desc = report_tags.describe(report_tags.load_reports(None))
    sig = reel.h("reports", report_tags.VERSION, desc)
    # a state from before the fix: one tag for the shared id, nothing for the claim that shared it, no claim hashes
    path.write_text(json.dumps({"version": report_tags.VERSION, "reports": sig, "tags": {"f-v1-100": {"r": "a"}, "f-v1-5": {}}}))
    sent = []

    def fake_cli(cfg, st, kind, reqs):
        for key, system, user, schema in reqs:
            sent.append(user)
            reel.cache_put(cfg, kind, key, {"status": "succeeded", "result": {"claims": []}})
        return len(reqs)
    monkeypatch.setattr(reel, "run_cli", fake_cli)
    monkeypatch.setattr(reel, "cache_get", lambda cfg, kind, key: {"status": "succeeded", "result": {"claims": []}} if sent else None)
    monkeypatch.setattr(reel, "cache_put", lambda *a: None)
    assert report_tags.run({}) == "done"
    assert len(sent) == 1 and "alpha" in sent[0] and "zeta" in sent[0] and ". Claim: x" not in sent[0]
    state = json.loads(path.read_text())
    assert state["tags"]["f-v1-100"] == {} and set(state["claims"]) == {"f-v1-100", "f-v1-100-2", "f-v1-5"}
