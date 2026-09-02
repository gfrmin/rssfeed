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
