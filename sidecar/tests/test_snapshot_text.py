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
