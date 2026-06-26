"""`_snapshot_has_text` decides whether a stored snapshot has real prose or is an
empty SPA shell — the latter must fall back to the RSS body instead of rendering
a blank article (e.g. NR fetches made before the browser-render tier existed)."""
from app.routes.entries import _snapshot_has_text


def test_none_snapshot():
    assert _snapshot_has_text(None) is False


def test_real_text():
    assert _snapshot_has_text({"content_text": "A real article body.", "content_html": ""}) is True


def test_empty_shell_no_visible_text():
    # The exact shape early NR fetches stored: markup, but zero visible text.
    shell = {"content_text": "", "content_html": '<p id="page" data-headless></p><p id="bc-root"></p>'}
    assert _snapshot_has_text(shell) is False


def test_html_only_with_visible_text():
    assert _snapshot_has_text({"content_text": "", "content_html": "<p>Hello world</p>"}) is True


def test_whitespace_only_text_is_empty():
    assert _snapshot_has_text({"content_text": "   \n  ", "content_html": ""}) is False


# --- _content_block_ctx: show empty full + RSS for toggle, never silent-swap ---
from datetime import datetime, timezone

from app.routes.entries import _content_block_ctx

_FETCHED = datetime(2026, 6, 23, tzinfo=timezone.utc)


def test_ctx_empty_snapshot_is_full_empty_with_rss_available():
    snap = {"content_text": "", "content_html": '<p id="page"></p>',
            "version": 1, "metadata": {}, "fetched_at": _FETCHED}
    cb = _content_block_ctx(5, snap, 1, rss_html="<p>teaser</p>")
    assert cb["has_full"] is True and cb["full_empty"] is True
    assert cb["rss_html"] == "<p>teaser</p>"  # RSS kept for the toggle


def test_ctx_good_snapshot_not_empty():
    snap = {"content_text": "Real body", "content_html": "<p>Real body</p>",
            "version": 2, "metadata": {"source": "browser"}, "fetched_at": _FETCHED}
    cb = _content_block_ctx(5, snap, 1, rss_html="<p>teaser</p>")
    assert cb["has_full"] is True and cb["full_empty"] is False
    assert cb["source"] == "browser"


def test_ctx_no_snapshot_is_rss():
    cb = _content_block_ctx(5, None, 0, rss_html="<p>teaser</p>")
    assert cb["has_full"] is False and cb["full_empty"] is False
    assert cb["body_html"] == "<p>teaser</p>"
