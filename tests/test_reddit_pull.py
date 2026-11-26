import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import reddit_pull  # noqa: E402


def test_weeks_are_finished_tuesday_weeks():
    got = reddit_pull.weeks("2026-11-03", date(2026, 11, 25))
    assert [d.isoformat() for d in got] == ["2026-11-03", "2026-11-10", "2026-11-17"]
    assert reddit_pull.weeks("2026-11-03", date(2026, 11, 9)) == []      # first week not over yet


def test_missing_skips_weeks_already_pulled_anywhere(tmp_path):
    panel = tmp_path / "nov10" / "2026-11-10_to_2026-11-16"
    panel.mkdir(parents=True)
    (panel / "r_LivestreamFail_posts_2026-11-10_to_2026-11-16.zip").write_bytes(b"")
    st = {**reddit_pull.DEFAULTS, "reddit_dir": str(tmp_path), "broad_since": "2026-11-10"}
    todo = dict(reddit_pull.missing(st, date(2026, 11, 25)))
    assert todo[date(2026, 11, 10)] == ["news", "politics", "worldnews"]
    assert todo[date(2026, 11, 17)] == ["news", "politics", "worldnews", "LivestreamFail"]
