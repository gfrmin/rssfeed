import json
import os

DATABASE_URL = os.environ["DATABASE_URL"]
MINIFLUX_URL = os.environ.get("MINIFLUX_URL", "http://localhost:9144")
MINIFLUX_API_KEY = os.environ.get("MINIFLUX_API_KEY", "")
BRIGHTDATA_PROXY = os.environ.get("BRIGHTDATA_PROXY", "")
BRIGHTDATA_UNLOCKER_PROXY = os.environ.get("BRIGHTDATA_UNLOCKER_PROXY", "")
# BrightData Browser-API CDP endpoint (a browser_api zone, e.g. cli_browser),
# used to drive JS/SPA paywall logins server-side. Form:
#   wss://brd-customer-<id>-zone-cli_browser:<pw>@brd.superproxy.io:9222
# Unset → username/password login is disabled (Firefox import / paste still work).
BRIGHTDATA_BROWSER_WSS = os.environ.get("BRIGHTDATA_BROWSER_WSS", "")
WORKER_POLL_INTERVAL = int(os.environ.get("WORKER_POLL_INTERVAL", "60"))

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

