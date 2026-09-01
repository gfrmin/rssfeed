"""Unit tests for the Feeds-page health summary line."""
from pathlib import Path

from app.routes.feeds import _health_summary


def _f(health):
    return {"_health": health}


def test_health_summary_empty_when_all_ok():
    assert _health_summary([_f("ok"), _f("ok")]) == ""


def test_health_summary_mixed_states():
    feeds = [_f("error"), _f("error"), _f("stale"), _f("ok")]
    assert _health_summary(feeds) == "2 erroring · 1 stale"


def test_health_summary_counts_quiet_feeds():
    assert _health_summary([_f("quiet"), _f("quiet"), _f("ok")]) == "2 quiet"


def test_health_summary_orders_worst_first():
    feeds = [_f("paused"), _f("quiet"), _f("stale"), _f("warn"), _f("error")]
    assert _health_summary(feeds) == "1 erroring · 1 warning · 1 stale · 1 quiet · 1 paused"


def test_stale_label_does_not_promise_item_recency():
    """`stale` measures time since the last POLL, not the last published item.

    The template used to read "no items 24h+", which describes `quiet` --
    a different bucket, computed from different data.
    """
    macros = (Path(__file__).parent.parent / "app" / "templates" / "_macros.html").read_text()
    stale_lines = [ln for ln in macros.splitlines() if '"stale"' in ln]
    assert stale_lines, "HEALTH_META lost its stale entry"
    assert "no items" not in stale_lines[0]
    assert "poll" in stale_lines[0].lower()


# ------------------------------------------------------------------------
# Decorating the feed list: Miniflux's feeds joined to what the shared
# Postgres knows about their publishing cadence. This is the seam where
# `quiet` either fires or silently never does, so it is tested end to end
# rather than by asserting a key name.
# ------------------------------------------------------------------------
from datetime import UTC, datetime, timedelta  # noqa: E402

from app.routes.feeds import decorate_feeds  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _feed(fid, title="A Feed", **kw):
    base = {
        "id": fid, "title": title, "feed_url": f"https://{fid}.example/rss",
        "checked_at": (NOW - timedelta(minutes=20)).isoformat(),
        "parsing_error_count": 0, "parsing_error_message": "", "disabled": False,
    }
    return base | kw


def _decorate(feeds, cadence=None, unreads=None, configs=None):
    return decorate_feeds(feeds, unreads=unreads or {}, cadence=cadence or {},
                          configs=configs or {}, now=NOW)


def test_decorate_feeds_stamps_the_latest_entry_and_its_age():
    latest = NOW - timedelta(hours=3)
    (f,) = _decorate([_feed(1)], {1: {"latest": latest, "median_gap_s": 3600.0}})
    assert f["latest_entry_at"] == latest
    assert f["latest_age_s"] == 3 * 3600


def test_decorate_feeds_marks_a_publisher_that_has_gone_quiet():
    """An hourly feed silent for two days is quiet — the whole point of the join."""
    cadence = {1: {"latest": NOW - timedelta(days=2), "median_gap_s": 3600.0}}
    (f,) = _decorate([_feed(1)], cadence)
    assert f["_health"] == "quiet"


def test_decorate_feeds_leaves_a_feed_inside_its_own_cadence_alone():
    """A weekly feed silent for two days is simply a weekly feed."""
    cadence = {1: {"latest": NOW - timedelta(days=2), "median_gap_s": 7 * 86400.0}}
    (f,) = _decorate([_feed(1)], cadence)
    assert f["_health"] == "ok"


def test_decorate_feeds_says_nothing_about_a_feed_with_no_cadence_baseline():
    cadence = {1: {"latest": NOW - timedelta(days=400), "median_gap_s": None}}
    (f,) = _decorate([_feed(1)], cadence)
    assert f["_health"] == "ok"


def test_decorate_feeds_handles_a_feed_with_no_entries_at_all():
    (f,) = _decorate([_feed(1)], cadence={})
    assert f["latest_entry_at"] is None
    assert f["latest_age_s"] == ""
    assert f["_health"] == "ok"


def test_decorate_feeds_ignores_a_degenerate_zero_gap():
    """Feeds whose items all share one timestamp would otherwise be quiet forever."""
    cadence = {1: {"latest": NOW - timedelta(days=30), "median_gap_s": 0.0}}
    (f,) = _decorate([_feed(1)], cadence)
    assert f["_health"] == "ok"


def test_decorate_feeds_prefers_a_fetch_error_over_a_quiet_publisher():
    cadence = {1: {"latest": NOW - timedelta(days=2), "median_gap_s": 3600.0}}
    (f,) = _decorate([_feed(1, parsing_error_count=5,
                            parsing_error_message="fetcher: access forbidden (403 status code)")],
                     cadence)
    assert f["_health"] == "error"
    assert f["_bucket"] == "forbidden"


def test_decorate_feeds_carries_unread_counts_and_config():
    (f,) = _decorate([_feed(1)], unreads={"1": 7},
                     configs={1: {"fetch_full_content": True, "priority": 1}})
    assert f["unread_count"] == 7
    assert f["priority"] == 1
    assert f["fetch_full_content"] is True


def test_decorate_feeds_defaults_config_for_a_feed_the_sidecar_has_never_seen():
    (f,) = _decorate([_feed(1)])
    assert f["priority"] == 2
    assert f["fetch_full_content"] is False
    assert f["unread_count"] == 0


def test_decorate_feeds_sorts_by_priority_then_most_recent():
    feeds = [
        _feed(1, "low prio, newest"),
        _feed(2, "must read, older"),
        _feed(3, "must read, newest"),
    ]
    cadence = {
        1: {"latest": NOW - timedelta(hours=1), "median_gap_s": None},
        2: {"latest": NOW - timedelta(days=5), "median_gap_s": None},
        3: {"latest": NOW - timedelta(hours=2), "median_gap_s": None},
    }
    configs = {2: {"priority": 1}, 3: {"priority": 1}}
    out = _decorate(feeds, cadence, configs=configs)
    assert [f["id"] for f in out] == [3, 2, 1]
