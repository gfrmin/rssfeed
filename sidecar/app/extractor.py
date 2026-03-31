import hashlib
import logging
from typing import Any

import httpx
from lxml import html as lxml_html
from trafilatura import extract

from app.config import BRIGHTDATA_PROXY

logger = logging.getLogger(__name__)


async def fetch_and_extract(url: str) -> dict[str, Any] | None:
    """Fetch a URL (direct, then proxy fallback) and extract article content."""
    html = await _fetch_html(url)
    if not html:
        return None
    return _extract(html, url)


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


def _clean_html(raw_html: str) -> str:
    """Strip sidebar widgets and non-content elements before extraction."""
    tree = lxml_html.fromstring(raw_html)
    # Remove custom widget elements (order-order.com pattern)
    for tag in list(tree.iter()):
        if isinstance(tag.tag, str) and tag.tag.startswith("widget-") and tag.getparent() is not None:
            tag.getparent().remove(tag)
    # Remove sidebar widgets, aside, nav
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


def _extract(html: str, url: str) -> dict[str, Any] | None:
    cleaned = _clean_html(html)
    text = extract(cleaned, url=url, include_comments=False, favor_precision=True, output_format="txt")
    if not text:
        return None
    html_content = extract(
        cleaned, url=url, include_comments=False, favor_precision=True,
        output_format="html", include_links=True, include_formatting=True, include_images=True,
    )
    return {
        "content_text": text,
        "content_html": html_content or text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "metadata": {},
    }
