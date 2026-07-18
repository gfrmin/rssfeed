"""HTML-sanitisation filters — the trust boundary for feed/web-supplied HTML.

`inline_html` (titles) and `clean_body` (article bodies) must preserve benign
formatting while stripping every XSS vector. These tests lock in that contract.
"""
from app import config
from app.templating import _clean_body, _inline_html, _youtube_id

# --- inline_html (titles) ---------------------------------------------------

def test_inline_html_keeps_italics():
    out = str(_inline_html("Hypocrisy in <i>Mullin v. Al Otro Lado</i>"))
    assert out == "Hypocrisy in <i>Mullin v. Al Otro Lado</i>"


def test_inline_html_strips_script_and_handlers():
    assert "script" not in str(_inline_html("A<script>alert(1)</script>B")).lower()
    assert "onerror" not in str(_inline_html("hi <img src=x onerror=alert(1)>")).lower()


def test_inline_html_drops_links_to_text():
    out = str(_inline_html('a <a href="javascript:alert(1)">b</a>'))
    assert "javascript" not in out.lower()
    assert "<a" not in out  # links aren't allowed in titles at all


def test_inline_html_empty():
    assert str(_inline_html("")) == ""
    assert str(_inline_html(None)) == ""


# --- clean_body (article bodies) --------------------------------------------

def test_clean_body_preserves_proxied_relative_image():
    src = "/proxy/image?url=https%3A%2F%2Fa.com%2Fb.jpg"
    out = str(_clean_body(f'<p>x</p><img src="{src}" alt="c">'))
    assert src in out  # the image proxy rewrite must survive sanitisation
    assert "<p>x</p>" in out


def test_clean_body_strips_event_handlers():
    out = str(_clean_body('<img src="/proxy/image?url=z" onerror="alert(1)">'))
    assert "onerror" not in out.lower()
    assert "/proxy/image?url=z" in out  # but keeps the (safe) image


def test_clean_body_drops_iframe_and_script():
    assert "iframe" not in str(_clean_body('a<iframe src="https://evil"></iframe>b')).lower()
    assert "script" not in str(_clean_body('<p>a</p><script>alert(1)</script>')).lower()


def test_clean_body_drops_javascript_urls():
    assert "javascript" not in str(_clean_body('<a href="javascript:alert(1)">x</a>')).lower()
    assert "javascript" not in str(_clean_body('<video src="javascript:alert(1)" controls></video>')).lower()


def test_clean_body_keeps_good_links_and_media():
    assert 'href="https://ok.com"' in str(_clean_body('<a href="https://ok.com">ok</a>'))
    out = str(_clean_body('<audio controls><source src="https://x/a.mp3" type="audio/mp3"></audio>'))
    assert "https://x/a.mp3" in out


# --- embeds: <iframe> becomes a click-through link (never an inline frame) ---

def test_youtube_id_forms():
    assert _youtube_id("https://www.youtube.com/embed/ABC123?rel=0") == "ABC123"
    assert _youtube_id("https://youtu.be/ABC123") == "ABC123"
    assert _youtube_id("https://www.youtube.com/watch?v=ABC123&t=5") == "ABC123"
    assert _youtube_id("https://www.youtube-nocookie.com/embed/ABC123") == "ABC123"
    assert _youtube_id("https://www.scribd.com/embeds/1") is None
    assert _youtube_id("https://example.com/x") is None


def test_clean_body_youtube_embed_defaults_to_youtube_link():
    out = str(_clean_body('<p><iframe src="https://www.youtube.com/embed/ABC123?rel=0"></iframe></p>'))
    assert "iframe" not in out.lower()                       # no inline frame
    assert 'href="https://www.youtube.com/watch?v=ABC123"' in out
    assert "/embed/" not in out                               # frame src not leaked


def test_clean_body_youtube_embed_routes_to_invidious(monkeypatch):
    monkeypatch.setattr(config, "INVIDIOUS_URL", "http://invidious.example")
    out = str(_clean_body('<iframe src="https://youtu.be/ABC123"></iframe>'))
    assert 'href="http://invidious.example/watch?v=ABC123"' in out
    assert "Invidious" in out


def test_clean_body_non_youtube_iframe_becomes_open_link():
    out = str(_clean_body('<iframe src="https://www.scribd.com/embeds/42/content"></iframe>'))
    assert "iframe" not in out.lower()
    assert 'href="https://www.scribd.com/embeds/42/content"' in out


def test_clean_body_embed_link_opens_new_tab_safely():
    out = str(_clean_body('<iframe src="https://www.youtube.com/embed/ABC123"></iframe>'))
    assert 'target="_blank"' in out
    assert "noopener" in out                                 # link_rel forces rel=noopener


def test_clean_body_srcless_iframe_is_dropped():
    out = str(_clean_body("<p>text</p><iframe></iframe>"))
    assert "iframe" not in out.lower()
    assert "<p>text</p>" in out
