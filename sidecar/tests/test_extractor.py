"""Unit tests for article extraction (extractor._extract).

Regression: a paywalled / JS-app page whose body is only empty wrapper
elements (e.g. National Review's ``<p id="page">``/``<p id="bc-root">`` React
mount points) must be treated as a *failed* extraction — returning None — not
stored as a blank snapshot that wipes the visible RSS content.
"""
from app.extractor import _extract, _has_media

# Mirrors the shell that actually got stored for the broken NR entry: the page
# scaffolding is present but carries zero article text.
EMPTY_SHELL = """<!DOCTYPE html><html><head><title>The Black Codes</title></head>
<body>
  <p id="page" class="site" data-headless></p>
  <p id="bc-root"></p>
</body></html>"""

REAL_ARTICLE = """<!DOCTYPE html><html><head><title>A Real Article</title></head>
<body><article><h1>A Real Article</h1>
<p>The Black Codes were a set of restrictive laws passed in the post-war South.
They constrained the freedoms of newly emancipated people in numerous ways, and
historians regard them as a cautionary tale rather than a useful precedent for
any modern policy debate about labour or movement.</p>
<p>This second paragraph exists so the body comfortably clears the extractor's
minimum-length thresholds and yields genuine prose for the reader pane.</p>
</article></body></html>"""


def test_empty_shell_returns_none():
    assert _extract(EMPTY_SHELL, "https://example.com/x", {}, proxy_images=False) is None


def test_real_article_extracts_text():
    result = _extract(REAL_ARTICLE, "https://example.com/x", {}, proxy_images=False)
    assert result is not None
    assert "Black Codes" in result["content_text"]
    assert result["content_text"].strip()


# --- text-free articles vs text-free shells -------------------------------
# The empty-shell guard rejects an extraction with no visible text. "No text"
# and "no article" aren't the same thing though: a photo essay or comic is
# legitimately text-free, and rejecting it sends a good fetch down the expensive
# tiers only to display the RSS body anyway. Media presence separates the two.

PHOTO_ESSAY = """<html><body><article>
  <figure><img src="https://example.com/1.jpg"></figure>
  <figure><img src="https://example.com/2.jpg"></figure>
</article></body></html>"""

VIDEO_POST = """<html><body><article>
  <video src="https://example.com/v.mp4"></video>
</article></body></html>"""

SHELL_WITH_PLACEHOLDER_IMG = """<html><body>
  <div id="app"><p id="page"><img></p><p id="bc-root"></p></div>
</body></html>"""


def test_image_only_article_is_kept():
    assert _extract(PHOTO_ESSAY, "https://example.com/p", {}) is not None


def test_video_only_article_is_kept():
    assert _extract(VIDEO_POST, "https://example.com/p", {}) is not None


def test_shell_with_srcless_placeholder_image_is_still_rejected():
    """An <img> with no src is placeholder chrome, not article media — the
    original empty-shell bug must stay fixed."""
    assert _extract(SHELL_WITH_PLACEHOLDER_IMG, "https://example.com/p", {}) is None


def test_has_media_requires_a_src():
    assert _has_media('<div><img src="https://example.com/a.png"></div>') is True
    assert _has_media("<div><img></div>") is False
    assert _has_media("<div><p>text</p></div>") is False
    assert _has_media("") is False
    assert _has_media(None) is False
