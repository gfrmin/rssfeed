# Cross-feed learning ranker — Credence skin wire (Part C)

Orders the cross-feed Unread/All views by inferred preference, learned from
engagement (star / thumbs / open-original / dwell — never plain reads, which are
swipe noise). It **degrades gracefully**: if the engine can't start or a call
errors, the reader falls back to priority+recency ordering, so ranking never
breaks. The Smart/Newest toggle and warmth meter live in `_list.html`.

## Architecture

The preference model is a **pure BDSL program** (`model.bdsl`) — a parametric MAUT
model where each feature (`feed:<id>`, `author:<slug>`, `tag:<slug>`, `recency`,
`priority`) has a signed Gaussian **weight** belief, and an article's score is
`Σ mean(wᵢ)·featureᵢ`.

It is consumed **only** over the **Credence skin wire** (JSON-RPC over stdio, via
the `credence-skin-client` package). The engine is **stateless-per-call**: the
model program is loaded once into a warm subprocess, and every durable belief lives
here in Postgres (`ranker_state.state_blob.weights` as plain
`{"type":"gaussian","mu":..,"sigma":..}` specs). Python never does probability
arithmetic — every update/score goes through `call_dsl` into the pure model
(`condition`/`mean`); Python only persists belief-specs, maps feature names ↔
positional indices, and maps each engagement signal to a signed evidence scalar.

```
engagement_events ──(worker)──▶ ranker_client.observe ──call_dsl observe──▶ skin
        feed_config priorities                                   │
ranker_state.weights ◀── persist (atomic w/ high-water mark) ◀───┘
entry_list (cross-feed) ── ranker_client.score ──call_dsl score-batch──▶ skin ──▶ order
```

- `app/ranker.py` — feature extraction (entries → `[name, value]` vectors; events → observations).
- `app/ranker_client.py` — the warm `SkinClient`, the weight store, `score`/`observe`/`sync_observations`, fail-open.
- `model.bdsl` — the pure model (`observe`, `score-batch`, `contributions`).

## Runtime

The engine spawn is config-driven (`app/config.py`):

- **Dev / this host (default):** spawn the Julia skin from the local credence
  checkout — `CREDENCE_SKIN_SERVER` (`~/git/credence/apps/skin/server.jl`) +
  `CREDENCE_SKIN_PROJECT` (`~/git/credence`). Needs `julia` on PATH and the credence
  Julia project instantiated (`julia --project=~/git/credence -e 'using Pkg; Pkg.instantiate()'`).
- **Portable / production:** set `CREDENCE_SKIN_COMMAND` to a JSON argv for the
  pinned image, e.g. `["docker","run","--rm","-i","ghcr.io/gfrmin/credence-skin:latest"]`.
  This overrides the local-Julia spawn — no Julia toolchain needed on the app side.

The `credence-skin-client` dependency is sourced from the sibling credence checkout
(`[tool.uv.sources]` in `pyproject.toml`); it is pure stdlib with no engine code.

## Verify

```
uv run python scripts/verify_skin_ranker.py   # wire proof + fail-open, needs the local skin
uv run pytest tests/test_credence_client.py    # adapter unit tests, engine mocked
```

## Disabling

Set `RANKER_ENABLED=0`. The reader is fully functional without it — ranking is
purely additive and always has the Newest escape hatch.
