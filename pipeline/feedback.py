"""Reader reports ("this looks wrong") on weekly-reel clips and fact-checks, and the owner's corrections.

  feedback.py list [--all]                     open reports (or every report)
  feedback.py resolve ID --dismiss
  feedback.py resolve ID --note "TEXT"         publish a correction note on the item
  feedback.py resolve ID --withdraw "TEXT"     remove the item from the reel; the reel says items were withdrawn

The public site may only add reports (it stores no IP address or name). Corrections are applied when a reel is
served: notes appear on the item, withdrawn items are left out and counted. Same actions in the admin app (flags.html).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

REASONS = {"misheard": "The transcript misheard what he said",
           "out_of_context": "Out of context: the surrounding talk changes the meaning",
           "wrong_speaker": "Someone else said it (guest, clip, chat being read)",
           "fact_check_wrong": "The fact-check is wrong",
           "not_egregious": "Doesn't belong in the reel",
           "other": "Something else"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS flags(
  id INTEGER PRIMARY KEY, created REAL, week TEXT, item TEXT, kind TEXT, reason TEXT, note TEXT,
  status TEXT DEFAULT 'open', resolved REAL, response TEXT);
CREATE INDEX IF NOT EXISTS idx_flags_status ON flags(status, created);
CREATE TABLE IF NOT EXISTS corrections(
  week TEXT, item TEXT, kind TEXT, action TEXT, text TEXT, created REAL, PRIMARY KEY(week, item));
"""


def db_path(cfg: dict) -> Path:
    p = Path(cfg.get("paths", {}).get("feedback", "feedback/feedback.db"))
    return p if p.is_absolute() else Path(cfg["_root"]) / p


def connect(cfg: dict, write: bool = True) -> sqlite3.Connection | None:
    path = db_path(cfg)
    if not write and not path.exists():
        return None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=10)
    else:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    if write:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
    return con


def item_id(kind: str, x: dict) -> str:
    """Stable id of a reel item: the stream and the second it starts."""
    if x.get("id"):
        return x["id"]
    return f"{'c' if kind == 'clip' else 'f'}-{x['video_id']}-{int(x['start'] if kind == 'clip' else x['t'])}"


def add_flag(cfg: dict, week: str, item: str, kind: str, reason: str, note: str) -> int:
    con = connect(cfg)
    cur = con.execute("INSERT INTO flags(created, week, item, kind, reason, note) VALUES(?,?,?,?,?,?)",
                      (time.time(), week, item, kind, reason, note[:1000]))
    con.commit()
    con.close()
    return cur.lastrowid


def corrections(cfg: dict, week: str) -> dict[str, dict]:
    con = connect(cfg, write=False)
    if con is None:
        return {}
    try:
        return {r["item"]: dict(r) for r in con.execute("SELECT * FROM corrections WHERE week=?", (week,))}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


def apply_corrections(cfg: dict, reel: dict) -> dict:
    """Corrections applied to a reel as served: ids added, notes attached, withdrawn items removed and counted."""
    fix = corrections(cfg, reel["week"])
    withdrawn = 0
    for kind, key in (("clip", "clips"), ("fact", "fact_checks")):
        keep = []
        for x in reel.get(key) or []:
            x["id"] = item_id(kind, x)
            c = fix.get(x["id"])
            if c and c["action"] == "withdraw":
                withdrawn += 1
                continue
            if c and c["action"] == "note":
                x["review_note"] = {"text": c["text"], "date": time.strftime("%Y-%m-%d", time.localtime(c["created"]))}
            keep.append(x)
        reel[key] = keep
    reel["withdrawn"] = withdrawn
    reel["withdrawn_notes"] = [c["text"] for c in fix.values() if c["action"] == "withdraw" and c["text"]]
    return reel


def resolve(cfg: dict, flag_id: int, action: str, text: str = "") -> dict:
    """action: dismiss | note | withdraw."""
    if action not in ("dismiss", "note", "withdraw"):
        raise ValueError("action must be dismiss, note or withdraw")
    con = connect(cfg)
    f = con.execute("SELECT * FROM flags WHERE id=?", (flag_id,)).fetchone()
    if not f:
        con.close()
        raise KeyError(f"no report {flag_id}")
    now = time.time()
    if action != "dismiss":
        con.execute("INSERT OR REPLACE INTO corrections(week, item, kind, action, text, created) VALUES(?,?,?,?,?,?)",
                    (f["week"], f["item"], f["kind"], action, text, now))
    # every open report on the same item is settled by one decision
    con.execute("UPDATE flags SET status=?, resolved=?, response=? WHERE week=? AND item=? AND status='open'",
                ("dismissed" if action == "dismiss" else "accepted", now, text, f["week"], f["item"]))
    con.commit()
    con.close()
    return {"item": f["item"], "week": f["week"], "action": action}


def list_flags(cfg: dict, everything: bool = False) -> list[dict]:
    con = connect(cfg)
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM flags " + ("" if everything else "WHERE status='open' ") + "ORDER BY created DESC")]
    con.close()
    return rows


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import load_config
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("list")
    l.add_argument("--all", action="store_true")
    r = sub.add_parser("resolve")
    r.add_argument("id", type=int)
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--dismiss", action="store_true")
    g.add_argument("--note")
    g.add_argument("--withdraw", metavar="TEXT")
    a = ap.parse_args()
    cfg = load_config()
    if a.cmd == "list":
        rows = list_flags(cfg, a.all)
        for f in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(f["created"]))
            print(f"#{f['id']} {when} [{f['status']}] week {f['week']} {f['item']}: {REASONS.get(f['reason'], f['reason'])}"
                  + (f"\n    \"{f['note']}\"" if f["note"] else ""))
        print(f"{len(rows)} report(s)")
        return 0
    action, text = (("dismiss", "") if a.dismiss else ("note", a.note) if a.note is not None else ("withdraw", a.withdraw))
    print(resolve(cfg, a.id, action, text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
