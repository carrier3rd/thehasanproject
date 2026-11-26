"""Weekly pull of the general-audience subreddits that Spotlight's blindspots are measured against.

  python pipeline/reddit_pull.py            pull every finished 7-day week since features.broad_since (Tuesday to Monday, like the panel) that is missing
  python pipeline/reddit_pull.py --dry-run  list what would be pulled

Posts and comments (features.broad_kinds; comments since 2026-11-26, the blindspot rule still counts posts only).
Uses the owner's socialscope puller (Arctic Shift) from features.socialscope_dir and writes
r_<sub>_<kind>_<Tue>_to_<Mon>.zip into <features.reddit_dir>/broad/, where
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
            "socialscope_dir": "~/socialscope", "broad_kinds": ["posts", "comments"]}


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


def current_week(since: str, today: date) -> date:
    """First day of the unfinished week that contains `today`."""
    d = date.fromisoformat(since)
    while d + timedelta(days=7) <= today:
        d += timedelta(days=7)
    return d


def drop_partials(out: Path, start: date, keep: str, subs: list[str], kinds=("posts",)) -> list[Path]:
    """Remove earlier partial pulls of this week (same first day, earlier last day) for these subs, so the scan never
    counts the same posts twice once a longer pull of the week exists."""
    gone = []
    for folder in out.glob(f"{start.isoformat()}_to_*"):
        if folder.name == keep or not folder.is_dir() or folder.name.split("_to_")[1] > keep.split("_to_")[1]:
            continue
        for sub in subs:
            for kind in kinds:
                z = folder / f"r_{sub}_{kind}_{folder.name}.zip"
                if z.exists():
                    z.unlink()
                    gone.append(z)
        if not any(folder.iterdir()):
            folder.rmdir()
    return gone


def missing(st: dict, today: date) -> list[tuple[date, list[str]]]:
    """(week, subs) with at least one kind not pulled yet; the puller skips the kinds it already has."""
    root = Path(st["reddit_dir"]).expanduser()
    have = {p.name for p in root.rglob("r_*.zip")}
    todo = []
    for mon in weeks(st["broad_since"], today):
        subs = [s for s in st["broad_subs"]
                if any(f"r_{s}_{k}_{label(mon)}.zip" not in have for k in st["broad_kinds"])]
        if subs:
            todo.append((mon, subs))
    return todo


def complete(out: Path, lab: str, subs: list[str], kinds) -> list[str]:
    return [s for s in subs if all((out / lab / f"r_{s}_{k}_{lab}.zip").exists() for k in kinds)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--this-week", action="store_true",
                    help="pull the unfinished current week up to now (replaces an earlier partial pull of it)")
    ap.add_argument("--subs", help="comma-separated subs for --this-week (default: features.broad_subs)")
    ap.add_argument("--reddit-dir", help="override features.reddit_dir (e.g. pulling on another machine)")
    ap.add_argument("--socialscope-dir", help="override features.socialscope_dir")
    a = ap.parse_args()
    st = settings(common.load_config())
    if a.reddit_dir:
        st["reddit_dir"] = a.reddit_dir
    if a.socialscope_dir:
        st["socialscope_dir"] = a.socialscope_dir
    if a.this_week:
        return this_week(st, a.subs.split(",") if a.subs else st["broad_subs"], a.dry_run)
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
        kinds = tuple(st["broad_kinds"])
        try:
            arctic.pull_subreddits(subs, mon.isoformat(), (mon + timedelta(days=7)).isoformat(), kinds=kinds,
                                   out_dir=str(out))
        except Exception as e:     # Arctic Shift too busy: keep what finished, the next run resumes the rest
            print(f"week {label(mon)} stopped: {e}")
        done = complete(out, label(mon), subs, kinds)
        for z in drop_partials(out, mon, label(mon), done, kinds):
            print(f"removed partial pull {z.name} (the full week replaces it)")
    left = missing(st, date.today())
    if left:   # Arctic Shift said "too busy" for some; the next run resumes them
        print("unfinished:", "; ".join(f"{label(m)} {','.join(s)}" for m, s in left))
        sys.exit(1)
    print("all weeks pulled")


def this_week(st: dict, subs: list[str], dry_run: bool) -> None:
    today = date.today()
    start = current_week(st["broad_since"], today)
    end = today + timedelta(days=1)           # end exclusive: today's posts so far
    keep = f"{start.isoformat()}_to_{today.isoformat()}"
    print(f"this week so far {keep}: {', '.join('r/' + s for s in subs)}")
    if dry_run:
        return
    sys.path.insert(0, str(Path(st["socialscope_dir"]).expanduser()))
    from socialscope import arctic
    out = Path(st["reddit_dir"]).expanduser() / "broad"
    kinds = tuple(st["broad_kinds"])
    try:
        arctic.pull_subreddits(subs, start.isoformat(), end.isoformat(), kinds=kinds, out_dir=str(out))
    except Exception as e:
        print(f"stopped: {e}")
    done = complete(out, keep, subs, kinds)
    for z in drop_partials(out, start, keep, done, kinds):
        print(f"removed older partial pull {z.name}")
    missing_subs = sorted(set(subs) - set(done))
    if missing_subs:
        print("unfinished:", ", ".join(missing_subs))
        sys.exit(1)
    print(f"pulled {len(done)} sub(s) for {keep}")


if __name__ == "__main__":
    main()
