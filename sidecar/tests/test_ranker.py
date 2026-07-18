"""Unit tests for ranker feature extraction + observation building (Part C)."""
from datetime import UTC, datetime, timedelta

import pytest

from app import ranker

NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


def test_feature_key_sanitizes():
    assert ranker.feature_key("author", "C. Jarrett Dieterle") == "author:c_jarrett_dieterle"
    assert ranker.feature_key("tag", "  AI / ML  ") == "tag:ai_ml"
    assert ranker.feature_key("tag", "***") == "tag:unknown"  # empty after sanitize
    assert ranker.feature_key("author", None) == "author:unknown"


def test_entry_features_shape():
    e = {
        "id": 1, "feed_id": 9, "author": "Jane Doe",
        "tags": ["AI", "Linux"], "published_at": NOW.isoformat(),
    }
    feats = dict(ranker.entry_features(e, priority=1, now=NOW))
    assert feats["feed:9"] == 1.0
    assert feats["author:jane_doe"] == 1.0
    assert feats["tag:ai"] == 1.0 and feats["tag:linux"] == 1.0
    assert feats["priority"] == 1.0          # must-read tier
    assert 0.0 <= feats["recency"] <= 1.0


def test_priority_scalar_tiers():
    base = {"id": 1, "feed_id": 1, "published_at": NOW.isoformat()}
    assert dict(ranker.entry_features(base, 1, NOW))["priority"] == 1.0
    assert dict(ranker.entry_features(base, 2, NOW))["priority"] == 0.5
    assert dict(ranker.entry_features(base, 3, NOW))["priority"] == 0.0
    assert dict(ranker.entry_features(base, 99, NOW))["priority"] == 0.5  # unknown -> mid


def test_recency_decay_monotonic():
    fresh = {"id": 1, "feed_id": 1, "published_at": NOW.isoformat()}
    old = {"id": 2, "feed_id": 1,
           "published_at": (NOW - timedelta(hours=72)).isoformat()}
    r_fresh = dict(ranker.entry_features(fresh, 2, NOW))["recency"]
    r_old = dict(ranker.entry_features(old, 2, NOW))["recency"]
    assert r_fresh > r_old
    assert r_fresh > 0.9                      # ~now
    # 72h = 2 half-lives (36h) -> ~0.25
    assert 0.2 < r_old < 0.3
    # missing date -> 0
    assert dict(ranker.entry_features({"id": 3, "feed_id": 1}, 2, NOW))["recency"] == 0.0


def test_feature_names_excludes_zero():
    # A low-priority, old, author/tag-less entry has priority 0.0 -> excluded.
    e = {"id": 1, "feed_id": 5,
         "published_at": (NOW - timedelta(hours=1)).isoformat()}
    names = ranker.feature_names(e, priority=3, now=NOW)
    assert "feed:5" in names
    assert "recency" in names
    assert "priority" not in names           # value 0.0 dropped


def test_build_observation():
    e = {"id": 7, "feed_id": 2, "author": "A", "tags": ["x"],
         "published_at": NOW.isoformat()}
    obs = ranker.build_observation(e, "dwell", 12.5, priority=2, now=NOW)
    assert obs["signal"] == "dwell"
    assert obs["value"] == 12.5
    feats = dict(obs["features"])                       # [name, value] pairs now
    assert feats["feed:2"] == 1.0 and feats["author:a"] == 1.0 and feats["tag:x"] == 1.0


def test_tag_objects_tolerated():
    e = {"id": 1, "feed_id": 1, "tags": [{"title": "Sec"}, "Net", {"name": "Ops"}],
         "published_at": NOW.isoformat()}
    feats = dict(ranker.entry_features(e, 2, NOW))
    assert feats.get("tag:sec") == 1.0
    assert feats.get("tag:net") == 1.0
    assert feats.get("tag:ops") == 1.0


def test_entry_features_embed_sim_optional():
    e = {"id": 1, "feed_id": 9, "published_at": NOW.isoformat()}
    base = dict(ranker.entry_features(e, 2, NOW))
    assert "embed_sim" not in base                      # absent by default
    with_sim = dict(ranker.entry_features(e, 2, NOW, embed_sim=0.73))
    assert with_sim["embed_sim"] == 0.73
    assert ranker.feature_label("embed_sim") == "similar to your taste"


def test_feature_label_humanizes():
    assert ranker.feature_label("recency") == "freshness"
    assert ranker.feature_label("priority") == "priority tier"
    assert ranker.feature_label("author:jane_doe") == "author Jane Doe"
    assert ranker.feature_label("tag:ai_ml") == "#aiml"
    assert ranker.feature_label("feed:9") == "this source"
    assert ranker.feature_label("feed:9", feed_title="Orbital Weekly") == "Orbital Weekly"


def test_build_articles_payload():
    es = [{"id": 1, "feed_id": 3, "published_at": NOW.isoformat()}]
    arts = ranker.build_articles(es, {3: 1}, NOW)
    assert arts[0]["entry_id"] == 1
    assert ["feed:3", 1.0] in arts[0]["features"]


def test_recency_public_alias_custom_halflife():
    six_hours_old = NOW - timedelta(hours=6)
    assert ranker.recency(six_hours_old.isoformat(), NOW, half_life_hours=6.0) == \
        pytest.approx(0.5, abs=0.01)


def test_build_mute_observation_single_feature():
    obs = ranker.build_mute_observation("mute_author", "Jane Doe")
    assert obs == {"signal": "mute_author", "value": 1.0,
                   "features": [["author:jane_doe", 1.0]]}
    obs = ranker.build_mute_observation("unmute_tag", "AI / ML")
    assert obs["features"] == [["tag:ai_ml", 1.0]]


def test_build_mute_observation_rejects_empty():
    assert ranker.build_mute_observation("mute_author", "") is None
    assert ranker.build_mute_observation("mute_author", None) is None
    assert ranker.build_mute_observation("star", "Jane") is None   # not a mute signal


def test_is_mute_signal():
    assert ranker.is_mute_signal("mute_tag") and not ranker.is_mute_signal("star")
