import fnmatch
import hashlib
import logging
from typing import Any

import httpx
from lxml import html as lxml_html
from trafilatura import extract

from app.config import BRIGHTDATA_PROXY

logger = logging.getLogger(__name__)


async def fetch_and_extract(
    url: str, extract_rules: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Fetch a URL (direct, then proxy fallback) and extract article content."""
    html = await _fetch_html(url)
    if not html:
        return None
    return _extract(html, url, extract_rules or {})


_HTTP_KWARGS = dict(
    timeout=30.0,
    follow_redirects=True,
    headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0"
    },
)


async def _fetch_html(url: str) -> str | None:
    # Try direct first
    try:
        async with httpx.AsyncClient(**_HTTP_KWARGS) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.text
    except Exception:
        logger.info("Direct fetch failed for %s, trying proxy", url)

    # Fall back to Brightdata proxy
    if not BRIGHTDATA_PROXY:
        logger.warning("No proxy configured, cannot retry %s", url)
        return None
    try:
        async with httpx.AsyncClient(proxy=BRIGHTDATA_PROXY, **_HTTP_KWARGS) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.text
    except Exception:
        logger.exception("Proxy fetch also failed for %s", url)
        return None


def _unwrap_elements(tree: lxml_html.HtmlElement, tag_name: str) -> None:
    """Replace elements matching tag_name with their children (unwrap)."""
    for el in list(tree.iter(tag_name)):
        parent = el.getparent()
        if parent is None:
            continue
        idx = list(parent).index(el)
        # Promote children into the parent
        for i, child in enumerate(list(el)):
            parent.insert(idx + i, child)
        # Preserve text content
        if el.text:
            prev = parent[idx - 1] if idx > 0 else None
            if prev is not None:
                prev.tail = (prev.tail or "") + el.text
            else:
                parent.text = (parent.text or "") + el.text
        parent.remove(el)


def _remove_elements(tree: lxml_html.HtmlElement, pattern: str) -> None:
    """Remove elements whose tag name matches a glob pattern (e.g. 'widget-*')."""
    for tag in list(tree.iter()):
        if isinstance(tag.tag, str) and fnmatch.fnmatch(tag.tag, pattern) and tag.getparent() is not None:
            tag.getparent().remove(tag)


def _clean_html(raw_html: str, rules: dict[str, Any]) -> str:
    """Clean HTML using universal rules plus feed-specific extract_rules."""
    tree = lxml_html.fromstring(raw_html)

    # Feed-specific: unwrap tags (e.g. <template> for Vue.js sites)
    for tag_name in rules.get("unwrap_tags", []):
        _unwrap_elements(tree, tag_name)

    # Feed-specific: remove tags by glob pattern (e.g. "widget-*")
    for pattern in rules.get("remove_tags", []):
        _remove_elements(tree, pattern)

    # Universal: remove sidebar widgets, aside, nav
    for xpath in [
        '//aside', '//nav',
        '//*[contains(@class, "sidebar-widget")]',
        '//*[contains(@class, "sidebar")]',
        '//*[@role="complementary"]',
    ]:
        for el in tree.xpath(xpath):
            if el.getparent() is not None:
                el.getparent().remove(el)

    return lxml_html.tostring(tree, encoding="unicode")


def _extract_by_xpath(html: str, xpath: str) -> str | None:
    """Extract inner HTML from the first element matching an XPath selector."""
    tree = lxml_html.fromstring(html)
    matches = tree.xpath(xpath)
    if not matches:
        return None
    el = matches[0]
    # Sanitize descendant tags but skip the matched root element itself
    for child in list(el.iterdescendants()):
        if isinstance(child.tag, str) and child.tag not in _ALLOWED_TAGS:
            child.drop_tag()
    parts = [el.text or '']
    for child in el:
        parts.append(lxml_html.tostring(child, encoding='unicode'))
    return ''.join(parts).strip() or None


def _extract(html: str, url: str, rules: dict[str, Any]) -> dict[str, Any] | None:
    cleaned = _clean_html(html, rules)
    text = extract(cleaned, url=url, include_comments=False, favor_precision=True, output_format="txt")

    # Try feed-specific content_xpath first, then readability
    content_xpath = rules.get("content_xpath")
    html_content = (
        _extract_by_xpath(cleaned, content_xpath) if content_xpath else None
    ) or _extract_html_readability(cleaned)

    if not text and not html_content:
        return None
    content_text = text or ""
    return {
        "content_text": content_text,
        "content_html": html_content or content_text,
        "content_hash": hashlib.sha256((content_text or html_content).encode()).hexdigest(),
        "metadata": {},
    }


_ALLOWED_TAGS = frozenset({
    'html', 'body', 'div', 'p', 'a', 'em', 'i', 'b', 'strong',
    'span', 'br', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'blockquote', 'figure', 'figcaption', 'img', 'iframe', 'video',
    'table', 'thead', 'tbody', 'tr', 'td', 'th', 'pre', 'code', 'sup', 'sub',
})


def _extract_html_readability(html: str) -> str | None:
    """Use readability-lxml for HTML — avoids trafilatura's HTML serialization bugs."""
    from readability import Document

    article_html = Document(html).summary()
    if not article_html:
        return None
    tree = lxml_html.fromstring(article_html)
    for el in list(tree.iter()):
        if isinstance(el.tag, str) and el.tag not in _ALLOWED_TAGS:
            el.drop_tag()
    body = tree.xpath('//body')
    target = body[0] if body else tree
    # Return inner HTML (children only) to avoid stray <body> wrapper
    parts = [target.text or '']
    for child in target:
        parts.append(lxml_html.tostring(child, encoding='unicode'))
    return ''.join(parts).strip()
