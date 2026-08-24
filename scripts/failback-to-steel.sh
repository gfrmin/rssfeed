#!/usr/bin/env bash
# Restore steel-legend as rssfeed's primary after a thinkpad failover, once
# steel is back online. Run this ON STEEL. See docs/failover.md.
#
# Because steel was genuinely offline for the whole outage, there's no real
# conflict to resolve — thinkpad's promoted copy strictly has everything
# steel had plus everything written during the outage. This script:
#   1. Sanity-checks thinkpad is up and really did get promoted (is a
#      writable primary, not still in recovery).
#   2. Stops steel's stale rssfeed-sidecar.service + db/miniflux containers.
#   3. Freezes writes on thinkpad (stops its sidecar + miniflux) — a short
#      downtime window while the cutover happens, unavoidable for
#      correctness.
#   4. Wipes steel's stale Postgres volume and re-seeds it via pg_basebackup
#      FROM thinkpad (now the authoritative copy) — as a normal primary,
#      not a standby.
#   5. Starts steel's db + miniflux + sidecar — steel is primary again.
#   6. Re-seeds thinkpad's standby volume via pg_basebackup FROM steel (-R),
#      restoring the original replication direction, and leaves only its
#      `db` service running (miniflux/sidecar stay stopped, as normal).
#
# This is a real, mildly involved cutover — REHEARSE it once on non-critical
# data before trusting it blind during a real incident. Requires SSH to
# thinkpad (already relied on throughout rssfeed's failover tooling).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

THINKPAD_HOST="thinkpad"          # Tailscale MagicDNS name
THINKPAD_IP="100.100.33.81"
STEEL_IP="100.114.52.102"
REPL_PASSWORD="$(secret-tool lookup service env key RSSFEED_REPLICATOR_PASSWORD)"

if [ -z "$REPL_PASSWORD" ]; then
    echo "Couldn't read RSSFEED_REPLICATOR_PASSWORD from the keyring." >&2
    exit 1
fi

confirm() {
    read -r -p "$1 [y/N] " reply
    [ "$reply" = "y" ] || { echo "Aborted."; exit 1; }
}

echo "[failback] Checking thinkpad's rssfeed-standby db is a writable primary..."
in_recovery=$(ssh "$THINKPAD_HOST" "podman exec rssfeed-standby_db_1 psql -U miniflux -d miniflux -tAc 'SELECT pg_is_in_recovery();'")
if [ "$in_recovery" != "f" ]; then
    echo "thinkpad's standby is still in recovery (not promoted) — nothing to fail back from." >&2
    exit 1
fi
echo "[failback] Confirmed: thinkpad is the current writable primary."

confirm "This will WIPE steel's current rssfeed Postgres data and replace it with thinkpad's copy. Continue?"

echo "[failback] Stopping steel's stale sidecar + containers..."
systemctl --user stop rssfeed-sidecar.service || true
podman-compose stop db miniflux || true

echo "[failback] Freezing writes on thinkpad (stopping its sidecar + miniflux)..."
ssh "$THINKPAD_HOST" "systemctl --user stop rssfeed-standby-sidecar.service || true; cd '$REPO_DIR' 2>/dev/null; podman-compose -f docker-compose.replica.yml stop miniflux || true"

echo "[failback] Re-seeding steel's Postgres from thinkpad..."
podman volume rm -f rssfeed_pgdata
podman volume create rssfeed_pgdata
podman run --rm -v rssfeed_pgdata:/data \
    -e PGPASSWORD="$REPL_PASSWORD" \
    docker.io/pgvector/pgvector:pg17 \
    bash -c "pg_basebackup -h ${THINKPAD_IP} -p 5433 -U replicator -D /data -Fp -Xs -P -v"
echo "[failback] Base backup from thinkpad complete (steel is NOT a standby — no -R)."

echo "[failback] Starting steel's db + miniflux..."
podman-compose up -d db miniflux
echo "[failback] Waiting for steel's db to become healthy..."
for _ in $(seq 30); do
    podman exec rssfeed_db_1 pg_isready -U miniflux >/dev/null 2>&1 && break
    sleep 1
done

echo "[failback] Starting steel's sidecar..."
systemctl --user start rssfeed-sidecar.service

echo "[failback] Steel is primary again. Re-seeding thinkpad's standby from steel..."
ssh "$THINKPAD_HOST" "podman volume rm -f rssfeed_standby_pgdata && podman volume create rssfeed_standby_pgdata"
ssh "$THINKPAD_HOST" "podman run --rm -v rssfeed_standby_pgdata:/data -e PGPASSWORD='${REPL_PASSWORD}' docker.io/pgvector/pgvector:pg17 bash -c 'pg_basebackup -h ${STEEL_IP} -p 5433 -U replicator -D /data -Fp -Xs -P -R -v'"
ssh "$THINKPAD_HOST" "cd '$REPO_DIR' 2>/dev/null; podman-compose -f docker-compose.replica.yml up -d db"

echo
echo "[failback] Done. steel is primary, thinkpad is streaming as standby again."
echo "Sanity-check replication is live: see docs/failover.md 'Checking replication health'."
