"""Unit tests for article extraction (extractor._extract).

Regression: a paywalled / JS-app page whose body is only empty wrapper
elements (e.g. National Review's ``<p id="page">``/``<p id="bc-root">`` React
mount points) must be treated as a *failed* extraction — returning None — not
stored as a blank snapshot that wipes the visible RSS content.
"""
from app.extractor import _extract

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
