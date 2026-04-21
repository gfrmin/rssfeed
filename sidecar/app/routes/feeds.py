import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from lxml import etree as lxml_etree, html as lxml_html

from app import miniflux_client
from app.config import BRIGHTDATA_PROXY
from app.db import get_conn
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


async def _feed_configs(conn) -> dict[int, dict]:
    cur = await conn.execute(
        "SELECT feed_id, fetch_full_content, priority FROM feed_config"
    )
    return {row["feed_id"]: row for row in await cur.fetchall()}


async def _latest_entry_dates_all() -> dict[int, str]:
    """Derive per-feed latest published_at from a single bulk /v1/entries call.

    Feeds whose latest entry is older than the top-1000 window get an empty
    date — they sort to the bottom of their priority bucket, matching the
    pre-existing behavior for feeds with zero entries.
    """
    data = await miniflux_client.get_entries(
        limit=1000, direction="desc", order="published_at",
    )
    latest: dict[int, str] = {}
    for e in data.get("entries") or []:
        fid = e.get("feed_id") or (e.get("feed") or {}).get("id")
        if fid is not None and fid not in latest:
            latest[fid] = e.get("published_at", "") or ""
    return latest


async def _fetch_feed_configs() -> dict[int, dict]:
    async with get_conn() as conn:
        return await _feed_configs(conn)


@router.get("/", response_class=HTMLResponse)
async def feed_list(request: Request):
    t0 = time.perf_counter()

    feeds, counters, latest_dates, categories, configs = await asyncio.gather(
        miniflux_client.get_feeds(),
        miniflux_client.get_feed_counters(),
        _latest_entry_dates_all(),
        miniflux_client.get_categories(),
        _fetch_feed_configs(),
    )
    t_fetch = time.perf_counter()

    unreads = counters.get("unreads", {})
    for feed in feeds:
        cfg = configs.get(feed["id"], {})
        feed["fetch_full_content"] = cfg.get("fetch_full_content", False)
        feed["priority"] = cfg.get("priority", 2)
        feed["unread_count"] = unreads.get(str(feed["id"]), 0)
        feed["latest_entry_at"] = latest_dates.get(feed["id"], "")

    # Stable sort: first by latest entry (most recent first), then by priority
    feeds.sort(key=lambda f: f.get("latest_entry_at", ""), reverse=True)
    feeds.sort(key=lambda f: f["priority"])
    t_sort = time.perf_counter()

    response = templates.TemplateResponse(
        request, "feeds.html", {"feeds": feeds, "categories": categories}
    )
    t_render = time.perf_counter()

    logger.info(
        "feed_list timings: fetch=%.0fms sort=%.0fms render=%.0fms feeds=%d",
        (t_fetch - t0) * 1000,
        (t_sort - t_fetch) * 1000,
        (t_render - t_sort) * 1000,
        len(feeds),
    )
    return response


@router.get("/categories", response_class=HTMLResponse)
async def category_list(request: Request):
    categories = await miniflux_client.get_categories()
    counters = await miniflux_client.get_feed_counters()
    unreads = counters.get("unreads", {})
    feeds = await miniflux_client.get_feeds()

    # Group feeds by category and sum unreads
    cat_feeds: dict[int, list] = {}
    cat_unreads: dict[int, int] = {}
    for feed in feeds:
        cid = feed.get("category", {}).get("id", 0)
        cat_feeds.setdefault(cid, []).append(feed)
        cat_unreads[cid] = cat_unreads.get(cid, 0) + unreads.get(str(feed["id"]), 0)

    for cat in categories:
        cat["feeds"] = cat_feeds.get(cat["id"], [])
        cat["unread_count"] = cat_unreads.get(cat["id"], 0)

    return templates.TemplateResponse(
        request, "categories.html", {"categories": categories}
    )


_STALE_SECONDS = 24 * 3600
_PERSISTENT_ERROR_THRESHOLD = 3

# Ordered: severity (persistent-error buckets first, then stale, paused, ok)
_BUCKET_ORDER = [
    "http_404", "not_a_feed", "bot_blocked", "server_5xx", "auth",
    "tls", "unsupported_scheme", "dns_fail", "connect_fail", "other",
    "stale", "paused", "ok",
]

_BUCKET_LABELS = {
    "http_404": "HTTP 404 — URL not found",
    "not_a_feed": "Not a feed — URL returns HTML",
    "bot_blocked": "Bot-protected — blocked by Cloudflare/WAF",
    "server_5xx": "Server error (5xx)",
    "auth": "Authentication required",
    "tls": "TLS / certificate error",
    "unsupported_scheme": "Unsupported URL scheme (not real feeds)",
    "dns_fail": "DNS lookup failed",
    "connect_fail": "Connection failed",
    "other": "Other errors",
    "stale": "Stale (not polled in 24h)",
    "paused": "Paused — polling disabled, entries kept",
    "ok": "Healthy",
}


def _error_bucket(msg: str) -> str:
    m = msg or ""
    if not m:
        return ""
    if "not found" in m and "resource" in m:
        return "http_404"
    if "Unable to detect feed format" in m:
        return "not_a_feed"
    if "bot protection" in m or "forbidden" in m:
        return "bot_blocked"
    if "server error" in m:
        return "server_5xx"
    if "not authorized" in m or "bad username" in m:
        return "auth"
    if "TLS" in m or "tls:" in m:
        return "tls"
    if "unsupported" in m.lower():
        return "unsupported_scheme"
    if "dial tcp" in m and "lookup" in m:
        return "dns_fail"
    if "dial tcp" in m:
        return "connect_fail"
    return "other"


def _annotate_health(feed: dict, now: datetime) -> None:
    checked = feed.get("checked_at", "")
    if checked:
        try:
            dt = datetime.fromisoformat(checked.replace("Z", "+00:00"))
            feed["_checked_ago"] = (now - dt).total_seconds()
        except Exception:
            feed["_checked_ago"] = None
    else:
        feed["_checked_ago"] = None

    feed["_error_count"] = feed.get("parsing_error_count", 0)
    feed["_has_error"] = bool(feed.get("parsing_error_message"))
    feed["_is_persistent"] = feed["_error_count"] >= _PERSISTENT_ERROR_THRESHOLD
    feed["_is_stale"] = (
        feed["_checked_ago"] is not None and feed["_checked_ago"] > _STALE_SECONDS
    )
    feed["_is_paused"] = bool(feed.get("disabled"))

    if feed["_is_paused"]:
        feed["_bucket"] = "paused"
    elif feed["_has_error"]:
        feed["_bucket"] = _error_bucket(feed.get("parsing_error_message", ""))
    elif feed["_is_stale"]:
        feed["_bucket"] = "stale"
    else:
        feed["_bucket"] = "ok"


@router.get("/feeds/health", response_class=HTMLResponse)
async def feed_health(request: Request):
    feeds = await miniflux_client.get_feeds()
    now = datetime.now(timezone.utc)

    for feed in feeds:
        _annotate_health(feed, now)

    # Group by bucket, preserving _BUCKET_ORDER
    groups: dict[str, list[dict]] = {b: [] for b in _BUCKET_ORDER}
    for feed in feeds:
        groups.setdefault(feed["_bucket"], []).append(feed)
    for bucket in groups:
        groups[bucket].sort(key=lambda f: f.get("title", "").lower())

    bucket_sections = [
        {
            "key": b,
            "label": _BUCKET_LABELS.get(b, b),
            "feeds": groups[b],
            "count": len(groups[b]),
        }
        for b in _BUCKET_ORDER
        if groups[b]
    ]

    return templates.TemplateResponse(
        request, "feed_health.html",
        {
            "bucket_sections": bucket_sections,
            "has_proxy": bool(BRIGHTDATA_PROXY),
        },
    )


@router.get("/feeds/health/summary", response_class=HTMLResponse)
async def feed_health_summary(request: Request):
    """Small HTML fragment for the nav badge — lazy-loaded via htmx."""
    feeds = await miniflux_client.get_feeds()
    now = datetime.now(timezone.utc)
    persistent = 0
    transient = 0
    stale = 0
    for feed in feeds:
        _annotate_health(feed, now)
        if feed["_is_paused"]:
            continue
        if feed["_is_persistent"]:
            persistent += 1
        elif feed["_has_error"]:
            transient += 1
        elif feed["_is_stale"]:
            stale += 1
    return templates.TemplateResponse(
        request, "feed_health_badge.html",
        {"persistent": persistent, "transient": transient, "stale": stale},
    )


_DISCOVERY_TYPES = (
    "application/rss+xml",
    "application/atom+xml",
    "application/feed+json",
    "application/json",
)


async def _fetch_with_proxy_fallback(url: str, *, accept: str = "text/html,application/xhtml+xml") -> str | None:
    """Fetch any URL with direct-then-BrightData-proxy fallback. Returns response text or None."""
    kwargs = dict(
        timeout=20.0,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
            "Accept": accept,
        },
    )
    try:
        async with httpx.AsyncClient(**kwargs) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.text
    except Exception as e:
        logger.info("Direct fetch failed for %s: %s", url, e)
    if BRIGHTDATA_PROXY:
        try:
            async with httpx.AsyncClient(proxy=BRIGHTDATA_PROXY, **kwargs) as c:
                r = await c.get(url)
                r.raise_for_status()
                return r.text
        except Exception as e:
            logger.info("Proxy fetch failed for %s: %s", url, e)
    return None


async def _fetch_html_for_discovery(url: str) -> str | None:
    """Fetch a page for feed auto-discovery. Tries direct then BrightData proxy."""
    return await _fetch_with_proxy_fallback(url)


def _infer_feed_type(ltype: str, href: str) -> str:
    """Normalise a feed type from a <link type=...> attr or URL suffix. Returns 'rss'|'atom'|'json'|'unknown'."""
    ltype = ltype.lower().strip()
    if "atom" in ltype:
        return "atom"
    if "json" in ltype:
        return "json"
    if "rss" in ltype or "xml" in ltype:
        return "rss"
    h = href.lower()
    if "atom" in h:
        return "atom"
    if h.endswith(".json") or "feed.json" in h:
        return "json"
    if "rss" in h or h.endswith(".xml") or "/feed" in h:
        return "rss"
    return "unknown"


def _parse_feed_links(html: str, base_url: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Parse HTML for feed-ish <link> tags.

    Returns (candidates, raw_links):
      candidates — list of {url, title, type} ready to subscribe (dedup'd, feed-like).
      raw_links  — every <link rel=alternate|feed> seen (rel, type, href, title), for debug display.
    """
    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return [], []
    raw_links: list[dict[str, str]] = []
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in tree.xpath("//link[@href and (@rel='alternate' or @rel='feed')]"):
        href = (link.get("href") or "").strip()
        if not href:
            continue
        abs_href = urljoin(base_url, href)
        rel = (link.get("rel") or "").strip()
        ltype = (link.get("type") or "").strip()
        title = (link.get("title") or "").strip()
        raw_links.append({"rel": rel, "type": ltype, "href": abs_href, "title": title})
        is_typed_feed = ltype.lower() in _DISCOVERY_TYPES
        is_href_feedish = any(k in href.lower() for k in ("/rss", "/atom", "/feed", ".xml"))
        if (is_typed_feed or is_href_feedish) and abs_href not in seen:
            seen.add(abs_href)
            candidates.append({
                "url": abs_href,
                "title": title,
                "type": _infer_feed_type(ltype, abs_href),
            })
    return candidates, raw_links


def _find_feed_link(html: str, base_url: str) -> str | None:
    """Back-compat wrapper: return the first discovered feed URL, or None."""
    candidates, _ = _parse_feed_links(html, base_url)
    return candidates[0]["url"] if candidates else None


async def _probe_one(feed: dict, sem: asyncio.Semaphore) -> dict:
    """Probe a feed's site for a new RSS/Atom link. Returns {feed_id, title, current_url, outcome, new_url}.

    outcome: 'candidate' | 'same' | 'no_candidate' | 'fetch_failed'
    """
    feed_id = feed["id"]
    current_url = feed.get("feed_url", "")
    site_url = feed.get("site_url") or current_url
    title = feed.get("title", "")
    result = {
        "feed_id": feed_id,
        "title": title,
        "current_url": current_url,
        "outcome": "fetch_failed",
        "new_url": None,
    }
    if not site_url:
        return result
    async with sem:
        html = await _fetch_html_for_discovery(site_url)
        if not html:
            return result
        new_url = _find_feed_link(html, site_url)
        if not new_url:
            result["outcome"] = "no_candidate"
            return result
        if new_url == current_url:
            result["outcome"] = "same"
            result["new_url"] = new_url
            return result
        result["outcome"] = "candidate"
        result["new_url"] = new_url
        return result


@router.post("/feeds/auto-discover")
async def auto_discover(request: Request, feed_ids: list[int] = Form(...)):
    """Probe-only. Returns a preview fragment with per-row Apply buttons — no mutations yet."""
    all_feeds = await miniflux_client.get_feeds()
    feeds_by_id = {f["id"]: f for f in all_feeds}
    targets = [feeds_by_id[i] for i in feed_ids if i in feeds_by_id]
    sem = asyncio.Semaphore(8)
    probes = await asyncio.gather(*[_probe_one(f, sem) for f in targets])

    candidates = [p for p in probes if p["outcome"] == "candidate"]
    counts = {
        "candidates": len(candidates),
        "same": sum(1 for p in probes if p["outcome"] == "same"),
        "no_candidate": sum(1 for p in probes if p["outcome"] == "no_candidate"),
        "fetch_failed": sum(1 for p in probes if p["outcome"] == "fetch_failed"),
    }

    return templates.TemplateResponse(
        request, "feed_discover_preview.html",
        {"candidates": candidates, "counts": counts},
    )


async def _record_url_change(feed_id: int, old_url: str, new_url: str, source: str) -> None:
    """Persist an old→new feed URL change for future reverse lookups (e.g. NewsBlur import)."""
    if not old_url or old_url == new_url:
        return
    try:
        async with get_conn() as conn:
            await conn.execute(
                "INSERT INTO feed_url_history (feed_id, old_url, new_url, source) VALUES (%s, %s, %s, %s)",
                (feed_id, old_url, new_url, source),
            )
            await conn.commit()
    except Exception as e:
        logger.warning("failed to record feed_url_history for feed %s: %s", feed_id, e)


@router.post("/feeds/apply-discovered")
async def apply_discovered(
    feed_id: int = Form(...),
    new_url: str = Form(...),
    new_title: str | None = Form(None),
    new_site_url: str | None = Form(None),
):
    """Apply a previewed feed_url change. Optionally also update title/site_url.

    Non-empty new_title / new_site_url are forwarded to Miniflux alongside the feed_url update.
    """
    updates: dict[str, Any] = {"feed_url": new_url}
    title_val = (new_title or "").strip()
    site_url_val = (new_site_url or "").strip()
    if title_val:
        updates["title"] = title_val
    if site_url_val:
        updates["site_url"] = site_url_val

    try:
        feed = await miniflux_client.get_feed(feed_id)
        old_url = feed.get("feed_url", "")
        await miniflux_client.update_feed(feed_id, **updates)
    except Exception as e:
        detail = _extract_miniflux_error(e) or str(e)
        logger.warning("apply_discovered(%s, %s) failed: %s", feed_id, new_url, detail)
        return HTMLResponse(f'<span class="error">Failed: {detail}</span>')
    try:
        await miniflux_client.refresh_feed(feed_id)
    except Exception as e:
        logger.info("post-apply refresh failed for feed %s: %s", feed_id, e)
    await _record_url_change(feed_id, old_url, new_url, source="auto-discover")
    extras = []
    if title_val:
        extras.append(f"title→{title_val!r}")
    if site_url_val:
        extras.append(f"site_url→{site_url_val!r}")
    extras_str = (" (" + ", ".join(extras) + ")") if extras else ""
    logger.info("feed %s feed_url change (auto-discover): %s -> %s%s", feed_id, old_url, new_url, extras_str)
    return HTMLResponse(
        f'<span class="success">Applied{extras_str}. Previous URL: <code class="text-detail">{old_url}</code></span>',
        headers={"HX-Refresh": "true"},
    )


@router.post("/feeds/discover")
async def discover_feeds(
    request: Request,
    url: str = Form(...),
    category_id: int | None = Form(None),
    feed_id: int | None = Form(None),
):
    """Discover candidate feeds for a page URL. Tries Miniflux /v1/discover, falls back to proxy-enabled HTML parse.

    Mode:
      - If `feed_id` is set, candidates render with "Apply" buttons that re-point that feed.
      - Otherwise (requires `category_id`), candidates render with "Subscribe" buttons for new feeds.
    """
    url = url.strip()
    if not url:
        return HTMLResponse('<span class="error">URL is required</span>', status_code=400)
    mode = "apply" if feed_id is not None else "subscribe"
    if mode == "subscribe" and category_id is None:
        return HTMLResponse('<span class="error">category_id is required</span>', status_code=400)

    current_feed: dict[str, Any] | None = None
    if feed_id is not None:
        try:
            current_feed = await miniflux_client.get_feed(feed_id)
        except Exception as e:
            logger.info("Failed to fetch feed %s for discover context: %s", feed_id, e)

    candidates: list[dict[str, str]] = []
    source_label = ""
    try:
        miniflux_result = await miniflux_client.discover(url)
        if miniflux_result:
            for item in miniflux_result:
                candidates.append({
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "type": (item.get("type") or "").lower() or "unknown",
                    "source": "miniflux",
                })
            source_label = "miniflux"
    except Exception as e:
        logger.info("Miniflux discover failed for %s: %s", url, e)

    raw_links: list[dict[str, str]] = []
    if not candidates:
        html = await _fetch_html_for_discovery(url)
        if html:
            fallback_candidates, raw_links = _parse_feed_links(html, url)
            for c in fallback_candidates:
                candidates.append({**c, "source": "proxy"})
            if fallback_candidates:
                source_label = "proxy"

    return templates.TemplateResponse(
        request, "feed_discover_results.html",
        {
            "candidates": candidates,
            "raw_links": raw_links,
            "category_id": category_id,
            "feed_id": feed_id,
            "mode": mode,
            "source": source_label,
            "url": url,
            "current_feed": current_feed,
        },
    )


@router.post("/feeds/apply-discovered/confirm")
async def apply_discovered_confirm(
    request: Request,
    feed_id: int = Form(...),
    new_url: str = Form(...),
    candidate_title: str | None = Form(None),
    typed_url: str | None = Form(None),
):
    """Render a confirmation panel: chosen candidate URL + editable title/site_url suggestions."""
    try:
        feed = await miniflux_client.get_feed(feed_id)
    except Exception as e:
        detail = _extract_miniflux_error(e) or str(e)
        return HTMLResponse(f'<span class="error">Failed: {detail}</span>')
    return templates.TemplateResponse(
        request, "feed_discover_confirm.html",
        {
            "feed_id": feed_id,
            "new_url": new_url,
            "candidate_title": (candidate_title or "").strip(),
            "typed_url": (typed_url or "").strip(),
            "current_feed": feed,
        },
    )


def _parse_feed_preview(text: str) -> list[dict[str, str]]:
    """Parse RSS or Atom XML and return up to 3 {title, published} entries."""
    try:
        raw = text.encode("utf-8") if isinstance(text, str) else text
        root = lxml_etree.fromstring(raw)
    except Exception:
        return []

    def _first_child_text(parent, localnames: tuple[str, ...]) -> str:
        for child in parent:
            if lxml_etree.QName(child).localname in localnames:
                return (child.text or "").strip()
        return ""

    items: list[dict[str, str]] = []
    for item in root.iter():
        if lxml_etree.QName(item).localname not in ("item", "entry"):
            continue
        title = _first_child_text(item, ("title",))
        published = _first_child_text(item, ("pubDate", "published", "updated", "date"))
        if title or published:
            items.append({"title": title, "published": published})
        if len(items) >= 3:
            break
    return items


@router.post("/feeds/discover/preview")
async def discover_preview(feed_url: str = Form(...)):
    """Fetch a candidate feed URL and return up to 3 latest entry titles as an htmx fragment."""
    feed_url = feed_url.strip()
    if not feed_url:
        return HTMLResponse('<span class="error">URL is required</span>', status_code=400)
    text = await _fetch_with_proxy_fallback(
        feed_url,
        accept="application/rss+xml,application/atom+xml,application/xml,text/xml",
    )
    if not text:
        return HTMLResponse('<span class="error">Could not fetch feed</span>', status_code=502)
    items = _parse_feed_preview(text)
    if not items:
        return HTMLResponse('<span class="error">Could not parse feed</span>', status_code=502)
    rows = "".join(
        f'<li class="text-detail"><span class="font-medium">{i["title"] or "(no title)"}</span>'
        f'{(" <span class=\"text-text-muted\">— " + i["published"] + "</span>") if i["published"] else ""}</li>'
        for i in items
    )
    return HTMLResponse(f'<ul class="list-disc ml-5 my-1">{rows}</ul>')


@router.post("/feeds/set-proxy")
async def set_proxy(feed_ids: list[int] = Form(...)):
    """Non-destructive: routes selected feeds through BRIGHTDATA_PROXY and refreshes them."""
    if not BRIGHTDATA_PROXY:
        return HTMLResponse(
            '<span class="error">BRIGHTDATA_PROXY not set in sidecar env — cannot route feeds through proxy</span>',
            status_code=400,
        )

    async def _set_one(fid: int) -> bool:
        try:
            await miniflux_client.update_feed(fid, proxy_url=BRIGHTDATA_PROXY, fetch_via_proxy=True)
            await miniflux_client.refresh_feed(fid)
            return True
        except Exception as e:
            logger.warning("set-proxy failed for feed %s: %s", fid, e)
            return False

    sem = asyncio.Semaphore(8)

    async def _bounded(fid: int) -> bool:
        async with sem:
            return await _set_one(fid)

    results = await asyncio.gather(*[_bounded(i) for i in feed_ids])
    ok = sum(1 for r in results if r)
    return HTMLResponse(
        f'<span class="success">Set proxy on {ok}/{len(results)} feeds</span>'
    )


@router.post("/feeds/bulk-refresh")
async def bulk_refresh(feed_ids: list[int] = Form(...)):
    """Non-destructive: triggers a re-poll on each selected feed."""
    sem = asyncio.Semaphore(8)

    async def _one(fid: int) -> bool:
        async with sem:
            try:
                await miniflux_client.refresh_feed(fid)
                return True
            except Exception as e:
                logger.warning("refresh failed for feed %s: %s", fid, e)
                return False

    results = await asyncio.gather(*[_one(i) for i in feed_ids])
    ok = sum(1 for r in results if r)
    return HTMLResponse(
        f'<span class="success">Refresh triggered on {ok}/{len(results)} feeds</span>'
    )


@router.post("/feeds/allow-self-signed")
async def allow_self_signed(feed_ids: list[int] = Form(...)):
    """Non-destructive: sets allow_self_signed_certificates=true then refreshes."""
    sem = asyncio.Semaphore(8)

    async def _one(fid: int) -> bool:
        async with sem:
            try:
                await miniflux_client.update_feed(fid, allow_self_signed_certificates=True)
                await miniflux_client.refresh_feed(fid)
                return True
            except Exception as e:
                logger.warning("allow-self-signed failed for feed %s: %s", fid, e)
                return False

    results = await asyncio.gather(*[_one(i) for i in feed_ids])
    ok = sum(1 for r in results if r)
    return HTMLResponse(
        f'<span class="success">Allowed self-signed on {ok}/{len(results)} feeds</span>'
    )


@router.post("/feeds/pause-polling")
async def pause_polling(feed_ids: list[int] = Form(...)):
    """Non-destructive: sets disabled=true on selected feeds. Entries remain; polling stops."""
    sem = asyncio.Semaphore(8)

    async def _one(fid: int) -> bool:
        async with sem:
            try:
                await miniflux_client.update_feed(fid, disabled=True)
                return True
            except Exception as e:
                logger.warning("pause-polling failed for feed %s: %s", fid, e)
                return False

    results = await asyncio.gather(*[_one(i) for i in feed_ids])
    ok = sum(1 for r in results if r)
    return HTMLResponse(
        f'<span class="success">Paused polling on {ok}/{len(results)} feeds · entries preserved</span>'
    )


@router.post("/feeds/resume-polling")
async def resume_polling(feed_ids: list[int] = Form(...)):
    """Non-destructive: sets disabled=false on selected feeds."""
    sem = asyncio.Semaphore(8)

    async def _one(fid: int) -> bool:
        async with sem:
            try:
                await miniflux_client.update_feed(fid, disabled=False)
                return True
            except Exception as e:
                logger.warning("resume-polling failed for feed %s: %s", fid, e)
                return False

    results = await asyncio.gather(*[_one(i) for i in feed_ids])
    ok = sum(1 for r in results if r)
    return HTMLResponse(
        f'<span class="success">Resumed polling on {ok}/{len(results)} feeds</span>'
    )


def _extract_miniflux_error(e: Exception) -> str | None:
    """If the exception is an httpx HTTPStatusError, try to read Miniflux's JSON error_message."""
    resp = getattr(e, "response", None)
    if resp is None:
        return None
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "").strip() or None
    if isinstance(body, dict):
        msg = body.get("error_message") or body.get("error")
        if isinstance(msg, str):
            return msg
    return None


def _looks_like_feed(text: str) -> bool:
    """Heuristic: does the fetched text look like a feed (RSS/Atom/RDF/JSON Feed)?"""
    if not text:
        return False
    try:
        raw = text.encode("utf-8") if isinstance(text, str) else text
        root = lxml_etree.fromstring(raw)
        local = lxml_etree.QName(root).localname.lower()
        if local in ("rss", "feed", "rdf"):
            return True
    except Exception:
        pass
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("version"), str) and "jsonfeed.org" in obj["version"]:
                return True
        except Exception:
            pass
    return False


@router.post("/feeds/{feed_id}/set-url")
async def set_feed_url(request: Request, feed_id: int, feed_url: str = Form(...)):
    """Update a feed's URL. Tries the URL as-is first; if it doesn't parse as a feed, runs discovery.

    - If the URL looks like a feed, update feed_url directly (non-destructive — entries preserved).
    - Otherwise, render the discovery candidates (apply mode) so the user can pick and apply.
    """
    new_url = feed_url.strip()
    if not new_url:
        return HTMLResponse('<span class="error">URL is required</span>', status_code=400)

    try:
        feed = await miniflux_client.get_feed(feed_id)
    except Exception as e:
        logger.warning("set-url feed %s lookup failed: %s", feed_id, e)
        return HTMLResponse(f'<span class="error">Failed: {e}</span>', status_code=500)
    old_url = feed.get("feed_url", "")
    if new_url == old_url:
        return HTMLResponse('<span class="text-text-muted">No change</span>')

    text = await _fetch_with_proxy_fallback(
        new_url,
        accept="application/rss+xml,application/atom+xml,application/feed+json,application/json,application/xml,text/xml,text/html",
    )
    if _looks_like_feed(text or ""):
        try:
            await miniflux_client.update_feed(feed_id, feed_url=new_url)
        except Exception as e:
            detail = _extract_miniflux_error(e) or str(e)
            logger.warning("set-url feed %s -> %s failed: %s", feed_id, new_url, detail)
            return HTMLResponse(f'<span class="error">Failed: {detail}</span>')
        try:
            await miniflux_client.refresh_feed(feed_id)
        except Exception as e:
            logger.info("post-set-url refresh failed for feed %s: %s", feed_id, e)
        await _record_url_change(feed_id, old_url, new_url, source="set-url")
        logger.info("feed %s feed_url change (set-url): %s -> %s", feed_id, old_url, new_url)
        return HTMLResponse(
            f'<span class="success">URL updated. Previous: <code class="text-detail">{old_url}</code></span>',
            headers={"HX-Refresh": "true"},
        )

    logger.info("set-url feed %s: URL %s is not a feed, falling back to discovery", feed_id, new_url)
    return await discover_feeds(request, url=new_url, feed_id=feed_id)


@router.get("/feeds/{feed_id}", response_class=HTMLResponse)
async def feed_settings(request: Request, feed_id: int):
    feed = await miniflux_client.get_feed(feed_id)
    _annotate_health(feed, datetime.now(timezone.utc))
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT fetch_full_content, priority, extract_rules FROM feed_config WHERE feed_id = %s",
            (feed_id,),
        )
        row = await cur.fetchone()
        fetch_full = row["fetch_full_content"] if row else False
        priority = row["priority"] if row else 2
        extract_rules = row["extract_rules"] if row else {}

        cur = await conn.execute(
            "SELECT old_url, new_url, source, changed_at FROM feed_url_history "
            "WHERE feed_id = %s ORDER BY changed_at DESC",
            (feed_id,),
        )
        url_history = await cur.fetchall()
    return templates.TemplateResponse(
        request, "feed_settings.html",
        {
            "feed": feed,
            "fetch_full_content": fetch_full,
            "priority": priority,
            "extract_rules_json": json.dumps(extract_rules or {}, indent=2),
            "url_history": url_history,
            "has_proxy": bool(BRIGHTDATA_PROXY),
        },
    )


@router.get("/feeds/{feed_id}/icon")
async def feed_icon(feed_id: int):
    """Serve feed favicon from Miniflux's own icons table."""
    async with get_conn() as conn:
        cur = await conn.execute(
            """
            SELECT i.content, i.mime_type
            FROM feed_icons fi
            JOIN icons i ON i.id = fi.icon_id
            WHERE fi.feed_id = %s
            """,
            (feed_id,),
        )
        row = await cur.fetchone()
    if not row:
        return Response(status_code=404)
    return Response(content=bytes(row["content"]), media_type=row["mime_type"])


@router.post("/feeds/subscribe")
async def subscribe_feed(feed_url: str = Form(...), category_id: int = Form(...)):
    feed_url = feed_url.strip()
    if not feed_url:
        return HTMLResponse('<span class="error">URL is required</span>', status_code=400)
    try:
        result = await miniflux_client.create_feed(feed_url, category_id)
        feed_id = result.get("feed_id", "")
        return HTMLResponse(
            f'<span class="success">Subscribed! <a href="/feeds/{feed_id}" class="text-link underline">Settings</a></span>'
        )
    except Exception as e:
        return HTMLResponse(f'<span class="error">Failed: {e}</span>', status_code=400)


@router.post("/feeds/{feed_id}/delete")
async def delete_feed(feed_id: int):
    await miniflux_client.delete_feed(feed_id)
    return HTMLResponse('<span class="success">Feed deleted</span>')


@router.put("/feeds/{feed_id}/refresh")
async def refresh_feed(feed_id: int):
    await miniflux_client.refresh_feed(feed_id)
    return HTMLResponse('<span class="success">Refresh triggered</span>')


@router.post("/feeds/{feed_id}/rename")
async def rename_feed(feed_id: int, title: str = Form(...)):
    title = title.strip()
    if not title:
        return HTMLResponse('<span class="error">Title cannot be empty</span>', status_code=400)
    await miniflux_client.update_feed(feed_id, title=title)
    return HTMLResponse('<span class="success">Title updated</span>')


@router.post("/feeds/fix-author-titles")
async def fix_author_titles():
    """Batch rename author-based feeds whose titles are missing the author name."""
    feeds = await miniflux_client.get_feeds()
    fixed = 0
    for feed in feeds:
        feed_url = feed.get("feed_url", "")
        if "/author/" not in feed_url:
            continue
        # Get author name from the first entry (more accurate than URL slug)
        data = await miniflux_client.get_entries(feed_id=feed["id"], limit=1)
        entries = data.get("entries") or []
        if entries and entries[0].get("author"):
            author = entries[0]["author"]
        else:
            # Fall back to URL slug
            slug = feed_url.rstrip("/").split("/author/")[-1].split("/")[0]
            author = slug.replace("-", " ").title()
        # Extract publication name from existing title (after | or whole title)
        existing = feed.get("title", "").strip()
        pub = existing.lstrip("| ").strip() if "|" in existing else existing
        new_title = f"{author} | {pub}" if pub else author
        if new_title != existing:
            await miniflux_client.update_feed(feed["id"], title=new_title)
            fixed += 1
    return HTMLResponse(f'<span class="success">Renamed {fixed} feeds</span>')


@router.post("/feeds/{feed_id}/set-priority")
async def set_priority(feed_id: int, priority: int = Form(2)):
    priority = max(1, min(3, priority))
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM feed_config WHERE feed_id = %s", (feed_id,),
        )
        if await cur.fetchone() is None:
            await conn.execute(
                "INSERT INTO feed_config (feed_id, priority) VALUES (%s, %s)",
                (feed_id, priority),
            )
        else:
            await conn.execute(
                "UPDATE feed_config SET priority = %s, updated_at = NOW() WHERE feed_id = %s",
                (priority, feed_id),
            )
        await conn.commit()
    labels = {1: "Must Read", 2: "Normal", 3: "Low"}
    options = "".join(
        f'<option value="{v}" {"selected" if v == priority else ""}>{l}</option>'
        for v, l in labels.items()
    )
    return HTMLResponse(
        f'<select name="priority" hx-post="/feeds/{feed_id}/set-priority" hx-swap="outerHTML">{options}</select>'
    )


@router.post("/feeds/{feed_id}/set-extract-rules")
async def set_extract_rules(feed_id: int, extract_rules: str = Form("")):
    try:
        rules = json.loads(extract_rules) if extract_rules.strip() else {}
    except json.JSONDecodeError as e:
        return HTMLResponse(f'<span class="error">Invalid JSON: {e}</span>', status_code=400)

    import psycopg.types.json

    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM feed_config WHERE feed_id = %s", (feed_id,),
        )
        if await cur.fetchone() is None:
            await conn.execute(
                "INSERT INTO feed_config (feed_id, extract_rules) VALUES (%s, %s::jsonb)",
                (feed_id, psycopg.types.json.Json(rules)),
            )
        else:
            await conn.execute(
                "UPDATE feed_config SET extract_rules = %s::jsonb, updated_at = NOW() WHERE feed_id = %s",
                (psycopg.types.json.Json(rules), feed_id),
            )
        await conn.commit()
    return HTMLResponse('<span class="success">Extract rules saved</span>')


@router.post("/feeds/{feed_id}/toggle-proxy")
async def toggle_proxy(feed_id: int):
    """Flip fetch_via_proxy for a single feed; refreshes on turn-on."""
    feed = await miniflux_client.get_feed(feed_id)
    currently_on = bool(feed.get("fetch_via_proxy"))
    if currently_on:
        # Miniflux 400s on empty proxy_url; leaving it stored is harmless when fetch_via_proxy=false.
        await miniflux_client.update_feed(feed_id, fetch_via_proxy=False)
        new_val = False
    else:
        if not BRIGHTDATA_PROXY:
            return HTMLResponse(
                '<span class="error">BRIGHTDATA_PROXY not set in sidecar env</span>',
                status_code=400,
            )
        await miniflux_client.update_feed(feed_id, proxy_url=BRIGHTDATA_PROXY, fetch_via_proxy=True)
        try:
            await miniflux_client.refresh_feed(feed_id)
        except Exception as e:
            logger.warning("refresh after toggle-proxy(%s) failed: %s", feed_id, e)
        new_val = True
    label = "ON" if new_val else "OFF"
    cls = "on" if new_val else "off"
    return HTMLResponse(
        f'<button hx-post="/feeds/{feed_id}/toggle-proxy" hx-swap="outerHTML" class="toggle {cls}">{label}</button>'
    )


@router.post("/feeds/{feed_id}/toggle-tls-verify")
async def toggle_tls_verify(feed_id: int):
    """Flip allow_self_signed_certificates (Miniflux's InsecureSkipVerify) for a single feed."""
    feed = await miniflux_client.get_feed(feed_id)
    currently_on = bool(feed.get("allow_self_signed_certificates"))
    new_val = not currently_on
    await miniflux_client.update_feed(feed_id, allow_self_signed_certificates=new_val)
    if new_val:
        try:
            await miniflux_client.refresh_feed(feed_id)
        except Exception as e:
            logger.warning("refresh after toggle-tls-verify(%s) failed: %s", feed_id, e)
    label = "SKIP" if new_val else "VERIFY"
    cls = "on" if new_val else "off"
    return HTMLResponse(
        f'<button hx-post="/feeds/{feed_id}/toggle-tls-verify" hx-swap="outerHTML" class="toggle {cls}">{label}</button>'
    )


@router.post("/feeds/{feed_id}/toggle-full-content")
async def toggle_full_content(feed_id: int):
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT fetch_full_content FROM feed_config WHERE feed_id = %s",
            (feed_id,),
        )
        row = await cur.fetchone()
        if row is None:
            await conn.execute(
                "INSERT INTO feed_config (feed_id, fetch_full_content) VALUES (%s, TRUE)",
                (feed_id,),
            )
            new_val = True
        else:
            new_val = not row["fetch_full_content"]
            await conn.execute(
                "UPDATE feed_config SET fetch_full_content = %s, updated_at = NOW() WHERE feed_id = %s",
                (new_val, feed_id),
            )
        await conn.commit()
    label = "ON" if new_val else "OFF"
    cls = "on" if new_val else "off"
    return HTMLResponse(
        f'<button hx-post="/feeds/{feed_id}/toggle-full-content" hx-swap="outerHTML" class="toggle {cls}">{label}</button>'
    )


@router.get("/opml/export")
async def opml_export():
    data = await miniflux_client.export_opml()
    return Response(
        content=data,
        media_type="application/xml",
        headers={"Content-Disposition": 'attachment; filename="feeds.opml"'},
    )


@router.post("/opml/import")
async def opml_import(file: UploadFile = File(...)):
    data = await file.read()
    await miniflux_client.import_opml(data)
    return HTMLResponse('<span class="success">OPML imported successfully</span>')
