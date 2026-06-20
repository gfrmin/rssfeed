# Cross-feed ranker runner

This directory is the home for the **ranker runner** — a small, long-lived process
that holds the warm learned posterior and scores articles for the cross-feed Unread/All
views. The rssfeed sidecar talks to it over a tiny localhost JSON contract and **degrades
gracefully**: if the runner is down or cold, the reader falls back to priority+recency
ordering (you'll see a `learning…` hint on the Smart toggle).

The rssfeed (Python) side is **done and proven against this contract** (see
`app/ranker.py`, `app/ranker_client.py`, and the `order=smart|new` path in
`app/routes/entries.py`). What lands here is the **credence-side** deliverable — see the
requirements handed to the credence developer at
`~/git/credence/docs/rssfeed-ranker-requirements.md`.

## The contract (the runner must speak this on `RANKER_URL`, default `http://localhost:8092`)

| method + path | request | response |
|---|---|---|
| `POST /load` | `{model_version, weights:{name:[p,q]}}` | `{loaded:n}` |
| `POST /observe` | `{events:[{signal, features:[name,…], value}]}` | `{weights:{name:[p,q]}, obs_count}` |
| `POST /score` | `{articles:[{entry_id, features:[[name,value],…]}]}` | `{scores:[[entry_id, score],…]}` |
| `GET /health` | — | `{status, obs_count, n_weights}` |

- `name` — stable feature key: `feed:<id>`, `author:<slug>`, `tag:<slug>`, `recency`, `priority`.
- `value` — feature value in [0,1] for scoring; for `/observe`, the event value
  (dwell seconds, ±1 for thumbs, 1 otherwise).
- `[p,q]` — serialized conjugate params (`[α,β]` for Beta, `[μ,σ]` for Gaussian). The
  sidecar persists these as JSON in the `ranker_state` Postgres row and replays them via
  `/load` when the runner restarts. **Do not** use credence's binary `save_state`.
- `signal` ∈ `star | unstar | thumb_up | thumb_down | open_original | dwell`.

## Files

- `model.bdsl` — starter parametric-MAUT model (Beta-only, runs on stock credence). A
  reference for the runner; the credence developer may refine or replace it.
- `run.jl` — **(to be provided by the credence developer)** the warm process: `using
  Credence` (via `LOAD_PATH`/`--project=$CREDENCE_SRC`), `load_dsl(model.bdsl)` once, hold
  the posterior under a lock, and serve the contract above. `apps/julia/rss/server.jl` in
  credence is a template for the HTTP+locked-global pattern.
- `rssfeed-ranker.service` — systemd **user** unit template. Once `run.jl` exists:
  `cp rssfeed-ranker.service ~/.config/systemd/user/ && systemctl --user enable --now rssfeed-ranker`.

## Disabling

Set `RANKER_ENABLED=0` (or just don't start the runner). The reader is fully functional
without it — ranking is purely additive.
