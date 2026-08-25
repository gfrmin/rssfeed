# steel → thinkpad failover

rssfeed normally runs entirely on steel-legend. This is a fallback for when
steel is genuinely unreachable (power/network/hardware issue) and you want
rssfeed without relying on any cloud service.

## How it works

The thinkpad runs a continuous, silent Postgres **streaming replica** of
steel's rssfeed database (`docker-compose.replica.yml`, service `db`). It's
seeded once via `pg_basebackup -R`, which writes `standby.signal` +
`primary_conninfo` straight into the data directory — after that it just
keeps streaming on its own, no cron/timer needed. Miniflux and the sidecar
stay **stopped** on the thinkpad: a hot standby is read-only, and they'd just
error trying to write to it.

Because the standby is a real streaming replica (not a periodic dump), it's
normally only seconds behind steel — promoting it loses at most a few
seconds of the very latest activity, not hours.

```
steel (primary)  --stream-->  thinkpad (standby, read-only, silent)
   db + miniflux + sidecar         db only (miniflux/sidecar stopped)
```

## If steel goes down

On the **thinkpad**:

```
cd ~/git/rssfeed
scripts/promote-standby.sh
```

This refuses to run if steel's Postgres is still reachable (protects against
promoting by mistake while steel is actually fine, which would fork the data
into two divergent copies). It promotes the standby to writable, then starts
miniflux and `rssfeed-standby-sidecar.service`. rssfeed becomes reachable at
`http://localhost:9145` (or `http://thinkpad:9145` over Tailscale from
another device).

**While the thinkpad is promoted, don't bring steel's `rssfeed-sidecar`
back up even if steel itself comes back briefly** — two writable copies
accepting independent writes is exactly the split-brain state this design
avoids. Steel's own containers should stay stopped until failback.

## Once steel is back

On **steel**:

```
cd ~/git/rssfeed
scripts/failback-to-steel.sh
```

Since steel was genuinely offline the whole time, it never accepted
conflicting writes — the thinkpad's copy strictly has everything steel had
plus everything written during the outage. So this isn't a merge, it's a
clean cutover:

1. Confirms the thinkpad really is a promoted (writable) primary.
2. Stops steel's stale containers/sidecar.
3. Freezes writes on the thinkpad (brief downtime).
4. Wipes steel's stale Postgres volume, re-seeds it via `pg_basebackup` from
   the thinkpad (as a normal primary, not a standby).
5. Starts steel's db + miniflux + sidecar — steel is primary again.
6. Re-seeds the thinkpad's standby volume from steel (`-R`), restoring the
   original replication direction.

It prompts for confirmation before wiping steel's volume.

**Rehearsed end-to-end on 2026-08-25** (live production data — drained writes
on steel first by stopping `miniflux` + the sidecar, confirmed the standby
had zero replication lag, then simulated an outage by stopping steel's `db`
too, so nothing was actually at risk). Found and fixed three real bugs:

- The standby sidecar had no `MINIFLUX_API_KEY` in its `.env` — only
  `POSTGRES_PASSWORD` had been synced during initial setup, so a promoted
  standby couldn't actually talk to its own miniflux.
- `docker-compose.replica.yml`'s `db` port was bound to `127.0.0.1:5433`
  only, which blocks the *failback* direction (steel pulling a base backup
  from a promoted thinkpad) even though it's fine for the normal direction.
  Now bound the same way steel's own compose file does (`5433:5432`) —
  password auth is the real gate either way, see the NAT note below.
- `pg_basebackup` copies the source's `pg_hba.conf` verbatim, so thinkpad's
  standby only ever had steel's bridge-subnet entry, not its own — meaning
  steel could never authenticate a replication connection *into* thinkpad
  during failback. Fixed by adding thinkpad's own bridge-subnet entry too
  (both entries now persist through re-seeds in either direction).

`scripts/failback-to-steel.sh` was also fixed: `podman-compose stop` leaves
containers around (just stopped), and `podman volume rm -f` can't force
through podman's container-dependency graph — it now removes the containers
explicitly before removing the volume, on both sides of the cutover.

## Checking replication health

From steel, at any time:

```
podman exec rssfeed_db_1 psql -U miniflux -d miniflux -c \
  "SELECT client_addr, state, sent_lsn, write_lsn, replay_lsn, write_lag, replay_lag FROM pg_stat_replication;"
```

An empty result means nothing is currently streaming — check the standby
container is up on the thinkpad (`podman ps --filter name=rssfeed-standby`)
and that `standby.signal`/`primary_conninfo` are intact in its data
directory.

## First-time setup on a new thinkpad checkout

```
ln -sfn ~/git/rssfeed/systemd/rssfeed-standby-sidecar.service \
  ~/.config/systemd/user/rssfeed-standby-sidecar.service
systemctl --user daemon-reload
```

(Symlinked, not copied, so `git pull` keeps it current. Not enabled — it's
only ever started explicitly by `scripts/promote-standby.sh`.)

## Setup notes (for rebuilding this if the standby is ever lost)

- Replication role: `replicator` (`REPLICATION LOGIN`), password in the
  keyring as `RSSFEED_REPLICATOR_PASSWORD` (`secret-tool lookup service env
  key RSSFEED_REPLICATOR_PASSWORD`).
- `pg_hba.conf` needs a `host replication replicator <bridge-subnet>
  scram-sha-256` line on **both** steel's and thinkpad's `db` — each one
  scoped to that machine's *own* rssfeed podman bridge subnet (`podman
  network inspect` on whichever network the `db` container is on), **not**
  the other machine's Tailscale IP: rootless podman's port-forwarding NATs
  the source address through the receiving side's own bridge before
  Postgres ever sees it, so per-real-IP scoping doesn't work here. Password
  auth is the actual gate. Because `pg_basebackup` copies `pg_hba.conf`
  verbatim from whichever side you seed from, both entries end up on both
  machines after the first failover+failback cycle and stay there.
- Thinkpad's `.env` needs the same `POSTGRES_PASSWORD` as steel's `.env` —
  it's what `docker-compose.replica.yml`'s `miniflux` service uses to
  connect, and must match the replicated `miniflux` Postgres role's
  password.
- To re-seed the standby from scratch:
  ```
  podman volume rm -f rssfeed_standby_pgdata && podman volume create rssfeed_standby_pgdata
  podman run --rm -v rssfeed_standby_pgdata:/data \
    -e PGPASSWORD="$(secret-tool lookup service env key RSSFEED_REPLICATOR_PASSWORD)" \
    docker.io/pgvector/pgvector:pg17 \
    bash -c 'pg_basebackup -h 100.114.52.102 -p 5433 -U replicator -D /data -Fp -Xs -P -R'
  podman-compose -f docker-compose.replica.yml up -d db
  ```
