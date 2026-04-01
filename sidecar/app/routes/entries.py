import difflib

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


async def _version_count(conn, entry_id: int) -> int:
    cur = await conn.execute(
        "SELECT COUNT(*) AS cnt FROM article_snapshots WHERE entry_id = %s",
        (entry_id,),
    )
    row = await cur.fetchone()
    return row["cnt"] if row else 0


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

    feed = None
    # If viewing a specific feed, no priority sorting needed
    if feed_id:
        feed = await miniflux_client.get_feed(feed_id)
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
            "feed": feed,
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
        vc = await _version_count(conn, entry_id) if snapshot else 0
    return templates.TemplateResponse(
        request,
        "entry.html",
        {"entry": entry, "snapshot": snapshot, "version_count": vc},
    )


@router.post("/entries/{entry_id}/fetch-full")
async def fetch_full_content(entry_id: int):
    """On-demand fetch of full article content, creating a new version if content changed."""
    entry = await miniflux_client.get_entry(entry_id)
    url = entry.get("url", "")
    if not url:
        return HTMLResponse('<span class="error">No URL for entry</span>')

    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT extract_rules FROM feed_config WHERE feed_id = %s",
            (entry.get("feed_id"),),
        )
        row = await cur.fetchone()
        extract_rules = (row["extract_rules"] if row else None) or {}

    extracted = await fetch_and_extract(url, extract_rules)
    if not extracted:
        return HTMLResponse('<span class="error">Extraction failed — no content found</span>')

    import psycopg.types.json

    async with get_conn() as conn:
        # Check if we already have this exact content
        latest = await _get_snapshot(conn, entry_id)
        if latest and latest["content_hash"] == extracted["content_hash"]:
            return HTMLResponse('<span class="success">No changes detected</span>')

        next_version = (latest["version"] + 1) if latest else 1
        await conn.execute(
            """
            INSERT INTO article_snapshots
                (entry_id, feed_id, url, content_text, content_html, content_hash, metadata, version)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                entry_id,
                entry["feed_id"],
                url,
                extracted["content_text"],
                extracted["content_html"],
                extracted["content_hash"],
                psycopg.types.json.Json(extracted["metadata"]),
                next_version,
            ),
        )
        await conn.commit()
    label = "Full article fetched" if next_version == 1 else f"Updated to v{next_version}"
    return HTMLResponse(f'<span class="success">{label} — reload to view</span>')


@router.get("/entries/{entry_id}/diff", response_class=HTMLResponse)
async def entry_diff(request: Request, entry_id: int):
    """Show content changes across snapshot versions."""
    entry = await miniflux_client.get_entry(entry_id)
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT version, fetched_at, content_text FROM article_snapshots "
            "WHERE entry_id = %s ORDER BY version",
            (entry_id,),
        )
        snapshots = await cur.fetchall()

    diffs = []
    for prev, curr in zip(snapshots, snapshots[1:]):
        diff_lines = list(difflib.unified_diff(
            (prev["content_text"] or "").splitlines(keepends=True),
            (curr["content_text"] or "").splitlines(keepends=True),
            fromfile=f"v{prev['version']} ({prev['fetched_at'].strftime('%Y-%m-%d %H:%M')})",
            tofile=f"v{curr['version']} ({curr['fetched_at'].strftime('%Y-%m-%d %H:%M')})",
        ))
        diffs.append({
            "from_version": prev["version"],
            "to_version": curr["version"],
            "lines": diff_lines,
        })

    return templates.TemplateResponse(
        request,
        "diff.html",
        {"entry": entry, "diffs": diffs, "version_count": len(snapshots)},
    )


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
