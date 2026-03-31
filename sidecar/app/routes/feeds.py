from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import miniflux_client
from app.db import get_conn
from app.templating import templates

router = APIRouter()


async def _feed_configs(conn) -> dict[int, bool]:
    cur = await conn.execute("SELECT feed_id, fetch_full_content FROM feed_config")
    return {row["feed_id"]: row["fetch_full_content"] for row in await cur.fetchall()}


@router.get("/", response_class=HTMLResponse)
async def feed_list(request: Request):
    feeds = await miniflux_client.get_feeds()
    async with get_conn() as conn:
        configs = await _feed_configs(conn)
    for feed in feeds:
        feed["fetch_full_content"] = configs.get(feed["id"], False)
    feeds.sort(key=lambda f: f.get("title", "").lower())
    return templates.TemplateResponse(request, "feeds.html", {"feeds": feeds})


@router.get("/feeds/{feed_id}", response_class=HTMLResponse)
async def feed_settings(request: Request, feed_id: int):
    feed = await miniflux_client.get_feed(feed_id)
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT fetch_full_content, priority FROM feed_config WHERE feed_id = %s",
            (feed_id,),
        )
        row = await cur.fetchone()
        fetch_full = row["fetch_full_content"] if row else False
        priority = row["priority"] if row else 2
    return templates.TemplateResponse(
        request, "feed_settings.html",
        {"feed": feed, "fetch_full_content": fetch_full, "priority": priority},
    )


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
