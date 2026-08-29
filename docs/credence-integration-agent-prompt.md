# Agent prompt — wire rssfeed to the Credence engine (skin wire)

> Input prompt for the developer agent that implements the consumer side of the
> RSS preference-ranking migration. The Credence **engine** side is already done
> and merged (in `~/git/credence`, decouple Moves 0 + 2). This task is the
> **consumer** side, in this repo (`~/git/rssfeed`).

> **Status: implemented and merged.** This is kept as the design record of *why*
> the ranker is shaped the way it is, not as work to do. The adapter it proposes
> as `sidecar/app/credence_client.py` landed as `sidecar/app/ranker_client.py`
> (with `sidecar/app/ranker.py` for the model side); the "Read first" list below
> points at files in the separate [credence](https://github.com/gfrmin/credence)
> repo, not this one.

## Role

You are implementing the consumer side of an RSS preference-ranking migration in
`~/git/rssfeed` (Python/FastAPI sidecar; `uv`; PostgreSQL 17 shared with Miniflux;
Docker Compose). Wire rssfeed to the Credence Bayesian engine over its **skin
wire** so a parametric **MAUT** preference model ranks articles and learns from
engagement.

## Read first (in `~/git/credence`, on `master`)

- `docs/rssfeed-ranker-requirements.md` — the contract rssfeed itself authored
  (the model, the engagement signals, the `/load /observe /score /health` shapes,
  plain-params persistence). Treat as requirements, but note it predates the final
  architecture; where it sketches a credence-provided HTTP server, that was
  **declined** — see Architecture below.
- `docs/decouple/move-2-design.md` and `docs/decouple/master-plan.md` — the
  architecture you must conform to.
- `examples/maut_demo.bdsl` — the domain-neutral **pure** model to mirror.
- `examples/maut_demo_wire.py` — a runnable end-to-end example of the exact wire
  dance.
- `apps/skin/protocol.md` — the JSON-RPC wire spec (protocol 1.1).
- `apps/skin/clients/python/` — the `credence-skin-client` package (your dependency).

## Architecture (non-negotiable — this is the whole point)

1. The credence engine is consumed **only** over the skin wire (JSON-RPC). Run the
   engine as the `ghcr.io/gfrmin/credence-skin` image and talk to it over **stdio**
   via the `credence-skin-client` `command` seam:
   `SkinClient(command=["docker","run","--rm","-i","ghcr.io/gfrmin/credence-skin:latest"])`.
   There is **no** HTTP engine server and you must not ask for one. (For local dev
   without pulling, you may instead point `credence-skin-client` at a local checkout's
   `apps/skin/server.jl` via `server_path=`+`project=`, but the image+`command` seam is
   the production path.)
2. **rssfeed is the only stateful layer.** All durable belief state lives in Postgres
   as plain JSON **belief-specs**: `{"type":"gaussian","mu":..,"sigma":..}` or
   `{"type":"beta","alpha":..,"beta":..}`. The engine is **stateless-per-call**.
3. The preference **model** is a **pure BDSL program** you write and ship in this repo
   (e.g. `rssfeed/model.bdsl`), loaded into the engine at runtime via
   `initialize(dsl_sources={"model": <source string>})`. The engine never reads your
   filesystem; you pass the BDSL **source** over the wire.
4. **No probability arithmetic in Python.** You never compute scores, posteriors, or
   means, and never touch `alpha`/`beta`/`mu`/`sigma` arithmetically in the body. Every
   belief update and every score goes through `call_dsl` into the pure model (which uses
   `condition`/`expect`/`mean`). Python only: persists belief-specs, maps feature
   names↔positional indices, extracts features, captures events, serves HTTP.
5. Beliefs cross the wire as declarative `{type, params}` specs. `call_dsl` is a pure
   belief-function evaluator: pass a belief-spec arg → it's reconstructed (not
   registered); a returned belief comes back as a belief-spec (no `state_id`). A returned
   numeric vector comes back as `{value:[...]}` (the client unwraps it). Non-numeric
   lists that aren't beliefs get wrapped as opaque state ids — so design model returns to
   be either beliefs, lists of beliefs, or numeric vectors. **Scores must be a positional
   numeric vector** matching your name→index order.

## Deliverables (in `~/git/rssfeed`)

**A. `rssfeed/model.bdsl` — the pure MAUT model.** Weights are a positional list of
per-feature belief-specs (you hold the name→index map in Python). Provide pure closures:

- `observe(weights, active_indices, signal, value) -> updated weights` — conditions the
  active features' weights with the kernel appropriate to `signal`; returns the full
  updated weight list. Pure: beliefs in, beliefs out.
- `score-batch(weights, articles) -> numeric vector` — `articles` = list of positional
  feature-value lists; score = `Σ mean(wᵢ)·featureᵢ` via `map`/`fold`; returns scores in
  input order.

Use the `:family` roster (see `router.bdsl` note + move-2 design): `:family bernoulli`
(obs ∈ {0,1}), `:family soft` / `:family weighted` (graded), `:family normal <σ>` (signed
Gaussian). Map engagement signals to kernels/observations:

| signal | evidence |
|--------|----------|
| `star`, `thumb_up` | strong positive (bernoulli obs=1, or weighted high `w`) |
| `thumb_down` | negative (obs=0) |
| `open_original` | positive |
| `dwell` (seconds, ≥4s) | graded positive via soft/weighted (map seconds → evidence) |

Plain reads are **never** fed in (the user swipe-reads everything; read-order is noise).
Prefer **signed** weights (Gaussian, `:family normal`) so `thumb_down` moves a weight
down. Feature *semantics* (which index is which feed/author/tag) live in Python; the
model is structurally domain-neutral.

**B. A thin Python adapter** (e.g. `sidecar/app/credence_client.py` + routes) exposing the
requirements' contract, each mapping to skin calls:

- `/load` → spawn/reuse a `SkinClient`; `initialize(dsl_sources={"model": model_src})`;
  load weight belief-specs from Postgres (or seed priors if absent).
- `/observe` → for each event: resolve active feature indices; `call_dsl("model",
  "observe", [weights, active_indices, signal, value])`; persist the returned updated
  weight specs to Postgres.
- `/score` → `call_dsl("model","score-batch",[weights, article_feature_lists])`; return
  `[(entry_id, score)]` by zipping with input order.
- `/health` → cheap liveness (engine reachable; `obs_count`; `n_weights`).

The `SkinClient` is **warm** (spawned once, reused) only to dodge Julia cold-start — it
holds **no** belief state; Postgres is the source of truth. Manage its lifecycle (systemd
or the FastAPI lifespan) and **fail open**: if the engine is unreachable, fall back to the
existing priority-tier sort (see `sidecar/app/routes/entries.py`) — ranking must never
break.

**C. Persistence** — a Postgres table for the weight beliefs (`model_version`, `name`,
belief-spec JSON) + `obs_count`. Plain JSON only — never the engine's binary snapshot.

**D. Feature extraction** (Python) — per-article positional feature vector (feed one-hot,
author, tags, recency-decay, etc.) + the name→index map. rss-specific; lives in the body.

**E. Event capture** — record `star`/`thumb_up`/`thumb_down`/`open_original`/`dwell`
events (the sidecar already tracks read/star — extend it; capture dwell ≥4s, drop
swipe-pasts) and feed them to `/observe`. Do **not** feed plain reads.

**F. Wire the ranked order** into the entry list (replace/augment the priority-tier sort in
`sidecar/app/routes/entries.py` with credence `/score` results; keep the tier sort as the
fail-open fallback).

**G. Dependency** — add `credence-skin-client` (pure stdlib, zero transitive deps). Not on
PyPI yet — depend on it from the credence repo via `uv` (path, or
`git+https://github.com/gfrmin/credence#subdirectory=apps/skin/clients/python`). Do **not**
add `juliacall` or any credence engine package — the wire is the only surface.

**H. Docker Compose** — add the credence-skin usage. Either run credence-skin as a
long-lived container the adapter execs into via `docker run -i`, or have the adapter spawn
it. Document the chosen shape.

## Constraints / style

- Match existing rssfeed conventions (FastAPI + htmx, `uv`, the existing route/db
  patterns). Read `sidecar/app/{main,db,worker,routes/entries,routes/feeds}.py` first.
- Pure functions preferred; state confined to Postgres + the warm client handle.
- **No probability math in Python.** If you find yourself writing `alpha+1` or
  `mean = a/(a+b)` in Python, STOP — that belongs in `model.bdsl` via `condition`/`expect`.
- Use git worktrees under `~/git/worktrees/rssfeed/<name>`; branch off rssfeed's default
  branch (check it — do not assume `main` vs `master`).

## Verify end-to-end

- A script (mirror `examples/maut_demo_wire.py`): load `model.bdsl` into credence-skin,
  seed two feature weights, send a `thumb_up` on one, confirm its weight moves and the
  other doesn't, score two articles, confirm the ranking reflects the update — all with
  belief-specs as plain JSON, no `state_id`, no Python math.
- Confirm fail-open: with the engine stopped, the entry list still renders via the tier
  sort.
- Run the existing rssfeed test/lint suite; add adapter tests (mock the engine for unit
  tests; one integration test against a real credence-skin).

Report what you changed with file paths, and surface any place the requirements doc
conflicts with the wire-only architecture (the architecture wins; flag the conflict).
