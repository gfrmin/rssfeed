"""Unit tests for reading lenses (deterministic orderings over the ranker)."""
from datetime import UTC, datetime, timedelta

from app import lenses
from app.routes.entries import _smart_eligible

NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


def _e(eid, hours_old=0.0, prio=2, muted=False):
    return {"id": eid, "_priority": prio, "_muted": muted,
            "published_at": (NOW - timedelta(hours=hours_old)).isoformat()}


def test_smart_eligible_time_filter_no_longer_blocks():
    assert _smart_eligible(starred=False, changed=False, search=None, status="unread")
    assert _smart_eligible(starred=False, changed=False, search=None, status=None)


def test_smart_eligible_blocks_lookup_surfaces():
    assert not _smart_eligible(starred=True, changed=False, search=None, status="unread")
    assert not _smart_eligible(starred=False, changed=True, search=None, status="unread")
    assert not _smart_eligible(starred=False, changed=False, search="q", status="unread")
    assert not _smart_eligible(starred=False, changed=False, search=None, status="read")


def test_normalize_precedence():
    assert lenses.normalize("dive", "catchup") == "dive"       # param wins
    assert lenses.normalize(None, "catchup") == "catchup"      # cookie next
    assert lenses.normalize("bogus", "also-bogus") == "smart"  # default
    assert lenses.normalize("new", None) == "new"              # legacy value ok


def test_minmax_degenerate():
    assert lenses._minmax({}) == {}
    assert lenses._minmax({1: 3.0, 2: 3.0}) == {1: 0.5, 2: 0.5}
    out = lenses._minmax({1: 0.0, 2: 2.0, 3: 1.0})
    assert out == {1: 0.0, 2: 1.0, 3: 0.5}


def test_smart_uses_scores_and_sinks_muted():
    es = [_e(1), _e(2), _e(3, muted=True)]
    out, ranked = lenses.order_entries("smart", es, {1: 0.1, 2: 0.9, 3: 5.0}, {}, NOW)
    assert ranked and [e["id"] for e in out] == [2, 1, 3]   # muted sinks despite top score


def test_smart_fallback_priority_then_newest():
    es = [_e(1, hours_old=1, prio=2), _e(2, hours_old=9, prio=1), _e(3, hours_old=0, prio=2)]
    out, ranked = lenses.order_entries("smart", es, None, {}, NOW)
    assert not ranked and [e["id"] for e in out] == [2, 3, 1]


def test_new_is_reverse_chron():
    es = [_e(1, hours_old=5), _e(2, hours_old=1)]
    out, ranked = lenses.order_entries("new", es, {1: 9.0, 2: 0.0}, {}, NOW)
    assert not ranked and [e["id"] for e in out] == [2, 1]   # scores ignored


def test_catchup_freshness_dominates_score():
    es = [_e(1, hours_old=0.5), _e(2, hours_old=48)]
    out, ranked = lenses.order_entries("catchup", es, {1: 0.0, 2: 10.0}, {}, NOW)
    assert ranked and [e["id"] for e in out] == [1, 2]       # 48h old loses despite score


def test_catchup_works_with_engine_down():
    es = [_e(1, hours_old=12, prio=1), _e(2, hours_old=0.1, prio=3)]
    out, ranked = lenses.order_entries("catchup", es, None, {}, NOW)
    assert not ranked and out[0]["id"] == 2                  # freshness still wins


def test_dive_ignores_age_prefers_similarity():
    es = [_e(1, hours_old=24 * 30), _e(2, hours_old=0.1)]
    out, ranked = lenses.order_entries("dive", es, {1: 5.0, 2: 5.0}, {1: 0.9, 2: 0.1}, NOW)
    assert ranked and [e["id"] for e in out] == [1, 2]       # month-old taste match wins


def test_dive_engine_down_uses_sim_and_priority():
    es = [_e(1, prio=3), _e(2, prio=1)]
    out, ranked = lenses.order_entries("dive", es, None, {1: 0.95}, NOW)
    assert not ranked and out[0]["id"] == 1                  # 0.7*0.95 > 0.7*0 + 0.3*1.0
