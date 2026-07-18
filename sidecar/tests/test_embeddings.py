"""Unit tests for the embedding-similarity feature (Part C phase 2).

Ollama and the database are not touched: cosine/text are pure, and embed_sims is
tested with the centroid/storage helpers monkeypatched.
"""
import asyncio

from app import embeddings


def run(coro):
    return asyncio.run(coro)


def test_cosine():
    assert embeddings.cosine([1, 0], [1, 0]) == 1.0
    assert embeddings.cosine([1, 0], [0, 1]) == 0.0
    assert embeddings.cosine([1, 2, 3], [1, 2]) == 0.0      # mismatched length
    assert embeddings.cosine([], [1]) == 0.0
    assert embeddings.cosine([0, 0], [1, 1]) == 0.0          # zero vector


def test_text_of_strips_and_truncates():
    e = {"title": "Hi", "content": "<p>Hello <b>world</b></p>"}
    t = embeddings._text_of(e)
    assert "<" not in t and "Hello world" in t and t.startswith("Hi.")
    long = {"title": "T", "content": "x " * 5000}
    assert len(embeddings._text_of(long)) <= embeddings._MAX_CHARS


def test_embed_sims_clamps_and_maps(monkeypatch):
    async def fake_centroids(conn):
        return [[1.0, 0.0]]

    async def fake_stored(conn, ids):
        return {1: [1.0, 0.0], 2: [0.0, 1.0], 3: [-1.0, 0.0]}

    monkeypatch.setattr(embeddings, "_centroids", fake_centroids)
    monkeypatch.setattr(embeddings, "_stored", fake_stored)
    out = run(embeddings.embed_sims(None, [1, 2, 3]))
    assert out[1] == 1.0            # identical → 1
    assert out[2] == 0.0           # orthogonal → 0
    assert out[3] == 0.0           # opposite → clamped to 0


def test_embed_sims_empty_without_centroid(monkeypatch):
    async def no_centroids(conn):
        return []

    monkeypatch.setattr(embeddings, "_centroids", no_centroids)
    assert run(embeddings.embed_sims(None, [1, 2])) == {}


def test_embed_sims_max_over_centroids(monkeypatch):
    async def cents(conn):
        return [[1.0, 0.0], [0.0, 1.0]]

    async def stored(conn, ids):
        return {1: [0.0, 1.0], 2: [0.7, 0.7]}

    monkeypatch.setattr(embeddings, "_centroids", cents)
    monkeypatch.setattr(embeddings, "_stored", stored)
    out = run(embeddings.embed_sims(None, [1, 2]))
    assert out[1] == 1.0                       # exact match to second centroid
    assert 0.70 < out[2] < 0.71                # max of the two ≈ 0.7071


# ---- taste_candidates: the deep pool's SQL-side candidate discovery (WP5/WP6) ----

def test_taste_candidates_no_centroid(monkeypatch):
    async def no_centroids(conn):
        return []

    monkeypatch.setattr(embeddings, "_centroids", no_centroids)
    assert run(embeddings.taste_candidates(None, [], limit=10)) == []


def test_taste_candidates_fails_open_on_query_error(monkeypatch):
    async def centroids(conn):
        return [[1.0, 0.0]]

    monkeypatch.setattr(embeddings, "_centroids", centroids)
    # conn=None → conn.execute raises → caught → []
    assert run(embeddings.taste_candidates(None, [1], limit=10)) == []


# ---- _kmeans: pure-Python k-means over embedding vectors (WP6) ----

def test_kmeans_separates_obvious_clusters():
    a = [[1.0, 0.0, 0.0]] * 6
    b = [[0.0, 1.0, 0.0]] * 6
    c = [[0.0, 0.0, 1.0]] * 6
    cents = embeddings._kmeans(a + b + c, 3)
    assert len(cents) == 3
    # each true direction is some centroid's dominant axis
    for axis in range(3):
        assert any(ct[axis] > 0.99 for ct in cents)


def test_kmeans_deterministic_and_degenerate():
    vecs = [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [0.1, 0.9], [0.5, 0.5]]
    assert embeddings._kmeans(vecs, 2) == embeddings._kmeans(vecs, 2)   # seeded
    assert embeddings._kmeans(vecs, 1) == [embeddings._mean_vec(vecs)]  # k=1 → mean
    assert embeddings._kmeans([[1.0, 0.0]], 4) == [[1.0, 0.0]]          # n <= k → mean
    assert embeddings._kmeans([], 4) == []


# ---- pick_related: turning nearest-neighbours into a useful list ----

def _cand(entry_id, title, sim, feed_id=1):
    return {"entry_id": entry_id, "title": title, "sim": sim, "feed_id": feed_id}


def test_pick_related_drops_the_same_story_back():
    """The nearest vector to an article is often the article itself, syndicated or
    re-snapshotted. Technically similar, useless to offer as further reading."""
    cands = [
        _cand(2, "Same Headline", 0.99, feed_id=9),
        _cand(3, "A Different Story", 0.8, feed_id=9),
    ]
    got = embeddings.pick_related(cands, "same headline")   # case/space-insensitive
    assert [c["entry_id"] for c in got] == [3]


def test_pick_related_dedupes_cross_posts_keeping_best():
    cands = [
        _cand(2, "Wire Story", 0.9, feed_id=1),
        _cand(3, "Wire Story", 0.85, feed_id=2),   # same headline elsewhere
        _cand(4, "Other", 0.8, feed_id=3),
    ]
    got = embeddings.pick_related(cands, "Target")
    assert [c["entry_id"] for c in got] == [2, 4]


def test_pick_related_caps_per_feed():
    cands = [_cand(i, f"Story {i}", 0.9, feed_id=7) for i in range(2, 8)]
    got = embeddings.pick_related(cands, "Target", per_feed=2)
    assert len(got) == 2


def test_pick_related_applies_similarity_floor():
    cands = [_cand(2, "Close", 0.75), _cand(3, "Distant", 0.4, feed_id=2)]
    got = embeddings.pick_related(cands, "Target", min_sim=0.7)
    assert [c["entry_id"] for c in got] == [2]


def test_pick_related_caps_and_preserves_order():
    cands = [_cand(i, f"S{i}", 0.9 - i / 100, feed_id=i) for i in range(2, 12)]
    got = embeddings.pick_related(cands, "Target", k=5)
    assert len(got) == 5
    assert [c["entry_id"] for c in got] == [2, 3, 4, 5, 6]


def test_pick_related_skips_untitled_candidates():
    got = embeddings.pick_related([_cand(2, None, 0.9), _cand(3, "Real", 0.85, 2)], "T")
    assert [c["entry_id"] for c in got] == [3]


# ---- _backfill_plan: what gets embedded, what only needs metadata ----

def test_backfill_plan_prefers_snapshot_text_over_rss_body():
    """The extracted article is the whole point of this app; the RSS body is often
    a truncated teaser. Embed the good text when we have it."""
    entries = [{"id": 1, "title": "T", "content": "<p>rss teaser</p>"}]
    to_embed, meta_only = embeddings._backfill_plan(
        entries, have_emb=set(), snapshot_texts={1: "the full extracted article"}
    )
    assert meta_only == []
    (_eid, text, _entry), = to_embed
    assert "full extracted article" in text and "teaser" not in text


def test_backfill_plan_falls_back_to_rss_when_no_snapshot():
    entries = [{"id": 1, "title": "T", "content": "<p>rss body</p>"}]
    to_embed, _ = embeddings._backfill_plan(entries, set(), {})
    assert "rss body" in to_embed[0][1]


def test_backfill_plan_already_embedded_needs_metadata_only():
    """Rows embedded before feed_id/title/published_at existed get them attached as
    the cursor sweeps past — without paying for a re-embed."""
    entries = [{"id": 1, "title": "T", "content": "x"}]
    to_embed, meta_only = embeddings._backfill_plan(entries, have_emb={1}, snapshot_texts={})
    assert to_embed == []
    assert [e["id"] for e in meta_only] == [1]


def test_backfill_plan_skips_entries_with_no_text():
    entries = [{"id": 1, "title": "", "content": ""}]
    to_embed, meta_only = embeddings._backfill_plan(entries, set(), {})
    assert to_embed == [] and meta_only == []


def test_backfill_plan_ignores_blank_snapshot():
    entries = [{"id": 1, "title": "T", "content": "<p>rss body</p>"}]
    to_embed, _ = embeddings._backfill_plan(entries, set(), {1: "   "})
    assert "rss body" in to_embed[0][1]
