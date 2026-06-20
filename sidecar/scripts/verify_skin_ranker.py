#!/usr/bin/env python
"""End-to-end check of the rssfeed ↔ Credence skin wire (Part C).

Mirrors ~/git/credence/examples/maut_demo_wire.py but in rssfeed terms: loads
ranker/model.bdsl into the skin, seeds two feature weights (two authors), sends a
thumb-up on one, confirms ITS weight moves and the other doesn't, scores two
articles, and confirms the ranking reflects the update — all with belief-specs as
plain JSON, no state_id, no probability math in Python. Then confirms fail-open:
a bogus engine command makes the adapter's score() return None.

    uv run python scripts/verify_skin_ranker.py
"""

import asyncio
import os
import sys
from pathlib import Path

SIDE = Path(__file__).resolve().parent.parent
MODEL = (SIDE / "ranker" / "model.bdsl").read_text()
HOME = os.path.expanduser("~")
SERVER = os.environ.get("CREDENCE_SKIN_SERVER", f"{HOME}/git/credence/apps/skin/server.jl")
PROJECT = os.environ.get("CREDENCE_SKIN_PROJECT", f"{HOME}/git/credence")


def wire_proof() -> None:
    from credence_skin_client import SkinClient

    skin = SkinClient(server_path=SERVER, project=PROJECT)
    try:
        info = skin.initialize(dsl_sources={"model": MODEL})
        print(f"engine {info['version']}, wire protocol {info['protocol']}")

        # Two author-weight priors as plain belief-specs (rssfeed's durable store
        # holds exactly these as JSON in ranker_state).
        prior = lambda: {"type": "gaussian", "mu": 0.0, "sigma": 1.0}
        names = ["author:alice", "author:bob"]
        weights = {n: prior() for n in names}

        # thumb_up on an article by alice → condition alice's weight only (+1.0).
        updated = skin.call_dsl("model", "observe-batch", [[weights["author:alice"]], 1.0])
        assert isinstance(updated, list) and "state_id" not in updated[0], updated
        weights["author:alice"] = updated[0]
        print(f"after thumb_up on alice: {weights}")
        assert weights["author:alice"]["mu"] > 0.0, "alice weight should rise"
        assert weights["author:bob"]["mu"] == 0.0, "bob weight must be untouched"

        # Score two articles: [by-alice], [by-bob], positional over [alice, bob].
        wl = [weights["author:alice"], weights["author:bob"]]
        scores = skin.call_dsl("model", "score-batch", [wl, [[1.0, 0.0], [0.0, 1.0]]])
        print(f"article scores [alice, bob]: {scores}")
        assert scores[0] > scores[1], scores
        print("OK: rss feature weights learn over the wire — no state_id, no Python math")
    finally:
        skin.shutdown()


def fail_open() -> None:
    """A broken engine must not raise into the caller — the adapter degrades to
    None so the reader keeps its priority+recency ordering. Tested at the engine
    layer (no DB needed): a bogus spawn command can't start, so _call → None."""
    os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
    os.environ["CREDENCE_SKIN_COMMAND"] = '["false"]'  # exits immediately, never ready
    sys.path.insert(0, str(SIDE))
    import importlib
    import app.config as cfg
    importlib.reload(cfg)
    import app.ranker_client as rc
    importlib.reload(rc)

    assert asyncio.run(rc._call("score-batch", [[], []])) is None, "engine should be down"
    print("OK: broken engine → adapter fails open to None (caller keeps tier sort)")


if __name__ == "__main__":
    wire_proof()
    fail_open()
    print("\nALL CHECKS PASSED")
