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
  <div id="app"><p id="page"><img></p><p id="app-root"></p></div>
</body></html>"""

# Article-shaped (so it survives readability and _has_media is genuinely asked
# about a non-empty body), but every player is a JS-hydrated skeleton whose src
# arrives from script that never ran on a plain fetch.
PLAYER_SKELETON = """<html><body><article>
  <figure class="video"><video class="player-skeleton" data-src="later.mp4"></video></figure>
  <figure class="video"><video class="player-skeleton"></video></figure>
</article></body></html>"""


def test_image_only_article_is_kept():
    assert _extract(PHOTO_ESSAY, "https://example.com/p", {}) is not None


def test_video_only_article_is_kept():
    assert _extract(VIDEO_POST, "https://example.com/p", {}) is not None


def test_shell_with_srcless_placeholder_image_is_still_rejected():
    """The original empty-shell bug must stay fixed.

    Coarse end-to-end check only: readability collapses a shell this small to an
    empty body, so _has_media is consulted with "" and the src filter itself never
    runs. PLAYER_SKELETON is the fixture that actually exercises it.
    """
    assert _extract(SHELL_WITH_PLACEHOLDER_IMG, "https://example.com/p", {}) is None


def test_srcless_player_skeleton_is_rejected():
    """The case that makes the src requirement earn its keep: a media-only page
    that survives readability, but whose <video> has no src yet. Page furniture,
    not an article — counting it would store a blank snapshot again."""
    assert _extract(PLAYER_SKELETON, "https://example.com/p", {}, proxy_images=False) is None


def test_has_media_requires_a_src():
    # img
    assert _has_media('<div><img src="https://example.com/a.png"></div>') is True
    assert _has_media("<div><img></div>") is False
    # video / audio — a src-less player skeleton must not count as media
    assert _has_media('<div><video src="https://example.com/v.mp4"></video></div>') is True
    assert _has_media('<div><video class="skeleton"></video></div>') is False
    assert _has_media('<div><audio src="https://example.com/a.mp3"></audio></div>') is True
    assert _has_media("<div><audio></audio></div>") is False
    # the common <video><source src> form still counts, via the //source clause
    assert _has_media('<div><video><source src="https://example.com/v.mp4"></video></div>') is True
    assert _has_media("<div><video><source></video></div>") is False
    # embeds
    assert _has_media('<div><iframe src="https://example.com/e"></iframe></div>') is True
    assert _has_media("<div><iframe></iframe></div>") is False
    # nothing at all
    assert _has_media("<div><p>text</p></div>") is False
    assert _has_media("") is False
    assert _has_media(None) is False
