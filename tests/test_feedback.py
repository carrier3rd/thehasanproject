import json

import pytest
from fastapi.testclient import TestClient

import reel
from test_reel import WEEK, FakeClient


@pytest.fixture()
def apps(project, tmp_path):
    import shutil
    from common import load_config
    from webcore import create_app
    root, _ = project
    alt = tmp_path / "proj"
    shutil.copytree(root, alt)
    cfg = load_config(alt / "config.yaml")
    reel.build(cfg, WEEK, client=FakeClient(), yes=True, wait=True)
    return cfg, TestClient(create_app(cfg, "public")), TestClient(create_app(cfg, "admin"))


def test_reports_corrections_and_withdrawals(apps):
    cfg, pub, adm = apps
    r = pub.get(f"/api/reels/{WEEK}").json()
    clip, fact = r["clips"][0], r["fact_checks"][0]
    assert clip["id"].startswith("c-v1001-") and fact["id"] == "f-v1001-6" and r["withdrawn"] == 0
    ok = {"week": WEEK, "item": clip["id"], "kind": "clip", "reason": "misheard", "note": "he said 'rent strike'"}
    assert pub.post("/api/flags", json=ok).json() == {"ok": True}
    for bad in ({**ok, "item": "c-nope-1"}, {**ok, "reason": "hate"}, {**ok, "kind": "video"}, {**ok, "week": "2026-11-08"}):
        assert pub.post("/api/flags", json=bad).status_code == 400
    assert pub.post("/api/flags", content="nope").status_code == 400
    # the public app can add reports, not read or resolve them; no admin page either
    assert pub.get("/api/flags").status_code in (404, 405)
    assert pub.post("/api/flags/1/resolve", json={"action": "dismiss"}).status_code in (404, 405)
    assert pub.get("/flags.html").status_code == 404 and adm.get("/flags.html").status_code == 200
    flags = adm.get("/api/flags").json()["flags"]
    assert len(flags) == 1 and flags[0]["subject"]["title"] == "Rent control segment" and "rent strike" in flags[0]["note"]
    # a correction note shows on the item
    assert adm.post(f"/api/flags/{flags[0]['id']}/resolve", json={"action": "note", "text": "Transcript checked: he said rent control."}).status_code == 200
    c = pub.get(f"/api/reels/{WEEK}").json()["clips"][0]
    assert c["correction"]["text"].startswith("Transcript checked")
    assert adm.get("/api/flags").json()["flags"] == [] and len(adm.get("/api/flags?all=1").json()["flags"]) == 1
    # withdrawing the fact-check removes it from the reel and the all-weeks totals
    pub.post("/api/flags", json={"week": WEEK, "item": fact["id"], "kind": "fact", "reason": "fact_check_wrong", "note": ""})
    fid = adm.get("/api/flags").json()["flags"][0]["id"]
    assert adm.post(f"/api/flags/{fid}/resolve", json={"action": "withdraw", "text": "Source did not say that."}).status_code == 200
    r = pub.get(f"/api/reels/{WEEK}").json()
    assert r["fact_checks"] == [] and r["withdrawn"] == 1 and r["withdrawn_notes"] == ["Source did not say that."]
    assert pub.get("/api/factchecks").json()["total"]["misleading"] == 0
    assert adm.post("/api/flags/999/resolve", json={"action": "dismiss"}).status_code == 404
    assert adm.post(f"/api/flags/{fid}/resolve", json={"action": "explode"}).status_code == 400


def test_report_rate_limit(apps):
    cfg, pub, _ = apps
    clip = pub.get(f"/api/reels/{WEEK}").json()["clips"][0]
    body = {"week": WEEK, "item": clip["id"], "kind": "clip", "reason": "other", "note": "x" * 5000}
    codes = [pub.post("/api/flags", json=body).status_code for _ in range(11)]
    assert codes == [200] * 10 + [429]
    import feedback
    assert len(feedback.list_flags(cfg)[0]["note"]) == 1000          # notes are capped


def test_feedback_cli(apps, capsys, monkeypatch):
    cfg, pub, _ = apps
    import feedback, sys
    clip = pub.get(f"/api/reels/{WEEK}").json()["clips"][0]
    pub.post("/api/flags", json={"week": WEEK, "item": clip["id"], "kind": "clip", "reason": "wrong_speaker", "note": "guest"})
    monkeypatch.setenv("VOD_ARCHIVE_CONFIG", str(cfg["_root"] + "/config.yaml"))
    monkeypatch.setattr(sys, "argv", ["feedback.py", "list"])
    feedback.main()
    assert "Someone else said it" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["feedback.py", "resolve", "1", "--dismiss"])
    feedback.main()
    assert feedback.list_flags(cfg) == []
