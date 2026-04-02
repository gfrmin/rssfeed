from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import get_conn
from app.templating import templates

router = APIRouter()


@router.get("/stats", response_class=HTMLResponse)
async def reading_stats(request: Request):
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    async with get_conn() as conn:
        # Articles read today
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cur = await conn.execute(
            "SELECT COUNT(*) AS cnt FROM read_events WHERE read_at >= %s",
            (today_start,),
        )
        today_count = (await cur.fetchone())["cnt"]

        # Articles per day (last 7 days)
        cur = await conn.execute(
            """SELECT DATE(read_at) AS day, COUNT(*) AS cnt
               FROM read_events WHERE read_at >= %s
               GROUP BY DATE(read_at) ORDER BY day""",
            (week_ago,),
        )
        daily = await cur.fetchall()

        # Articles per week (last 4 weeks)
        cur = await conn.execute(
            """SELECT DATE_TRUNC('week', read_at) AS week, COUNT(*) AS cnt
               FROM read_events WHERE read_at >= %s
               GROUP BY DATE_TRUNC('week', read_at) ORDER BY week""",
            (month_ago,),
        )
        weekly = await cur.fetchall()

        # Most-read feeds (last 30 days)
        cur = await conn.execute(
            """SELECT feed_id, COUNT(*) AS cnt
               FROM read_events WHERE read_at >= %s
               GROUP BY feed_id ORDER BY cnt DESC LIMIT 10""",
            (month_ago,),
        )
        top_feeds_raw = await cur.fetchall()

        # Total articles read
        cur = await conn.execute("SELECT COUNT(*) AS cnt FROM read_events")
        total = (await cur.fetchone())["cnt"]

    # Get feed names for top feeds
    from app import miniflux_client
    feeds = await miniflux_client.get_feeds()
    feed_names = {f["id"]: f.get("title", f"Feed {f['id']}") for f in feeds}

    top_feeds = [
        {"name": feed_names.get(r["feed_id"], f"Feed {r['feed_id']}"), "count": r["cnt"], "feed_id": r["feed_id"]}
        for r in top_feeds_raw
    ]

    # Compute max for bar chart scaling
    max_daily = max((d["cnt"] for d in daily), default=1)
    max_feed = max((f["count"] for f in top_feeds), default=1)

    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "today_count": today_count,
            "total": total,
            "daily": daily,
            "weekly": weekly,
            "top_feeds": top_feeds,
            "max_daily": max_daily,
            "max_feed": max_feed,
        },
    )
