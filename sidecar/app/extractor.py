import asyncio
import fnmatch
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from lxml import html as lxml_html
from trafilatura import extract

from app import browser_login, egress
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
        # DNS + connect happen at the remote proxy, so IP-range checks here
        # would be meaningless — scheme/host sanity is the applicable policy.
        egress.check_scheme_host(url)
        kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**kwargs) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.text
    r = await egress.guarded_get(kwargs, url)
    return r.text


async def _fetch_html(
    url: str, cookies: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Return (html, source_tier) trying progressively heavier fetch methods."""
    try:
        egress.check_scheme_host(url)
    except egress.EgressBlockedError as exc:
        logger.warning("egress blocked %s: %s", url, exc)
        return None, None

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

    Raises EgressBlockedError when the URL fails the egress policy — the
    /proxy/image route turns that into a 403 rather than a soft 404.

    Images only try direct + static proxy — never the web_unlocker (per-request
    billing makes it prohibitive for the dozens of images per article).
    """
    egress.check_scheme_host(url)

    async def _get(client_kwargs: dict, proxy: str | None) -> tuple[bytes, str] | None:
        if proxy:
            kwargs = {**client_kwargs, "proxy": proxy}
            async with httpx.AsyncClient(**kwargs) as client:
                r = await client.get(url)
                r.raise_for_status()
        else:
            r = await egress.guarded_get(client_kwargs, url)
        ct = r.headers.get("content-type", "image/jpeg")
        return r.content, ct

    try:
        return await _get(_HTTP_KWARGS, proxy=None)
    except egress.EgressBlockedError:
        raise  # blocked is blocked — don't hand the URL to the proxy either
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

    # Universal: drop inert/opaque subtrees. noscript in particular hides tracking
    # <iframe>s (GTM) that would otherwise be collected as article media.
    for el in tree.xpath('//script | //style | //noscript'):
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

    # Universal: ad/share/related/newsletter/widget furniture (guarded).
    _strip_boilerplate(tree)

    return lxml_html.tostring(tree, encoding="unicode")


_URL_SCHEMES = frozenset({"http", "https", "mailto"})


def _inner_html(el: lxml_html.HtmlElement) -> str:
    """Serialize an element's leading text + children as an HTML fragment."""
    parts = [el.text or ""]
    for child in el:
        parts.append(lxml_html.tostring(child, encoding="unicode"))
    return "".join(parts).strip()


# Boilerplate containers that survive the sidebar/nav sweep and then get mistaken
# for the article body: ad slots, share bars, related-story recirculation,
# newsletter forms, and recurring editorial widgets (e.g. a "quote of the day"
# block that trafilatura, being text-dense, scores over a video-only post). Run in
# _clean_html so no extractor tier — nor the reference-text yardstick — ever sees
# them; the guard in _strip_boilerplate keeps them from eating a real body.
#
# Each entry is (xpath, protect_media). "Hard" furniture (protect_media=False —
# ads, related/recirc, widgets) is never article content and is removed even when
# it carries media (a related-video block is not the article). "Soft" wrappers
# (protect_media=True — share/social) may wrap the article's *own* embed, so a node
# holding all the page's media is left alone rather than taking the player with it.
_BOILERPLATE_XPATHS = (
    ('//*[starts-with(@id, "div-gpt-ad")]', False),
    ('//*[@id="fb-pxl-ajax-code"]', False),
    ('//*[starts-with(@id, "google_ads_")]', False),
    ('//ins[contains(concat(" ", normalize-space(@class), " "), " adsbygoogle ")]', False),
    ('//*[contains(concat(" ", normalize-space(@class), " "), " ad ")]', False),
    ('//*[contains(@class, "ad-container")]', False),
    ('//*[contains(@class, "advert")]', False),
    ('//*[starts-with(local-name(), "widget-")]', False),   # custom-element widgets, e.g. <widget-qotd>
    ('//*[contains(@class, "widget-")]', False),
    ('//*[contains(@class, "widget_")]', False),
    ('//*[contains(@class, "share")]', True),
    ('//*[contains(@class, "social")]', True),
    ('//*[contains(@class, "addtoany")]', False),
    ('//*[contains(@class, "sharedaddy")]', False),
    ('//*[contains(@class, "related")]', False),
    ('//*[contains(@class, "recirc")]', False),
    ('//*[contains(@class, "read-more")]', False),
    ('//*[contains(@class, "more-stories")]', False),
    ('//*[contains(@class, "outbrain")]', False),
    ('//*[contains(@class, "taboola")]', False),
    ('//*[contains(@class, "newsletter")]', False),
    ('//*[contains(@class, "subscribe")]', False),
    ('//form', False),
)


def _strip_boilerplate(tree: lxml_html.HtmlElement) -> int:
    """Remove ad/share/related/newsletter/widget furniture in place.

    Guards (see _BOILERPLATE_XPATHS): never strip the node holding (almost) all the
    tree's text — that's the article body wearing a furniture-ish class; and, for
    "soft" wrappers only, never strip the node holding all the tree's media — it may
    be the article's own embed inside a share-classed wrapper. Returns nodes removed.
    """
    total_text = len(tree.text_content())
    total_media = len(tree.xpath(_MEDIA_XPATH))
    hits = 0
    for xp, protect_media in _BOILERPLATE_XPATHS:
        for el in list(tree.xpath(xp)):
            parent = el.getparent()
            if parent is None:
                continue
            if total_text and len(el.text_content()) >= 0.9 * total_text:
                continue
            if protect_media and total_media and len(el.xpath(_MEDIA_XPATH)) >= total_media:
                continue
            parent.remove(el)
            hits += 1
    return hits


@dataclass(frozen=True)
class NormalizedCandidate:
    tier: str
    trust: int          # 0 highest .. 2 lowest — a selection tiebreaker only
    html: str
    anchors: int
    media: int
    text: str


def _normalize(raw_html: str, base_url: str, tier: str, trust: int,
               proxy_images: bool = True) -> NormalizedCandidate | None:
    """The one cleaner every candidate passes through: allowlist tags,
    resolve/scheme-restrict URLs, proxy images, then measure. (Boilerplate is
    already gone — _clean_html stripped it before any candidate was generated.)"""
    if not raw_html:
        return None
    try:
        tree = lxml_html.fromstring(f"<div>{raw_html}</div>")
    except Exception:
        return None
    for el in list(tree.iter()):
        if el is tree or not isinstance(el.tag, str):
            continue
        if el.tag in _DROP_TREE_TAGS:
            el.drop_tree()
        elif el.tag not in _ALLOWED_TAGS:
            el.drop_tag()
    # Anchors: resolve relative → absolute (the reader is served from our own
    # origin, so a relative href would otherwise point back at us); drop the href
    # on a non-web scheme, keeping the visible text.
    for a in tree.xpath("//a[@href]"):
        absolute = urljoin(base_url, (a.get("href") or "").strip())
        if urlparse(absolute).scheme in _URL_SCHEMES:
            a.set("href", absolute)
        else:
            del a.attrib["href"]
    # Non-image media (iframe/video/audio/source): resolve + scheme-restrict; drop
    # the element on a bad scheme. Images are left for the proxy rewrite below.
    for el in tree.xpath("//*[@src][not(self::img)]"):
        absolute = urljoin(base_url, (el.get("src") or "").strip())
        if urlparse(absolute).scheme in ("http", "https"):
            el.set("src", absolute)
        elif el.getparent() is not None:
            el.getparent().remove(el)
    if proxy_images:
        _rewrite_image_srcs(tree, base_url)
    return NormalizedCandidate(
        tier=tier, trust=trust, html=_inner_html(tree),
        anchors=len(tree.xpath("//a[@href]")),
        media=len(tree.xpath(_MEDIA_XPATH)),
        text=" ".join(tree.text_content().split()),
    )


def _gen_xpath(cleaned: str, xpath: str) -> str | None:
    """Candidate tier 0: inner HTML of the operator-configured content container."""
    try:
        matches = lxml_html.fromstring(cleaned).xpath(xpath)
    except Exception:
        return None
    return (_inner_html(matches[0]) or None) if matches else None


def _gen_readability(cleaned: str) -> str | None:
    """Candidate tier 1: readability-lxml's main-article summary."""
    from readability import Document
    try:
        article_html = Document(cleaned).summary()
    except Exception:
        return None
    if not article_html:
        return None
    try:
        tree = lxml_html.fromstring(article_html)
    except Exception:
        return None
    body = tree.xpath("//body")
    return _inner_html(body[0] if body else tree) or None


def _gen_trafilatura_html(cleaned: str, url: str) -> str | None:
    """Candidate tier 2: trafilatura HTML — the only tier that keeps prose *and*
    links (`include_links=True`, the fix for the old fallback's wholesale anchor loss)."""
    traf_html = extract(
        cleaned, url=url, include_comments=False, favor_precision=False,
        output_format="html", include_links=True, include_images=True,
        include_formatting=True,
    )
    if not traf_html:
        return None
    try:
        tree = lxml_html.fromstring(traf_html)
    except Exception:
        return None
    for g in tree.xpath("//graphic"):  # trafilatura emits <graphic>, not <img>
        img = lxml_html.Element("img")
        for attr in ("src", "alt", "title"):
            if g.get(attr):
                img.set(attr, g.get(attr))
        if g.getparent() is not None:
            g.getparent().replace(g, img)
    body = tree.xpath("//body")
    return _inner_html(body[0] if body else tree) or None


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


def _reference_text(cleaned: str, url: str) -> str:
    """Trafilatura plain text — the runtime coverage yardstick (we have no golden).

    Precision first; fall back to recall when precision rejects the whole body as
    boilerplate (e.g. en.globes.co.il, where only the image caption survives)."""
    text = extract(cleaned, url=url, include_comments=False, favor_precision=True, output_format="txt")
    if not text or len(text) < 200:
        recall = extract(cleaned, url=url, include_comments=False, favor_precision=False, output_format="txt")
        if recall and len(recall) > len(text or ""):
            text = recall
    return text or ""


def _retention(count: int, dom_total: int, cand_max: int) -> float:
    """Share of the page's links/media a candidate kept, in [0,1].

    No per-article golden exists at runtime, so retention is measured two ways and
    the kinder taken: against the boilerplate-stripped whole page (`dom_total`) and
    relative to the best candidate (`cand_max`). Nothing to retain → 1.0."""
    if dom_total <= 0 and cand_max <= 0:
        return 1.0
    abs_ret = min(count, dom_total) / dom_total if dom_total > 0 else 0.0
    rel_ret = count / cand_max if cand_max > 0 else 0.0
    return max(abs_ret, rel_ret)


# Scoring weights — tuned against the extraction corpus. A module constant so the
# corpus/unit tests can assert on individual signals independently of the blend.
_SCORE_WEIGHTS = {"coverage": 0.45, "start": 0.15, "link": 0.25, "media": 0.15}


def _score(cand: NormalizedCandidate, *, reference_text: str,
           dom_anchors: int, dom_media: int, max_anchors: int, max_media: int) -> float:
    """Rank a normalized candidate: prose coverage + kept lede + link/media
    retention. Pure — unit-tested off synthetic HTML. (No boilerplate term: it's
    stripped in _clean_html, so every candidate is already furniture-free.)"""
    ref_len = len(reference_text)
    coverage = min(len(cand.text) / ref_len, 1.0) if ref_len else (1.0 if cand.text else 0.0)
    if reference_text:
        prefix = " ".join(reference_text[:100].split())
        start = 1.0 if prefix and prefix in cand.text else 0.0
    else:
        start = 1.0
    w = _SCORE_WEIGHTS
    return (
        w["coverage"] * coverage
        + w["start"] * start
        + w["link"] * _retention(cand.anchors, dom_anchors, max_anchors)
        + w["media"] * _retention(cand.media, dom_media, max_media)
    )


def _pack(content_text: str, content_html: str) -> dict[str, Any]:
    return {
        "content_text": content_text,
        "content_html": content_html or content_text,
        "content_hash": hashlib.sha256(f"{content_text}\n{content_html}".encode()).hexdigest(),
        "metadata": {},
    }


# Embeds carry no text, so readability/trafilatura discard them — yet a YouTube or
# livestream frame is the whole point of a "LIVE:/WATCH:" post. Collect them from
# the cleaned article region so the winner can carry them regardless of tier.
_EMBED_XPATH = "//iframe[@src] | //video[@src] | //audio[@src]"
# Analytics/tag frames masquerade as embeds — never article content.
_TRACKING_HOSTS = (
    "googletagmanager.com", "google-analytics.com", "doubleclick.net",
    "facebook.com/tr", "scorecardresearch.com", "quantserve.com",
)


def _collect_embeds(tree: lxml_html.HtmlElement, base_url: str) -> list[tuple[str, str]]:
    """(absolute-src, outer-HTML) for each http(s) content embed in the cleaned region."""
    out = []
    for el in tree.xpath(_EMBED_XPATH):
        absolute = urljoin(base_url, (el.get("src") or "").strip())
        if urlparse(absolute).scheme not in ("http", "https"):
            continue
        if any(host in absolute for host in _TRACKING_HOSTS):
            continue
        el.set("src", absolute)
        out.append((absolute, lxml_html.tostring(el, encoding="unicode").strip()))
    return out


def _reinject_embeds(content_html: str, embeds: list[tuple[str, str]]) -> str:
    """Append any collected embed the winning candidate doesn't already show."""
    missing = [h for src, h in embeds if src not in content_html]
    if not missing:
        return content_html
    return (content_html + "\n" + "\n".join(missing)).strip()


def _extract(html: str, url: str, rules: dict[str, Any], proxy_images: bool = True) -> dict[str, Any] | None:
    """Candidate → normalize → score → select, then re-inject dropped embeds.
    Reject only a true empty shell (no text *and* no media). Public contract
    unchanged: returns {content_text, content_html, content_hash, metadata={}} or None."""
    cleaned = _clean_html(html, rules)
    reference_text = _reference_text(cleaned, url)

    # Whole-page (already boilerplate-free) anchor/media counts = retention
    # denominator; and the embed set the library tiers will drop.
    try:
        clean_tree = lxml_html.fromstring(cleaned)
        dom_anchors = len(clean_tree.xpath("//a[@href]"))
        dom_media = len(clean_tree.xpath(_MEDIA_XPATH))
        embeds = _collect_embeds(clean_tree, url)
    except Exception:
        dom_anchors = dom_media = 0
        embeds = []

    raw: list[tuple[str, int, str | None]] = []
    content_xpath = rules.get("content_xpath")
    if content_xpath:
        raw.append(("xpath", 0, _gen_xpath(cleaned, content_xpath)))
    raw.append(("readability", 1, _gen_readability(cleaned)))
    raw.append(("trafilatura_html", 2, _gen_trafilatura_html(cleaned, url)))

    candidates = [
        c
        for tier, trust, frag in raw
        if frag and (c := _normalize(frag, url, tier, trust, proxy_images=proxy_images))
    ]

    if candidates:
        max_anchors = max(c.anchors for c in candidates)
        max_media = max(c.media for c in candidates)
        winner = max(candidates, key=lambda c: (
            _score(c, reference_text=reference_text, dom_anchors=dom_anchors,
                   dom_media=dom_media, max_anchors=max_anchors, max_media=max_media),
            -c.trust,
        ))
        content_text, content_html = reference_text or winner.text, winner.html
    else:
        # No structured body (e.g. a video-only post the libraries emptied).
        content_text, content_html = reference_text, ""

    content_html = _reinject_embeds(content_html, embeds)

    # Reject a genuine empty shell: no text anywhere *and* no real (src-bearing)
    # media. A text-free photo/video post has media, so it survives.
    if not content_text.strip() and not _has_media(content_html):
        return None
    return _pack(content_text, content_html)


_ALLOWED_TAGS = frozenset({
    'html', 'body', 'div', 'p', 'a', 'em', 'i', 'b', 'strong',
    'span', 'br', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'blockquote', 'figure', 'figcaption', 'img', 'iframe', 'video',
    'table', 'thead', 'tbody', 'tr', 'td', 'th', 'pre', 'code', 'sup', 'sub',
    'audio', 'source',
})


_DROP_TREE_TAGS = frozenset({'style', 'script', 'noscript'})
