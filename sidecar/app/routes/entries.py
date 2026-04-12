import difflib
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import miniflux_client
from app.db import get_conn
from app.extractor import fetch_and_extract
from app.routes.cookies import get_cookies_for_url
from app.templating import templates

logger = logging.getLogger(__name__)

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


async def _entry_tags(conn, entry_ids: list[int]) -> dict[int, list[str]]:
    """Get LLM-generated tags for a list of entries."""
    if not entry_ids:
        return {}
    cur = await conn.execute(
        "SELECT entry_id, tag FROM article_tags WHERE entry_id = ANY(%s)",
        (entry_ids,),
    )
    result: dict[int, list[str]] = {}
    for row in await cur.fetchall():
        result.setdefault(row["entry_id"], []).append(row["tag"])
    return result


async def _entries_with_changes(conn) -> set[int]:
    """Get entry IDs that have more than one snapshot version."""
    cur = await conn.execute(
        "SELECT entry_id FROM article_snapshots GROUP BY entry_id HAVING COUNT(*) > 1"
    )
    return {row["entry_id"] for row in await cur.fetchall()}


def _time_filter_params(time_filter: str | None) -> dict[str, str]:
    """Convert a time filter name to after/before timestamps."""
    if not time_filter:
        return {}
    now = datetime.now(timezone.utc)
    if time_filter == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_filter == "24h":
        start = now - timedelta(hours=24)
    elif time_filter == "week":
        start = now - timedelta(days=7)
    else:
        return {}
    return {"after": str(int(start.timestamp()))}


@router.get("/entries", response_class=HTMLResponse)
async def entry_list(
    request: Request,
    feed_id: int | None = None,
    status: str | None = None,
    offset: int = 0,
    search: str | None = None,
    starred: bool = False,
    category_id: int | None = None,
    time_filter: str | None = None,
    tag: list[str] = Query(default=[]),
    changed: bool = False,
):
    limit = 50
    show_all = status == "all"
    if show_all:
        status = None
    elif status is None:
        status = "unread"
    time_params = _time_filter_params(time_filter)

    feed = None
    if feed_id:
        feed = await miniflux_client.get_feed(feed_id)
        data = await miniflux_client.get_entries(
            feed_id=feed_id, status=status, limit=limit, offset=offset,
            search=search, starred=starred, **time_params,
        )
        entries = data.get("entries", [])
        total = data.get("total", 0)
    else:
        data = await miniflux_client.get_entries(
            status=status, limit=200, offset=offset,
            search=search, starred=starred, category_id=category_id,
            **time_params,
        )
        all_entries = data.get("entries", [])
        total = data.get("total", 0)

        async with get_conn() as conn:
            priorities = await _feed_priorities(conn)
        for entry in all_entries:
            entry["_priority"] = priorities.get(entry.get("feed_id"), 2)

        from itertools import groupby
        all_entries.sort(key=lambda e: (
            e["_priority"],
            "".join(c for c in (e.get("published_at") or "") if c not in ":-TZ"),
        ))
        entries = []
        for _, group in groupby(all_entries, key=lambda e: e["_priority"]):
            tier = list(group)
            tier.sort(key=lambda e: e.get("published_at", ""), reverse=True)
            entries.extend(tier)
        entries = entries[:limit]

    # Enrich with tags and change indicators
    entry_ids = [e["id"] for e in entries]
    async with get_conn() as conn:
        tags_map = await _entry_tags(conn, entry_ids)
        changed_ids = await _entries_with_changes(conn) if changed or True else set()

    for entry in entries:
        entry["_tags"] = tags_map.get(entry["id"], [])
        entry["_has_changes"] = entry["id"] in changed_ids

    # Filter by tag if requested
    if tag:
        tag_set = set(tag)
        entries = [e for e in entries if tag_set & set(e["_tags"])]

    # Filter to changed-only if requested
    if changed:
        entries = [e for e in entries if e["_has_changes"]]

    # Gather all unique tags for the tag cloud
    all_tags = sorted({t for tags in tags_map.values() for t in tags})

    return templates.TemplateResponse(
        request,
        "entries.html",
        {
            "entries": entries,
            "feed": feed,
            "feed_id": feed_id,
            "status": status,
            "show_all": show_all,
            "offset": offset,
            "limit": limit,
            "total": total,
            "search": search or "",
            "starred": starred,
            "category_id": category_id,
            "time_filter": time_filter or "",
            "tag": tag,
            "changed": changed,
            "all_tags": all_tags,
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
        # Record read event
        await conn.execute(
            "INSERT INTO read_events (entry_id, feed_id) VALUES (%s, %s)",
            (entry_id, entry.get("feed_id", 0)),
        )
        await conn.commit()
    async with get_conn() as conn:
        # Get tags
        cur = await conn.execute(
            "SELECT tag FROM article_tags WHERE entry_id = %s", (entry_id,)
        )
        llm_tags = [row["tag"] for row in await cur.fetchall()]
        # Get summary from snapshot metadata
        summary = (snapshot.get("metadata") or {}).get("summary") if snapshot else None
        # Check for similar articles via embeddings
        similar = []
        cur2 = await conn.execute(
            "SELECT 1 FROM article_embeddings WHERE entry_id = %s", (entry_id,)
        )
        if await cur2.fetchone():
            from app.llm import find_similar
            cur3 = await conn.execute(
                "SELECT embedding FROM article_embeddings WHERE entry_id = %s", (entry_id,)
            )
            emb_row = await cur3.fetchone()
            if emb_row:
                similar = await find_similar(conn, entry_id, emb_row["embedding"])

    # Detect audio enclosures for podcast player
    enclosures = entry.get("enclosures") or []
    audio_enclosure = next(
        (e for e in enclosures if (e.get("mime_type") or "").startswith("audio/")),
        None,
    )

    # Determine prev/next entries in the same feed for swipe navigation
    prev_entry_id = None
    next_entry_id = None
    pub = entry.get("published_at", "")
    feed_id = entry.get("feed_id")
    if pub and feed_id:
        pub_ts = str(int(datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()))
        newer = await miniflux_client.get_entries(
            feed_id=feed_id, after=pub_ts, direction="asc", limit=1,
        )
        older = await miniflux_client.get_entries(
            feed_id=feed_id, before=pub_ts, direction="desc", limit=1,
        )
        prev_entries = newer.get("entries") or []
        next_entries = older.get("entries") or []
        if prev_entries:
            prev_entry_id = prev_entries[0]["id"]
        if next_entries:
            next_entry_id = next_entries[0]["id"]

    return templates.TemplateResponse(
        request,
        "entry.html",
        {
            "entry": entry,
            "snapshot": snapshot,
            "version_count": vc,
            "llm_tags": llm_tags,
            "summary": summary,
            "audio_enclosure": audio_enclosure,
            "similar": similar,
            "prev_entry_id": prev_entry_id,
            "next_entry_id": next_entry_id,
        },
    )


@router.post("/entries/{entry_id}/generate-summary")
async def generate_summary(request: Request, entry_id: int):
    """Return SSE-connected fragment to kick off streaming summarization."""
    async with get_conn() as conn:
        snapshot = await _get_snapshot(conn, entry_id)
        if not snapshot:
            return HTMLResponse('<span class="text-danger">No full-text content available</span>')
        text = snapshot.get("content_text") or ""
        if not text:
            return HTMLResponse('<span class="text-danger">No text content to summarize</span>')

    return templates.TemplateResponse(request, "summary_stream.html", {"entry_id": entry_id})


@router.get("/entries/{entry_id}/summary-stream")
async def summary_stream(entry_id: int):
    """SSE endpoint that generates and streams summary tokens."""
    from html import escape
    from fastapi.responses import StreamingResponse
    from app.llm import _ollama_generate_stream, SUMMARIZE_SYSTEM

    async with get_conn() as conn:
        snapshot = await _get_snapshot(conn, entry_id)
        if not snapshot:
            return HTMLResponse("")
        text = snapshot.get("content_text") or ""
        if not text:
            return HTMLResponse("")
        version = snapshot["version"]

    truncated = " ".join(text.split()[:4000])

    async def sse():
        full = []
        try:
            async for token in _ollama_generate_stream(truncated, SUMMARIZE_SYSTEM):
                full.append(token)
                yield f"event: token\ndata: <span>{escape(token)}</span>\n\n"
        except Exception as exc:
            logger.exception("Summary stream failed for entry %s", entry_id)
            msg = escape(f"Summarization failed: {exc}")
            yield f'event: done\ndata: <span class="text-danger">{msg}</span>\n\n'
            return

        summary_text = "".join(full).strip()
        if summary_text:
            import psycopg.types.json
            async with get_conn() as conn:
                await conn.execute(
                    "UPDATE article_snapshots SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb "
                    "WHERE entry_id = %s AND version = %s",
                    (psycopg.types.json.Json({"summary": summary_text}), entry_id, version),
                )
                await conn.commit()

        done_html = (
            '<details class="bg-surface border border-border rounded-lg px-5 py-4 mb-6" open>'
            '<summary class="cursor-pointer font-semibold text-accent text-[0.8rem] uppercase tracking-wide">AI Summary</summary>'
            f'<p class="mt-2 text-[0.95rem] leading-relaxed">{escape(summary_text)}</p>'
            '</details>'
        )
        yield f"event: done\ndata: {done_html}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _render_content_block(entry_id: int, snapshot: dict, version_count: int, message: str | None = None) -> str:
    """Render the badge + article HTML for the #entry-content swap."""
    from html import escape
    fetched = snapshot["fetched_at"].strftime("%Y-%m-%d %H:%M") if snapshot.get("fetched_at") else "unknown"
    diff_link = (
        f' &middot; <a href="/entries/{entry_id}/diff" class="text-white underline">'
        f'{version_count} versions — view changes</a>'
    ) if version_count > 1 else ""
    msg_part = f' &middot; <span class="font-normal">{escape(message)}</span>' if message else ""
    badge = (
        f'<div class="block w-fit px-3 py-0.5 text-xs rounded-lg mb-5 bg-accent text-white cursor-pointer"'
        f' hx-post="/entries/{entry_id}/fetch-full" hx-target="#entry-content" hx-swap="innerHTML"'
        f' hx-indicator="#loading" title="Click to refetch">'
        f'Full article v{snapshot["version"]} (fetched {fetched}){diff_link}{msg_part}</div>'
    )
    return badge + (snapshot.get("content_html") or snapshot.get("content_text") or "")


@router.post("/entries/{entry_id}/fetch-full")
async def fetch_full_content(entry_id: int):
    """On-demand fetch of full article content, creating a new version if content changed."""
    entry = await miniflux_client.get_entry(entry_id)
    url = entry.get("url", "")
    if not url:
        return HTMLResponse('<span class="text-danger text-detail">No URL for entry</span>')

    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT extract_rules FROM feed_config WHERE feed_id = %s",
            (entry.get("feed_id"),),
        )
        row = await cur.fetchone()
        extract_rules = (row["extract_rules"] if row else None) or {}

    cookies = await get_cookies_for_url(url)
    extracted = await fetch_and_extract(url, extract_rules, cookies=cookies)
    if not extracted:
        return HTMLResponse('<span class="text-danger text-detail">Extraction failed — no content found</span>')

    import hashlib
    import psycopg.types.json

    source_hash = hashlib.sha256(entry.get("content", "").encode()).hexdigest()

    async with get_conn() as conn:
        latest = await _get_snapshot(conn, entry_id)
        if latest and latest["content_hash"] == extracted["content_hash"]:
            vc = await _version_count(conn, entry_id)
            return HTMLResponse(_render_content_block(entry_id, latest, vc, "No changes detected"))

        next_version = (latest["version"] + 1) if latest else 1
        await conn.execute(
            """
            INSERT INTO article_snapshots
                (entry_id, feed_id, url, content_text, content_html, content_hash, metadata, version, source_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
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
                source_hash,
            ),
        )
        await conn.commit()
        snapshot = await _get_snapshot(conn, entry_id)
        vc = await _version_count(conn, entry_id)

    return HTMLResponse(_render_content_block(entry_id, snapshot, vc))


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


@router.post("/entries/{entry_id}/toggle-star")
async def toggle_star(entry_id: int):
    await miniflux_client.toggle_bookmark(entry_id)
    # Miniflux toggles, so we re-fetch to get current state
    entry = await miniflux_client.get_entry(entry_id)
    starred = entry.get("starred", False)
    cls = "starred" if starred else ""
    label = "&#9733; Starred" if starred else "&#9734; Star"
    return HTMLResponse(
        f'<button hx-post="/entries/{entry_id}/toggle-star" hx-swap="outerHTML" class="star-btn {cls}">{label}</button>'
    )


@router.post("/entries/mark-all-read")
async def mark_all_read(request: Request):
    """Mark all visible entries as read. Accepts JSON body with entry_ids."""
    body = await request.json()
    entry_ids = body.get("entry_ids", [])
    if entry_ids:
        await miniflux_client.update_entry_status(entry_ids, "read")
    return JSONResponse({"ok": True, "count": len(entry_ids)})


@router.get("/entries/{entry_id}/export-md")
async def export_markdown(entry_id: int):
    """Export entry as Markdown file with YAML frontmatter."""
    from markdownify import markdownify as md

    entry = await miniflux_client.get_entry(entry_id)
    async with get_conn() as conn:
        snapshot = await _get_snapshot(conn, entry_id)
        cur = await conn.execute(
            "SELECT tag FROM article_tags WHERE entry_id = %s", (entry_id,)
        )
        tags = [row["tag"] for row in await cur.fetchall()]

    content_html = (snapshot["content_html"] if snapshot else entry.get("content", ""))
    content_md = md(content_html, heading_style="ATX", strip=["script", "style"])

    feed_title = entry.get("feed", {}).get("title", "")
    published = (entry.get("published_at") or "")[:10]
    title = entry.get("title", "Untitled")

    frontmatter = f"""---
title: "{title.replace('"', '\\"')}"
author: "{entry.get('author', '')}"
url: "{entry.get('url', '')}"
feed: "{feed_title}"
date: "{published}"
tags: [{', '.join(tags)}]
---

"""
    filename = "".join(c if c.isalnum() or c in " -_" else "" for c in title)[:80] + ".md"

    return HTMLResponse(
        content=frontmatter + content_md,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/markdown; charset=utf-8",
        },
    )


@router.get("/api/new-count")
async def new_count(since: int = 0):
    """Return count of unread entries, optionally since a timestamp."""
    params = {"status": "unread", "limit": 0}
    if since:
        params["after"] = str(since)
    data = await miniflux_client.get_entries(status="unread", limit=1)
    return JSONResponse({"count": data.get("total", 0)})
