import json
import os

DATABASE_URL = os.environ["DATABASE_URL"]
MINIFLUX_URL = os.environ.get("MINIFLUX_URL", "http://localhost:9144")
MINIFLUX_API_KEY = os.environ.get("MINIFLUX_API_KEY", "")
BRIGHTDATA_PROXY = os.environ.get("BRIGHTDATA_PROXY", "")
BRIGHTDATA_UNLOCKER_PROXY = os.environ.get("BRIGHTDATA_UNLOCKER_PROXY", "")
# BrightData Browser-API CDP endpoint (a browser_api zone, e.g. cli_browser).
# NOTE: NOT used for paywall login — BrightData's Scraping Browser forbids typing
# into password fields ("Forbidden action: password typing is not allowed"), so
# logins run on a self-hosted Chromium (see browser_login.py). Kept for a possible
# future SPA *fetch* tier (rendering article HTML), which has no such restriction.
BRIGHTDATA_BROWSER_WSS = os.environ.get("BRIGHTDATA_BROWSER_WSS", "")
# Optional proxy for the self-hosted login browser, used only if a paywall blocks
# steel's IP. Form: http://user:pass@host:port (e.g. a BrightData residential zone).
# Empty → the login browser connects directly. Plain proxies don't block password
# entry, so this is purely an IP-reputation escape hatch.
LOGIN_BROWSER_PROXY = os.environ.get("LOGIN_BROWSER_PROXY", "")
WORKER_POLL_INTERVAL = int(os.environ.get("WORKER_POLL_INTERVAL", "60"))

# Per-domain fetch rate limiting — keep article fetching polite so a publisher
# (especially one we're logged into, like National Review) doesn't flag the
# traffic as abusive. Minimum seconds between consecutive fetches to the SAME
# domain; browser renders get a longer gap since they're heavier and more
# bot-like. Set to 0 to disable.
FETCH_MIN_INTERVAL_S = float(os.environ.get("FETCH_MIN_INTERVAL_S", "2"))
RENDER_MIN_INTERVAL_S = float(os.environ.get("RENDER_MIN_INTERVAL_S", "8"))

# Don't auto-backfill a feed's whole archive when full-content is first enabled:
# the worker only fetches never-seen entries published within this window. Older
# unfetched entries are left for on-demand "Fetch full text". 0 = no limit.
WORKER_BACKFILL_MAX_AGE_DAYS = int(os.environ.get("WORKER_BACKFILL_MAX_AGE_DAYS", "3"))

# Cross-feed learning ranker (Part C) — consumed over the Credence skin wire
# (JSON-RPC/stdio via credence-skin-client). The engine is stateless-per-call; all
# durable belief state lives here in Postgres. When the engine is unreachable the
# reader falls back to priority+recency ordering, so this is always optional.
RANKER_ENABLED = os.environ.get("RANKER_ENABLED", "1") not in ("0", "false", "")
RANKER_MODEL_VERSION = os.environ.get("RANKER_MODEL_VERSION", "maut-skin-v1")

# Dev / this-host default: spawn the Julia skin from the local credence checkout
# (no Docker). Production/portable: set CREDENCE_SKIN_COMMAND to a JSON argv for
# the pinned image, e.g. '["docker","run","--rm","-i","ghcr.io/gfrmin/credence-skin:latest"]',
# which overrides the local-Julia spawn.
_HOME = os.path.expanduser("~")
CREDENCE_SKIN_SERVER = os.environ.get(
    "CREDENCE_SKIN_SERVER", f"{_HOME}/git/credence/apps/skin/server.jl")
CREDENCE_SKIN_PROJECT = os.environ.get(
    "CREDENCE_SKIN_PROJECT", f"{_HOME}/git/credence")
_skin_cmd = os.environ.get("CREDENCE_SKIN_COMMAND", "").strip()
CREDENCE_SKIN_COMMAND = json.loads(_skin_cmd) if _skin_cmd else None

# Embedding-similarity feature (Part C phase 2). The worker embeds article text via
# Ollama (nomic-embed-text) and ranks partly by cosine to a taste centroid. Optional:
# if Ollama is down, embed_sim is simply absent and the structured ranker is unaffected.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
EMBED_ENABLED = os.environ.get("EMBED_ENABLED", "1") not in ("0", "false", "")

# Archive backfill: the worker embeds older articles a page per cycle so that
# related-articles reaches across history rather than only recent items.
#   BATCH        — entries per worker cycle. 150 at a 60s poll ≈ 9k/hour, which keeps
#                  each pass well inside the poll interval. 0 disables the sweep.
#   MAX_AGE_DAYS — how far back to reach; default ~2 years. 0 means the whole archive.
#                  Raising it later just resumes the walk from the stored cursor.
EMBED_BACKFILL_BATCH = int(os.environ.get("EMBED_BACKFILL_BATCH", "150"))
EMBED_BACKFILL_MAX_AGE_DAYS = int(os.environ.get("EMBED_BACKFILL_MAX_AGE_DAYS", "730"))

