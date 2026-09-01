"""Single source of truth for feed health classification.

Collapses Miniflux feed state (disabled, parsing_error_count,
parsing_error_message, checked_at) into one record shared by the sidebar
dots (routes/entries.py) and the feed settings page/overlay (routes/feeds.py).

Policy: amber "warn" on the first failed poll (parsing_error_count >= 1);
red "error" once failures are persistent (>= PERSISTENT_ERROR_THRESHOLD).

Two distinct silences, deliberately kept apart:
  "stale" -- Miniflux has not POLLED the feed in STALE_SECONDS. Our problem.
  "quiet" -- polling is fine, the PUBLISHER has stopped. Their problem.
A fixed threshold cannot express the second: a feed posting hourly is silent
at six hours, a monthly one is not. So "quiet" is measured against the feed's
own median gap, and never fires before QUIET_MIN_SECONDS regardless.
"""
import re
from dataclasses import dataclass
from datetime import datetime

STALE_SECONDS = 24 * 3600
PERSISTENT_ERROR_THRESHOLD = 3

# "Quiet" = silent for longer than QUIET_GAP_MULTIPLE times this feed's own
# median gap, and never sooner than QUIET_MIN_SECONDS however chatty it is.
QUIET_GAP_MULTIPLE = 4
QUIET_MIN_SECONDS = 6 * 3600

# Short human labels for the fine-grained buckets (feeds-page row labels).
BUCKET_LABELS = {
    "ok": "", "stale": "not polled", "paused": "paused", "quiet": "quiet",
    "http_404": "404", "not_a_feed": "not a feed", "bot_blocked": "bot-blocked",
    "cloudflare": "cloudflare", "forbidden": "403",
    "server_5xx": "5xx", "auth": "auth", "tls": "TLS",
    "unsupported_scheme": "unsupported URL", "dns_fail": "DNS",
    "connect_fail": "connect", "other": "error",
}


@dataclass(frozen=True)
class FeedHealth:
    state: str            # "ok" | "warn" | "error" | "stale" | "quiet" | "paused"
    bucket: str           # state name, or a fine-grained error bucket when warn/error
    persistent: bool
    error_count: int
    has_error: bool
    is_stale: bool
    is_quiet: bool
    checked_ago: float | None   # seconds since checked_at; None if missing/unparseable
    since_latest_entry: float | None  # seconds since the newest entry; None if unknown


def _seconds_since(value, now: datetime) -> float | None:
    """Seconds between `value` (datetime or ISO string) and now; None if unusable."""
    if not value:
        return None
    dt = value
    if not isinstance(dt, datetime):
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    try:
        return (now - dt).total_seconds()
    except TypeError:   # naive vs. aware
        return None


def median_gap(timestamps) -> float | None:
    """Median seconds between consecutive timestamps, or None under two entries.

    Median rather than mean so one hiatus does not redefine a feed's cadence.
    """
    ts = sorted(timestamps)
    if len(ts) < 2:
        return None
    gaps = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:], strict=False))
    mid = len(gaps) // 2
    if len(gaps) % 2:
        return gaps[mid]
    return (gaps[mid - 1] + gaps[mid]) / 2


# Whatever Miniflux has no specific wording for arrives as a bare status code.
# Reading the number beats matching the surrounding prose.
_STATUS_CODE = re.compile(r"unexpected HTTP status code: (\d{3})")


def error_bucket(msg: str) -> str:
    """Map a Miniflux parsing_error_message to a cause.

    Ordering is load-bearing in one place: Miniflux's own 403 text is
    "Access to this website is forbidden. Perhaps, this website has a bot
    protection mechanism?" — the 403 is the fact and the bot protection is
    Miniflux speculating, so `forbidden` is tested first. Reversed, every 403
    lands in `bot_blocked` and the finer bucket never fires at all.
    """
    m = msg or ""
    if not m:
        return ""
    code = _STATUS_CODE.search(m)
    if code:
        status = int(code.group(1))
        if status >= 500:
            return "server_5xx"
        if status == 403:
            return "forbidden"
        if status == 404:
            return "http_404"
    if "not found" in m and "resource" in m:
        return "http_404"
    if "Unable to detect feed format" in m:
        return "not_a_feed"
    if "cloudflare" in m.lower():
        return "cloudflare"
    if "forbidden" in m.lower():
        return "forbidden"
    if "bot protection" in m:
        return "bot_blocked"
    if "server error" in m:
        return "server_5xx"
    if "not authorized" in m or "bad username" in m or "Auth failed" in m:
        return "auth"
    if "TLS" in m or "tls:" in m:
        return "tls"
    if "unsupported" in m.lower():
        return "unsupported_scheme"
    if "dial tcp" in m and "lookup" in m:
        return "dns_fail"
    # Everything else that never got a response: refused, reset, timed out,
    # hung up mid-body. One cause from the reader's point of view.
    if ("dial tcp" in m or "context deadline exceeded" in m
            or "connection reset" in m or "EOF" in m):
        return "connect_fail"
    return "other"


def classify(feed: dict, now: datetime) -> FeedHealth:
    checked_ago = _seconds_since(feed.get("checked_at", ""), now)
    error_count = feed.get("parsing_error_count") or 0
    has_error = bool(feed.get("parsing_error_message")) or error_count >= 1
    persistent = error_count >= PERSISTENT_ERROR_THRESHOLD
    is_stale = checked_ago is not None and checked_ago > STALE_SECONDS

    # Publisher silence, judged against this feed's own rhythm. Without a
    # baseline we say nothing -- a slow week and a dead feed look identical.
    since_latest_entry = _seconds_since(feed.get("latest_entry_at"), now)
    baseline = feed.get("median_gap_s")
    is_quiet = (
        baseline is not None and baseline > 0
        and since_latest_entry is not None
        and since_latest_entry > max(QUIET_MIN_SECONDS, QUIET_GAP_MULTIPLE * baseline)
    )

    if feed.get("disabled"):
        state = "paused"
    elif persistent:
        state = "error"
    elif has_error:
        state = "warn"
    elif is_stale:
        # not polling it means we cannot claim to know the publisher went silent
        state = "stale"
    elif is_quiet:
        state = "quiet"
    else:
        state = "ok"
    if state in ("warn", "error"):
        bucket = error_bucket(feed.get("parsing_error_message", "")) or "other"
    else:
        bucket = state
    return FeedHealth(state=state, bucket=bucket, persistent=persistent,
                      error_count=error_count, has_error=has_error,
                      is_stale=is_stale, is_quiet=is_quiet, checked_ago=checked_ago,
                      since_latest_entry=since_latest_entry)


def annotate(feed: dict, now: datetime) -> FeedHealth:
    """Classify and stamp the template-facing underscore keys onto the feed dict."""
    h = classify(feed, now)
    feed["_health"] = h.state
    feed["_bucket"] = h.bucket
    feed["_bucket_label"] = BUCKET_LABELS.get(h.bucket, h.bucket)
    feed["_error_count"] = h.error_count
    feed["_has_error"] = h.has_error
    feed["_is_persistent"] = h.persistent
    feed["_is_stale"] = h.is_stale
    feed["_is_quiet"] = h.is_quiet
    feed["_is_paused"] = h.state == "paused"
    feed["_checked_ago"] = h.checked_ago
    feed["_since_latest_entry"] = h.since_latest_entry
    return h
