#!/bin/sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load env vars from .env
set -a
. "$SCRIPT_DIR/.env"
set +a

: "${RSSFEED_DB_PORT:=5433}"
export DATABASE_URL="${DATABASE_URL:-postgres://miniflux:${POSTGRES_PASSWORD:-miniflux}@localhost:${RSSFEED_DB_PORT}/miniflux}"
export MINIFLUX_URL="${MINIFLUX_URL:-http://localhost:9144}"

# Loopback by default. The reader has no authentication of its own, so binding
# 0.0.0.0 publishes your subscriptions and your article archive to every network
# this machine joins -- including whatever cafe wifi a laptop connects to next.
# Set RSSFEED_BIND in .env to expose it deliberately, ideally to a VPN address.
cd "$SCRIPT_DIR/sidecar"
exec uv run uvicorn app.main:app \
    --host "${RSSFEED_BIND:-127.0.0.1}" --port "${RSSFEED_PORT:-9145}" --reload
