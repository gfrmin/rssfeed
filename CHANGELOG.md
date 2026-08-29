# Changelog

## Versioning

`0.x` while this is a single-user application with one operator: the version is a
marker for "what you are running", not a compatibility promise. Within that:

- **Patch** — fixes and internal work.
- **Minor** — new features, and anything that changes behaviour you would notice.
- The database migrates itself forward on start (`run_migrations`), so upgrading is
  pulling and restarting. There is no downgrade path; take a `pg_dump` first if that
  matters to you.

Anything that needs a manual step to keep working will say so under **Upgrading**.

## v0.2.0 — 2026-08-30

First tagged release. The reader has been in daily use since March; this is the
point at which someone else could reasonably run it.

### What it is

A sidecar alongside [Miniflux](https://miniflux.app) that adds the two things
Miniflux deliberately leaves out — **the actual full article**, and **an order for
the firehose that reflects what you like**.

- **Full-text extraction with versioning.** trafilatura + readability, over a
  tiered fetch (direct → static proxy → Web Unlocker → Wayback), with per-feed XPath
  rules, per-domain cookies and real-browser login for paywalled subscriptions. Every
  fetch is hashed and a new version stored only when the text changes, so the
  "Changed" view shows what a publisher revised after publication.
- **A learning ranker that explains itself.** Bayesian linear regression over
  per-feed / author / tag weights, recency, priority tier and embedding similarity,
  running on [Credence](https://github.com/gfrmin/credence). Learns from quality
  signals only — star, thumbs, open-original, dwell ≥4s. Every row can show the ±
  feature contributions behind its position.
- **Embedding similarity.** Local Ollama embeddings maintain a taste centroid and
  a deep candidate pool, so an old high-affinity article can resurface instead of
  falling out of the newest-200 window.
- **A fast reader.** Three panes on desktop, single-pane on mobile, full keyboard
  control, dark and light, PWA with offline reading, all assets local.

The ranker and the embeddings are both **optional and fail open**: with Credence or
Ollama unreachable the reader falls back to priority + recency and keeps working.

### Made usable by someone other than the author

Most of the work immediately before this tag was closing the gap between "runs here"
and "runs anywhere":

- The ranker's wire client was a **required** dependency pinned to a relative path
  (`../../credence/...`), so `uv sync` failed for anyone without a sibling checkout.
  It is an optional extra resolved from its own repo now, and CI installs the base
  set only — so the project failing to be installable on its own breaks the build.
- `docker-compose.yml` published Postgres on **every interface** with a default
  password. Postgres, Miniflux and the reader now bind loopback unless you widen
  them on purpose.
- `base.html` had linked `/static/tailwind.css` since the design-system port and
  nothing ever built it, so every clone rendered feed management, cookies and the
  diff overlay unstyled. The built stylesheet is committed.
- This fleet's deployment — failover scripts, host units, a runbook naming real
  machines — moved out of the public repo entirely.
- Every setting is documented in [docs/configuration.md](docs/configuration.md),
  enforced by a test that fails when `config.py` and the reference disagree.
- Screenshots, from a stack that has never seen a real feed.

### Security

- **The reader has no authentication, by design** — the boundary is network
  placement. See "Security model" in the README before exposing it anywhere.
- SSRF egress guard on all server-side fetches (image proxy, extraction, feed
  discovery): loopback/private/link-local answers refused, every redirect re-checked.
- Credentials are scoped to trusted frames during subscription login, and never
  reach an untrusted origin.
- A structural PII guard runs in the pre-commit/pre-push hooks and, in shape-only
  mode, in CI.

### Upgrading

Nothing to do — there is no earlier release to upgrade from.
