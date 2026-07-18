"""Cross-feed learning ranker — drives the Credence engine over the skin wire.

The engine is consumed ONLY over the skin (JSON-RPC/stdio via `credence-skin-client`)
and is **stateless-per-call**: the preference model is a pure BDSL program
(`ranker/model.bdsl`) loaded once into a warm subprocess, and every durable belief
lives here in Postgres as a plain Gaussian belief-spec
(`{"type":"gaussian","mu":..,"sigma":..}`). Python never does probability
arithmetic — the Bayesian-linear-regression update and the scoring both run in the
model via `call_dsl`; Python only persists belief-specs, maps feature names↔
positional indices, and maps each engagement signal to a regression target y.

Every call **fails open**: if the engine can't start or a call errors, `score()`
returns None and the caller keeps its priority+recency ordering, so ranking never
breaks the reader.
"""

import asyncio
import logging
import math
from datetime import UTC, datetime
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
# Prior for an unseen feature: a Gaussian weight with no opinion yet.
_PRIOR = {"type": "gaussian", "mu": 0.0, "sigma": 1.0}
# Per-feature prior overrides. embed_sim (taste-centroid cosine, Part C phase 2) is
# seeded positive so taste-similar articles get a lift before the weight has learned.
_SEED_PRIORS = {"embed_sim": {"type": "gaussian", "mu": 0.5, "sigma": 0.5}}
# σ²: observation noise on the score in the Bayesian-linear-regression update. Larger
# → a single like/dislike moves the weights less (more robust to one-off posts). The
# linear-gaussian kernel takes σ (stddev), so we pass √_NOISE over the wire.
_NOISE = 1.0
_SIGMA_OBS = math.sqrt(_NOISE)
_MIN_VAR = 1e-6   # floor so a weight never collapses to a delta

# signal → engagement target y for the regression. NOT probability math — just how
# strongly each quality signal counts; the model does the Bayesian update. Plain
# reads (swipe / mark-all) are never captured, so they never reach here.
_EVIDENCE = {
    "star": 1.0,
    "thumb_up": 1.0,
    "open_original": 0.5,
    "unstar": -0.5,
    "thumb_down": -1.0,
    "mute_author": -1.0,
    "mute_tag": -1.0,
    "unmute_author": 0.5,
    "unmute_tag": 0.5,
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
            # Reuse an existing client if we have one (its subprocess self-respawns on
            # the next use if it died) — so re-initialising after a crash never leaks a
            # second Julia process. initialize() (re)loads the model env either way.
            client = _skin

            def _spawn():
                s = client
                if s is None:
                    s = (SkinClient(command=CREDENCE_SKIN_COMMAND) if CREDENCE_SKIN_COMMAND
                         else SkinClient(server_path=CREDENCE_SKIN_SERVER,
                                         project=CREDENCE_SKIN_PROJECT))
                s.initialize(dsl_sources={"model": model_src})
                return s

            _skin = await asyncio.to_thread(_spawn)
            _started = True
            logger.info("credence skin ready (model %s)", RANKER_MODEL_VERSION)
            return True
        except Exception as exc:
            logger.warning("credence skin unavailable, ranking disabled: %s", exc)
            _started = False  # keep _skin for reuse; retry (re)init next call
            return False


async def _call(function: str, args: list):
    """Run one model function over the wire, serialized and off the loop. Returns
    the result, or None on any error (caller falls back)."""
    global _started
    if not await _ensure_started():
        return None
    try:
        async with _lock:
            return await asyncio.to_thread(_skin.call_dsl, "model", function, args)
    except Exception as exc:
        logger.debug("skin call_dsl %s failed: %s", function, exc)
        _started = False  # force a (re)initialise — and respawn if the engine died
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


def _spec(weights: dict, name: str) -> dict:
    """The Gaussian belief-spec for a feature, seeding a prior if unseen."""
    return weights.get(name) or _SEED_PRIORS.get(name, _PRIOR)


def _mean(spec: dict) -> float:
    return float(spec.get("mu", 0.0))


def _var(spec: dict) -> float:
    s = float(spec.get("sigma", 1.0))
    return s * s


def _to_spec(mu: float, var: float) -> dict:
    return {"type": "gaussian", "mu": float(mu), "sigma": math.sqrt(max(var, _MIN_VAR))}


def _mv_spec(means: list[float], variances: list[float]) -> dict:
    """A joint Gaussian belief-spec over the active weights for the wire. We persist
    only per-feature marginals, so the prior covariance is diagonal (Σ = diag(var));
    the engine's conjugate induces the off-diagonal explaining-away within the
    update, and we read the posterior marginals back off its diagonal."""
    k = len(means)
    rows = [[variances[i] if i == j else 0.0 for j in range(k)] for i in range(k)]
    return {"type": "mv_gaussian", "mu": [float(m) for m in means], "sigma": rows}


def _read_mv(spec: dict) -> tuple[list[float], list[float]] | None:
    """Pull (means, marginal variances=diag Σ) out of an mv_gaussian posterior spec."""
    if not isinstance(spec, dict) or spec.get("type") != "mv_gaussian":
        return None
    mu = spec.get("mu")
    sigma = spec.get("sigma")  # Σ as rows
    if not isinstance(mu, list) or not isinstance(sigma, list) or len(sigma) != len(mu):
        return None
    try:  # stay fail-open even on a ragged/short Σ from the engine
        return [float(m) for m in mu], [float(sigma[i][i]) for i in range(len(mu))]
    except (IndexError, TypeError, ValueError):
        return None


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
        means = [_mean(_spec(weights, n)) for n in names]

        vectors = []
        for a in articles:
            vec = [0.0] * len(names)
            for name, v in a["features"]:
                vec[idx[name]] = float(v)
            vectors.append(vec)

        res = await _call("score-batch", [means, vectors])
        if res is None or len(res) != len(articles):
            return None
        return {int(a["entry_id"]): float(s) for a, s in zip(articles, res, strict=True)}
    except Exception as exc:  # never break the reader on a ranking hiccup
        logger.debug("score failed open: %s", exc)
        return None


async def explain(article: dict, top: int = 4) -> list[dict] | None:
    """The largest ± per-feature contributions to one article's score (the "why
    ranked"). `article` is one ranker.build_articles() entry. Returns
    [{name, value, dir}] sorted by |contribution|, or None if unavailable. The
    contribution math (mean(wᵢ)·featureᵢ) runs in the model, never in Python."""
    feats = article.get("features", [])
    if not feats:
        return None
    try:
        weights = _weights_of(await read_state())
        names = [n for n, _v in feats]
        means = [_mean(_spec(weights, n)) for n in names]
        values = [float(v) for _n, v in feats]
        res = await _call("contributions", [means, values])
        if res is None or len(res) != len(names):
            return None
        pairs = [(n, float(c)) for n, c in zip(names, res, strict=True) if abs(float(c)) > 1e-6]
        pairs.sort(key=lambda p: abs(p[1]), reverse=True)
        return [{"name": n, "value": c, "dir": "up" if c > 0 else "down"}
                for n, c in pairs[:top]]
    except Exception as exc:
        logger.debug("explain failed open: %s", exc)
        return None


async def observe(events: list[dict], base_weights: dict | None = None) -> dict | None:
    """Fold a batch of observations into the weights via the Bayesian-linear-
    regression update. `events` is ranker.build_observation() output —
    [{signal, value, features:[[name, value], ...]}, ...]. Each event is one noisy
    measurement of the whole score (y ~ Normal(Σ wᵢxᵢ, σ²)), so its error is shared
    across the active features instead of duplicated onto each. Returns
    {"weights": {...}, "obs_count": n_applied} layered on `base_weights`, or None if
    the engine is unavailable (so the caller leaves the high-water mark and retries)."""
    if not events:
        return None
    if not await _ensure_started():
        return None
    weights = dict(base_weights if base_weights is not None else _weights_of(await read_state()))
    applied = 0
    for ev in events:
        y = _evidence(ev["signal"], ev.get("value"))
        # active features = those actually present (nonzero value)
        active = [(n, float(v)) for n, v in ev.get("features", []) if float(v) != 0.0]
        if y == 0.0 or not active:
            continue
        names = [n for n, _v in active]
        xs = [v for _n, v in active]
        specs = [_spec(weights, n) for n in names]
        prior = _mv_spec([_mean(s) for s in specs], [_var(s) for s in specs])
        # The engine's (MvGaussian, LinearGaussian) conjugate does the joint update.
        out = _read_mv(await _call("observe", [prior, xs, y, _SIGMA_OBS]))
        if out is None or len(out[0]) != len(names):
            return None  # fail open — don't lose events
        mus, variances = out
        for name, mu, var in zip(names, mus, variances, strict=True):
            weights[name] = _to_spec(mu, var)
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
            "SELECT id, entry_id, signal, value, detail FROM engagement_events "
            "WHERE id > %s ORDER BY id LIMIT %s",
            (last_id, limit),
        )
        rows = await cur.fetchall()
        pcur = await conn.execute("SELECT feed_id, priority FROM feed_config")
        priorities = {r["feed_id"]: r["priority"] for r in await pcur.fetchall()}
    if not rows:
        return 0

    # embed_sim for the touched entries (empty if no taste centroid yet) so the
    # embedding feature learns alongside the structured ones. Mute rows have no
    # entry (or an incidental one — see build_mute_observation) so they're excluded.
    from app import embeddings
    async with get_conn() as conn:
        sims = await embeddings.embed_sims(conn, list({
            r["entry_id"] for r in rows
            if r["entry_id"] is not None and not ranker.is_mute_signal(r["signal"])
        }))

    now = datetime.now(UTC)
    entry_cache: dict[int, dict] = {}
    events, max_id = [], last_id
    for r in rows:
        max_id = max(max_id, r["id"])
        if ranker.is_mute_signal(r["signal"]):
            obs = ranker.build_mute_observation(r["signal"], r["detail"])
            if obs is not None:
                events.append(obs)
            continue
        eid = r["entry_id"]
        if eid is None:
            continue
        if eid not in entry_cache:
            try:
                entry_cache[eid] = await miniflux_client.get_entry(eid)
            except Exception:
                entry_cache[eid] = {}
        entry = entry_cache[eid]
        prio = priorities.get(entry.get("feed_id"), 2)
        events.append(ranker.build_observation(
            entry, r["signal"], r["value"] or 1.0, prio, now, sims.get(eid)))

    res = await observe(events, base_weights=base_weights)
    if res is None:
        return 0  # engine down — leave the mark so we retry next tick
    await _persist_state(res["weights"], prev_obs + res["obs_count"], max_id)
    logger.info("ranker folded %d engagement events (through id %d)", len(events), max_id)
    return len(events)
