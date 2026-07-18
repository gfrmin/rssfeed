"""Unit tests for article extraction (extractor._extract).

Regression: a paywalled / JS-app page whose body is only empty React mount points
must be treated as a *failed* extraction — returning None — not stored as a blank
snapshot that wipes the visible RSS content.
"""
import lxml.html as L

from app.extractor import (
    NormalizedCandidate,
    _clean_html,
    _extract,
    _has_media,
    _normalize,
    _score,
    _strip_boilerplate,
)

# The shape a paywalled SPA serves before its JS runs: page scaffolding present,
# carrying zero article text.
EMPTY_SHELL = """<!DOCTYPE html><html><head><title>An Article Title</title></head>
<body>
  <p id="page" class="site" data-headless></p>
  <p id="app-root"></p>
</body></html>"""

REAL_ARTICLE = """<!DOCTYPE html><html><head><title>A Real Article</title></head>
<body><article><h1>A Real Article</h1>
<p>This paragraph is ordinary prose standing in for a genuine article body, long
enough that the extractor treats it as real content rather than boilerplate or a
stray navigation fragment picked up from the page chrome.</p>
<p>This second paragraph exists so the body comfortably clears the extractor's
minimum-length thresholds and yields genuine prose for the reader pane.</p>
</article></body></html>"""


def test_empty_shell_returns_none():
    assert _extract(EMPTY_SHELL, "https://example.com/x", {}, proxy_images=False) is None


def test_real_article_extracts_text():
    result = _extract(REAL_ARTICLE, "https://example.com/x", {}, proxy_images=False)
    assert result is not None
    assert "ordinary prose" in result["content_text"]
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


# --- pipeline internals: the pieces the scored-candidate design is built from ---
# These are the fast, deterministic core (synthetic HTML, no network, no corpus
# blobs). The real-world corpus in test_extraction_corpus.py is the backstop.

# _normalize -----------------------------------------------------------------

def test_normalize_resolves_relative_anchor():
    base = "https://example.com/section/index.html"
    nc = _normalize('<p>see <a href="/story/x">this</a></p>', base, "t", 1, proxy_images=False)  # PII-OK: synthetic
    assert nc.anchors == 1
    assert 'href="https://example.com/story/x"' in nc.html


def test_normalize_drops_bad_scheme_href_but_keeps_text():
    nc = _normalize('<p><a href="javascript:evil()">click</a></p>', "https://example.com/", "t", 1, proxy_images=False)
    assert nc.anchors == 0
    assert "click" in nc.text
    assert "javascript:" not in nc.html


def test_normalize_unwraps_disallowed_tag_keeping_text():
    nc = _normalize("<p><marquee>hello</marquee> world</p>", "https://example.com/", "t", 1, proxy_images=False)
    assert "hello world" in nc.text
    assert "<marquee" not in nc.html


# _strip_boilerplate ---------------------------------------------------------

def test_strip_boilerplate_removes_ad_div():
    tree = L.fromstring(
        '<div><p id="div-gpt-ad-1">advert</p>'
        "<p>The genuine article body text that is the real content here.</p></div>"
    )
    hits = _strip_boilerplate(tree)
    assert hits == 1
    assert not tree.xpath('//*[@id="div-gpt-ad-1"]')
    assert "genuine article body" in tree.text_content()


def test_strip_boilerplate_guard_never_empties_body():
    # The whole body wears a widget-ish class — the guard must NOT nuke it.
    tree = L.fromstring('<body><div class="related">'
                        "<p>the entire article body lives here and only here</p></div></body>")
    _strip_boilerplate(tree)
    assert "entire article body" in tree.text_content()


def test_clean_html_strips_custom_widget_and_tracking_noscript():
    raw = (
        "<html><body><article><p>Body text of the actual story goes here.</p>"
        "<widget-qotd><p>Quote of the day filler that is not article content</p></widget-qotd>"
        '<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X"></iframe></noscript>'
        "</article></body></html>"
    )
    cleaned = _clean_html(raw, {})
    assert "Quote of the day" not in cleaned
    assert "googletagmanager" not in cleaned
    assert "Body text of the actual story" in cleaned


# _score ---------------------------------------------------------------------

def test_score_prefers_link_retaining_candidate():
    ref = "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu. " * 3
    linky = NormalizedCandidate("trafilatura_html", 2, "<p>x</p>", anchors=3, media=0, text=ref)
    linkless = NormalizedCandidate("readability", 1, "<p>x</p>", anchors=0, media=0, text=ref)
    kw = dict(reference_text=ref, dom_anchors=3, dom_media=0, max_anchors=3, max_media=0)
    assert _score(linky, **kw) > _score(linkless, **kw)


# end-to-end pipeline behaviours ---------------------------------------------

INLINE_LINK_ARTICLE = (
    "<html><body><article><h1>T</h1>"
    '<p>The full report is available <a href="https://example.org/report">here</a>, and a good '
    "deal more context follows in this deliberately long paragraph so the extractor treats it as a "
    "genuine article body rather than a stray navigation fragment from the page chrome.</p>"
    "</article></body></html>"
)


def test_inline_link_survives_extraction():
    """The headline fix: inline anchors must reach content_html (the old trafilatura
    fallback stripped them by omitting include_links)."""
    r = _extract(INLINE_LINK_ARTICLE, "https://example.org/a", {}, proxy_images=False)
    assert r is not None
    assert 'href="https://example.org/report"' in r["content_html"]


VIDEO_ONLY_POST = (
    "<html><body><article><p>"
    '<iframe src="https://www.youtube.com/embed/ABC123"></iframe></p></article></body></html>'
)


def test_video_embed_is_reinjected():
    """A text-free video post: the libraries drop the iframe, so re-injection must
    carry it through — otherwise the article renders empty."""
    r = _extract(VIDEO_ONLY_POST, "https://example.com/v", {}, proxy_images=False)
    assert r is not None
    assert "youtube.com/embed/ABC123" in r["content_html"]


def test_tracking_iframe_is_not_kept_as_media():
    html = (
        "<html><body><article><p>A short but genuine article body sentence for length here.</p>"
        '<iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X"></iframe>'
        "</article></body></html>"
    )
    r = _extract(html, "https://example.com/a", {}, proxy_images=False)
    assert r is not None
    assert "googletagmanager" not in r["content_html"]
