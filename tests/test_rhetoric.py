"""pipeline/rhetoric.py end to end on synthetic chat, with a fake Batches client (no network, no spend)."""
import json
from types import SimpleNamespace as NS

import pytest

import rhetoric
from common import P

LINES = [
    "hello chat", "KEKW", "the holocaust never happened", "jews control the media", "free palestine",
    "israel is no different from the nazis", "zionists control congress", "liberals get the bullet too",
    "america is the biggest terrorist state on earth", "nato expansion provoked russia into the war",
    "Destiny said the holocaust never happened lol what a clown", "OMEGALUL", "rent is too high", "W take",
]


class FakeBatches:
    """Codes by rule: sincere if the line has no 'said'; mirrors what Claude would return."""
    def __init__(self):
        self.requests = None

    def create(self, requests):
        self.requests = requests
        return NS(id="msgbatch_test")

    def retrieve(self, bid):
        return NS(processing_status="ended")

    def results(self, bid):
        for r in self.requests:
            text = r["params"]["messages"][0]["content"].split("TARGET MESSAGE")[1].split(":\n", 1)[1].split("\n\n")[0]
            cats, ihra, jda = [], False, False
            if "holocaust" in text:
                cats, ihra, jda = ["holocaust_distortion"], True, True
            elif "jews control" in text:
                cats, ihra, jda = ["antisemitism"], True, True
            elif "zionists control" in text:
                cats, ihra = ["antisemitism"], True
            elif "nazis" in text:
                cats, ihra = ["israel"], True
            elif "bullet" in text:
                cats = ["anti_liberal"]
            elif "terrorist" in text:
                cats = ["anti_us"]
            elif "provoked" in text:
                cats = ["russia"]
            stance = "not_applicable" if not cats else ("quote_or_report" if "said" in text else "endorse")
            if stance != "endorse":
                ihra = jda = False
            body = {"categories": cats, "stance": stance, "ihra_antisemitic": ihra, "jda_antisemitic": jda,
                    "us_target": "system" if "terrorist" in text else "none", "pattern_ids": [],
                    "quote": text[:20] if cats else "", "reason": "test"}
            msg = NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(body))],
                     usage=NS(input_tokens=100, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0))
            yield NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=msg))


@pytest.fixture()
def chat_project(project):
    root, cfg = project
    rows = [{"t": i * 3.0, "user_id": f"id{i % 5}", "author": f"name{i % 5}", "text": t, "badges": ""}
            for i, t in enumerate(LINES * 3)]
    (P(cfg, "chat") / "v9001.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    yield cfg
    (P(cfg, "chat") / "v9001.jsonl").unlink()


def test_scan_code_collect_report(chat_project, capsys):
    cfg = chat_project
    stats = rhetoric.scan(cfg, ["v9001"])["v9001"]
    assert stats["messages"] == len(LINES) * 3
    # 8 flagged lines per repeat (incl. the quoted one); "free palestine" and chatter are topic-only or nothing
    assert stats["candidates"] == 8 * 3
    assert stats["topic_messages"]["topic_israel"] >= 3

    fake = NS(messages=NS(batches=FakeBatches()))
    assert rhetoric.code(cfg, ["v9001"], yes=False, client=fake) is None      # estimate only, nothing sent
    assert fake.messages.batches.requests is None
    assert rhetoric.code(cfg, ["v9001"], yes=True, client=fake, kind="candidate") == "msgbatch_test"
    assert len(fake.messages.batches.requests) == 24                          # candidates only
    assert rhetoric.collect(cfg, client=fake) == 24
    assert rhetoric.code(cfg, ["v9001"], yes=True, client=fake) == "msgbatch_test"   # now the sample
    sent = fake.messages.batches.requests
    assert all("name" not in r["params"]["messages"][0]["content"] for r in sent), "account names must not be sent"
    assert rhetoric.code(cfg, ["v9001"], yes=True, client=fake) is None       # already submitted

    assert len(sent) == stats["sample"]
    assert rhetoric.collect(cfg, client=fake) == len(sent)
    text = rhetoric.report(cfg)
    assert "name" not in text.split("## v9001")[1].split("## ")[0], "report must not name accounts"
    assert "| antisemitism | 6 |" in text and "| holocaust_distortion | 3 |" in text   # the 'said' quote is not counted
    assert "IHRA: 12 messages" in text and "JDA: 6" in text
    accounts = (rhetoric.out_root(cfg) / "accounts.tsv").read_text()
    assert "name" in accounts


def test_key_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("HF_TOKEN=hf_x\nexport ANTHROPIC_API_KEY='sk-ant-test'\n")
    assert rhetoric.key_from_dotenv(path=env)
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    env.write_text("HF_TOKEN=hf_x\n")
    assert not rhetoric.key_from_dotenv(path=env)
