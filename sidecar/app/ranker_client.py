"""Client for the cross-feed ranker runner (Part C).

Speaks the small localhost JSON contract the Julia/credence runner exposes
(/load, /observe, /score, /health) and owns the ranker_state row in Postgres
(Python is the durable store; the runner holds the warm posterior). Every call
degrades gracefully: if the runner is disabled or unreachable, score() returns
None and the caller falls back to priority+recency ordering.
"""

import logging
from datetime import datetime, timezone

import httpx
from psycopg.types.json import Jsonb

from app import miniflux_client, ranker
from app.config import RANKER_ENABLED, RANKER_MODEL_VERSION, RANKER_URL
from app.db import get_conn

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(2.5, connect=0.5)


async def _request(method: str, path: str, payload: dict | None = None):
    if not RANKER_ENABLED:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, RANKER_URL + path, json=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # unreachable / timeout / bad status -> graceful fallback
        logger.debug("ranker %s %s unavailable: %s", method, path, exc)
        return None


async def health() -> dict | None:
    return await _request("GET", "/health")


async def score(articles: list[dict]) -> dict[int, float] | None:
    """Return {entry_id: score} for the given /score-shaped articles, or None if
    the ranker is unavailable (caller then keeps its default ordering)."""
    if not articles:
        return {}
    res = await _request("POST", "/score", {"articles": articles})
    if not res or "scores" not in res:
        return None
    try:
        return {int(eid): float(s) for eid, s in res["scores"]}
    except (TypeError, ValueError):
        return None


async def observe(events: list[dict]) -> dict | None:
    """Condition the model on a batch of observations; persist returned weights."""
    if not events:
        return None
    return await _request("POST", "/observe", {"events": events})


# ---- ranker_state persistence (Python owns the durable copy) ----

async def read_state() -> dict | None:
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT model_version, state_blob, last_event_id FROM ranker_state WHERE id = 1"
        )
        return await cur.fetchone()


async def load_state() -> bool:
    """Push the persisted posterior into a freshly-started runner. Called on app
    startup. Returns True if a /load round-trip succeeded."""
    row = await read_state()
    # state_blob may be SQL/JSON null on a partially-written row — coalesce.
    weights = ((row or {}).get("state_blob") or {}).get("weights", {})
    res = await _request("POST", "/load", {
        "model_version": (row or {}).get("model_version", RANKER_MODEL_VERSION),
        "weights": weights,
    })
    return res is not None


async def _persist_state(weights: dict, obs_count, last_event_id: int) -> None:
    """Persist the updated weights AND advance the high-water mark in a SINGLE
    transaction, so a crash can't leave the mark behind the folded weights (which
    would re-fold the same events and skew the model)."""
    blob = {"weights": weights, "obs_count": obs_count}
    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO ranker_state (id, model_version, state_blob, last_event_id, updated_at)
            VALUES (1, %s, %s, %s, NOW())
            ON CONFLICT (id) DO UPDATE
              SET model_version = EXCLUDED.model_version,
                  state_blob = EXCLUDED.state_blob,
                  last_event_id = GREATEST(ranker_state.last_event_id, EXCLUDED.last_event_id),
                  updated_at = NOW()
            """,
            (RANKER_MODEL_VERSION, Jsonb(blob), last_event_id),
        )
        await conn.commit()


async def sync_observations(limit: int = 200) -> int:
    """Fold newly-captured engagement_events into the model. Reads rows past the
    high-water mark, attaches each entry's features, conditions the model, and
    advances the mark — but only if the runner accepted the batch (so events are
    never lost while the runner is down). Called periodically by the worker."""
    if not RANKER_ENABLED:
        return 0
    row = await read_state()
    last_id = (row or {}).get("last_event_id", 0) or 0
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT id, entry_id, signal, value FROM engagement_events "
            "WHERE id > %s ORDER BY id LIMIT %s",
            (last_id, limit),
        )
        rows = await cur.fetchall()
        pcur = await conn.execute("SELECT feed_id, priority FROM feed_config")
        priorities = {r["feed_id"]: r["priority"] for r in await pcur.fetchall()}
    if not rows:
        return 0

    now = datetime.now(timezone.utc)
    entry_cache: dict[int, dict] = {}
    events, max_id = [], last_id
    for r in rows:
        max_id = max(max_id, r["id"])
        eid = r["entry_id"]
        if eid not in entry_cache:
            try:
                entry_cache[eid] = await miniflux_client.get_entry(eid)
            except Exception:
                entry_cache[eid] = {}
        entry = entry_cache[eid]
        prio = priorities.get(entry.get("feed_id"), 2)
        ev = ranker.build_observation(entry, r["signal"], r["value"] or 1.0, prio, now)
        ev["id"] = r["id"]  # lets a stateful runner dedupe if a batch is retried
        events.append(ev)

    res = await observe(events)
    if res is None:
        return 0  # runner down — leave the high-water mark so we retry next tick
    # Persist returned weights and advance the mark together (atomic).
    await _persist_state(res.get("weights", {}), res.get("obs_count"), max_id)
    logger.info("ranker folded %d engagement events (through id %d)", len(events), max_id)
    return len(events)
