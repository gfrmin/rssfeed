"""How often each feed publishes, and when it last did.

Both come off one window over Miniflux's `entries` table. The median gap is
what `feed_health` measures publisher silence against: a feed posting hourly is
silent at six hours and a monthly one is not, so a fixed threshold cannot say
anything useful about either.

Cached, and deliberately for far longer than the sidebar's own cache. Publishing
rhythm changes over weeks; recomputing it on every navigation would be a full
scan of `entries` to learn nothing new.
"""
import asyncio
import logging
import time

from app.db import get_conn

logger = logging.getLogger(__name__)

# How many recent entries define a feed's rhythm. Enough that one burst or one
# hiatus cannot set the median; few enough that a feed which changed cadence a
# year ago is judged on what it does now.
CADENCE_WINDOW = 20

CACHE_TTL = 300.0

_CADENCE_SQL = """
WITH recent AS (
    SELECT feed_id, published_at FROM (
        SELECT feed_id, published_at,
               ROW_NUMBER() OVER (PARTITION BY feed_id ORDER BY published_at DESC) AS rn
        FROM entries
    ) ranked
    WHERE rn <= %s
),
gaps AS (
    SELECT feed_id, published_at,
           EXTRACT(EPOCH FROM (published_at - LAG(published_at)
               OVER (PARTITION BY feed_id ORDER BY published_at))) AS gap
    FROM recent
)
SELECT feed_id,
       MAX(published_at) AS latest,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY gap) AS median_gap_s
FROM gaps
GROUP BY feed_id
"""

_CACHE: dict[str, tuple[float, dict[int, dict]]] = {}
_CACHE_LOCK = asyncio.Lock()
_KEY = "all"


def invalidate() -> None:
    _CACHE.clear()


async def _query() -> dict[int, dict]:
    """Per-feed {latest, median_gap_s}, straight from the shared Miniflux DB.

    `percentile_cont` ignores the leading NULL gap, and yields NULL for a feed
    with a single entry — which is exactly "no baseline", the value
    feed_health.classify already reads as "say nothing about this one".
    """
    async with get_conn() as conn:
        cur = await conn.execute(_CADENCE_SQL, (CADENCE_WINDOW,))
        return {
            row["feed_id"]: {"latest": row["latest"],
                             "median_gap_s": row["median_gap_s"]}
            for row in await cur.fetchall()
        }


async def all_feeds() -> dict[int, dict]:
    """Cached per-feed cadence (TTL + double-checked lock)."""
    cached = _CACHE.get(_KEY)
    if cached and time.monotonic() - cached[0] < CACHE_TTL:
        return cached[1]
    async with _CACHE_LOCK:
        cached = _CACHE.get(_KEY)
        if cached and time.monotonic() - cached[0] < CACHE_TTL:
            return cached[1]
        t0 = time.perf_counter()
        try:
            data = await _query()
        except Exception:
            # Fails open, like the ranker and the embeddings. The sidebar reads
            # this on every navigation, and no scan of `entries` is worth a
            # reader that will not open: feeds just lose the `quiet` state.
            logger.exception("cadence query failed; continuing without baselines")
            return {}
        logger.info("cadence: %d feeds in %.0fms", len(data),
                    (time.perf_counter() - t0) * 1000)
        _CACHE[_KEY] = (time.monotonic(), data)
        return data
