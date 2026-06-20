"""Article embeddings + taste-centroid similarity (Part C phase 2).

The worker embeds article text via Ollama (nomic-embed-text), maintains a **taste
centroid** = mean embedding of positively-engaged articles, and exposes
`embed_sim` = cosine(article, centroid) as one more ranker feature (learned weight,
seeded positive). Everything fails open: if Ollama is down or an entry has no
embedding yet, embed_sim is simply absent and the structured ranker is unaffected.
"""

import logging
import math
import re

import httpx
from psycopg.types.json import Jsonb

from app import miniflux_client
from app.config import EMBED_ENABLED, EMBED_MODEL, OLLAMA_URL
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
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ---- storage ----

async def _stored(conn, entry_ids: list[int]) -> dict[int, list]:
    if not entry_ids:
        return {}
    cur = await conn.execute(
        "SELECT entry_id, vec FROM entry_embeddings WHERE entry_id = ANY(%s)",
        (list(entry_ids),),
    )
    return {r["entry_id"]: r["vec"] for r in await cur.fetchall()}


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


# ---- worker passes ----

async def embed_pending(limit: int = 30) -> int:
    """Embed unembedded articles — positively-engaged ones first (they feed the
    centroid), then the most recent (the scoring candidates). Returns count embedded."""
    if not EMBED_ENABLED:
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

    embedded = 0
    async with get_conn() as conn:
        for eid in todo:
            text = text_by_id.get(eid)
            if text is None:
                try:
                    text = _text_of(await miniflux_client.get_entry(eid))
                except Exception:
                    continue
            vec = await embed_text(text)
            if vec is None:
                continue
            await conn.execute(
                "INSERT INTO entry_embeddings (entry_id, model, vec) VALUES (%s, %s, %s) "
                "ON CONFLICT (entry_id) DO NOTHING",
                (eid, EMBED_MODEL, Jsonb(vec)),
            )
            embedded += 1
        await conn.commit()
    if embedded:
        logger.info("embedded %d articles", embedded)
    return embedded


async def recompute_centroid() -> int:
    """Rebuild the taste centroid from the embeddings of positively-engaged
    articles. Returns the number of vectors averaged."""
    if not EMBED_ENABLED:
        return 0
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT vec FROM entry_embeddings WHERE entry_id IN "
            "(SELECT DISTINCT entry_id FROM engagement_events WHERE signal = ANY(%s))",
            (list(_POSITIVE),),
        )
        vecs = [r["vec"] for r in await cur.fetchall()]
        if not vecs:
            return 0
        dim = len(vecs[0])
        centroid = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
        await conn.execute(
            "INSERT INTO ranker_taste (id, centroid, n, updated_at) VALUES (1, %s, %s, NOW()) "
            "ON CONFLICT (id) DO UPDATE SET centroid = EXCLUDED.centroid, "
            "n = EXCLUDED.n, updated_at = NOW()",
            (Jsonb(centroid), len(vecs)),
        )
        await conn.commit()
    return len(vecs)
