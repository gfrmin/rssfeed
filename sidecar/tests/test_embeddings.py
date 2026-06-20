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
    async def fake_centroid(conn):
        return [1.0, 0.0]

    async def fake_stored(conn, ids):
        return {1: [1.0, 0.0], 2: [0.0, 1.0], 3: [-1.0, 0.0]}

    monkeypatch.setattr(embeddings, "_centroid", fake_centroid)
    monkeypatch.setattr(embeddings, "_stored", fake_stored)
    out = run(embeddings.embed_sims(None, [1, 2, 3]))
    assert out[1] == 1.0            # identical → 1
    assert out[2] == 0.0           # orthogonal → 0
    assert out[3] == 0.0           # opposite → clamped to 0


def test_embed_sims_empty_without_centroid(monkeypatch):
    async def no_centroid(conn):
        return None

    monkeypatch.setattr(embeddings, "_centroid", no_centroid)
    assert run(embeddings.embed_sims(None, [1, 2])) == {}
