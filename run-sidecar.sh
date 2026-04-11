#!/bin/sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load env vars from .env
set -a
. "$SCRIPT_DIR/.env"
set +a

export DATABASE_URL="postgres://miniflux:${POSTGRES_PASSWORD:-miniflux}@localhost:5433/miniflux"
export MINIFLUX_URL="http://localhost:9144"

cd "$SCRIPT_DIR/sidecar"
exec uv run uvicorn app.main:app --host 127.0.0.1 --port 9145 --reload
