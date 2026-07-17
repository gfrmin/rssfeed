import asyncio
import fnmatch
import hashlib
import logging
import time
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from lxml import html as lxml_html
from trafilatura import extract

from app import browser_login
from app.config import (
    BRIGHTDATA_PROXY,
    BRIGHTDATA_UNLOCKER_PROXY,
    FETCH_MIN_INTERVAL_S,
    RENDER_MIN_INTERVAL_S,
)

logger = logging.getLogger(__name__)

# Below this much extracted text we treat the page as "no real content" — for a
# logged-in/known-SPA domain that triggers the browser-render fetch tier.
_RENDER_TEXT_THRESHOLD = 200

# Per-domain rate limiting: serialize + space out fetches to the same site.
_domain_locks: dict[str, asyncio.Lock] = {}
_domain_last: dict[str, float] = {}


def _domain_of(url: str) -> str | None:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.") or None
    except Exception:
        return None


async def _throttle(domain: str | None, min_interval: float) -> None:
    """Ensure at least ``min_interval`` seconds between fetches to ``domain``."""
    if not domain or min_interval <= 0:
        return
    lock = _domain_locks.setdefault(domain, asyncio.Lock())
    async with lock:
        wait = min_interval - (time.monotonic() - _domain_last.get(domain, 0.0))
        if wait > 0:
            logger.debug("Rate-limiting %s: waiting %.1fs", domain, wait)
            await asyncio.sleep(wait)
        _domain_last[domain] = time.monotonic()


def _needs_browser_render(domain: str | None, result: dict[str, Any] | None,
                          cookies: dict[str, str] | None) -> bool:
    """True when the cheap fetch came back empty/short on a domain worth rendering
    in a real browser — i.e. a known SPA paywall or one we hold a session for."""
    text_len = len(result["content_text"]) if result else 0
    if text_len >= _RENDER_TEXT_THRESHOLD:
        return False
    if not browser_login.login_available():
        return False
    return browser_login.has_login_recipe(domain) or bool(cookies)


async def fetch_and_extract(
    url: str,
    extract_rules: dict[str, Any] | None = None,
    proxy_images: bool = True,
    cookies: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Fetch a URL (direct → proxy → Wayback, then a browser-render tier for SPAs)
    and extract article content. Rate-limited per domain to stay polite."""
    rules = extract_rules or {}
    domain = _domain_of(url)

    await _throttle(domain, FETCH_MIN_INTERVAL_S)
    html, source = await _fetch_html(url, cookies=cookies)
    result = _extract(html, url, rules, proxy_images=proxy_images) if html else None
    if result and source:
        result["metadata"]["source"] = source

    # SPA fallback: render in a real browser when httpx only got an empty shell.
    if _needs_browser_render(domain, result, cookies):
        logger.info("Browser-render fallback for %s (httpx text too short)", url)
        await _throttle(domain, RENDER_MIN_INTERVAL_S)
        rendered = await browser_login.render_page_html(url, cookies)
        if rendered:
            r2 = _extract(rendered, url, rules, proxy_images=proxy_images)
            if r2 and len(r2["content_text"]) > len((result or {}).get("content_text", "")):
                r2["metadata"]["source"] = "browser"
                return r2

    return result


_HTTP_KWARGS = dict(
    timeout=30.0,
    follow_redirects=True,
    headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0"
    },
)

# web_unlocker MITMs TLS to inject its own cert, so skip verification when
# routing through it. The static zone is a plain CONNECT tunnel — verify as normal.
_UNLOCKER_KWARGS = {**_HTTP_KWARGS, "verify": False}


async def _get_via(
    client_kwargs: dict, url: str, proxy: str | None, cookies: dict[str, str] | None,
) -> str:
    kwargs = {**client_kwargs}
    if cookies:
        kwargs["cookies"] = cookies
    if proxy:
        kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**kwargs) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text


async def _fetch_html(
    url: str, cookies: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Return (html, source_tier) trying progressively heavier fetch methods."""
    # 1. Direct (free)
    try:
        return await _get_via(_HTTP_KWARGS, url, proxy=None, cookies=cookies), "direct"
    except Exception:
        logger.info("Direct fetch failed for %s, trying static proxy", url)

    # 2. Static datacenter proxy (cheap — bandwidth only, different IP)
    if BRIGHTDATA_PROXY:
        try:
            return await _get_via(_HTTP_KWARGS, url, proxy=BRIGHTDATA_PROXY, cookies=cookies), "static_proxy"
        except Exception:
            logger.info("Static proxy failed for %s, trying web_unlocker", url)

    # 3. Web Unlocker (expensive — per-request anti-bot bypass)
    if BRIGHTDATA_UNLOCKER_PROXY:
        try:
            return await _get_via(
                _UNLOCKER_KWARGS, url, proxy=BRIGHTDATA_UNLOCKER_PROXY, cookies=cookies,
            ), "web_unlocker"
        except Exception:
            logger.info("Web Unlocker failed for %s, trying Wayback Machine", url)

    # 4. Wayback Machine (free last resort — no cookies, site-specific creds irrelevant)
    try:
        wayback_url = f"https://web.archive.org/web/{quote(url, safe='')}"
        return await _get_via(_HTTP_KWARGS, wayback_url, proxy=None, cookies=None), "wayback"
    except Exception:
        logger.warning("All fetch methods failed for %s", url)
        return None, None


async def fetch_proxied_image(url: str) -> tuple[bytes, str] | None:
    """Fetch an image, returning (bytes, content_type) or None.

    Images only try direct + static proxy — never the web_unlocker (per-request
    billing makes it prohibitive for the dozens of images per article).
    """
    async def _get(client_kwargs: dict, proxy: str | None) -> tuple[bytes, str] | None:
        kwargs = {**client_kwargs}
        if proxy:
            kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**kwargs) as client:
            r = await client.get(url)
            r.raise_for_status()
            ct = r.headers.get("content-type", "image/jpeg")
            return r.content, ct

    try:
        return await _get(_HTTP_KWARGS, proxy=None)
    except Exception:
        pass
    if BRIGHTDATA_PROXY:
        try:
            return await _get(_HTTP_KWARGS, proxy=BRIGHTDATA_PROXY)
        except Exception:
            pass
    return None


def _unwrap_elements(tree: lxml_html.HtmlElement, tag_name: str) -> None:
    """Replace elements matching tag_name with their children (unwrap)."""
    for el in list(tree.iter(tag_name)):
        parent = el.getparent()
        if parent is None:
            continue
        idx = list(parent).index(el)
        for i, child in enumerate(list(el)):
            parent.insert(idx + i, child)
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


def _rewrite_image_srcs(tree: lxml_html.HtmlElement, base_url: str) -> None:
    """Rewrite img src attributes to go through the image proxy."""
    for img in tree.xpath("//img[@src]"):
        src = img.get("src", "")
        if not src or src.startswith("data:") or src.startswith("/proxy/image"):
            continue
        absolute = urljoin(base_url, src)
        img.set("src", f"/proxy/image?url={quote(absolute, safe='')}")


def _clean_html(raw_html: str, rules: dict[str, Any]) -> str:
    """Clean HTML using universal rules plus feed-specific extract_rules."""
    tree = lxml_html.fromstring(raw_html)

    # Feed-specific: unwrap tags (e.g. <template> for Vue.js sites)
    for tag_name in rules.get("unwrap_tags", []):
        _unwrap_elements(tree, tag_name)

    # Feed-specific: remove tags by glob pattern (e.g. "widget-*")
    for pattern in rules.get("remove_tags", []):
        _remove_elements(tree, pattern)

    # Feed-specific: remove elements by XPath (e.g. '//img[@class="loadingImg"]')
    for xpath in rules.get("remove_xpath", []):
        for el in tree.xpath(xpath):
            if el.getparent() is not None:
                el.getparent().remove(el)

    # Universal: remove sidebar widgets, aside, nav
    for xpath in [
        '//aside', '//nav',
        '//*[contains(@class, "sidebar-widget")]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " sidebar ")]',
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
    for child in list(el.iterdescendants()):
        if isinstance(child.tag, str) and child.tag in _DROP_TREE_TAGS:
            child.drop_tree()
        elif isinstance(child.tag, str) and child.tag not in _ALLOWED_TAGS:
            child.drop_tag()
    parts = [el.text or '']
    for child in el:
        parts.append(lxml_html.tostring(child, encoding='unicode'))
    return ''.join(parts).strip() or None


# Every tag requires a src, video/audio included: a JS-hydrated player skeleton
# (`<video class="skeleton">`, src set later by script that never ran on a plain
# fetch) is page furniture, not article media. Counting it would let an empty
# shell back through — the exact bug this guard exists to stop. The common
# `<video><source src=…>` form is still caught, by the //source clause.
_MEDIA_XPATH = "//img[@src] | //video[@src] | //audio[@src] | //iframe[@src] | //source[@src]"


def _has_media(html_content: str | None) -> bool:
    """Does the extracted region contain real embedded media?

    Used to tell a text-free *article* (photo essay, comic) apart from a text-free
    *shell* (paywall wrapper divs). Requires a src so an empty <img> placeholder
    in a shell doesn't count.
    """
    if not html_content:
        return False
    try:
        return bool(lxml_html.fromstring(html_content).xpath(_MEDIA_XPATH))
    except Exception:
        return False


def _extract(html: str, url: str, rules: dict[str, Any], proxy_images: bool = True) -> dict[str, Any] | None:
    cleaned = _clean_html(html, rules)
    text = extract(cleaned, url=url, include_comments=False, favor_precision=True, output_format="txt")
    # Precision mode sometimes rejects the whole article body as boilerplate
    # (e.g. en.globes.co.il where only the image caption survives). Retry in
    # recall mode and prefer the longer result.
    if not text or len(text) < 200:
        recall = extract(cleaned, url=url, include_comments=False, favor_precision=False, output_format="txt")
        if recall and len(recall) > len(text or ""):
            text = recall

    content_xpath = rules.get("content_xpath")
    html_content = (
        _extract_by_xpath(cleaned, content_xpath) if content_xpath else None
    ) or _extract_html_readability(cleaned)

    # Sanity checks: readability HTML vs trafilatura text
    if text and html_content:
        readability_text = lxml_html.fromstring(html_content).text_content()
        readability_text_len = len(readability_text)

        # Check 1: readability output is way too short
        too_short = readability_text_len < len(text) * 0.4

        # Check 2: readability missed the beginning of the article
        first_chunk = text[:100].strip()
        missed_start = bool(first_chunk) and " ".join(first_chunk.split()) not in " ".join(readability_text.split())

        if too_short or missed_start:
            logger.info(
                "Readability output %s (%d vs %d chars), falling back to trafilatura HTML",
                "too short" if too_short else "missed article start",
                readability_text_len, len(text),
            )
            traf_html = extract(
                cleaned, url=url, include_comments=False,
                favor_precision=False, output_format="html",
                include_images=True,
            )
            if traf_html:
                tree = lxml_html.fromstring(traf_html)
                # Convert trafilatura's <graphic> to <img>
                for g in tree.xpath("//graphic"):
                    img = lxml_html.Element("img")
                    for attr in ("src", "alt", "title"):
                        if g.get(attr):
                            img.set(attr, g.get(attr))
                    g.getparent().replace(g, img)
                body = tree.xpath("//body")
                target = body[0] if body else tree
                parts = [target.text or ""]
                for child in target:
                    parts.append(lxml_html.tostring(child, encoding="unicode"))
                fallback = "".join(parts).strip()
                if fallback:
                    html_content = fallback

    # A fetch can return page scaffolding with no real article text — e.g. a
    # paywall / JS-app shell whose body is only empty React mount points. Treat
    # that as a *failed* extraction rather than storing a blank snapshot that
    # would replace the visible RSS content.
    visible_text = (text or "").strip()
    if not visible_text and html_content:
        try:
            visible_text = lxml_html.fromstring(html_content).text_content().strip()
        except Exception:
            visible_text = ""
    # ...but "no text" isn't the same as "no article". A photo essay, comic, or
    # video post is legitimately text-free, and rejecting it sends a perfectly
    # good fetch down the expensive tiers (proxy → unlocker → Wayback) only to
    # end up showing the RSS body. Media in the extracted region is evidence we
    # really did find the article; an empty shell has neither text nor media.
    if not visible_text and not _has_media(html_content):
        return None

    # Proxy images through our endpoint
    if proxy_images and html_content:
        try:
            tree = lxml_html.fromstring(f"<div>{html_content}</div>")
            _rewrite_image_srcs(tree, url)
            html_content = lxml_html.tostring(tree, encoding="unicode")
            # Strip the wrapper div
            html_content = html_content.removeprefix("<div>").removesuffix("</div>")
        except Exception:
            pass

    content_text = text or ""
    return {
        "content_text": content_text,
        "content_html": html_content or content_text,
        "content_hash": hashlib.sha256(f"{content_text}\n{html_content}".encode()).hexdigest(),
        "metadata": {},
    }


_ALLOWED_TAGS = frozenset({
    'html', 'body', 'div', 'p', 'a', 'em', 'i', 'b', 'strong',
    'span', 'br', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'blockquote', 'figure', 'figcaption', 'img', 'iframe', 'video',
    'table', 'thead', 'tbody', 'tr', 'td', 'th', 'pre', 'code', 'sup', 'sub',
    'audio', 'source',
})


_DROP_TREE_TAGS = frozenset({'style', 'script', 'noscript'})


def _extract_html_readability(html: str) -> str | None:
    """Use readability-lxml for HTML — avoids trafilatura's HTML serialization bugs."""
    from readability import Document

    article_html = Document(html).summary()
    if not article_html:
        return None
    tree = lxml_html.fromstring(article_html)
    for el in list(tree.iter()):
        if isinstance(el.tag, str) and el.tag in _DROP_TREE_TAGS:
            el.drop_tree()
        elif isinstance(el.tag, str) and el.tag not in _ALLOWED_TAGS:
            el.drop_tag()
    body = tree.xpath('//body')
    target = body[0] if body else tree
    parts = [target.text or '']
    for child in target:
        parts.append(lxml_html.tostring(child, encoding='unicode'))
    return ''.join(parts).strip()
