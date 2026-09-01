"""Unit tests for the unified feed health classifier."""
from datetime import UTC, datetime, timedelta

import pytest

from app.feed_health import (
    BUCKET_LABELS,
    annotate,
    classify,
    error_bucket,
    median_gap,
)

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
    ("blocked by Cloudflare challenge (403 status code)", "cloudflare"),
    ("access forbidden (403 status code)", "forbidden"),
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
        "cloudflare", "forbidden",
        "ok", "stale", "paused", "quiet",
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


# ---------------------------------------------------------------- quiet feeds

def test_median_gap_of_a_regular_cadence():
    ts = [NOW - timedelta(hours=h) for h in (0, 2, 4, 6)]
    assert median_gap(ts) == 2 * 3600


def test_median_gap_is_order_independent():
    ts = [NOW - timedelta(hours=h) for h in (4, 0, 6, 2)]
    assert median_gap(ts) == 2 * 3600


def test_median_gap_resists_one_long_hiatus():
    # five 1h gaps and one 100h gap: the median stays 1h, the mean would not
    hours = [0, 1, 2, 3, 4, 5, 105]
    ts = [NOW - timedelta(hours=h) for h in hours]
    assert median_gap(ts) == 3600


def test_median_gap_needs_at_least_two_entries():
    assert median_gap([NOW]) is None
    assert median_gap([]) is None


def test_quiet_when_silent_far_beyond_its_own_cadence():
    h = classify(_f(latest_entry_at=(NOW - timedelta(hours=19)).isoformat(),
                    median_gap_s=2 * 3600), NOW)
    assert h.state == "quiet"
    assert h.is_quiet is True
    assert h.since_latest_entry == pytest.approx(19 * 3600)


def test_not_quiet_inside_its_own_cadence():
    # a monthly feed silent for 19h is behaving normally
    h = classify(_f(latest_entry_at=(NOW - timedelta(hours=19)).isoformat(),
                    median_gap_s=30 * 24 * 3600), NOW)
    assert h.state == "ok"
    assert h.is_quiet is False


def test_quiet_never_fires_before_the_floor():
    # posts every 5 minutes; 40 minutes is 8x cadence but under the absolute floor
    h = classify(_f(latest_entry_at=(NOW - timedelta(minutes=40)).isoformat(),
                    median_gap_s=300), NOW)
    assert h.state == "ok"
    assert h.is_quiet is False


def test_quiet_needs_cadence_data():
    # without a baseline we cannot tell silence from a slow week
    h = classify(_f(latest_entry_at=(NOW - timedelta(days=90)).isoformat()), NOW)
    assert h.state == "ok"
    assert h.is_quiet is False


def test_fetch_errors_outrank_quiet():
    h = classify(_f(parsing_error_count=3, parsing_error_message="access forbidden (403 status code)",
                    latest_entry_at=(NOW - timedelta(days=90)).isoformat(),
                    median_gap_s=3600), NOW)
    assert h.state == "error"
    assert h.bucket == "forbidden"


def test_stale_outranks_quiet():
    # if we are not polling it, we cannot claim to know the publisher went silent
    h = classify(_f(checked_at=(NOW - timedelta(hours=25)).isoformat(),
                    latest_entry_at=(NOW - timedelta(days=90)).isoformat(),
                    median_gap_s=3600), NOW)
    assert h.state == "stale"


def test_annotate_stamps_quiet_keys():
    feed = _f(latest_entry_at=(NOW - timedelta(hours=19)).isoformat(), median_gap_s=2 * 3600)
    h = annotate(feed, NOW)
    assert feed["_health"] == "quiet"
    assert feed["_is_quiet"] is True
    assert feed["_since_latest_entry"] == h.since_latest_entry


def test_every_bucket_error_bucket_can_return_has_a_label():
    messages = ["resource not found", "Unable to detect feed format", "bot protection",
                "blocked by Cloudflare challenge (403 status code)",
                "access forbidden (403 status code)", "server error", "not authorized",
                "TLS handshake", "unsupported scheme", "dial tcp: lookup example.org",
                "dial tcp 192.0.2.1:443", "mystery"]
    for msg in messages:
        assert error_bucket(msg) in BUCKET_LABELS


# ---------------------------------------------------------------------------
# The real Miniflux corpus.
#
# The taxonomy cases above use short invented substrings, which is how
# `forbidden` came to be dead code: Miniflux's actual 403 message also
# speculates about bot protection, so a `bot protection` test ahead of a
# `forbidden` one matched first and nothing ever reached the new bucket.
# These are the message shapes this instance actually produces, verbatim
# except for hostnames and URLs. Anything that goes into `other` here is a
# gap in the taxonomy, not a feed with an exotic problem.
# ---------------------------------------------------------------------------

_NET = 'Miniflux is not able to reach this website due to a network error: Get "https://feed.invalid/rss": '

MINIFLUX_MESSAGES = [
    ("Unable to detect feed format: parser: unable to detect feed format.", "not_a_feed"),
    ("The requested resource is not found. Please, verify the URL.", "http_404"),
    ("Access to this website is forbidden. Perhaps, this website has a bot "
     "protection mechanism?", "forbidden"),
    ("This website is protected by a Cloudflare bot challenge (CAPTCHA or "
     "JavaScript verification). Miniflux cannot solve this challenge "
     "automatically.", "cloudflare"),
    ("Access to this website is not authorized. It could be a bad username or "
     "password.", "auth"),
    ("The website is not available at the moment due to a server error. The "
     "problem is not on Miniflux side. Please, try again later.", "server_5xx"),
    ("The website is not available at the moment due to an unexpected HTTP "
     "status code: 525. The problem is not on Miniflux side. Please, try "
     "again later.", "server_5xx"),
    ("The website is not available at the moment due to an unexpected HTTP "
     "status code: 520. The problem is not on Miniflux side. Please, try "
     "again later.", "server_5xx"),
    ("The website is not available at the moment due to an unexpected HTTP "
     "status code: 403. The problem is not on Miniflux side. Please, try "
     "again later.", "forbidden"),
    ("The website is not available at the moment due to an unexpected HTTP "
     "status code: 404. The problem is not on Miniflux side. Please, try "
     "again later.", "http_404"),
    (_NET + "Auth failed.", "auth"),
    (_NET + "dial tcp: lookup host.invalid on 10.0.0.1:53: no such host.", "dns_fail"),
    (_NET + "dial tcp: lookup host.invalid on 10.0.0.1:53: server misbehaving.", "dns_fail"),
    (_NET + "dial tcp: lookup host.invalid: i/o timeout.", "dns_fail"),
    (_NET + "dial tcp: lookup host.invalid on 10.0.0.1:53: read udp "
            "10.0.0.3:54535->10.0.0.1:53: i/o timeout.", "dns_fail"),
    (_NET + "dial tcp 192.0.2.1:80: i/o timeout.", "connect_fail"),
    (_NET + "context deadline exceeded (Client.Timeout exceeded while awaiting "
            "headers).", "connect_fail"),
    (_NET + "read tcp 10.0.0.3:32778->192.0.2.5:443: read: connection reset by "
            "peer.", "connect_fail"),
    (_NET + "EOF.", "connect_fail"),
    (_NET + "tls: failed to verify certificate: x509: certificate has expired "
            "or is not yet valid: current time 2026-07-18T22:34:14Z is after "
            "2026-03-18T07:28:32Z.", "tls"),
    ('TLS error: "Get \\"https://feed.invalid/rss\\": tls: failed to verify '
     'certificate: x509: certificate is valid for a.example, not b.example". '
     "You could disable TLS verification in the feed settings if you would "
     "like.", "tls"),
]


@pytest.mark.parametrize("msg,bucket", MINIFLUX_MESSAGES,
                         ids=[b for _, b in MINIFLUX_MESSAGES])
def test_real_miniflux_messages_land_in_the_right_bucket(msg, bucket):
    assert error_bucket(msg) == bucket


def test_the_forbidden_message_is_not_eaten_by_the_bot_protection_rule():
    """Miniflux's 403 text speculates about bot protection; the 403 is the fact."""
    forbidden = ("Access to this website is forbidden. Perhaps, this website "
                 "has a bot protection mechanism?")
    assert error_bucket(forbidden) == "forbidden"


def test_cloudflare_and_forbidden_are_told_apart_by_their_real_messages():
    cf = ("This website is protected by a Cloudflare bot challenge (CAPTCHA or "
          "JavaScript verification). Miniflux cannot solve this challenge "
          "automatically.")
    fb = ("Access to this website is forbidden. Perhaps, this website has a "
          "bot protection mechanism?")
    assert error_bucket(cf) != error_bucket(fb)


def test_no_real_message_falls_through_to_other():
    unclassified = [m for m, _ in MINIFLUX_MESSAGES if error_bucket(m) == "other"]
    assert not unclassified, unclassified


def test_a_genuinely_unknown_message_still_lands_in_other():
    assert error_bucket("This feed already exists.") == "other"
