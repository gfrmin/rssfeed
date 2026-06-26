"""HTML-sanitisation filters — the trust boundary for feed/web-supplied HTML.

`inline_html` (titles) and `clean_body` (article bodies) must preserve benign
formatting while stripping every XSS vector. These tests lock in that contract.
"""
from app.templating import _clean_body, _inline_html


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
