# RSS Sidecar

A self-hosted, single-user RSS reader that runs as a sidecar alongside [Miniflux](https://miniflux.app). It does two things Miniflux intentionally leaves out: **show you the actual full article** (full-text extraction with versioning) and **order the firehose by what you actually like** (a learning ranker that explains itself) — wrapped in a fast, distraction-free reader.

## Features

**Reading experience**
- Three-pane reader on desktop; single list-then-article view on mobile
- Keyboard shortcuts (`j`/`k` navigate, `o`/`Enter` open, `m` mark read, `s` star, `r` mark all read, `v` open original, `/` search)
- Dark/light theme toggle
- Estimated reading time
- Podcast/audio player for feed enclosures
- Mobile swipe between articles

**Content extraction (the core)**
- Full-text article fetching with [trafilatura](https://github.com/adbar/trafilatura) + [readability-lxml](https://github.com/buriy/python-readability)
- Multi-tier fetch: direct → BrightData static proxy → Web Unlocker → Wayback Machine
- Per-domain cookies for paywalled sites
- Per-feed extract rules (XPath selectors, tag manipulation)
- Image proxying (avoids tracking pixels and broken hotlinks)
- Article versioning with unified/split diff view — track how articles change over time (the "Changed" view)

**Feed management**
- Feed priority tiers (Must Read / Normal / Low) — must-read feeds bubble to the top
- Feed favicons + at-a-glance health dots
- Auto-discovery / URL repair for moved or broken feeds
- OPML import/export
- Per-feed full-text, proxy, and TLS-verification toggles

**Browsing**
- Views: Unread / All / Read / Starred / Changed, plus per-feed lists
- Full-text search (via Miniflux API)

**Learning ranker** (optional — cross-feed Unread/All views)
- Orders cross-feed views by inferred preference, with a **Smart / Newest** toggle; per-feed lists stay strictly reverse-chronological
- Learns from *quality* signals only — star, thumbs up/down, open-original, dwell (≥4s) — recorded in `engagement_events`
- **Bayesian linear regression**: `score = Σ wᵢ·featureᵢ`, where each engagement is one noisy measurement of the score (`y ~ Normal(Σwx, σ²)`). Evidence is shared across co-occurring features by uncertainty, so a single thumbs-down doesn't clobber a confidently-liked source
- Features: per-feed / per-author / per-tag weights, recency (exponential half-life), priority tier, and embedding similarity to a learned taste centroid
- **"Why this ranked"** — every Smart-ordered row can explain its top ± feature contributions
- Inference runs on [Credence](https://github.com/gfrmin/credence) over the skin wire (JSON-RPC/stdio); the engine is stateless-per-call and all belief state lives in this app's Postgres. **Fails open**: if the engine is unreachable, ordering falls back to priority + recency
- Author/tag mutes sink unwanted items to the bottom cross-feed (and hard-filter on the feed's own page)

**Embedding similarity** (optional)
- Article text is embedded locally via [Ollama](https://ollama.com) (`nomic-embed-text`); the worker maintains a taste centroid from positively-engaged articles
- Cosine similarity to that centroid becomes one more ranker feature ("similar to your taste"). Fails open if Ollama is down

**Self-hosted friendly**
- All assets bundled locally (no CDN dependencies)
- PWA with service worker for offline reading
- Single Docker Compose stack

## Architecture

```
┌──────────┐     ┌──────────┐     ┌────────────┐
│ Miniflux │◄───►│ Postgres │◄───►│  Sidecar   │
│ :9144    │     │          │     │  :9145     │
└──────────┘     └──────────┘     └─────┬──────┘
                                        │ optional, fails open
                        ┌───────────────┴───────────────┐
                        ▼                               ▼
                 ┌─────────────┐                 ┌─────────────┐
                 │  Credence   │                 │   Ollama    │
                 │ skin engine │                 │ embeddings  │
                 │ (JSON-RPC)  │                 │  :11434     │
                 └─────────────┘                 └─────────────┘
```

The sidecar is a FastAPI + htmx application that:
- Uses Miniflux's API for feed/entry management (Miniflux owns all feed/entry/read/star state)
- Stores its own data (article snapshots, feed config, cookies, URL history, engagement events, ranker weights, embeddings) in the shared PostgreSQL database
- Runs a background worker that auto-extracts full-text for feeds with extraction enabled, maintains embeddings, and folds new engagement events into the ranker's learned weights

Both optional dependencies degrade gracefully: without Credence the reader orders by priority + recency; without Ollama the `embed_sim` feature is simply absent.

## Setup

### Prerequisites
- Docker and Docker Compose

### Quick start

```bash
# Clone the repo
git clone https://github.com/gfrmin/rssfeed.git
cd rssfeed

# Configure
cp .env.example .env
# Edit .env — set at minimum:
#   MINIFLUX_ADMIN_PASSWORD (something secure)
#   MINIFLUX_API_KEY (generate after first login, see below)

# Start
docker compose up -d

# 1. Open Miniflux at http://localhost:9144, log in with admin/your-password
# 2. Go to Settings → API Keys → Create a new API key
# 3. Add the key to .env as MINIFLUX_API_KEY
# 4. Restart: docker compose restart sidecar
# 5. Open the sidecar at http://localhost:9145
```

## Configuration

All configuration is via environment variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_PASSWORD` | `miniflux` | PostgreSQL password |
| `MINIFLUX_ADMIN_USER` | `admin` | Miniflux admin username |
| `MINIFLUX_ADMIN_PASSWORD` | `changeme` | Miniflux admin password |
| `MINIFLUX_API_KEY` | (required) | Miniflux API key |
| `MINIFLUX_URL` | `http://localhost:9144` | Miniflux base URL |
| `BRIGHTDATA_PROXY` | | HTTP proxy URL (static) for fetching blocked content |
| `BRIGHTDATA_UNLOCKER_PROXY` | | Web Unlocker proxy URL for anti-bot sites |
| `WORKER_POLL_INTERVAL` | `60` | Seconds between background extraction polls |
| `LOGIN_RECIPES_FILE` | `~/.config/rssfeed/login-recipes.json` | Per-site login recipes — see below |

### Subscription login recipes (optional)

If you subscribe to a paywalled site, the sidecar can log in with a real browser
and reuse the session cookies. Most plain login forms work with no configuration:
the generic heuristics find the username/password/submit fields. You only need a
recipe where those misfire — typically an ad-heavy page carrying several
`button[type=submit]`, where the generic selector clicks the wrong one.

Recipes are **configuration, not code**, and live outside the repo by default: the
set of sites you can log into is the set of subscriptions you hold, which is data
about you, not about the program. Copy the example and fill in your own:

```bash
mkdir -p ~/.config/rssfeed
cp sidecar/config/login-recipes.example.json ~/.config/rssfeed/login-recipes.json
```

A missing or malformed file just means "no recipes" — every site falls back to the
heuristic path and the app runs unchanged. A failed login leaves a screenshot in
`/tmp/rssfeed-login-debug` to help you find the right selectors.

### Learning ranker (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `RANKER_ENABLED` | `1` | Set `0` to disable smart ordering entirely (falls back to priority + recency) |
| `RANKER_MODEL_VERSION` | `maut-skin-v1` | Bump to re-learn from scratch; the worker refolds all `engagement_events` |
| `CREDENCE_SKIN_SERVER` | `~/git/credence/apps/skin/server.jl` | Julia skin entrypoint spawned from a local Credence checkout |
| `CREDENCE_SKIN_PROJECT` | `~/git/credence` | Julia project dir for the local skin |
| `CREDENCE_SKIN_COMMAND` | | JSON argv overriding the local-Julia spawn, e.g. `["docker","run","--rm","-i","ghcr.io/gfrmin/credence-skin:latest"]` |

### Embeddings (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `EMBED_ENABLED` | `1` | Set `0` to disable embedding similarity |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint for embedding article text |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model (768-dim) |

## Per-feed extract rules

For sites where automatic extraction doesn't work well, you can set custom rules in each feed's settings page (JSON):

```json
{
  "content_xpath": "//article//div[@class='post-body']",
  "unwrap_tags": ["template"],
  "remove_tags": ["widget-*", "related-posts"]
}
```

- `content_xpath` — XPath selector for the main content element
- `unwrap_tags` — HTML tags to unwrap (promote children), useful for Vue.js/Web Component sites
- `remove_tags` — Glob patterns for tags to remove entirely

## Development

The sidecar is managed with [uv](https://docs.astral.sh/uv/). Tests and lint must run through it — the system Python has none of the dependencies:

```bash
cd sidecar
uv run pytest          # test suite
uv run ruff check .    # lint
uv run ruff format .   # format
```

## Tech stack

- **Backend**: Python 3.12, FastAPI, psycopg3
- **Frontend**: htmx, vanilla JS, CSS custom properties
- **Database**: PostgreSQL 17 (shared with Miniflux)
- **Extraction**: trafilatura, readability-lxml, lxml
- **Ranking**: Credence (Bayesian inference engine, consumed over the skin wire); Ollama for embeddings
- **Containerization**: Docker Compose

## License

[GNU Affero General Public License v3.0](LICENSE)
