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
# this host's IP. Form: http://user:pass@host:port (e.g. a BrightData residential
# zone).
# Empty → the login browser connects directly. Plain proxies don't block password
# entry, so this is purely an IP-reputation escape hatch.
LOGIN_BROWSER_PROXY = os.environ.get("LOGIN_BROWSER_PROXY", "")
WORKER_POLL_INTERVAL = int(os.environ.get("WORKER_POLL_INTERVAL", "60"))

# Per-site login recipes (see sidecar/config/login-recipes.example.json).
#
# This file lives OUTSIDE the repo by default and is deliberately not shipped: a
# list of the sites you can log into is a list of the subscriptions you pay for —
# data about the operator, not program logic. Absent, every site uses the generic
# heuristic login path; the app works fine with no recipes at all.
_HOME = os.path.expanduser("~")
LOGIN_RECIPES_FILE = os.environ.get(
    "LOGIN_RECIPES_FILE", f"{_HOME}/.config/rssfeed/login-recipes.json")

# Per-domain fetch rate limiting — keep article fetching polite so a publisher
# (especially one we hold a subscription session with) doesn't flag the traffic
# as abusive. Minimum seconds between consecutive fetches to the SAME domain;
# browser renders get a longer gap since they're heavier and more bot-like.
# Set to 0 to disable.
FETCH_MIN_INTERVAL_S = float(os.environ.get("FETCH_MIN_INTERVAL_S", "2"))
RENDER_MIN_INTERVAL_S = float(os.environ.get("RENDER_MIN_INTERVAL_S", "8"))

# SSRF egress guard for server-side fetches (/proxy/image, article extraction,
# feed discovery). Direct fetches resolve the target and refuse loopback/private/
# link-local/reserved answers, re-checking every redirect hop; proxy-routed
# fetches (connect happens at the remote proxy) get scheme/host sanity only.
# Set 0 to disable — local debugging only.
EGRESS_GUARD = os.environ.get("EGRESS_GUARD", "1") not in ("0", "false", "")

# Don't auto-backfill a feed's whole archive when full-content is first enabled:
# a never-seen entry only qualifies for extraction if it was already this fresh
# (by Miniflux's own created_at, i.e. when we first saw it — NOT re-evaluated on
# later polls) the first time the worker's cursor reached it. Older unfetched
# entries are left for on-demand "Fetch full text" (or scripts/backfill_orphaned_
# extractions.py). 0 = no limit. See WORKER_EXTRACT_MAX_ATTEMPTS below for the
# separate question of how long a *fresh* entry gets retried after it qualifies.
WORKER_BACKFILL_MAX_AGE_DAYS = int(os.environ.get("WORKER_BACKFILL_MAX_AGE_DAYS", "3"))

# Extraction retry/backoff for entries that fail full-content extraction
# (worker.py process_new_entries). A never-snapshotted entry that qualifies per
# WORKER_BACKFILL_MAX_AGE_DAYS above gets retried with exponential backoff,
# capped, and eventually given up on + logged rather than retried forever with
# no backoff (as observed: one entry retried ~1,850 times in 17h before silently
# falling out of the old recency-window query).
#
# Delays double each attempt (BASE, 2x, 4x, ...) up to MAX_ATTEMPTS-1 times,
# capped at BACKOFF_MAX_MIN, before giving up — MAX_ATTEMPTS must be high enough
# that the cap actually gets reached at least once (else it's dead config): with
# the defaults below, delays are 5/10/20/40/80/160/320/480(capped) minutes across
# 9 attempts, ~18.6h total, so a multi-hour site outage doesn't give up on
# everything that happened to arrive during it.
WORKER_EXTRACT_MAX_ATTEMPTS = int(os.environ.get("WORKER_EXTRACT_MAX_ATTEMPTS", "9"))
WORKER_EXTRACT_BACKOFF_BASE_MIN = int(os.environ.get("WORKER_EXTRACT_BACKOFF_BASE_MIN", "5"))
WORKER_EXTRACT_BACKOFF_MAX_MIN = int(os.environ.get("WORKER_EXTRACT_BACKOFF_MAX_MIN", "480"))

# Entries per feed per poll cycle, for BOTH extraction passes:
#   - discovery (_process_feed_cursor): ascending-entry-id walk for
#     never-before-seen entries, immune to a feed's publish volume pushing an
#     unresolved entry out of the window, unlike the old plain "50 most
#     recent" query.
#   - recency (_process_feed_recency): the most-recent-by-published_at window
#     (the old query's shape, preserved) watched for RSS content changes on
#     entries that already have a snapshot.
WORKER_EXTRACT_BATCH = int(os.environ.get("WORKER_EXTRACT_BATCH", "50"))

# Bound on how many previously-failed, not-yet-given-up, due-for-retry entries
# get retried per poll cycle, across all feeds combined.
WORKER_EXTRACT_RETRY_BATCH = int(os.environ.get("WORKER_EXTRACT_RETRY_BATCH", "20"))

# Cross-feed learning ranker (Part C) — consumed over the Credence skin wire
# (JSON-RPC/stdio via credence-skin-client). The engine is stateless-per-call; all
# durable belief state lives here in Postgres. When the engine is unreachable the
# reader falls back to priority+recency ordering, so this is always optional.
RANKER_ENABLED = os.environ.get("RANKER_ENABLED", "1") not in ("0", "false", "")
RANKER_MODEL_VERSION = os.environ.get("RANKER_MODEL_VERSION", "maut-skin-v1")

# Mutes as negative ranker evidence: muting an author/tag logs a mute_* engagement
# event (folded into the model by the worker). Kill-switch for the logging only —
# already-logged events keep folding either way.
MUTE_SIGNALS_ENABLED = os.environ.get("MUTE_SIGNALS_ENABLED", "1") not in ("0", "false", "")

# Cyclical time-of-engagement features (hour-of-day sin/cos + weekend flag) fed to
# the ranker. Linear model ⇒ they can't reorder a single batch at scoring time;
# they exist to absorb when-you-read bias out of the feed/author weights during
# observation. Kill-switch while the weights bed in.
RANKER_TIME_FEATURES = os.environ.get("RANKER_TIME_FEATURES", "1") not in ("0", "false", "")

# Dev / this-host default: spawn the Julia skin from the local credence checkout
# (no Docker). Production/portable: set CREDENCE_SKIN_COMMAND to a JSON argv for
# the pinned image, e.g. '["docker","run","--rm","-i","ghcr.io/gfrmin/credence-skin:latest"]',
# which overrides the local-Julia spawn.
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

# Deep candidate pool: union the newest-200 unread with the top unread articles by
# embedding-cosine to the taste centroid, so Smart/Deep-dive can resurface an old
# high-affinity item that fell off the newest-200 window. Needs pgvector + Ollama;
# degrades to newest-only silently when either is off.
DEEP_POOL_ENABLED = os.environ.get("DEEP_POOL_ENABLED", "1") not in ("0", "false", "")
DEEP_POOL_LIMIT = int(os.environ.get("DEEP_POOL_LIMIT", "100"))

# Where to point YouTube "Watch" links in article bodies. Empty (the default, so no
# private host lands in this public repo) sends them to youtube.com; set it to your
# own Invidious instance — INVIDIOUS_URL=http://<host>:<port> in the systemd unit —
# to route watch links there instead. Embeds render as click-through links, never
# inline third-party frames.
INVIDIOUS_URL = os.environ.get("INVIDIOUS_URL", "").rstrip("/")

