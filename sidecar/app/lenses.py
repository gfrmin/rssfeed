"""Reading lenses — deterministic orderings layered on the learned ranker.

A lens is a *presentation* of the same candidate pool: `smart` is the raw learned
score, `new` is reverse-chron, and `catchup`/`dive` are fixed blends of the score
with recency / taste-similarity. Blends are post-processing only — no lens needs
its own trained model, and every lens degrades to something sensible when the
engine or the embeddings are down.
"""

from datetime import datetime

from app import ranker

LENSES = ("smart", "new", "catchup", "dive")
DEFAULT_LENS = "smart"
CATCHUP_HALFLIFE_HOURS = 6.0


def normalize(order: str | None, cookie: str | None = None) -> str:
    """Resolve the active lens: explicit param > cookie > default."""
    if order in LENSES:
        return order
    if cookie in LENSES:
        return cookie
    return DEFAULT_LENS


def _minmax(scores: dict[int, float]) -> dict[int, float]:
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    if hi <= lo:
        return {k: 0.5 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


def blend(lens: str, entry: dict, s_norm: float | None,
         sim: float, now: datetime) -> float:
    """The descending sort value for one entry under a blend lens."""
    pri = ranker.PRIORITY_SCALAR.get(entry.get("_priority", 2), 0.5)
    if lens == "catchup":
        rec6 = ranker.recency(entry.get("published_at"), now,
                              half_life_hours=CATCHUP_HALFLIFE_HOURS)
        if s_norm is None:
            return 0.8 * rec6 + 0.2 * pri
        return 0.7 * rec6 + 0.2 * s_norm + 0.1 * pri
    # dive — deliberately recency-light rather than recency-free: the learned
    # score already contains its own recency term (RECENCY_HALFLIFE_HOURS), so
    # a month-old high-affinity item can still lose to a fresh mediocre one when
    # scores are in play. The relative weighting toward similarity is the point.
    if s_norm is None:
        return 0.7 * sim + 0.3 * pri
    return 0.5 * s_norm + 0.5 * sim


def order_entries(lens: str, entries: list[dict],
                  scores: dict[int, float] | None,
                  sims: dict[int, float], now: datetime) -> tuple[list[dict], bool]:
    """Sort the candidate pool for a lens. Returns (sorted_entries, ranked) where
    `ranked` means the learned score influenced the order (drives the why-chips).
    Annotates `_score` on entries; the list itself is a new sorted copy — muted
    entries always sink, and newest is always the tiebreak."""
    out = sorted(entries, key=lambda e: e.get("published_at") or "", reverse=True)
    if lens == "new":
        return out, False
    if lens == "smart":
        if scores:
            for e in out:
                e["_score"] = scores.get(e["id"], 0.0)
            out.sort(key=lambda e: (e.get("_muted", False), -e.get("_score", 0.0)))
            return out, True
        out.sort(key=lambda e: (e.get("_muted", False), e.get("_priority", 2)))
        return out, False
    # blend lenses
    s_norm = _minmax(scores) if scores else None
    for e in out:
        e["_score"] = blend(lens, e,
                            None if s_norm is None else s_norm.get(e["id"], 0.5),
                            sims.get(e["id"], 0.0), now)
    out.sort(key=lambda e: (e.get("_muted", False), -e.get("_score", 0.0)))
    return out, bool(scores)
