import secrets
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import miniflux_client
from app.db import get_conn
from app.templating import templates

router = APIRouter()


@router.post("/entries/{entry_id}/share")
async def create_share_link(request: Request, entry_id: int):
    token = secrets.token_urlsafe(16)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)

    async with get_conn() as conn:
        await conn.execute(
            "INSERT INTO share_links (entry_id, token, expires_at) VALUES (%s, %s, %s)",
            (entry_id, token, expires_at),
        )
        await conn.commit()

    host = request.headers.get("host", "localhost")
    scheme = request.headers.get("x-forwarded-proto", "http")
    link = f"{scheme}://{host}/shared/{token}"

    return HTMLResponse(
        f'<span class="success">Share link (7 days): <a href="/shared/{token}">{link}</a></span>'
    )


@router.get("/shared/{token}", response_class=HTMLResponse)
async def view_shared(request: Request, token: str):
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT * FROM share_links WHERE token = %s", (token,)
        )
        link = await cur.fetchone()

    if not link:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)

    if link["expires_at"] and link["expires_at"] < datetime.now(timezone.utc):
        return HTMLResponse("<h1>This share link has expired</h1>", status_code=410)

    entry = await miniflux_client.get_entry(link["entry_id"])

    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT content_html FROM article_snapshots WHERE entry_id = %s ORDER BY version DESC LIMIT 1",
            (link["entry_id"],),
        )
        snapshot = await cur.fetchone()

    content = snapshot["content_html"] if snapshot else entry.get("content", "")

    return templates.TemplateResponse(
        request, "shared.html",
        {"entry": entry, "content": content},
    )
