"""Unit tests for the unified feed health classifier."""
from datetime import UTC, datetime, timedelta

import pytest

from app.feed_health import BUCKET_LABELS, annotate, classify, error_bucket

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _f(**kw):
    base = {"id": 1, "checked_at": (NOW - timedelta(hours=1)).isoformat(),
            "parsing_error_count": 0, "parsing_error_message": "", "disabled": False}
    base.update(kw)
    return base


def test_fresh_no_errors_is_ok():
    h = classify(_f(), NOW)
    assert h.state == "ok"
    assert h.bucket == "ok"
    assert h.checked_ago == pytest.approx(3600)


def test_paused_wins_over_persistent_errors():
    h = classify(_f(disabled=True, parsing_error_count=5), NOW)
    assert h.state == "paused"


def test_single_error_is_warn_not_persistent():
    h = classify(_f(parsing_error_count=1), NOW)
    assert h.state == "warn"
    assert h.persistent is False


def test_two_errors_with_message_bucket_server_5xx():
    h = classify(_f(parsing_error_count=2, parsing_error_message="server error"), NOW)
    assert h.state == "warn"
    assert h.bucket == "server_5xx"


def test_three_errors_is_persistent_error():
    h = classify(_f(parsing_error_count=3), NOW)
    assert h.state == "error"
    assert h.persistent is True


def test_message_with_zero_count_is_warn():
    h = classify(_f(parsing_error_count=0, parsing_error_message="resource not found"), NOW)
    assert h.state == "warn"


def test_stale_threshold():
    h = classify(_f(checked_at=(NOW - timedelta(hours=25)).isoformat()), NOW)
    assert h.state == "stale"
    h = classify(_f(checked_at=(NOW - timedelta(hours=23)).isoformat()), NOW)
    assert h.state == "ok"


def test_unparseable_checked_at_is_ok_not_stale():
    h = classify(_f(checked_at="garbage"), NOW)
    assert h.checked_ago is None
    assert h.state == "ok"


@pytest.mark.parametrize("msg,bucket", [
    ("resource not found", "http_404"),
    ("Unable to detect feed format", "not_a_feed"),
    ("bot protection", "bot_blocked"),
    ("server error", "server_5xx"),
    ("not authorized", "auth"),
    ("TLS handshake", "tls"),
    ("unsupported scheme", "unsupported_scheme"),
    ("dial tcp: lookup example.org", "dns_fail"),
    ("dial tcp 192.0.2.1:443", "connect_fail"),
    ("mystery", "other"),
    ("", ""),
])
def test_error_bucket_taxonomy(msg, bucket):
    assert error_bucket(msg) == bucket


def test_bucket_labels_cover_every_bucket():
    buckets = {
        "http_404", "not_a_feed", "bot_blocked", "server_5xx", "auth", "tls",
        "unsupported_scheme", "dns_fail", "connect_fail", "other",
        "ok", "stale", "paused",
    }
    for bucket in buckets:
        assert bucket in BUCKET_LABELS


def test_annotate_stamps_all_underscore_keys():
    feed = _f(parsing_error_count=1, parsing_error_message="server error")
    h = annotate(feed, NOW)
    assert feed["_health"] == h.state == "warn"
    assert feed["_bucket"] == h.bucket == "server_5xx"
    assert feed["_bucket_label"] == BUCKET_LABELS["server_5xx"]
    assert feed["_error_count"] == h.error_count == 1
    assert feed["_has_error"] == h.has_error is True
    assert feed["_is_persistent"] == h.persistent is False
    assert feed["_is_stale"] == h.is_stale is False
    assert feed["_is_paused"] is False
    assert feed["_checked_ago"] == h.checked_ago
