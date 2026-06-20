"""Cross-feed learning ranker — drives the Credence engine over the skin wire.

The engine is consumed ONLY over the skin (JSON-RPC/stdio via `credence-skin-client`)
and is **stateless-per-call**: the preference model is a pure BDSL program
(`ranker/model.bdsl`) loaded once into a warm subprocess, and every durable belief
lives here in Postgres as a plain Gaussian belief-spec
(`{"type":"gaussian","mu":..,"sigma":..}`). Python never does probability
arithmetic — every belief update and score goes through `call_dsl` into the pure
model; Python only persists belief-specs, maps feature names↔positional indices,
and maps engagement signals to a signed evidence scalar.

Every call **fails open**: if the engine can't start or a call errors, `score()`
returns None and the caller keeps its priority+recency ordering, so ranking never
breaks the reader.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from app import miniflux_client, ranker
from app.config import (
    CREDENCE_SKIN_COMMAND,
    CREDENCE_SKIN_PROJECT,
    CREDENCE_SKIN_SERVER,
    RANKER_ENABLED,
    RANKER_MODEL_VERSION,
)
from app.db import get_conn

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).resolve().parent.parent / "ranker" / "model.bdsl"
# Prior for an unseen feature: a signed weight with no opinion yet.
_PRIOR = {"type": "gaussian", "mu": 0.0, "sigma": 1.0}

# signal → signed evidence scalar. NOT probability math — just how strongly each
# quality signal counts as evidence; the model does the Bayesian update. Plain
# reads (swipe / mark-all) are never captured, so they never reach here.
_EVIDENCE = {
    "star": 1.0,
    "thumb_up": 1.0,
    "open_original": 0.5,
    "unstar": -0.5,
    "thumb_down": -1.0,
}


def _evidence(signal: str, value) -> float:
    """Map an engagement signal to signed evidence in roughly [-1, 1]."""
    if signal == "dwell":
        # Graded: ~0.2 at the 4s capture floor, saturating toward 1.0 by ~90s.
        secs = float(value or 0.0)
        return max(0.2, min(1.0, secs / 90.0))
    return _EVIDENCE.get(signal, 0.0)


# ---- warm skin lifecycle (one subprocess, serialized, off the event loop) ----

_skin = None
_started = False
_lock = asyncio.Lock()


async def _ensure_started() -> bool:
    """Spawn the skin subprocess and load the model once. Returns True if usable.
    Idempotent; fails open (returns False) if the engine can't start."""
    global _skin, _started
    if not RANKER_ENABLED:
        return False
    if _started and _skin is not None:
        return True
    async with _lock:
        if _started and _skin is not None:
            return True
        try:
            from credence_skin_client import SkinClient

            model_src = MODEL_PATH.read_text()

            def _spawn():
                if CREDENCE_SKIN_COMMAND:
                    s = SkinClient(command=CREDENCE_SKIN_COMMAND)
                else:
                    s = SkinClient(server_path=CREDENCE_SKIN_SERVER,
                                   project=CREDENCE_SKIN_PROJECT)
                s.initialize(dsl_sources={"model": model_src})
                return s

            _skin = await asyncio.to_thread(_spawn)
            _started = True
            logger.info("credence skin ready (model %s)", RANKER_MODEL_VERSION)
            return True
        except Exception as exc:
            logger.warning("credence skin unavailable, ranking disabled: %s", exc)
            _skin = None
            _started = False
            return False


async def _call(function: str, args: list):
    """Run one model function over the wire, serialized and off the loop. Returns
    the result, or None on any error (caller falls back)."""
    if not await _ensure_started():
        return None
    try:
        async with _lock:
            return await asyncio.to_thread(_skin.call_dsl, "model", function, args)
    except Exception as exc:
        logger.debug("skin call_dsl %s failed: %s", function, exc)
        return None


async def shutdown() -> None:
    """Tear down the skin subprocess (called from the app lifespan)."""
    global _skin, _started
    s, _skin, _started = _skin, None, False
    if s is not None:
        try:
            await asyncio.to_thread(s.shutdown)
        except Exception:
            pass


# ---- weight store: Postgres is the source of truth ----

async def read_state() -> dict | None:
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT model_version, state_blob, last_event_id FROM ranker_state WHERE id = 1"
        )
        return await cur.fetchone()


def _weights_of(row: dict | None) -> dict:
    """The {name: belief-spec} map from a ranker_state row, null-coalesced."""
    return ((row or {}).get("state_blob") or {}).get("weights", {}) or {}


def _weight(weights: dict, name: str) -> dict:
    spec = weights.get(name)
    return dict(spec) if spec else dict(_PRIOR)


async def _persist_state(weights: dict, obs_count, last_event_id: int) -> None:
    """Persist updated weights AND advance the high-water mark in ONE transaction,
    so a crash can't leave the mark ahead of the folded weights (re-folding events
    would skew the model)."""
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


# ---- public API (consumed by main lifespan, the worker, and entry_list) ----

async def load_state() -> bool:
    """Warm the skin at app startup (loads the model program; weights stay in
    Postgres and ride along on each call). Returns True if the engine is up."""
    return await _ensure_started()


async def health() -> dict | None:
    """Cheap liveness for diagnostics: engine reachable + state sizes."""
    if await _call("score-batch", [[], []]) is None:
        return None
    row = await read_state()
    blob = ((row or {}).get("state_blob") or {})
    return {"obs_count": blob.get("obs_count", 0), "n_weights": len(_weights_of(row))}


async def score(articles: list[dict]) -> dict[int, float] | None:
    """Rank a batch. `articles` is ranker.build_articles() output —
    [{entry_id, features:[[name, value], ...]}, ...]. Returns {entry_id: score},
    or None if the engine is unavailable (caller keeps its default ordering)."""
    if not articles:
        return {}
    try:
        weights = _weights_of(await read_state())

        # Stable name→index map over the batch's feature union.
        names: list[str] = []
        seen: set[str] = set()
        for a in articles:
            for name, _v in a["features"]:
                if name not in seen:
                    seen.add(name)
                    names.append(name)
        if not names:
            return None
        idx = {n: i for i, n in enumerate(names)}
        weight_list = [_weight(weights, n) for n in names]

        vectors = []
        for a in articles:
            vec = [0.0] * len(names)
            for name, v in a["features"]:
                vec[idx[name]] = float(v)
            vectors.append(vec)

        res = await _call("score-batch", [weight_list, vectors])
        if res is None or len(res) != len(articles):
            return None
        return {int(a["entry_id"]): float(s) for a, s in zip(articles, res)}
    except Exception as exc:  # never break the reader on a ranking hiccup
        logger.debug("score failed open: %s", exc)
        return None


async def observe(events: list[dict], base_weights: dict | None = None) -> dict | None:
    """Fold a batch of observations into the weights. `events` is
    ranker.build_observation() output — [{signal, value, features:[name,...]}, ...].
    Returns {"weights": {...}, "obs_count": n_applied} layered on `base_weights`
    (read from Postgres if not given), or None if the engine is unavailable (so the
    caller leaves the high-water mark and retries)."""
    if not events:
        return None
    if not await _ensure_started():
        return None
    weights = dict(base_weights if base_weights is not None else _weights_of(await read_state()))
    applied = 0
    for ev in events:
        obs = _evidence(ev["signal"], ev.get("value"))
        active = ev.get("features", [])
        if obs == 0.0 or not active:
            continue
        specs = [_weight(weights, n) for n in active]
        updated = await _call("observe-batch", [specs, obs])
        if updated is None or len(updated) != len(active):
            return None  # fail open — don't lose events
        for name, spec in zip(active, updated):
            weights[name] = spec
        applied += 1
    return {"weights": weights, "obs_count": applied}


async def sync_observations(limit: int = 200) -> int:
    """Fold newly-captured engagement_events into the model. Reads rows past the
    high-water mark, attaches each entry's features, conditions the model, and
    persists the updated weights + advanced mark atomically — but only if the
    engine accepted the batch (events are never lost while it's down). Called
    periodically by the worker."""
    if not RANKER_ENABLED:
        return 0
    row = await read_state()
    last_id = (row or {}).get("last_event_id", 0) or 0
    base_weights = _weights_of(row)
    prev_obs = ((row or {}).get("state_blob") or {}).get("obs_count", 0) or 0

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
        events.append(ranker.build_observation(entry, r["signal"], r["value"] or 1.0, prio, now))

    res = await observe(events, base_weights=base_weights)
    if res is None:
        return 0  # engine down — leave the mark so we retry next tick
    await _persist_state(res["weights"], prev_obs + res["obs_count"], max_id)
    logger.info("ranker folded %d engagement events (through id %d)", len(events), max_id)
    return len(events)
