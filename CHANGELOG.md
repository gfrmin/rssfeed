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

## v0.3.0 — 2026-09-02

Feed problems became something you can see and act on. Everything the reader
could already do to a broken feed was four clicks deep behind `/`, and half of
it only appeared once you ticked a checkbox; the classifier that decided *why*
a feed was broken spent its answer on a tooltip.

### New

- **Triage (`/triage`).** Feeds that need attention, grouped by cause rather
  than listed by name, with the remedies bound to the **group**. On this
  instance 21 failing feeds were two causes wearing 21 names — a remedy bound
  to the row makes you fix the same problem twenty-one times. A **Needs
  attention** row in the sidebar carries the count.
- **A `quiet` health state.** `error` means we cannot fetch it; `quiet` means
  polling is fine and the *publisher* has stopped. Backed by a per-feed
  publishing baseline — the median gap over a feed's last 20 entries — so
  "silent" is measured against that feed's own cadence, not a flat threshold.
  Groups show their age spread, because a bare count and its distribution are
  different facts: on this instance at the time of writing, 141 quiet feeds
  meant 10 that went quiet this week and 107 that stopped over a year ago.
- **A command palette** (`Ctrl+K` / `⌘K`): views, causes and feeds in one
  field. `Shift+Enter` on a feed opens its settings from anywhere in the app —
  previously reachable only by finding the row on `/feeds`.
- **`/feeds` rebuilt around the states it can now produce.** Cause chips across
  the top, a Cause column linking into triage, `quiet` in the health filter,
  and a bulk bar that is always visible — dimmed until you select something,
  rather than absent.

### Fixed

- **The failure classifier was collapsing causes that need different fixes.**
  Miniflux's own 403 text says both *"forbidden"* and *"bot protection"*, and
  the bot rule was tested first, so every 403 landed in `bot_blocked` and the
  finer bucket was dead code. It reads the status code now, and Cloudflare
  challenges, plain 403s, 404s, 5xx, auth failures, TLS and transport errors
  are separate causes. On this instance that splits one undifferentiated pile
  of 21 into 10 Cloudflare and 11 forbidden. Four more real messages that had
  been falling through to `other` are classified.
- **`stale` did not measure what it said.** The label read *"no items 24h+"*
  but the value came from `checked_at` — when Miniflux last *polled*, not when
  the publisher last *posted*. It now says "not polled in 24h+", and the
  question it was mistaken for is answered by `quiet`.
- **The Category and Latest columns on `/feeds` had never rendered, at any
  width.** They are `hidden sm:table-cell`; `style.css` defined
  `.hidden { display: none !important }` and loads *after* `tailwind.css`, so
  the override won everywhere. A media query adds no specificity and nothing
  failed, because a column that never appears looks like a decision. Guarded by
  a test now.
- Tailwind's preflight sets `img { display: block }`, which had been putting
  every feed favicon on a line of its own.
- The palette hint printed `⌘K` flatly, naming a key most keyboards here do not
  have. It is relabelled per platform.

### Upgrading

Nothing to do — no migration, no configuration change.

The `quiet` state needs a per-feed publishing baseline, which is one aggregate
query over `entries`, cached for five minutes. It **fails open** like the ranker
and the embeddings: if that query is slow or the database is unreachable, feeds
lose the `quiet` state and the reader still opens.

Note that **Needs attention** deliberately excludes paused feeds. Pausing is a
decision somebody made, not an unresolved problem; paused feeds are still listed
in triage so a batch can be resumed.

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
