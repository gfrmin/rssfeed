import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from lxml import html as lxml_html

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


async def _latest_entry_date(feed_id: int) -> tuple[int, str]:
    data = await miniflux_client.get_entries(
        feed_id=feed_id, limit=1, direction="desc", order="published_at",
    )
    entries = data.get("entries") or []
    return (feed_id, entries[0].get("published_at", "") if entries else "")


@router.get("/", response_class=HTMLResponse)
async def feed_list(request: Request):
    feeds = await miniflux_client.get_feeds()
    counters = await miniflux_client.get_feed_counters()
    unreads = counters.get("unreads", {})
    async with get_conn() as conn:
        configs = await _feed_configs(conn)

    latest_dates = dict(await asyncio.gather(
        *[_latest_entry_date(f["id"]) for f in feeds]
    ))

    for feed in feeds:
        cfg = configs.get(feed["id"], {})
        feed["fetch_full_content"] = cfg.get("fetch_full_content", False)
        feed["priority"] = cfg.get("priority", 2)
        feed["unread_count"] = unreads.get(str(feed["id"]), 0)
        feed["latest_entry_at"] = latest_dates.get(feed["id"], "")

    # Stable sort: first by latest entry (most recent first), then by priority
    feeds.sort(key=lambda f: f.get("latest_entry_at", ""), reverse=True)
    feeds.sort(key=lambda f: f["priority"])

    categories = await miniflux_client.get_categories()

    return templates.TemplateResponse(request, "feeds.html", {"feeds": feeds, "categories": categories})


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


async def _fetch_html_for_discovery(url: str) -> str | None:
    """Fetch a page for feed auto-discovery. Tries direct then BrightData proxy."""
    kwargs = dict(
        timeout=20.0,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        async with httpx.AsyncClient(**kwargs) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.text
    except Exception as e:
        logger.info("Direct discovery fetch failed for %s: %s", url, e)
    if BRIGHTDATA_PROXY:
        try:
            async with httpx.AsyncClient(proxy=BRIGHTDATA_PROXY, **kwargs) as c:
                r = await c.get(url)
                r.raise_for_status()
                return r.text
        except Exception as e:
            logger.info("Proxy discovery fetch failed for %s: %s", url, e)
    return None


def _find_feed_link(html: str, base_url: str) -> str | None:
    """Parse HTML for <link rel=alternate type=application/rss+xml|atom+xml>."""
    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return None
    for link in tree.xpath("//link[@rel='alternate' and @href]"):
        ltype = (link.get("type") or "").lower().strip()
        if ltype in _DISCOVERY_TYPES:
            href = link.get("href", "").strip()
            if href:
                return urljoin(base_url, href)
    # Fallback: any rel=alternate href that looks like a feed
    for link in tree.xpath("//link[@rel='alternate' and @href]"):
        href = link.get("href", "").strip().lower()
        if any(k in href for k in ("/rss", "/atom", "/feed", ".xml")):
            return urljoin(base_url, link.get("href", "").strip())
    return None


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
async def apply_discovered(feed_id: int = Form(...), new_url: str = Form(...)):
    """Apply a previewed feed_url change. Records history, logs, and echoes the old URL."""
    try:
        feed = await miniflux_client.get_feed(feed_id)
        old_url = feed.get("feed_url", "")
        await miniflux_client.update_feed(feed_id, feed_url=new_url)
    except Exception as e:
        logger.warning("apply_discovered(%s, %s) failed: %s", feed_id, new_url, e)
        return HTMLResponse(f'<span class="error">Failed: {e}</span>', status_code=500)
    await _record_url_change(feed_id, old_url, new_url, source="auto-discover")
    logger.info("feed %s feed_url change (auto-discover): %s -> %s", feed_id, old_url, new_url)
    return HTMLResponse(
        f'<span class="success">Applied. Previous URL: <code class="text-detail">{old_url}</code></span>'
    )


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


@router.post("/feeds/{feed_id}/set-url")
async def set_feed_url(feed_id: int, feed_url: str = Form(...)):
    """Update a feed's URL in place. Non-destructive — Miniflux preserves entries."""
    new_url = feed_url.strip()
    if not new_url:
        return HTMLResponse('<span class="error">URL is required</span>', status_code=400)
    try:
        feed = await miniflux_client.get_feed(feed_id)
        old_url = feed.get("feed_url", "")
        if new_url == old_url:
            return HTMLResponse('<span class="text-text-muted">No change</span>')
        await miniflux_client.update_feed(feed_id, feed_url=new_url)
    except Exception as e:
        logger.warning("set-url feed %s -> %s failed: %s", feed_id, new_url, e)
        return HTMLResponse(f'<span class="error">Failed: {e}</span>', status_code=500)
    await _record_url_change(feed_id, old_url, new_url, source="set-url")
    logger.info("feed %s feed_url change (set-url): %s -> %s", feed_id, old_url, new_url)
    return HTMLResponse(
        f'<span class="success">URL updated. Previous: <code class="text-detail">{old_url}</code></span>'
    )


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
