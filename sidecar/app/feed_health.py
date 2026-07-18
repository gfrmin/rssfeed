"""Single source of truth for feed health classification.

Collapses Miniflux feed state (disabled, parsing_error_count,
parsing_error_message, checked_at) into one record shared by the sidebar
dots (routes/entries.py) and the feed settings page/overlay (routes/feeds.py).

Policy: amber "warn" on the first failed poll (parsing_error_count >= 1);
red "error" once failures are persistent (>= PERSISTENT_ERROR_THRESHOLD).
"""
from dataclasses import dataclass
from datetime import datetime

STALE_SECONDS = 24 * 3600
PERSISTENT_ERROR_THRESHOLD = 3

# Short human labels for the fine-grained buckets (feeds-page row labels).
BUCKET_LABELS = {
    "ok": "", "stale": "stale", "paused": "paused",
    "http_404": "404", "not_a_feed": "not a feed", "bot_blocked": "bot-blocked",
    "server_5xx": "5xx", "auth": "auth", "tls": "TLS",
    "unsupported_scheme": "unsupported URL", "dns_fail": "DNS",
    "connect_fail": "connect", "other": "error",
}


@dataclass(frozen=True)
class FeedHealth:
    state: str            # "ok" | "warn" | "error" | "stale" | "paused"
    bucket: str           # state name, or a fine-grained error bucket when warn/error
    persistent: bool
    error_count: int
    has_error: bool
    is_stale: bool
    checked_ago: float | None   # seconds since checked_at; None if missing/unparseable


def error_bucket(msg: str) -> str:
    m = msg or ""
    if not m:
        return ""
    if "not found" in m and "resource" in m:
        return "http_404"
    if "Unable to detect feed format" in m:
        return "not_a_feed"
    if "bot protection" in m or "forbidden" in m:
        return "bot_blocked"
    if "server error" in m:
        return "server_5xx"
    if "not authorized" in m or "bad username" in m:
        return "auth"
    if "TLS" in m or "tls:" in m:
        return "tls"
    if "unsupported" in m.lower():
        return "unsupported_scheme"
    if "dial tcp" in m and "lookup" in m:
        return "dns_fail"
    if "dial tcp" in m:
        return "connect_fail"
    return "other"


def classify(feed: dict, now: datetime) -> FeedHealth:
    checked_ago = None
    checked = feed.get("checked_at", "")
    if checked:
        try:
            dt = datetime.fromisoformat(checked.replace("Z", "+00:00"))
            checked_ago = (now - dt).total_seconds()
        except Exception:
            checked_ago = None
    error_count = feed.get("parsing_error_count") or 0
    has_error = bool(feed.get("parsing_error_message")) or error_count >= 1
    persistent = error_count >= PERSISTENT_ERROR_THRESHOLD
    is_stale = checked_ago is not None and checked_ago > STALE_SECONDS
    if feed.get("disabled"):
        state = "paused"
    elif persistent:
        state = "error"
    elif has_error:
        state = "warn"
    elif is_stale:
        state = "stale"
    else:
        state = "ok"
    if state in ("warn", "error"):
        bucket = error_bucket(feed.get("parsing_error_message", "")) or "other"
    else:
        bucket = state
    return FeedHealth(state=state, bucket=bucket, persistent=persistent,
                      error_count=error_count, has_error=has_error,
                      is_stale=is_stale, checked_ago=checked_ago)


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
    feed["_is_paused"] = h.state == "paused"
    feed["_checked_ago"] = h.checked_ago
    return h
