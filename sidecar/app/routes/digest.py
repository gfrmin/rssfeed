from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import miniflux_client
from app.db import get_conn
from app.templating import templates

router = APIRouter()


@router.get("/digest", response_class=HTMLResponse)
async def daily_digest(request: Request):
    """Show today's top articles from high-priority feeds, with summaries if available."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    after_ts = str(int(today_start.timestamp()))

    # Get priority feeds
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT feed_id, priority FROM feed_config WHERE priority = 1"
        )
        priority_feeds = {row["feed_id"]: row["priority"] for row in await cur.fetchall()}

    # Fetch today's entries
    data = await miniflux_client.get_entries(
        after=after_ts, limit=200, status="unread",
    )
    entries = data.get("entries", [])

    # Split into priority vs regular
    priority_entries = [e for e in entries if e.get("feed_id") in priority_feeds]
    other_entries = [e for e in entries if e.get("feed_id") not in priority_feeds]

    # Get summaries for all entries: prefer the "default" prompt, fall back to any available
    entry_ids = [e["id"] for e in entries]
    summaries = {}
    if entry_ids:
        async with get_conn() as conn:
            cur = await conn.execute(
                "SELECT entry_id, metadata FROM article_snapshots "
                "WHERE entry_id = ANY(%s) "
                "AND (metadata ? 'summaries' OR metadata ? 'summary') "
                "ORDER BY version DESC",
                (entry_ids,),
            )
            for row in await cur.fetchall():
                eid = row["entry_id"]
                if eid in summaries:
                    continue
                meta = row["metadata"] or {}
                dict_sums = meta.get("summaries") or {}
                chosen = (
                    dict_sums.get("default")
                    or (next(iter(dict_sums.values())) if dict_sums else None)
                    or meta.get("summary")
                )
                if chosen:
                    summaries[eid] = chosen

    for entry in entries:
        entry["_summary"] = summaries.get(entry["id"], "")

    return templates.TemplateResponse(
        request,
        "digest.html",
        {
            "priority_entries": priority_entries,
            "other_entries": other_entries[:20],
            "total_unread": data.get("total", 0),
            "date": now.strftime("%A, %B %d, %Y"),
        },
    )
