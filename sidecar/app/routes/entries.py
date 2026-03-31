from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import miniflux_client
from app.db import get_conn
from app.extractor import fetch_and_extract
from app.templating import templates

router = APIRouter()


async def _get_snapshot(conn, entry_id: int) -> dict | None:
    cur = await conn.execute(
        "SELECT * FROM article_snapshots WHERE entry_id = %s ORDER BY version DESC LIMIT 1",
        (entry_id,),
    )
    return await cur.fetchone()


async def _feed_priorities(conn) -> dict[int, int]:
    cur = await conn.execute("SELECT feed_id, priority FROM feed_config")
    return {row["feed_id"]: row["priority"] for row in await cur.fetchall()}


@router.get("/entries", response_class=HTMLResponse)
async def entry_list(
    request: Request,
    feed_id: int | None = None,
    status: str | None = None,
    offset: int = 0,
):
    limit = 50

    # If viewing a specific feed, no priority sorting needed
    if feed_id:
        data = await miniflux_client.get_entries(
            feed_id=feed_id, status=status, limit=limit, offset=offset
        )
        entries = data.get("entries", [])
        total = data.get("total", 0)
    else:
        # Fetch a larger batch and sort by priority then date
        data = await miniflux_client.get_entries(
            status=status, limit=200, offset=offset
        )
        all_entries = data.get("entries", [])
        total = data.get("total", 0)

        async with get_conn() as conn:
            priorities = await _feed_priorities(conn)

        for entry in all_entries:
            entry["_priority"] = priorities.get(entry.get("feed_id"), 2)

        # Sort: priority ascending, then newest first within tier
        all_entries.sort(key=lambda e: (
            e["_priority"],
            "".join(c for c in (e.get("published_at") or "") if c not in ":-TZ"),
        ))
        # Reverse date within each priority group
        from itertools import groupby
        entries = []
        for _, group in groupby(all_entries, key=lambda e: e["_priority"]):
            tier = list(group)
            tier.sort(key=lambda e: e.get("published_at", ""), reverse=True)
            entries.extend(tier)

        entries = entries[:limit]

    return templates.TemplateResponse(
        request,
        "entries.html",
        {
            "entries": entries,
            "feed_id": feed_id,
            "status": status,
            "offset": offset,
            "limit": limit,
            "total": total,
        },
    )


@router.get("/entries/{entry_id}", response_class=HTMLResponse)
async def entry_detail(request: Request, entry_id: int):
    entry = await miniflux_client.get_entry(entry_id)
    if entry.get("status") == "unread":
        await miniflux_client.update_entry_status([entry_id], "read")
        entry["status"] = "read"
    async with get_conn() as conn:
        snapshot = await _get_snapshot(conn, entry_id)
    return templates.TemplateResponse(
        request,
        "entry.html",
        {"entry": entry, "snapshot": snapshot},
    )


@router.post("/entries/{entry_id}/fetch-full")
async def fetch_full_content(entry_id: int):
    """On-demand fetch of full article content for a single entry."""
    entry = await miniflux_client.get_entry(entry_id)
    url = entry.get("url", "")
    if not url:
        return HTMLResponse('<span class="error">No URL for entry</span>')

    extracted = await fetch_and_extract(url)
    if not extracted:
        return HTMLResponse('<span class="error">Extraction failed</span>')

    async with get_conn() as conn:
        import psycopg.types.json

        await conn.execute(
            """
            INSERT INTO article_snapshots
                (entry_id, feed_id, url, content_text, content_html, content_hash, metadata, version)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 1)
            ON CONFLICT DO NOTHING
            """,
            (
                entry_id,
                entry["feed_id"],
                url,
                extracted["content_text"],
                extracted["content_html"],
                extracted["content_hash"],
                psycopg.types.json.Json(extracted["metadata"]),
            ),
        )
        await conn.commit()
    return HTMLResponse('<span class="success">Full article fetched — reload to view</span>')


@router.post("/entries/{entry_id}/mark-read")
async def mark_read(entry_id: int):
    await miniflux_client.update_entry_status([entry_id], "read")
    return HTMLResponse(
        f'<button hx-post="/entries/{entry_id}/mark-unread" hx-swap="outerHTML">Mark unread</button>'
    )


@router.post("/entries/{entry_id}/mark-unread")
async def mark_unread(entry_id: int):
    await miniflux_client.update_entry_status([entry_id], "unread")
    return HTMLResponse(
        f'<button hx-post="/entries/{entry_id}/mark-read" hx-swap="outerHTML">Mark read</button>'
    )
