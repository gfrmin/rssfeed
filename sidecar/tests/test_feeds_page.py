"""Unit tests for the Feeds-page health summary line."""
from app.routes.feeds import _health_summary


def _f(health):
    return {"_health": health}


def test_health_summary_empty_when_all_ok():
    assert _health_summary([_f("ok"), _f("ok")]) == ""


def test_health_summary_mixed_states():
    feeds = [_f("error"), _f("error"), _f("stale"), _f("ok")]
    assert _health_summary(feeds) == "2 erroring · 1 stale"
