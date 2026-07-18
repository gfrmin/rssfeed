"""Unit tests for the deep-pool merge helpers (WP5)."""
from app.routes.entries import _dedup_by_id


def test_dedup_by_id_keeps_first():
    es = [{"id": 1, "a": "x"}, {"id": 2}, {"id": 1, "a": "y"}]
    out = _dedup_by_id(es)
    assert [e["id"] for e in out] == [1, 2] and out[0]["a"] == "x"
