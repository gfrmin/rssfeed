import asyncio
import difflib
import logging
import re
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Form, Query, Request
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


async def _list_prompts(conn) -> list[dict]:
    """Return all saved summary prompts (built-ins first, then custom by name)."""
    cur = await conn.execute(
        "SELECT id, name, system_prompt, is_builtin FROM summary_prompts "
        "ORDER BY is_builtin DESC, name ASC"
    )
    return [dict(row) for row in await cur.fetchall()]


def _extract_summaries(metadata: dict | None) -> dict[str, str]:
    """Read summaries dict, falling back to legacy single-summary field under the 'default' key."""
    if not metadata:
        return {}
    out = dict(metadata.get("summaries") or {})
    legacy = metadata.get("summary")
    if legacy and "default" not in out:
        out["default"] = legacy
    return out


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s[:50] or "custom"


async def _unique_slug(conn, base: str) -> str:
    slug = base
    suffix = 2
    while True:
        cur = await conn.execute(
            "SELECT 1 FROM summary_prompts WHERE id = %s", (slug,),
        )
        if await cur.fetchone() is None:
            return slug
        slug = f"{base}_{suffix}"
        suffix += 1


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


# ============================================================
#  Three-pane shell: sidebar context + list enrichment
# ============================================================

TIERS = [(1, "Must read"), (2, "Normal"), (3, "Low priority")]

VIEW_TITLES = {
    "unread": "Unread", "all": "All articles", "starred": "Starred",
    "ranked": "Top ranked", "changed": "Changed",
    "t:today": "Today", "t:24h": "Last 24 hours", "t:week": "This week",
}

_STALE_SECONDS = 24 * 3600


def _feed_health_state(feed: dict, now: datetime) -> str:
    """Collapse Miniflux feed state into a dot state: ok/stale/error/paused."""
    if feed.get("disabled"):
        return "paused"
    if feed.get("parsing_error_message") or (feed.get("parsing_error_count") or 0) >= 1:
        return "error"
    checked = feed.get("checked_at", "")
    if checked:
        try:
            dt = datetime.fromisoformat(checked.replace("Z", "+00:00"))
            if (now - dt).total_seconds() > _STALE_SECONDS:
                return "stale"
        except Exception:
            pass
    return "ok"


def _rank_score(entry: dict, now: datetime) -> int:
    """Derived relevance score (0-100) from priority tier + recency + signals.
    No stored score exists; this gives the rank meter a sortable value."""
    score = {1: 84, 2: 56, 3: 30}.get(entry.get("_priority", 2), 50)
    pub = entry.get("published_at") or ""
    try:
        dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        hrs = (now - dt).total_seconds() / 3600
        if hrs < 24:
            score += 12
        elif hrs < 72:
            score += 7
        elif hrs < 168:
            score += 3
    except Exception:
        pass
    if entry.get("_has_changes"):
        score += 5
    if entry.get("starred"):
        score += 4
    return max(0, min(100, score))


async def _snapshot_versions(conn, entry_ids: list[int]) -> dict[int, dict]:
    """Map entry_id -> {version, count} for entries that have full-text snapshots."""
    if not entry_ids:
        return {}
    cur = await conn.execute(
        "SELECT entry_id, MAX(version) AS v, COUNT(*) AS c "
        "FROM article_snapshots WHERE entry_id = ANY(%s) GROUP BY entry_id",
        (entry_ids,),
    )
    return {r["entry_id"]: {"version": r["v"], "count": r["c"]} for r in await cur.fetchall()}


def _audio_enclosure(entry: dict) -> dict | None:
    for enc in entry.get("enclosures") or []:
        if (enc.get("mime_type") or "").startswith("audio"):
            return enc
    return None


async def _enrich_entries(conn, entries: list[dict], now: datetime) -> None:
    """Attach _tags, _has_changes, _rank, _full, _audio to each entry in place."""
    entry_ids = [e["id"] for e in entries]
    tags_map = await _entry_tags(conn, entry_ids)
    changed_ids = await _entries_with_changes(conn)
    snaps = await _snapshot_versions(conn, entry_ids)
    priorities = await _feed_priorities(conn)
    for e in entries:
        e["_tags"] = tags_map.get(e["id"], [])
        e["_mtags"] = e.get("tags") or []
        e["_has_changes"] = e["id"] in changed_ids
        e["_priority"] = priorities.get(e.get("feed_id"), 2)
        e["_full"] = snaps.get(e["id"])
        e["_audio"] = _audio_enclosure(e)
        e["_rank"] = _rank_score(e, now)


def _active_view(*, feed_id, view, starred, changed, time_filter, sort, show_all) -> str | None:
    if feed_id:
        return None
    if view in VIEW_TITLES:
        return view
    if starred:
        return "starred"
    if changed:
        return "changed"
    if time_filter in ("today", "24h", "week"):
        return "t:" + time_filter
    if sort == "rank" and not show_all:
        return "ranked"
    if show_all:
        return "all"
    return "unread"


async def build_sidebar(*, active_view: str | None, active_feed_id: int | None,
                        group_by: str = "tier") -> dict:
    """Assemble the persistent left-rail context (feeds by tier, counts, health)."""
    now = datetime.now(timezone.utc)
    feeds, counters, categories, starred_data = await asyncio.gather(
        miniflux_client.get_feeds(),
        miniflux_client.get_feed_counters(),
        miniflux_client.get_categories(),
        miniflux_client.get_entries(starred=True, limit=1),
    )
    unreads = counters.get("unreads", {}) or {}
    reads = counters.get("reads", {}) or {}

    async with get_conn() as conn:
        configs = await _feed_configs_full(conn)
        changed_ids = await _entries_with_changes(conn)
        searches, search_counts = await _saved_searches_with_counts(conn, unreads)

    health_alert = 0
    for f in feeds:
        cfg = configs.get(f["id"], {})
        f["_priority"] = cfg.get("priority", 2)
        f["_proxy"] = bool(cfg.get("fetch_full_content"))
        f["_health"] = _feed_health_state(f, now)
        f["_unread"] = unreads.get(str(f["id"]), 0)
        f["_cat"] = (f.get("category") or {}).get("id")
        if f["_health"] == "error":
            health_alert += 1

    if group_by == "cat":
        groups = [
            {"key": c["id"], "label": c.get("title", "—"),
             "items": [f for f in feeds if f["_cat"] == c["id"]]}
            for c in categories
        ]
    else:
        groups = [
            {"key": p, "label": label,
             "items": sorted([f for f in feeds if f["_priority"] == p],
                             key=lambda f: f.get("title", "").lower())}
            for p, label in TIERS
        ]
    groups = [g for g in groups if g["items"]]

    unread_total = sum(int(v) for v in unreads.values())
    all_total = unread_total + sum(int(v) for v in reads.values())
    counts = {
        "unread": unread_total,
        "all": all_total,
        "starred": starred_data.get("total", 0),
        "changed": len(changed_ids),
    }

    return {
        "groups": groups,
        "group_by": group_by,
        "counts": counts,
        "view": active_view,
        "view_title": VIEW_TITLES.get(active_view, "Articles") if active_view else "Feed",
        "feed_id": active_feed_id,
        "health_alert": health_alert,
        "searches": searches,
        "search_counts": search_counts,
    }


async def _feed_configs_full(conn) -> dict[int, dict]:
    cur = await conn.execute(
        "SELECT feed_id, fetch_full_content, priority FROM feed_config"
    )
    return {row["feed_id"]: row for row in await cur.fetchall()}


async def _saved_searches_with_counts(conn, unreads) -> tuple[list[dict], dict]:
    """Saved searches + live unread counts. Returns ([], {}) until Phase 4 adds the table."""
    try:
        cur = await conn.execute(
            "SELECT id, name, icon, query, tags, view FROM saved_searches ORDER BY created_at"
        )
        rows = [dict(r) for r in await cur.fetchall()]
    except Exception:
        return [], {}
    # Counts are computed in Phase 4 (match_search over the unread set); 0 for now.
    return rows, {r["id"]: 0 for r in rows}


def _wants_list_fragment(request: Request) -> bool:
    """True when HTMX is swapping just the list pane (sidebar reloads out-of-band)."""
    return request.headers.get("HX-Request") == "true" and \
        request.headers.get("HX-Target") in ("list-col", None)


@router.get("/entries", response_class=HTMLResponse)
async def entry_list(
    request: Request,
    feed_id: int | None = None,
    view: str | None = None,
    status: str | None = None,
    offset: int = 0,
    search: str | None = None,
    starred: bool = False,
    category_id: int | None = None,
    time_filter: str | None = None,
    sort: str | None = None,
    tag: list[str] = Query(default=[]),
    changed: bool = False,
    group_by: str = "tier",
):
    limit = 50
    now = datetime.now(timezone.utc)

    # Normalize the smart-view selector (sidebar uses ?view=…; legacy params still work).
    if view == "starred":
        starred = True
    elif view == "changed":
        changed = True
    elif view == "ranked":
        sort = "rank"
        status = "unread"
    elif view == "all":
        status = "all"
    elif view and view.startswith("t:"):
        time_filter = view[2:]
    elif view == "unread":
        status = "unread"

    show_all = status == "all"
    if show_all:
        status = None
    elif status is None and not starred:
        status = "unread"
    sort = sort or "rank"
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
        entries = data.get("entries", [])
        total = data.get("total", 0)

    async with get_conn() as conn:
        await _enrich_entries(conn, entries, now)

    # Tag filter (client-side, against LLM + manual tags)
    if tag:
        tag_set = set(tag)
        entries = [e for e in entries if tag_set & (set(e["_tags"]) | set(e["_mtags"]))]
    if changed:
        entries = [e for e in entries if e["_has_changes"]]

    # Ordering
    if sort == "newest":
        entries.sort(key=lambda e: e.get("published_at") or "", reverse=True)
    else:  # rank: priority tier then recency
        entries.sort(key=lambda e: e.get("published_at") or "", reverse=True)
        entries.sort(key=lambda e: (e.get("_priority", 2), -e.get("_rank", 0)))
    if not feed_id:
        entries = entries[:limit]

    all_tags = sorted({t for e in entries for t in (e["_tags"] + e["_mtags"])})

    active_view = _active_view(
        feed_id=feed_id, view=view, starred=starred, changed=changed,
        time_filter=time_filter, sort=sort, show_all=show_all,
    )
    sidebar = await build_sidebar(
        active_view=active_view, active_feed_id=feed_id, group_by=group_by,
    )

    ctx = {
        "entries": entries,
        "feed": feed,
        "feed_id": feed_id,
        "view": active_view,
        "status": status,
        "show_all": show_all,
        "offset": offset,
        "limit": limit,
        "total": total,
        "search": search or "",
        "starred": starred,
        "category_id": category_id,
        "time_filter": time_filter or "",
        "sort": sort,
        "tag": tag,
        "changed": changed,
        "all_tags": all_tags,
        "title": (feed.get("title") if feed else VIEW_TITLES.get(active_view, "Articles")),
        "sidebar": sidebar,
    }
    template = "entries_fragment.html" if _wants_list_fragment(request) else "entries.html"
    return templates.TemplateResponse(request, template, ctx)


@router.get("/triage", response_class=HTMLResponse)
async def triage(request: Request):
    """Card-by-card walk through unread, ranked. The deck is snapshotted
    server-side so marking-read doesn't shift it underfoot."""
    now = datetime.now(timezone.utc)
    data = await miniflux_client.get_entries(
        status="unread", limit=200, direction="desc", order="published_at",
    )
    entries = data.get("entries", [])
    async with get_conn() as conn:
        await _enrich_entries(conn, entries, now)
    entries.sort(key=lambda e: e.get("published_at") or "", reverse=True)
    entries.sort(key=lambda e: (e.get("_priority", 2), -e.get("_rank", 0)))
    entries = entries[:40]
    for e in entries:
        f = e.get("feed")
        if f:
            f["_health"] = _feed_health_state(f, now)
    return templates.TemplateResponse(request, "_triage.html", {"queue": entries})


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
        # Get summaries (keyed by prompt slug) and prompt library
        summaries = _extract_summaries(snapshot.get("metadata") if snapshot else None)
        prompts = await _list_prompts(conn)
        prompt_names = {p["id"]: p["name"] for p in prompts}
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

    content_block = _content_block_ctx(
        entry_id, snapshot, vc, rss_html=(entry.get("content") or ""),
    )
    ctx = {
        "entry": entry,
        "snapshot": snapshot,
        "version_count": vc,
        "llm_tags": llm_tags,
        "summaries": summaries,
        "prompts": prompts,
        "prompt_names": prompt_names,
        "audio_enclosure": audio_enclosure,
        "similar": similar,
        "prev_entry_id": prev_entry_id,
        "next_entry_id": next_entry_id,
        "content_block": content_block,
        "title": entry.get("title", "Article"),
    }
    # HTMX row-click loads only the reader pane; a direct visit renders the full shell.
    if request.headers.get("HX-Target") == "reader-col":
        return templates.TemplateResponse(request, "_reader.html", ctx)
    ctx["sidebar"] = await build_sidebar(
        active_view=None, active_feed_id=entry.get("feed_id"),
    )
    return templates.TemplateResponse(request, "entry.html", ctx)


@router.post("/entries/{entry_id}/generate-summary")
async def generate_summary(
    request: Request,
    entry_id: int,
    prompt_id: str = Form(""),
    inline_prompt: str = Form(""),
    save_as: str = Form(""),
):
    """Kick off streaming summarisation.

    Form accepts either `prompt_id` (use a saved prompt) OR `inline_prompt` (ad-hoc text).
    If `save_as` is set alongside `inline_prompt`, the prompt is persisted to the library first.
    """
    async with get_conn() as conn:
        snapshot = await _get_snapshot(conn, entry_id)
        if not snapshot:
            return HTMLResponse('<span class="text-danger">No full-text content available</span>')
        text = snapshot.get("content_text") or ""
        if not text:
            return HTMLResponse('<span class="text-danger">No text content to summarize</span>')

        resolved_prompt_id: str | None = None
        prompt_label = "AI Summary"

        if prompt_id:
            cur = await conn.execute(
                "SELECT id, name FROM summary_prompts WHERE id = %s", (prompt_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return HTMLResponse('<span class="text-danger">Unknown prompt</span>')
            resolved_prompt_id = row["id"]
            prompt_label = row["name"]
        elif inline_prompt.strip():
            if save_as.strip():
                slug = await _unique_slug(conn, _slugify(save_as))
                await conn.execute(
                    "INSERT INTO summary_prompts (id, name, system_prompt, is_builtin) "
                    "VALUES (%s, %s, %s, FALSE)",
                    (slug, save_as.strip(), inline_prompt.strip()),
                )
                await conn.commit()
                resolved_prompt_id = slug
                prompt_label = save_as.strip()
            else:
                prompt_label = "Custom (one-off)"
        else:
            return HTMLResponse('<span class="text-danger">Pick a prompt or write a custom one</span>')

    return templates.TemplateResponse(
        request,
        "summary_stream.html",
        {
            "entry_id": entry_id,
            "prompt_id": resolved_prompt_id or "",
            "inline_prompt": "" if resolved_prompt_id else inline_prompt,
            "prompt_label": prompt_label,
        },
    )


@router.get("/entries/{entry_id}/summary-stream")
async def summary_stream(
    entry_id: int,
    prompt_id: str = "",
    inline_prompt: str = "",
):
    """SSE endpoint that generates and streams summary tokens for one prompt."""
    from html import escape
    from fastapi.responses import StreamingResponse
    from app.llm import _ollama_generate_stream

    async with get_conn() as conn:
        snapshot = await _get_snapshot(conn, entry_id)
        if not snapshot:
            return HTMLResponse("")
        text = snapshot.get("content_text") or ""
        if not text:
            return HTMLResponse("")
        version = snapshot["version"]

        prompt_label = "AI Summary"
        if prompt_id:
            cur = await conn.execute(
                "SELECT name, system_prompt FROM summary_prompts WHERE id = %s", (prompt_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return HTMLResponse("")
            system_prompt = row["system_prompt"]
            prompt_label = row["name"]
        elif inline_prompt.strip():
            system_prompt = inline_prompt.strip()
            prompt_label = "Custom (one-off)"
        else:
            return HTMLResponse("")

    truncated = " ".join(text.split()[:4000])

    async def sse():
        full = []
        try:
            async for token in _ollama_generate_stream(truncated, system_prompt):
                full.append(token)
                yield f"event: token\ndata: <span>{escape(token)}</span>\n\n"
        except Exception as exc:
            logger.exception("Summary stream failed for entry %s", entry_id)
            msg = escape(f"Summarization failed: {exc}")
            yield f'event: done\ndata: <span class="text-danger">{msg}</span>\n\n'
            return

        summary_text = "".join(full).strip()
        if summary_text and prompt_id:
            import psycopg.types.json
            async with get_conn() as conn:
                await conn.execute(
                    "UPDATE article_snapshots "
                    "SET metadata = jsonb_set("
                    "  COALESCE(metadata, '{}'::jsonb), "
                    "  '{summaries}', "
                    "  COALESCE(metadata->'summaries', '{}'::jsonb) || %s::jsonb, "
                    "  true"
                    ") "
                    "WHERE entry_id = %s AND version = %s",
                    (psycopg.types.json.Json({prompt_id: summary_text}), entry_id, version),
                )
                await conn.commit()

        from app.templating import _md
        rendered = str(_md(summary_text)).replace("\n", "")
        done_html = (
            '<details class="summary-card" open>'
            f'<summary><span class="sum-name">{escape(prompt_label)}</span><span class="sum-tag">Ollama</span></summary>'
            f'<div class="summary-body">{rendered}</div>'
            '</details>'
        )
        yield f"event: done\ndata: {done_html}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _content_block_ctx(entry_id: int, snapshot: dict | None, version_count: int,
                       message: str | None = None, rss_html: str = "") -> dict:
    """Build the `cb` context for _content_block.html (extraction bar + body)."""
    if snapshot:
        meta = snapshot.get("metadata") or {}
        return {
            "entry_id": entry_id,
            "has_full": True,
            "version": snapshot.get("version"),
            "fetched": snapshot["fetched_at"].strftime("%Y-%m-%d %H:%M") if snapshot.get("fetched_at") else "unknown",
            "version_count": version_count,
            "source": meta.get("source"),
            "message": message,
            "body_html": snapshot.get("content_html") or snapshot.get("content_text") or "",
        }
    return {"entry_id": entry_id, "has_full": False, "body_html": rss_html, "message": message}


def _render_content_block(entry_id: int, snapshot: dict, version_count: int, message: str | None = None) -> str:
    """Render the extraction bar + article HTML for the #entry-content swap."""
    cb = _content_block_ctx(entry_id, snapshot, version_count, message)
    return templates.env.get_template("_content_block.html").render(cb=cb)


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


def _structured_diff_lines(prev_text: str, curr_text: str) -> list[dict]:
    """unified_diff output → ``[{t, s}]`` with file/hunk headers stripped.

    ``t`` is one of ``" "`` (context), ``"+"`` (added), ``"-"`` (removed);
    ``s`` is the line text without the marker or trailing newline.
    """
    lines: list[dict] = []
    for raw in difflib.unified_diff(
        (prev_text or "").splitlines(),
        (curr_text or "").splitlines(),
        lineterm="",
    ):
        if raw[:3] in ("---", "+++") or raw.startswith("@@") or raw.startswith("\\"):
            continue
        t = raw[0] if raw and raw[0] in " +-" else " "
        lines.append({"t": t, "s": raw[1:]})
    return lines


def to_split_rows(lines: list[dict]) -> list[dict]:
    """Pair a unified line list into side-by-side rows for the split view.

    Faithful port of the prototype's ``toSplitRows`` (overlays.jsx): consecutive
    removed/added lines pair by index (``left``/``right`` go ``None`` when one
    side runs out → a ``.nil`` filler); context lines mirror to both sides.
    """
    rows: list[dict] = []
    dels: list[str] = []
    adds: list[str] = []

    def flush() -> None:
        for i in range(max(len(dels), len(adds))):
            rows.append({
                "left": dels[i] if i < len(dels) else None,
                "right": adds[i] if i < len(adds) else None,
                "ctx": False,
            })
        dels.clear()
        adds.clear()

    for line in lines:
        if line["t"] == "-":
            dels.append(line["s"])
        elif line["t"] == "+":
            adds.append(line["s"])
        else:
            flush()
            rows.append({"left": line["s"], "right": line["s"], "ctx": True})
    flush()
    return rows


@router.get("/entries/{entry_id}/diff", response_class=HTMLResponse)
async def entry_diff(request: Request, entry_id: int):
    """Content changes across snapshot versions, rendered as an overlay."""
    entry = await miniflux_client.get_entry(entry_id)
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT version, fetched_at, content_text FROM article_snapshots "
            "WHERE entry_id = %s ORDER BY version",
            (entry_id,),
        )
        snapshots = await cur.fetchall()

    sets = []
    for prev, curr in zip(snapshots, snapshots[1:]):
        lines = _structured_diff_lines(prev["content_text"], curr["content_text"])
        sets.append({
            "from": prev["version"],
            "to": curr["version"],
            "when": curr["fetched_at"].strftime("%Y-%m-%d %H:%M") if curr["fetched_at"] else "",
            "lines": lines,
            "split_rows": to_split_rows(lines),
        })

    ctx = {"entry": entry, "sets": sets, "version_count": len(snapshots)}
    template = "_diff_overlay.html" if request.headers.get("HX-Request") else "diff.html"
    return templates.TemplateResponse(request, template, ctx)


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
