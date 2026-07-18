"""Article embeddings + taste-centroid similarity (Part C phase 2).

The worker embeds article text via Ollama (nomic-embed-text), maintains a **taste
centroid** = mean embedding of positively-engaged articles, and exposes
`embed_sim` = cosine(article, centroid) as one more ranker feature (learned weight,
seeded positive). Everything fails open: if Ollama is down or an entry has no
embedding yet, embed_sim is simply absent and the structured ranker is unaffected.
"""

import json
import logging
import math
import re
from datetime import UTC, datetime, timedelta

import httpx
from psycopg.types.json import Jsonb

from app import db, miniflux_client
from app.config import (
    EMBED_BACKFILL_BATCH,
    EMBED_BACKFILL_MAX_AGE_DAYS,
    EMBED_ENABLED,
    EMBED_MODEL,
    OLLAMA_URL,
)
from app.db import get_conn

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TIMEOUT = httpx.Timeout(20.0, connect=2.0)
_POSITIVE = ("star", "thumb_up", "open_original", "dwell")
_MAX_CHARS = 1800


def _text_of(entry: dict) -> str:
    """Plain-text gist of an entry for embedding: title + de-tagged content."""
    title = entry.get("title") or ""
    content = _TAG_RE.sub(" ", entry.get("content") or "")
    return _WS_RE.sub(" ", f"{title}. {content}").strip()[:_MAX_CHARS]


def _has_words(text: str) -> bool:
    """Is there anything here worth embedding?

    _text_of always emits at least the "." from its "{title}. {content}" join, so a
    title-less, body-less entry yields "." — truthy, and an embedding of a full stop
    is both a wasted Ollama call and a junk neighbour for every other article.
    """
    return any(ch.isalnum() for ch in text)


async def embed_text(text: str) -> list[float] | None:
    """Embed text via Ollama; None on any failure (caller skips it)."""
    if not EMBED_ENABLED or not text:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": text},
            )
            resp.raise_for_status()
            vec = resp.json().get("embedding")
            return vec if isinstance(vec, list) and vec else None
    except Exception as exc:
        logger.debug("embed failed: %s", exc)
        return None


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ---- storage ----
#
# Vectors live in the pgvector `emb` column. We deliberately avoid a pgvector Python
# adapter: a vector literal is just "[0.1,0.2,...]", which compact json.dumps produces
# and json.loads parses, so `%s::vector` on write and `emb::text` on read is the whole
# integration. Callers keep handling plain float lists, so cosine()/embed_sims() and
# every ranker caller are unchanged.

def _vec_literal(vec: list[float]) -> str:
    return json.dumps(vec, separators=(",", ":"))


async def _stored(conn, entry_ids: list[int]) -> dict[int, list]:
    if not entry_ids:
        return {}
    cur = await conn.execute(
        "SELECT entry_id, emb::text AS emb FROM entry_embeddings "
        "WHERE entry_id = ANY(%s) AND emb IS NOT NULL",
        (list(entry_ids),),
    )
    return {r["entry_id"]: json.loads(r["emb"]) for r in await cur.fetchall()}


async def _upsert(conn, entry_id: int, vec: list[float] | None, entry: dict | None) -> None:
    """Store an embedding and/or refresh the denormalized render metadata.

    COALESCE on emb means a metadata-only pass (vec=None) never clobbers an existing
    vector, which is what lets the backfill sweep cheaply attach feed_id/title/
    published_at to rows embedded before those columns existed.
    """
    entry = entry or {}
    await conn.execute(
        """
        INSERT INTO entry_embeddings (entry_id, model, emb, feed_id, title, published_at)
        VALUES (%s, %s, %s::vector, %s, %s, %s)
        ON CONFLICT (entry_id) DO UPDATE
          SET emb          = COALESCE(entry_embeddings.emb, EXCLUDED.emb),
              feed_id      = COALESCE(EXCLUDED.feed_id, entry_embeddings.feed_id),
              title        = COALESCE(EXCLUDED.title, entry_embeddings.title),
              published_at = COALESCE(EXCLUDED.published_at, entry_embeddings.published_at)
        """,
        (
            entry_id,
            EMBED_MODEL,
            _vec_literal(vec) if vec else None,
            entry.get("feed_id") or (entry.get("feed") or {}).get("id"),
            entry.get("title"),
            entry.get("published_at"),
        ),
    )


async def _centroid(conn) -> list | None:
    cur = await conn.execute("SELECT centroid FROM ranker_taste WHERE id = 1")
    row = await cur.fetchone()
    return (row or {}).get("centroid")


async def embed_sims(conn, entry_ids: list[int]) -> dict[int, float]:
    """embed_sim (cosine to the taste centroid, clamped to [0,1]) for each given
    entry that has an embedding. Empty if there's no centroid yet."""
    centroid = await _centroid(conn)
    if not centroid:
        return {}
    embs = await _stored(conn, entry_ids)
    return {eid: max(0.0, cosine(vec, centroid)) for eid, vec in embs.items()}


async def taste_candidates(conn, exclude_ids: list[int], limit: int = 100) -> list[int]:
    """Unread entry ids closest to the taste centroid — the 'deep' half of the
    smart candidate pool (WP5). Joins Miniflux's own entries table for the unread
    filter (nearest-to-taste in the archive is mostly already-read, so filtering
    over REST would starve). Fails open to [] on any error, including the join
    target not existing on a non-Miniflux database."""
    centroid = await _centroid(conn)
    if not centroid:
        return []
    try:
        cur = await conn.execute(
            """
            SELECT ee.entry_id
              FROM entry_embeddings ee
              JOIN entries e ON e.id = ee.entry_id
             WHERE e.status = 'unread' AND ee.emb IS NOT NULL
               AND NOT (ee.entry_id = ANY(%s))
             ORDER BY ee.emb <=> %s::vector
             LIMIT %s
            """,
            (list(exclude_ids), _vec_literal(centroid), limit),
        )
        return [r["entry_id"] for r in await cur.fetchall()]
    except Exception as exc:
        logger.debug("taste_candidates failed open: %s", exc)
        return []


# ---- related articles ----

# Calibrated against the real corpus rather than guessed: nomic-embed-text cosines are
# compressed into a narrow band (a typical article's nearest neighbour scores ~0.74, its
# 8th ~0.67), so a naive low floor would let everything through and "Related" would
# always show 5 items — including unrelated ones. Sampling real neighbours showed the
# quality gradient tracks the score closely: >0.79 is the same story from another
# source, ~0.73-0.76 is the same topic, and by ~0.65 you get articles that merely share
# a proper noun (a Gemini security bug "related" to a Gemini jetlag tip). 0.70 keeps the
# first two and cuts the third.
_REL_MIN_SIM = 0.70
_REL_PER_FEED = 2       # don't let one chatty source fill the list
_REL_SHOW = 5


def _norm_title(title: str | None) -> str:
    return _WS_RE.sub(" ", (title or "").casefold()).strip()


def pick_related(
    candidates: list[dict],
    target_title: str | None,
    k: int = _REL_SHOW,
    min_sim: float = _REL_MIN_SIM,
    per_feed: int = _REL_PER_FEED,
) -> list[dict]:
    """Filter nearest-neighbour candidates down to a useful "related" list.

    Pure, so the judgement calls are testable. Nearest-by-cosine alone is a poor
    list: the closest vectors to an article tend to be *the same article* —
    syndicated copies, or another version of it we snapshotted — which is
    technically correct and useless to read. Title matching catches those cheaply,
    since cross-posts almost always keep the headline.
    """
    seen_titles = {_norm_title(target_title)}
    per_feed_count: dict[int, int] = {}
    out: list[dict] = []
    for c in candidates:
        if c.get("sim", 0.0) < min_sim:
            continue
        title = _norm_title(c.get("title"))
        if not title or title in seen_titles:
            continue
        feed_id = c.get("feed_id")
        if per_feed_count.get(feed_id, 0) >= per_feed:
            continue
        seen_titles.add(title)
        per_feed_count[feed_id] = per_feed_count.get(feed_id, 0) + 1
        out.append(c)
        if len(out) >= k:
            break
    return out


async def related_candidates(conn, entry_id: int, k: int = 15) -> tuple[list[dict], str | None]:
    """Nearest neighbours by cosine, plus the target's own title.

    Deliberately two queries rather than one self-join. pgvector's HNSW index only
    engages when the ORDER BY operand is a *parameter*; comparing against a column
    from a joined row makes the planner fall back to a sequential scan over the whole
    corpus (measured: 59ms seq-scan vs 2.4ms indexed on 8.4k rows — and the gap widens
    as the archive grows, since only the index scan is sub-linear).

    No Miniflux round-trip either: feed_id/title/published_at are denormalized onto
    entry_embeddings for exactly this.
    """
    cur = await conn.execute(
        "SELECT emb::text AS emb, title FROM entry_embeddings "
        "WHERE entry_id = %s AND emb IS NOT NULL",
        (entry_id,),
    )
    target = await cur.fetchone()
    if not target:
        return [], None

    cur = await conn.execute(
        """
        SELECT entry_id, feed_id, title, published_at,
               1 - (emb <=> %s::vector) AS sim
          FROM entry_embeddings
         WHERE entry_id <> %s AND emb IS NOT NULL
         ORDER BY emb <=> %s::vector
         LIMIT %s
        """,
        (target["emb"], entry_id, target["emb"], k),
    )
    return [dict(r) for r in await cur.fetchall()], target["title"]


# ---- worker passes ----

async def embed_pending(limit: int = 30) -> int:
    """Embed unembedded articles — positively-engaged ones first (they feed the
    centroid), then the most recent (the scoring candidates). Returns count embedded.

    This is the fast path for *new* articles; `embed_backfill` sweeps the archive.
    """
    if not (EMBED_ENABLED and db.VECTOR_READY):
        return 0
    try:
        recent = (await miniflux_client.get_entries(limit=200)).get("entries", [])
    except Exception:
        recent = []
    text_by_id = {e["id"]: _text_of(e) for e in recent}

    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT entry_id FROM engagement_events WHERE signal = ANY(%s)",
            (list(_POSITIVE),),
        )
        engaged = [r["entry_id"] for r in await cur.fetchall()]
        have = set(await _stored(conn, list(text_by_id) + engaged))

    todo = [e for e in engaged if e not in have]
    todo += [e for e in text_by_id if e not in have and e not in set(engaged)]
    todo = todo[:limit]
    if not todo:
        return 0

    entry_by_id = {e["id"]: e for e in recent}
    embedded = 0
    async with get_conn() as conn:
        for eid in todo:
            entry = entry_by_id.get(eid)
            text = text_by_id.get(eid)
            if text is None:
                try:
                    entry = await miniflux_client.get_entry(eid)
                    text = _text_of(entry)
                except Exception:
                    continue
            vec = await embed_text(text)
            if vec is None:
                continue
            await _upsert(conn, eid, vec, entry)
            embedded += 1
        await conn.commit()
    if embedded:
        logger.info("embedded %d articles", embedded)
    return embedded


def _backfill_plan(
    entries: list[dict],
    have_emb: set[int],
    snapshot_texts: dict[int, str],
) -> tuple[list[tuple[int, str, dict]], list[dict]]:
    """Split a page of entries into (to_embed, meta_only).

    Pure, so the interesting decisions are testable without a DB or Ollama:
      * an already-embedded row needs no Ollama call — just a metadata refresh,
        which is how rows embedded before feed_id/title/published_at existed get
        backfilled as the cursor sweeps past them;
      * extracted snapshot text beats the RSS body, which is often a truncated
        teaser — the whole point of this app is that it has the real article;
      * an entry with no usable text at all is neither embedded nor retried.
    """
    to_embed: list[tuple[int, str, dict]] = []
    meta_only: list[dict] = []
    for entry in entries:
        eid = entry["id"]
        if eid in have_emb:
            meta_only.append(entry)
            continue
        snap = (snapshot_texts.get(eid) or "").strip()
        text = (
            _WS_RE.sub(" ", f"{entry.get('title') or ''}. {snap}").strip()[:_MAX_CHARS]
            if snap
            else _text_of(entry)
        )
        if _has_words(text):
            to_embed.append((eid, text, entry))
    return to_embed, meta_only


async def _backfill_cursor(conn) -> tuple[int, bool]:
    cur = await conn.execute("SELECT cursor_entry_id, done FROM embed_backfill WHERE id = 1")
    row = await cur.fetchone()
    if row is None:
        await conn.execute("INSERT INTO embed_backfill (id) VALUES (1) ON CONFLICT DO NOTHING")
        await conn.commit()
        return 0, False
    return row["cursor_entry_id"], row["done"]


async def _snapshot_texts(conn, entry_ids: list[int]) -> dict[int, str]:
    """Latest extracted text per entry — the good stuff, when we have it."""
    if not entry_ids:
        return {}
    cur = await conn.execute(
        "SELECT DISTINCT ON (entry_id) entry_id, content_text FROM article_snapshots "
        "WHERE entry_id = ANY(%s) AND content_text IS NOT NULL "
        "ORDER BY entry_id, version DESC",
        (list(entry_ids),),
    )
    return {r["entry_id"]: r["content_text"] for r in await cur.fetchall()}


async def embed_backfill(limit: int = EMBED_BACKFILL_BATCH) -> int:
    """Walk the archive by ascending entry id, embedding as we go. One page per
    worker cycle; resumable via the embed_backfill cursor.

    Ordering by id (not date) gives a stable cursor that new articles can't disturb —
    they arrive with higher ids and are handled by embed_pending anyway.

    On an Ollama failure the cursor does NOT advance past the failed entry, so the
    next cycle retries it rather than leaving a permanent hole.
    """
    if not (EMBED_ENABLED and db.VECTOR_READY) or limit <= 0:
        return 0
    async with get_conn() as conn:
        cursor, done = await _backfill_cursor(conn)
    if done:
        return 0

    try:
        page = await miniflux_client.get_entries(
            limit=limit, order="id", direction="asc", after_entry_id=cursor,
            published_after=_backfill_floor(),
        )
    except Exception as exc:
        logger.debug("embed backfill: entry fetch failed: %s", exc)
        return 0

    entries = page.get("entries", [])
    if not entries:
        async with get_conn() as conn:
            await conn.execute(
                "UPDATE embed_backfill SET done = TRUE, updated_at = NOW() WHERE id = 1"
            )
            await conn.commit()
        logger.info("embed backfill complete (cursor=%d)", cursor)
        return 0

    ids = [e["id"] for e in entries]
    async with get_conn() as conn:
        have = set(await _stored(conn, ids))
        snaps = await _snapshot_texts(conn, ids)
    to_embed, meta_only = _backfill_plan(entries, have, snaps)

    embedded = 0
    reached = cursor
    async with get_conn() as conn:
        for entry in meta_only:
            await _upsert(conn, entry["id"], None, entry)
            reached = max(reached, entry["id"])
        for eid, text, entry in to_embed:
            vec = await embed_text(text)
            if vec is None:
                # Ollama is probably down. Stop here and keep the cursor behind this
                # entry so the next cycle picks it up again.
                await conn.commit()
                logger.info(
                    "embed backfill: paused at %d (embed failed); %d embedded this pass",
                    eid, embedded,
                )
                await _save_cursor(reached)
                return embedded
            await _upsert(conn, eid, vec, entry)
            embedded += 1
            reached = max(reached, eid)
        await conn.commit()

    await _save_cursor(reached)
    logger.info(
        "embed backfill: %d embedded, %d meta-only, cursor=%d, total≈%s",
        embedded, len(meta_only), reached, page.get("total", "?"),
    )
    return embedded


async def _save_cursor(entry_id: int) -> None:
    async with get_conn() as conn:
        await conn.execute(
            "UPDATE embed_backfill SET cursor_entry_id = GREATEST(cursor_entry_id, %s), "
            "updated_at = NOW() WHERE id = 1",
            (entry_id,),
        )
        await conn.commit()


def _backfill_floor() -> int | None:
    """Oldest publish date the backfill reaches back to, as a Unix timestamp
    (Miniflux's published_after wants seconds). None = no floor, walk everything."""
    if EMBED_BACKFILL_MAX_AGE_DAYS <= 0:
        return None
    floor = datetime.now(UTC) - timedelta(days=EMBED_BACKFILL_MAX_AGE_DAYS)
    return int(floor.timestamp())


async def recompute_centroid() -> int:
    """Rebuild the taste centroid from the embeddings of positively-engaged
    articles. Returns the number of vectors averaged.

    Membership stays limited to _POSITIVE signals: the centroid is what "your taste"
    means, so a plain read shouldn't drag it around.
    """
    if not (EMBED_ENABLED and db.VECTOR_READY):
        return 0
    async with get_conn() as conn:
        # pgvector has a native vector avg(), so the mean is one aggregate rather
        # than pulling every 768-float vector into Python to average by hand.
        cur = await conn.execute(
            "SELECT avg(emb)::text AS centroid, count(*) AS n FROM entry_embeddings "
            "WHERE emb IS NOT NULL AND entry_id IN "
            "(SELECT DISTINCT entry_id FROM engagement_events WHERE signal = ANY(%s))",
            (list(_POSITIVE),),
        )
        row = await cur.fetchone()
        if not row or not row["n"] or not row["centroid"]:
            return 0
        centroid, n_vecs = json.loads(row["centroid"]), row["n"]
        await conn.execute(
            "INSERT INTO ranker_taste (id, centroid, n, updated_at) VALUES (1, %s, %s, NOW()) "
            "ON CONFLICT (id) DO UPDATE SET centroid = EXCLUDED.centroid, "
            "n = EXCLUDED.n, updated_at = NOW()",
            (Jsonb(centroid), n_vecs),
        )
        await conn.commit()
    return n_vecs
