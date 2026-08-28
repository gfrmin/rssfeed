#!/usr/bin/env bash
# Promote this machine's rssfeed standby to a live, writable primary — for
# use ONLY when steel-legend is actually unreachable. Run this ON THE THINKPAD.
# See docs/failover.md for the full runbook.
#
# What it does:
#   1. Refuses to run if steel's Postgres is still reachable — promoting
#      while steel is still up would fork the data into two divergent,
#      un-mergeable copies. Override with FORCE=1 if you're certain steel's
#      box is down but this specific check is a false positive.
#   2. Promotes the standby Postgres to a writable primary (pg_promote()).
#   3. Starts miniflux + the sidecar, now pointed at a writable local db.
#
# After steel comes back: run scripts/failback-to-steel.sh. Do NOT bring
# steel's rssfeed-sidecar.service back up before that, or you'll end up with
# two writable primaries silently diverging.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

STEEL_HOST="100.114.52.102"
STANDBY_DB="rssfeed-standby_db_1"

echo "[promote-standby] Checking steel is actually unreachable..."
if timeout 3 bash -c "cat < /dev/null > /dev/tcp/${STEEL_HOST}/5433" 2>/dev/null; then
    echo "steel's Postgres (port 5433) is still reachable. Refusing to promote —" >&2
    echo "if steel is actually fine, you don't need this. If you're SURE steel's" >&2
    echo "box is down but this specific check is a false positive, re-run with" >&2
    echo "FORCE=1 scripts/promote-standby.sh" >&2
    [ "${FORCE:-0}" = "1" ] || exit 1
    echo "[promote-standby] FORCE=1 set, continuing anyway."
fi

echo "[promote-standby] Promoting standby to writable primary..."
podman exec "$STANDBY_DB" psql -U miniflux -d miniflux -c "SELECT pg_promote();"

echo "[promote-standby] Waiting for promotion to complete..."
in_recovery="t"
for _ in $(seq 30); do
    in_recovery=$(podman exec "$STANDBY_DB" psql -U miniflux -d miniflux -tAc "SELECT pg_is_in_recovery();")
    [ "$in_recovery" = "f" ] && break
    sleep 1
done
if [ "$in_recovery" != "f" ]; then
    echo "Promotion didn't complete after 30s — check: podman logs $STANDBY_DB" >&2
    exit 1
fi
echo "[promote-standby] Promoted. Now writable."

echo "[promote-standby] Starting miniflux..."
podman-compose -f docker-compose.replica.yml up -d miniflux

echo "[promote-standby] Starting the sidecar..."
systemctl --user start rssfeed-standby-sidecar.service

echo
echo "[promote-standby] Done. rssfeed should be reachable at http://thinkpad:9145"
echo "(the sidecar binds the tailnet IP, not 0.0.0.0, so use the MagicDNS name —"
echo "localhost:9145 will NOT answer. Works from this laptop and from the phone.)"
echo
echo "IMPORTANT: don't bring steel's rssfeed-sidecar.service back up until"
echo "you've run scripts/failback-to-steel.sh — this machine is now the only"
echo "writable copy."
