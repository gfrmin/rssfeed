"""Feature extraction + observation building for the cross-feed ranker (Part C).

Turns Miniflux entries into the (feature-name, value) vectors the credence runner
scores, and engagement_events into observations to condition the model on. Feature
names are stable string keys; values are in [0, 1]. This module is pure Python and
has no dependency on the runner being up — it just shapes data for the contract.
"""

import re
from datetime import UTC, datetime

_SANITIZE = re.compile(r"[^a-z0-9]+")

# Recency decays with this half-life so fresh items keep a baseline edge even
# before the model has learned much.
RECENCY_HALFLIFE_HOURS = 36.0

_PRIORITY_SCALAR = {1: 1.0, 2: 0.5, 3: 0.0}


def feature_key(prefix: str, raw: str) -> str:
    s = _SANITIZE.sub("_", (raw or "").strip().casefold()).strip("_") or "unknown"
    return f"{prefix}:{s}"


def _tag_names(entry: dict) -> list[str]:
    out = []
    for t in entry.get("tags") or []:
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, dict):
            name = t.get("title") or t.get("name")
            if name:
                out.append(name)
    return out


def _recency(published_at, now: datetime,
            half_life_hours: float = RECENCY_HALFLIFE_HOURS) -> float:
    if not published_at:
        return 0.0
    try:
        dt = (published_at if isinstance(published_at, datetime)
              else datetime.fromisoformat(str(published_at).replace("Z", "+00:00")))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    except Exception:
        return 0.0
    age_h = max(0.0, (now - dt).total_seconds() / 3600.0)
    return 0.5 ** (age_h / half_life_hours)


def recency(published_at, now: datetime,
           half_life_hours: float = RECENCY_HALFLIFE_HOURS) -> float:
    """Public alias — the lenses blend on recency at other half-lives."""
    return _recency(published_at, now, half_life_hours)


PRIORITY_SCALAR = _PRIORITY_SCALAR


def entry_features(entry: dict, priority: int, now: datetime,
                   embed_sim: float | None = None) -> list[list]:
    """The [name, value] feature vector for one entry. Binary one-hots for
    feed/author/tag, continuous recency, the manual priority tier as a scalar, and
    (when available) embed_sim — cosine to the taste centroid (Part C phase 2)."""
    feats: list[list] = []
    fid = entry.get("feed_id")
    if fid:
        feats.append([f"feed:{fid}", 1.0])
    author = entry.get("author")
    if author:
        feats.append([feature_key("author", author), 1.0])
    for t in _tag_names(entry)[:5]:
        feats.append([feature_key("tag", t), 1.0])
    feats.append(["recency", round(_recency(entry.get("published_at"), now), 4)])
    feats.append(["priority", _PRIORITY_SCALAR.get(priority, 0.5)])
    if embed_sim is not None:
        feats.append(["embed_sim", round(float(embed_sim), 4)])
    return feats


def feature_names(entry: dict, priority: int, now: datetime,
                  embed_sim: float | None = None) -> list[str]:
    """The active feature names for an entry — the weights an observation updates."""
    return [name for name, val in entry_features(entry, priority, now, embed_sim)
            if val > 0]


def build_articles(entries: list[dict], priorities: dict[int, int], now: datetime,
                   embed_sims: dict[int, float] | None = None) -> list[dict]:
    """Shape entries into the score request payload (with embed_sim when known)."""
    sims = embed_sims or {}
    return [
        {
            "entry_id": e["id"],
            "features": entry_features(
                e, priorities.get(e.get("feed_id"), 2), now, sims.get(e["id"])),
        }
        for e in entries
    ]


def feature_label(name: str, feed_title: str | None = None) -> str:
    """Human-readable label for a feature key, for the 'why ranked' explanation."""
    if name == "recency":
        return "freshness"
    if name == "priority":
        return "priority tier"
    if name == "embed_sim":
        return "similar to your taste"
    if name.startswith("feed:"):
        return feed_title or "this source"
    if name.startswith("author:"):
        return "author " + name[len("author:"):].replace("_", " ").strip().title()
    if name.startswith("tag:"):
        return "#" + name[len("tag:"):].replace("_", "")
    return name


def build_observation(entry: dict, signal: str, value: float,
                      priority: int, now: datetime,
                      embed_sim: float | None = None) -> dict:
    """Shape one engagement event + its entry into an observe event. Features are
    [name, value] pairs (the BLR update needs the values, not just the names)."""
    return {
        "signal": signal,
        "value": value,
        "features": entry_features(entry, priority, now, embed_sim),
    }
