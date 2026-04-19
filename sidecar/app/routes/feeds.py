import asyncio
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
            "SELECT fetch_full_content, priority, extract_rules FROM feed_config WHERE feed_id = %s",
            (feed_id,),
        )
        row = await cur.fetchone()
        fetch_full = row["fetch_full_content"] if row else False
        priority = row["priority"] if row else 2
        extract_rules = row["extract_rules"] if row else {}
    return templates.TemplateResponse(
        request, "feed_settings.html",
        {
            "feed": feed,
            "fetch_full_content": fetch_full,
            "priority": priority,
            "extract_rules_json": json.dumps(extract_rules or {}, indent=2),
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
