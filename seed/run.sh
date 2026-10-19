#!/bin/sh
# Bring the seeded instance up or down: seed server, throwaway Postgres +
# Miniflux, and a sidecar pointed at them. See seed/README.md.
#   seed/run.sh up     # ~1 min; leaves everything running, prints the URL
#   seed/run.sh down
set -eu
SEED="$(cd "$(dirname "$0")" && pwd)"
RUN="${XDG_RUNTIME_DIR:-/tmp}/rssfeed-seed"
SERVER_PORT="${SEED_SERVER_PORT:-9148}"
SIDECAR_PORT="${SEED_SIDECAR_PORT:-9147}"
DB_PORT="${SEED_DB_PORT:-5434}"
MF_PORT="${SEED_MINIFLUX_PORT:-9146}"
export SEED_DB_PORT="$DB_PORT" SEED_MINIFLUX_PORT="$MF_PORT"

wait_for() {  # url, seconds
  i=0; until curl -sf -o /dev/null "$1"; do
    i=$((i + 1)); [ "$i" -ge "$2" ] && { echo "timed out waiting for $1" >&2; exit 1; }; sleep 1
  done
}

stop_pid() { [ -f "$RUN/$1.pid" ] && kill "$(cat "$RUN/$1.pid")" 2>/dev/null; rm -f "$RUN/$1.pid"; return 0; }

case "${1:-}" in
up)
  mkdir -p "$RUN"
  python3 "$SEED/server.py" --port "$SERVER_PORT" >"$RUN/server.log" 2>&1 &
  echo $! >"$RUN/server.pid"
  podman-compose -f "$SEED/compose.yml" up -d >"$RUN/compose.log" 2>&1
  wait_for "http://127.0.0.1:$MF_PORT/healthcheck" 90
  wait_for "http://127.0.0.1:$SERVER_PORT/feeds/tidewater.xml" 10
  python3 "$SEED/seed.py" miniflux --env-out "$RUN/env"
  # uvicorn is started directly (not via `uv run`) so its pid is the one `down` kills.
  (cd "$SEED/../sidecar" && uv sync -q)
  # A clean environment: no keyring secrets, proxies or ranker from the caller's shell.
  (cd "$SEED/../sidecar" && exec env -i HOME="$HOME" PATH="$PATH" \
      DATABASE_URL="postgres://miniflux:seed@127.0.0.1:$DB_PORT/miniflux" \
      MINIFLUX_URL="http://127.0.0.1:$MF_PORT" \
      "$(cat "$RUN/env")" \
      EGRESS_GUARD=0 RANKER_ENABLED=0 EMBED_ENABLED=0 DEEP_POOL_ENABLED=0 \
      WORKER_POLL_INTERVAL=5 FETCH_MIN_INTERVAL_S=0 \
      .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$SIDECAR_PORT" \
      >"$RUN/sidecar.log" 2>&1) &
  echo $! >"$RUN/sidecar.pid"
  wait_for "http://127.0.0.1:$SIDECAR_PORT/" 90
  python3 "$SEED/seed.py" sidecar
  echo "seeded instance: http://127.0.0.1:$SIDECAR_PORT/  (logs in $RUN)"
  ;;
down)
  stop_pid sidecar; stop_pid server
  podman-compose -f "$SEED/compose.yml" down >/dev/null 2>&1 || true
  echo "seeded instance stopped"
  ;;
*) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
