"""Unit tests for the Credence-skin ranker adapter (Part C).

The engine is mocked: we monkeypatch `read_state` (the Postgres weight store) and
`_call` (the skin wire) so these run with no engine and no database — exercising
the name→index mapping, evidence handling, weight folding, and fail-open paths.
"""
import asyncio

import pytest

from app import ranker_client as rc


def run(coro):
    return asyncio.run(coro)


# ---- evidence mapping ----

def test_evidence_signal_scalars():
    assert rc._evidence("star", 1) == 1.0
    assert rc._evidence("thumb_up", 1) == 1.0
    assert rc._evidence("open_original", 1) == 0.5
    assert rc._evidence("unstar", 1) == -0.5
    assert rc._evidence("thumb_down", 1) == -1.0
    assert rc._evidence("read", 1) == 0.0          # plain reads carry no evidence
    assert rc._evidence("unknown", 1) == 0.0


def test_evidence_dwell_graded_and_clamped():
    assert rc._evidence("dwell", 4) == pytest.approx(0.2)     # at/under the floor
    assert rc._evidence("dwell", 45) == pytest.approx(0.5)    # mid
    assert rc._evidence("dwell", 1000) == 1.0                 # saturates


# ---- weight helpers ----

def test_weight_seeds_prior_for_unseen():
    w = rc._weight({}, "author:x")
    assert w == rc._PRIOR and w is not rc._PRIOR        # a copy, not the shared prior
    existing = {"author:y": {"type": "gaussian", "mu": 0.5, "sigma": 0.3}}
    assert rc._weight(existing, "author:y")["mu"] == 0.5


def test_weights_of_null_coalesces():
    assert rc._weights_of(None) == {}
    assert rc._weights_of({"state_blob": None}) == {}
    assert rc._weights_of({"state_blob": {"weights": {"a": 1}}}) == {"a": 1}


# ---- score(): name→index mapping + fail-open ----

def _patch(monkeypatch, weights=None, call=None):
    async def fake_read_state():
        return {"state_blob": {"weights": weights or {}}}
    monkeypatch.setattr(rc, "read_state", fake_read_state)
    if call is not None:
        async def fake_call(function, args):
            return call(function, args)
        monkeypatch.setattr(rc, "_call", fake_call)


def test_score_builds_positional_vectors(monkeypatch):
    captured = {}

    def call(function, args):
        captured["function"] = function
        captured["weights"], captured["vectors"] = args
        return [9.0, 1.0]  # one score per article, in input order

    _patch(monkeypatch, weights={"author:a": {"type": "gaussian", "mu": 0.8, "sigma": 0.4}},
           call=call)
    articles = [
        {"entry_id": 11, "features": [["author:a", 1.0], ["recency", 0.5]]},
        {"entry_id": 22, "features": [["feed:3", 1.0]]},
    ]
    out = run(rc.score(articles))
    assert out == {11: 9.0, 22: 1.0}
    # union order is first-seen: author:a, recency, feed:3
    assert captured["function"] == "score-batch"
    assert len(captured["weights"]) == 3
    assert captured["weights"][0]["mu"] == 0.8          # known weight passed through
    assert captured["weights"][2] == rc._PRIOR          # feed:3 seeded prior
    assert captured["vectors"][0] == [1.0, 0.5, 0.0]    # article 1 over [a, recency, feed:3]
    assert captured["vectors"][1] == [0.0, 0.0, 1.0]    # article 2


def test_score_empty_articles_is_empty_not_none(monkeypatch):
    _patch(monkeypatch, call=lambda f, a: [])
    assert run(rc.score([])) == {}


def test_score_fails_open_when_engine_down(monkeypatch):
    _patch(monkeypatch, call=lambda f, a: None)   # engine unreachable → _call None
    out = run(rc.score([{"entry_id": 1, "features": [["feed:1", 1.0]]}]))
    assert out is None


def test_score_fails_open_on_length_mismatch(monkeypatch):
    _patch(monkeypatch, call=lambda f, a: [1.0])  # fewer scores than articles
    out = run(rc.score([
        {"entry_id": 1, "features": [["feed:1", 1.0]]},
        {"entry_id": 2, "features": [["feed:2", 1.0]]},
    ]))
    assert out is None


# ---- observe(): evidence application + fail-open ----

def test_observe_folds_active_weights(monkeypatch):
    calls = []

    def call(function, args):
        calls.append(args)
        specs, obs = args
        # fake conjugate move: bump mu by obs for each active weight
        return [{"type": "gaussian", "mu": s.get("mu", 0.0) + obs, "sigma": 0.4} for s in specs]

    _patch(monkeypatch, call=call)
    monkeypatch.setattr(rc, "_ensure_started", _true)
    events = [
        {"signal": "star", "value": 1, "features": ["author:a", "feed:1"]},
        {"signal": "thumb_down", "value": 1, "features": ["author:b"]},
        {"signal": "read", "value": 1, "features": ["author:c"]},  # no evidence → skipped
    ]
    res = run(rc.observe(events, base_weights={}))
    assert res["obs_count"] == 2                        # read skipped
    assert res["weights"]["author:a"]["mu"] == 1.0      # star +1
    assert res["weights"]["author:b"]["mu"] == -1.0     # thumb_down -1
    assert "author:c" not in res["weights"]
    assert len(calls) == 2


def test_observe_fails_open_when_engine_down(monkeypatch):
    _patch(monkeypatch, call=lambda f, a: None)
    monkeypatch.setattr(rc, "_ensure_started", _true)
    res = run(rc.observe([{"signal": "star", "value": 1, "features": ["author:a"]}],
                         base_weights={}))
    assert res is None


async def _true():
    return True


# ---- explain(): per-feature contributions ----

def test_explain_ranks_contributions(monkeypatch):
    def call(function, args):
        assert function == "contributions"
        specs, vals = args
        return [s.get("mu", 0.0) * v for s, v in zip(specs, vals)]  # mean*feature

    _patch(monkeypatch, weights={
        "author:a": {"type": "gaussian", "mu": 0.9, "sigma": 0.4},
        "tag:x": {"type": "gaussian", "mu": -0.6, "sigma": 0.4},
    }, call=call)
    article = {"entry_id": 1, "features": [
        ["author:a", 1.0], ["tag:x", 1.0], ["recency", 0.0]]}  # recency contributes 0 → dropped
    out = run(rc.explain(article, top=3))
    assert [o["name"] for o in out] == ["author:a", "tag:x"]   # sorted by |contribution|
    assert out[0]["dir"] == "up" and out[1]["dir"] == "down"


def test_explain_fails_open(monkeypatch):
    _patch(monkeypatch, call=lambda f, a: None)
    assert run(rc.explain({"entry_id": 1, "features": [["feed:1", 1.0]]})) is None


def test_explain_no_features(monkeypatch):
    _patch(monkeypatch, call=lambda f, a: [])
    assert run(rc.explain({"entry_id": 1, "features": []})) is None
