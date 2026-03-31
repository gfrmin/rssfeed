import asyncio
import logging

import psycopg

from app import miniflux_client
from app.config import WORKER_POLL_INTERVAL
from app.db import get_conn
from app.extractor import fetch_and_extract

logger = logging.getLogger(__name__)


async def _get_enabled_feed_ids(conn: psycopg.AsyncConnection) -> set[int]:
    cur = await conn.execute(
        "SELECT feed_id FROM feed_config WHERE fetch_full_content = TRUE"
    )
    return {row["feed_id"] for row in await cur.fetchall()}


async def _has_snapshot(conn: psycopg.AsyncConnection, entry_id: int) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM article_snapshots WHERE entry_id = %s LIMIT 1",
        (entry_id,),
    )
    return (await cur.fetchone()) is not None


async def _store_snapshot(
    conn: psycopg.AsyncConnection,
    entry_id: int,
    feed_id: int,
    url: str,
    extracted: dict,
) -> None:
    await conn.execute(
        """
        INSERT INTO article_snapshots
            (entry_id, feed_id, url, content_text, content_html, content_hash, metadata, version)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 1)
        """,
        (
            entry_id,
            feed_id,
            url,
            extracted["content_text"],
            extracted["content_html"],
            extracted["content_hash"],
            psycopg.types.json.Json(extracted["metadata"]),
        ),
    )
    await conn.commit()


async def process_new_entries() -> int:
    """Check for new entries in full-content feeds and extract them. Returns count processed."""
    processed = 0
    async with get_conn() as conn:
        enabled = await _get_enabled_feed_ids(conn)
        if not enabled:
            return 0

        for feed_id in enabled:
            try:
                data = await miniflux_client.get_entries(feed_id=feed_id, limit=50)
            except Exception:
                logger.exception("Failed to fetch entries for feed %d", feed_id)
                continue

            for entry in data.get("entries", []):
                entry_id = entry["id"]
                url = entry.get("url", "")
                if not url:
                    continue

                if await _has_snapshot(conn, entry_id):
                    continue

                logger.info("Extracting entry %d: %s", entry_id, url)
                extracted = await fetch_and_extract(url)
                if extracted:
                    await _store_snapshot(conn, entry_id, feed_id, url, extracted)
                    processed += 1
                else:
                    logger.warning("Extraction failed for entry %d: %s", entry_id, url)

    return processed


async def worker_loop() -> None:
    """Background loop that continuously processes new entries."""
    logger.info("Worker started, polling every %ds", WORKER_POLL_INTERVAL)
    while True:
        try:
            count = await process_new_entries()
            if count:
                logger.info("Processed %d new entries", count)
        except Exception:
            logger.exception("Worker error")
        await asyncio.sleep(WORKER_POLL_INTERVAL)
