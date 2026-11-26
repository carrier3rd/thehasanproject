"""Weekly pull of the general-audience subreddits that Spotlight's blindspots are measured against.

  python pipeline/reddit_pull.py            pull every finished 7-day week since features.broad_since (Tuesday to Monday, like the panel) that is missing
  python pipeline/reddit_pull.py --dry-run  list what would be pulled

Posts only (titles are what the keyword scan reads). Uses the owner's socialscope puller (Arctic Shift) from
features.socialscope_dir and writes r_<sub>_posts_<Tue>_to_<Mon>.zip into <features.reddit_dir>/broad/, where
features.py already looks. A week is skipped when a file with the same name exists anywhere under reddit_dir, so a
week the weekly panel already pulled (r/LivestreamFail) is never counted twice. Rerunning resumes a stopped pull.
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DEFAULTS = {"broad_subs": ["news", "politics", "worldnews", "LivestreamFail"], "broad_since": "2026-11-03",
            "socialscope_dir": "~/socialscope"}


def settings(cfg) -> dict:
    st = {**DEFAULTS, **(cfg.get("features") or {})}
    if not st.get("reddit_dir"):
        sys.exit("features.reddit_dir is not set")
    return st


def weeks(since: str, today: date) -> list[date]:
    """First days of every finished 7-day week counted from `since` (a Tuesday, like the weekly panel's weeks)."""
    d, out = date.fromisoformat(since), []
    while d + timedelta(days=7) <= today:
        out.append(d)
        d += timedelta(days=7)
    return out


def label(monday: date) -> str:
    return f"{monday.isoformat()}_to_{(monday + timedelta(days=6)).isoformat()}"


def missing(st: dict, today: date) -> list[tuple[date, list[str]]]:
    root = Path(st["reddit_dir"]).expanduser()
    have = {p.name for p in root.rglob("r_*_posts_*.zip")}
    todo = []
    for mon in weeks(st["broad_since"], today):
        subs = [s for s in st["broad_subs"] if f"r_{s}_posts_{label(mon)}.zip" not in have]
        if subs:
            todo.append((mon, subs))
    return todo


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    st = settings(common.load_config())
    todo = missing(st, date.today())
    if not todo:
        print("nothing to pull")
        return
    for mon, subs in todo:
        print(f"week {label(mon)}: {', '.join('r/' + s for s in subs)}")
    if a.dry_run:
        return
    sys.path.insert(0, str(Path(st["socialscope_dir"]).expanduser()))
    from socialscope import arctic
    out = Path(st["reddit_dir"]).expanduser() / "broad"
    for mon, subs in todo:
        arctic.pull_subreddits(subs, mon.isoformat(), (mon + timedelta(days=7)).isoformat(), kinds=("posts",),
                               out_dir=str(out))
    left = missing(st, date.today())
    if left:   # Arctic Shift said "too busy" for some; the next run resumes them
        print("unfinished:", "; ".join(f"{label(m)} {','.join(s)}" for m, s in left))
        sys.exit(1)
    print("all weeks pulled")


if __name__ == "__main__":
    main()
