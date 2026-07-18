"""`_snapshot_has_content` decides whether a stored snapshot has real content —
prose or media — or is an empty SPA shell; the latter must fall back to the RSS
body instead of rendering a blank article (as paywalled fetches did before the
browser-render tier). A text-free photo/video post is content, not a shell."""
from app.routes.entries import _snapshot_has_content


def test_none_snapshot():
    assert _snapshot_has_content(None) is False


def test_real_text():
    assert _snapshot_has_content({"content_text": "A real article body.", "content_html": ""}) is True


def test_empty_shell_no_visible_text():
    # The shape an early paywalled fetch stored: markup, but zero visible text.
    shell = {"content_text": "", "content_html": '<p id="page" data-headless></p><p id="app-root"></p>'}
    assert _snapshot_has_content(shell) is False


def test_html_only_with_visible_text():
    assert _snapshot_has_content({"content_text": "", "content_html": "<p>Hello world</p>"}) is True


def test_whitespace_only_text_is_empty():
    assert _snapshot_has_content({"content_text": "   \n  ", "content_html": ""}) is False


def test_media_only_is_content():
    # A text-free video/photo post — its content is the embed/image, not prose.
    video = {"content_text": "", "content_html": '<iframe src="https://www.youtube.com/embed/X"></iframe>'}
    assert _snapshot_has_content(video) is True
    photo = {"content_text": "", "content_html": '<figure><img src="https://x/a.jpg"></figure>'}
    assert _snapshot_has_content(photo) is True


def test_srcless_media_is_not_content():
    # A JS-hydrated player skeleton (no src yet) is furniture, not content.
    shell = {"content_text": "", "content_html": "<p><iframe></iframe><img></p>"}
    assert _snapshot_has_content(shell) is False


# --- _content_block_ctx: show empty full + RSS for toggle, never silent-swap ---
from datetime import UTC, datetime

from app.routes.entries import _content_block_ctx

_FETCHED = datetime(2026, 6, 23, tzinfo=UTC)


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


def test_ctx_media_only_snapshot_not_empty():
    # The render-side fix: a video post is no longer flagged "full fetch · empty".
    snap = {"content_text": "", "content_html": '<iframe src="https://www.youtube.com/embed/X"></iframe>',
            "version": 1, "metadata": {}, "fetched_at": _FETCHED}
    cb = _content_block_ctx(5, snap, 1, rss_html="<p>teaser</p>")
    assert cb["has_full"] is True and cb["full_empty"] is False


def test_ctx_no_snapshot_is_rss():
    cb = _content_block_ctx(5, None, 0, rss_html="<p>teaser</p>")
    assert cb["has_full"] is False and cb["full_empty"] is False
    assert cb["body_html"] == "<p>teaser</p>"
