"""Ollama LLM integration for summarization, tagging, and embeddings."""

import logging
from typing import Any

import ollama

from app.config import OLLAMA_EMBED_MODEL, OLLAMA_MODEL, OLLAMA_URL

logger = logging.getLogger(__name__)

_client = ollama.AsyncClient(host=OLLAMA_URL, timeout=120.0)


async def _ollama_generate(prompt: str, system: str = "") -> str | None:
    """Call Ollama generate endpoint."""
    try:
        r = await _client.generate(model=OLLAMA_MODEL, prompt=prompt, system=system)
        return r.response.strip()
    except Exception:
        logger.exception("Ollama generate failed")
        return None


async def _ollama_generate_stream(prompt: str, system: str = ""):
    """Yield tokens from Ollama generate endpoint."""
    stream = await _client.generate(model=OLLAMA_MODEL, prompt=prompt, system=system, stream=True)
    async for chunk in stream:
        token = chunk.response
        if token:
            yield token


SUMMARIZE_SYSTEM = (
    "You are a concise article summarizer. Output ONLY the summary itself — "
    "2-3 sentences stating the key facts and takeaways. "
    "Do not include any preamble, meta-commentary, or framing. "
    "Never begin with phrases like 'Here is', 'This article', 'The article', "
    "'In summary', 'Summary:', or similar. Start directly with the content."
)


async def summarize(text: str) -> str | None:
    """Generate a 2-3 sentence summary of article text."""
    if not text or len(text) < 100:
        return None
    truncated = " ".join(text.split()[:4000])
    return await _ollama_generate(prompt=truncated, system=SUMMARIZE_SYSTEM)


async def classify(text: str) -> list[str]:
    """Classify article into topic tags."""
    if not text or len(text) < 50:
        return []
    truncated = " ".join(text.split()[:2000])
    result = await _ollama_generate(
        prompt=truncated,
        system=(
            "You are a topic classifier. Read the article and return 1-5 topic tags "
            "that best describe it. Return ONLY a comma-separated list of lowercase tags, "
            "nothing else. Example: technology, ai, privacy"
        ),
    )
    if not result:
        return []
    tags = [t.strip().lower().strip('"\'') for t in result.split(",")]
    return [t for t in tags if t and len(t) < 50][:5]


async def embed(text: str) -> list[float] | None:
    """Generate an embedding vector for text using Ollama."""
    if not text:
        return None
    truncated = " ".join(text.split()[:1200])
    try:
        r = await _client.embed(model=OLLAMA_EMBED_MODEL, input=truncated)
        embeddings = r.embeddings
        return embeddings[0] if embeddings else None
    except Exception:
        logger.exception("Ollama embed failed")
        return None


async def find_similar(
    conn: Any, entry_id: int, embedding: list[float], threshold: float = 0.85, limit: int = 5
) -> list[dict]:
    """Find entries with similar embeddings using cosine similarity."""
    cur = await conn.execute(
        """
        WITH target AS (
            SELECT %s::float8[] AS vec
        ),
        similarities AS (
            SELECT
                ae.entry_id,
                (
                    SELECT SUM(a * b)
                    FROM unnest(ae.embedding, t.vec) AS u(a, b)
                ) / (
                    SQRT((SELECT SUM(a * a) FROM unnest(ae.embedding) AS u(a)))
                    * SQRT((SELECT SUM(b * b) FROM unnest(t.vec) AS u(b)))
                ) AS similarity
            FROM article_embeddings ae, target t
            WHERE ae.entry_id != %s
        )
        SELECT entry_id, similarity
        FROM similarities
        WHERE similarity >= %s
        ORDER BY similarity DESC
        LIMIT %s
        """,
        (embedding, entry_id, threshold, limit),
    )
    return [dict(row) for row in await cur.fetchall()]
