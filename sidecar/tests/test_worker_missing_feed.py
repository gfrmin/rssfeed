"""Worker resilience to feeds deleted from Miniflux (e.g. feed 1): a 400/404 on a
feed's entries must be recognised as 'feed gone' so it's skipped non-destructively
rather than spamming an ERROR every poll."""
import httpx

from app.worker import _feed_gone_from_miniflux


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://localhost:9144/v1/feeds/1/entries")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)


def test_400_invalid_feed_id_is_gone():
    assert _feed_gone_from_miniflux(_status_error(400)) is True


def test_404_not_found_is_gone():
    assert _feed_gone_from_miniflux(_status_error(404)) is True


def test_500_is_not_treated_as_gone():
    # A server error is transient — keep logging/ retrying, don't mark missing.
    assert _feed_gone_from_miniflux(_status_error(500)) is False


def test_non_http_error_is_not_gone():
    assert _feed_gone_from_miniflux(ConnectionError("boom")) is False
