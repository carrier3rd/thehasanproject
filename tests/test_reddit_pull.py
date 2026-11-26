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


def test_this_week_starts_on_the_panel_tuesday():
    assert reddit_pull.current_week("2026-11-03", date(2026, 11, 26)) == date(2026, 11, 24)
    assert reddit_pull.current_week("2026-11-03", date(2026, 11, 24)) == date(2026, 11, 24)
    assert reddit_pull.current_week("2026-11-03", date(2026, 11, 23)) == date(2026, 11, 17)


def test_a_longer_pull_replaces_the_partial_one(tmp_path):
    def zip_at(label, sub):
        d = tmp_path / label
        d.mkdir(exist_ok=True)
        (d / f"r_{sub}_posts_{label}.zip").write_bytes(b"x")
    zip_at("2026-11-24_to_2026-11-26", "news")
    zip_at("2026-11-24_to_2026-11-26", "worldnews")          # a sub the new pull didn't fetch: kept
    zip_at("2026-11-24_to_2026-11-30", "news")
    zip_at("2026-11-17_to_2026-11-23", "news")              # another week: kept
    gone = reddit_pull.drop_partials(tmp_path, date(2026, 11, 24), "2026-11-24_to_2026-11-30", ["news"])
    assert [g.name for g in gone] == ["r_news_posts_2026-11-24_to_2026-11-26.zip"]
    assert (tmp_path / "2026-11-24_to_2026-11-26" / "r_worldnews_posts_2026-11-24_to_2026-11-26.zip").exists()
    assert (tmp_path / "2026-11-17_to_2026-11-23" / "r_news_posts_2026-11-17_to_2026-11-23.zip").exists()
    # a shorter pull never removes a longer one
    assert reddit_pull.drop_partials(tmp_path, date(2026, 11, 24), "2026-11-24_to_2026-11-25", ["news"]) == []
