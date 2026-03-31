from fastapi import APIRouter, Request
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
