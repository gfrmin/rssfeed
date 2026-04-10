import asyncio
import base64
import json
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, Response

from app import miniflux_client
from app.db import get_conn
from app.templating import templates

router = APIRouter()


async def _feed_configs(conn) -> dict[int, dict]:
    cur = await conn.execute(
        "SELECT feed_id, fetch_full_content, priority, summarize FROM feed_config"
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

    return templates.TemplateResponse(request, "feeds.html", {"feeds": feeds})


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


@router.get("/feeds/health", response_class=HTMLResponse)
async def feed_health(request: Request):
    feeds = await miniflux_client.get_feeds()
    now = datetime.now(timezone.utc)

    for feed in feeds:
        # Parse checked_at
        checked = feed.get("checked_at", "")
        if checked:
            try:
                dt = datetime.fromisoformat(checked.replace("Z", "+00:00"))
                feed["_checked_ago"] = (now - dt).total_seconds()
            except Exception:
                feed["_checked_ago"] = None
        else:
            feed["_checked_ago"] = None

        feed["_has_error"] = bool(feed.get("parsing_error_message"))
        feed["_is_stale"] = (
            feed["_checked_ago"] is not None and feed["_checked_ago"] > 30 * 86400
        )
        feed["_error_count"] = feed.get("parsing_error_count", 0)

    # Sort: errors first, then stale, then OK
    feeds.sort(key=lambda f: (
        0 if f["_has_error"] else 1 if f["_is_stale"] else 2,
        f.get("title", "").lower(),
    ))

    return templates.TemplateResponse(
        request, "feed_health.html", {"feeds": feeds}
    )


@router.get("/feeds/{feed_id}", response_class=HTMLResponse)
async def feed_settings(request: Request, feed_id: int):
    feed = await miniflux_client.get_feed(feed_id)
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT fetch_full_content, priority, extract_rules, summarize FROM feed_config WHERE feed_id = %s",
            (feed_id,),
        )
        row = await cur.fetchone()
        fetch_full = row["fetch_full_content"] if row else False
        priority = row["priority"] if row else 2
        extract_rules = row["extract_rules"] if row else {}
        summarize = row["summarize"] if row else False
    return templates.TemplateResponse(
        request, "feed_settings.html",
        {
            "feed": feed,
            "fetch_full_content": fetch_full,
            "priority": priority,
            "extract_rules_json": json.dumps(extract_rules or {}, indent=2),
            "summarize": summarize,
        },
    )


@router.get("/feeds/{feed_id}/icon")
async def feed_icon(feed_id: int):
    """Serve feed favicon, caching in sidecar DB."""
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT icon_data, icon_mime FROM feed_icons WHERE feed_id = %s",
            (feed_id,),
        )
        cached = await cur.fetchone()
        if cached and cached["icon_data"]:
            return Response(
                content=bytes(cached["icon_data"]),
                media_type=cached["icon_mime"] or "image/png",
            )

    # Fetch from Miniflux
    icon = await miniflux_client.get_feed_icon(feed_id)
    if not icon:
        return Response(status_code=404)

    icon_data = base64.b64decode(icon.get("data", ""))
    icon_mime = icon.get("mime_type", "image/png")

    async with get_conn() as conn:
        await conn.execute(
            """INSERT INTO feed_icons (feed_id, icon_data, icon_mime)
               VALUES (%s, %s, %s)
               ON CONFLICT (feed_id) DO UPDATE SET icon_data = %s, icon_mime = %s, fetched_at = NOW()""",
            (feed_id, icon_data, icon_mime, icon_data, icon_mime),
        )
        await conn.commit()

    return Response(content=icon_data, media_type=icon_mime)


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


def _summarize_section_html(feed_id: int, fetch_full: bool, summarize: bool) -> str:
    """Render the summarize settings section."""
    if fetch_full:
        cls = "on" if summarize else "off"
        label = "ON" if summarize else "OFF"
        btn = f'<button hx-post="/feeds/{feed_id}/toggle-summarize" hx-swap="outerHTML" class="toggle {cls}">{label}</button>'
    else:
        btn = '<button class="toggle off" disabled>OFF</button><span class="hint">Requires full content fetching</span>'
    return (
        f'<div class="settings-section" id="summarize-section">'
        f'<label>LLM Summarization</label>{btn}</div>'
    )


@router.post("/feeds/{feed_id}/toggle-full-content")
async def toggle_full_content(feed_id: int):
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT fetch_full_content, summarize FROM feed_config WHERE feed_id = %s",
            (feed_id,),
        )
        row = await cur.fetchone()
        if row is None:
            await conn.execute(
                "INSERT INTO feed_config (feed_id, fetch_full_content) VALUES (%s, TRUE)",
                (feed_id,),
            )
            new_val = True
            summarize = False
        else:
            new_val = not row["fetch_full_content"]
            summarize = row["summarize"]
            await conn.execute(
                "UPDATE feed_config SET fetch_full_content = %s, updated_at = NOW() WHERE feed_id = %s",
                (new_val, feed_id),
            )
        await conn.commit()
    label = "ON" if new_val else "OFF"
    cls = "on" if new_val else "off"
    btn = f'<button hx-post="/feeds/{feed_id}/toggle-full-content" hx-swap="outerHTML" class="toggle {cls}">{label}</button>'
    oob = _summarize_section_html(feed_id, new_val, summarize).replace(
        'id="summarize-section"', 'id="summarize-section" hx-swap-oob="outerHTML:#summarize-section"'
    )
    return HTMLResponse(btn + oob)


@router.post("/feeds/{feed_id}/toggle-summarize")
async def toggle_summarize(feed_id: int):
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT fetch_full_content, summarize FROM feed_config WHERE feed_id = %s",
            (feed_id,),
        )
        row = await cur.fetchone()
        fetch_full = row["fetch_full_content"] if row else False
        if not fetch_full:
            return HTMLResponse(
                '<button class="toggle off" disabled>OFF</button>'
                '<span class="hint">Requires full content fetching</span>'
            )
        if row is None:
            await conn.execute(
                "INSERT INTO feed_config (feed_id, summarize) VALUES (%s, TRUE)",
                (feed_id,),
            )
            new_val = True
        else:
            new_val = not row["summarize"]
            await conn.execute(
                "UPDATE feed_config SET summarize = %s, updated_at = NOW() WHERE feed_id = %s",
                (new_val, feed_id),
            )
        await conn.commit()
    label = "ON" if new_val else "OFF"
    cls = "on" if new_val else "off"
    return HTMLResponse(
        f'<button hx-post="/feeds/{feed_id}/toggle-summarize" hx-swap="outerHTML" class="toggle {cls}">{label}</button>'
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
