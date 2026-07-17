#!/usr/bin/env python
"""End-to-end check of the rssfeed ↔ Credence skin wire (Part C).

Mirrors ~/git/credence/examples/maut_demo_wire.py but in rssfeed terms: loads
ranker/model.bdsl into the skin and drives the NATIVE linear-Gaussian conjugate.
It (1) sends a thumb-up on a single-feature article and confirms that weight
rises; (2) sends a thumb-down on a two-feature article — a confident source plus a
diffuse author — and confirms the engine's joint update shares the error by
uncertainty (the confident weight barely moves, the diffuse one absorbs it:
explaining-away); (3) scores two articles and confirms the ranking reflects the
learned weights. Beliefs cross as plain {type:mv_gaussian/gaussian} JSON — no
state_id, no probability math in Python. Then confirms fail-open: a bogus engine
command makes the adapter's score() return None.

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

        # (1) thumb_up (y=+1) on a single-feature article by alice. The active
        # weights cross as a JOINT mv_gaussian (here 1-D); the native conjugate
        # returns the conditioned belief — no state_id, no Python math.
        mv = {"type": "mv_gaussian", "mu": [0.0], "sigma": [[1.0]]}
        post = skin.call_dsl("model", "observe", [mv, [1.0], 1.0, 1.0])
        assert post["type"] == "mv_gaussian" and "state_id" not in post, post
        alice_mu = post["mu"][0]
        print(f"after thumb_up on alice: mu {alice_mu:.3f}")
        assert alice_mu > 0.0, "alice weight should rise"

        # (2) thumb_down (y=−1) on an article from a CONFIDENT liked source (small
        # σ) + a DIFFUSE author. The engine's joint update must share the error by
        # uncertainty: the confident weight barely moves, the diffuse one absorbs it.
        joint = {"type": "mv_gaussian", "mu": [0.8, 0.0],
                 "sigma": [[0.09, 0.0], [0.0, 1.0]]}        # σ 0.3 (confident), 1.0 (diffuse)
        post2 = skin.call_dsl("model", "observe", [joint, [1.0, 1.0], -1.0, 1.0])
        src_mu, auth_mu = post2["mu"]
        print(f"after thumb_down on [confident src, diffuse author]: "
              f"src 0.800->{src_mu:.3f}, author 0.000->{auth_mu:.3f}")
        assert abs(src_mu - 0.8) < abs(auth_mu - 0.0), "confident source must move less"
        assert post2["sigma"][0][1] < 0.0, "joint update induces explaining-away covariance"

        # (3) Score two articles over [alice, bob] using the learned means.
        means = [alice_mu, 0.0]
        scores = skin.call_dsl("model", "score-batch", [means, [[1.0, 0.0], [0.0, 1.0]]])
        print(f"article scores [alice, bob]: {scores}")
        assert scores[0] > scores[1], scores
        print("OK: rss feature weights learn over the wire via the native "
              "linear-Gaussian conjugate — no state_id, no Python math")
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
