"""research/watchlist.csv: every pattern compiles, known examples hit, ordinary chat doesn't trip the explicit tier."""
import csv
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ROWS = list(csv.DictReader(open(ROOT / "research" / "watchlist.csv", newline="")))
PAT = {r["id"]: re.compile(r["pattern"], re.I) for r in ROWS}

HITS = [
    ("holohoax", "the holohoax lol"),
    ("gorillion", "6 gorillion"),
    ("holocaust_denial", "the holocaust never happened"),
    ("holocaust_denial", "6 million is a lie"),
    ("6mwe", "hitler did nothing wrong"),
    ("jewish_question", "time to talk about the JQ"),
    ("nazi_codes", "1488 brother"),
    ("echo", "(((them))) again"),
    ("kill_jews", "gas the jews"),
    ("jewish_control", "jews control the media"),
    ("zionist_control", "zionists control congress"),
    ("dual_loyalty", "their loyalty is to israel not america"),
    ("israel_did_911", "mossad did 9/11"),
    ("khazar", "they're khazars not real jews"),
    ("israel_nazi_compare", "israel is no different from the nazis"),
    ("gaza_holocaust", "gaza is a concentration camp"),
    ("holocaust_industry", "the holocaust industry is real"),
    ("oct7_praise", "october 7th was justified"),
    ("oct7_praise", "honor the heroic resistance"),
    ("fink_oct7_framing", "they were concentration camp inmates who burst the gates"),
    ("oct7_sv_denial", "the rapes on october 7 never happened"),
    ("west_fault", "NATO expansion provoked russia into the war"),
    ("west_fault", "it's the west's fault"),
    ("putin_not_imperial", "putin never wanted to conquer ukraine"),
    ("ukraine_pawn", "they'll fight to the last ukrainian"),
    ("us_worst_terror", "america is the biggest terrorist state on earth"),
    ("us_worst_terror", "the US is worse than al-qaeda"),
    ("deserved_911", "america deserved 9/11"),
    ("amerikkka", "amerikkka strikes again"),
    ("us_decline_hegemony", "us hegemony is collapsing"),
    ("mearsheimer", "mearsheimer was right"),
    ("finkelstein", "norm finkelstein on the show"),
]

BENIGN = [
    "I'm Jewish and I love this stream", "jewish people protesting outside the capitol", "hebrew school memories",
    "hitler was a monster", "88 points in that game", "zionist", "the holocaust was the worst crime in history",
    "holocaust museum trip", "6 million people watched the debate", "the kids are alright", "proxy server down",
    "OMEGALUL", "KEKW", "that's a W", "yo chat what's up", "free palestine", "israel", "nato summit", "putin is a dictator",
    "the US bombed another country today", "imperialism is bad", "jewelry", "jewel", "kikeriki", "doors",
]


def test_patterns_compile_and_ids_unique():
    assert len(PAT) == len(ROWS) >= 60
    for r in ROWS:
        assert r["tier"] in {"explicit", "trope", "contested", "talking_point", "mention", "charged", "topic"}, r["id"]
        assert r["ihra"] in {"yes", "no", "context", "n/a"} and r["jda"] in {"yes", "no", "context", "n/a"}, r["id"]
        assert r["source"], r["id"]


@pytest.mark.parametrize("pid,text", HITS)
def test_known_examples_match(pid, text):
    assert PAT[pid].search(text), f"{pid} missed: {text!r}"


def test_ordinary_chat_does_not_trip_explicit_or_trope_patterns():
    strict = [r["id"] for r in ROWS if r["tier"] in {"explicit", "trope"}]
    hits = [(pid, t) for t in BENIGN for pid in strict if PAT[pid].search(t)]
    assert not hits, hits


def test_kremlin_narratives_present():
    rows = list(csv.DictReader(open(ROOT / "research" / "kremlin_narratives.csv", newline="")))
    assert len(rows) == 18
    for r in rows:
        re.compile(r["pattern"], re.I)
