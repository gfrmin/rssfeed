# Configuration reference

Every environment variable the sidecar reads, with its default. `README.md` covers
the ones you are likely to *set*; this is the complete list, including the tuning
knobs that exist mainly so you can change them without editing code.

All of them are read **once, at import time** (`sidecar/app/config.py`), so a change
needs a restart. Nothing here is required except `DATABASE_URL`.

`sidecar/tests/test_config_documented.py` fails if this file and `config.py` ever
disagree, so a new setting cannot land undocumented.

## Core

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | **required** | Postgres connection string, shared with Miniflux |
| `MINIFLUX_URL` | `http://localhost:9144` | Miniflux base URL |
| `MINIFLUX_API_KEY` | (empty) | Miniflux API key — Settings → API Keys |
| `WORKER_POLL_INTERVAL` | `60` | Seconds between background worker cycles |
| `EGRESS_GUARD` | `1` | SSRF guard on server-side fetches. `0` disables it — local debugging only |
| `LOGIN_RECIPES_FILE` | `~/.config/rssfeed/login-recipes.json` | Per-site login recipes. Outside the repo on purpose: the sites you can log into are the subscriptions you hold |
| `INVIDIOUS_URL` | (empty) | Send YouTube "Watch" links to your own [Invidious](https://invidious.io) instead of youtube.com. Embeds are always click-through links, never inline third-party frames |

## Fetching and proxies

The extraction tiers are: direct → BrightData static proxy → Web Unlocker → Wayback.
Each proxy is optional; absent, that tier is skipped.

| Variable | Default | Description |
|---|---|---|
| `BRIGHTDATA_PROXY` | (empty) | Static-datacentre proxy, for geo-routing and simple bot walls |
| `BRIGHTDATA_UNLOCKER_PROXY` | (empty) | Web Unlocker, for interstitials and CAPTCHAs. Much more expensive per request |
| `BRIGHTDATA_BROWSER_WSS` | (empty) | Browser-API CDP endpoint. **Not** used for paywall logins — the Scraping Browser forbids typing into password fields; kept for a possible future SPA-render tier |
| `LOGIN_BROWSER_PROXY` | (empty) | Proxy for the self-hosted login browser (`http://user:pass@host:port`). An IP-reputation escape hatch only; plain proxies don't block password entry |
| `FETCH_MIN_INTERVAL_S` | `2` | Minimum seconds between fetches to the **same** domain. Politeness, so a publisher you hold a subscription with doesn't flag the traffic. `0` disables |
| `RENDER_MIN_INTERVAL_S` | `8` | The same gap for browser renders, which are heavier and more bot-like |

## Extraction backfill and retry

A never-seen entry only qualifies for extraction if it was already this fresh when
the worker's cursor first reached it — otherwise enabling full-content on an old feed
would backfill its entire archive. Older entries stay available on demand via
"Fetch full text".

| Variable | Default | Description |
|---|---|---|
| `WORKER_BACKFILL_MAX_AGE_DAYS` | `3` | Age cutoff for that first-sight test. `0` = no limit |
| `WORKER_EXTRACT_BATCH` | `50` | Entries per feed per cycle, for both the discovery walk and the recency window |
| `WORKER_EXTRACT_RETRY_BATCH` | `20` | Due-for-retry entries per cycle, across all feeds |
| `WORKER_EXTRACT_MAX_ATTEMPTS` | `9` | Attempts before an entry is given up on and logged |
| `WORKER_EXTRACT_BACKOFF_BASE_MIN` | `5` | First retry delay, doubling each attempt |
| `WORKER_EXTRACT_BACKOFF_MAX_MIN` | `480` | Cap on that delay |

The defaults give 5/10/20/40/80/160/320/480/480 minutes ≈ 18.6h of retries, so a
multi-hour site outage doesn't permanently abandon everything that arrived during it.
Keep `MAX_ATTEMPTS` high enough that the cap is actually reached, or it is dead config.

## Learning ranker

Optional, and **fails open** — if the engine is unreachable the reader orders by
priority + recency. See "Learning ranker" in `README.md` for installing the extra.

| Variable | Default | Description |
|---|---|---|
| `RANKER_ENABLED` | `1` | `0` disables smart ordering entirely |
| `RANKER_MODEL_VERSION` | `maut-skin-v1` | Bump to re-learn from scratch; the worker refolds all `engagement_events` |
| `MUTE_SIGNALS_ENABLED` | `1` | Log author/tag mutes as negative evidence. Kill-switch for the *logging*; already-logged events keep folding either way |
| `RANKER_TIME_FEATURES` | `1` | Hour-of-day sin/cos + weekend flag. A linear model can't reorder a single batch with them — they exist to absorb when-you-read bias out of the feed/author weights |
| `CREDENCE_SKIN_SERVER` | `~/git/credence/apps/skin/server.jl` | Julia skin entrypoint, spawned from a local checkout |
| `CREDENCE_SKIN_PROJECT` | `~/git/credence` | Julia project dir for that checkout |
| `CREDENCE_SKIN_COMMAND` | (empty) | JSON argv overriding the local spawn, e.g. `["docker","run","--rm","-i","ghcr.io/gfrmin/credence-skin:latest"]` |

## Embeddings and the deep pool

Optional, and also fails open — without Ollama the `embed_sim` feature is simply
absent and the structured ranker is unaffected.

| Variable | Default | Description |
|---|---|---|
| `EMBED_ENABLED` | `1` | `0` disables embedding similarity |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model (768-dim) |
| `EMBED_BACKFILL_BATCH` | `150` | Archive entries embedded per worker cycle, so related-articles reaches across history. `0` disables the sweep |
| `EMBED_BACKFILL_MAX_AGE_DAYS` | `730` | How far back that sweep reaches. `0` = the whole archive; raising it later resumes from the stored cursor |
| `DEEP_POOL_ENABLED` | `1` | Union the newest-200 unread with the top unread by cosine to the taste centroid, so an old high-affinity item can resurface. Needs pgvector + Ollama; degrades to newest-only |
| `DEEP_POOL_LIMIT` | `100` | How many such articles to add to the pool |

## Not read by the app

These configure the *deployment*, not the program — `docker-compose.yml`,
`run-sidecar.sh` and `systemd/rssfeed-sidecar.service` read them. They are in
`.env.example` for that reason and will never appear in `config.py`.

| Variable | Default | Description |
|---|---|---|
| `RSSFEED_BIND` | `127.0.0.1` | Interface the reader listens on. Loopback by default because the reader has **no authentication** — see "Security model" in `README.md` |
| `RSSFEED_PORT` | `9145` | Reader port |
| `RSSFEED_DB_PORT` | `5433` | Host port Postgres is published on |
| `RSSFEED_PUBLISH_HOST` | `127.0.0.1` | Interface the compose services publish on |
| `RSSFEED_UV_EXTRAS` | (empty) | Extras passed through to `uv run`, e.g. `--extra ranker` |
| `POSTGRES_PASSWORD` | `miniflux` | Postgres password; also builds the default `DATABASE_URL` |
| `MINIFLUX_ADMIN_USER` | `admin` | Miniflux admin user, created on first boot |
| `MINIFLUX_ADMIN_PASSWORD` | `changeme` | Miniflux admin password — change it |
