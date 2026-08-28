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
`http://thinkpad:9145` — from this laptop and from the phone alike. Note it
is **not** on `localhost:9145`: the sidecar binds the tailnet IP (see
"Binding and exposure" below), so the MagicDNS name is the only way in.

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
  It was bound to all interfaces (`5433:5432`) as the initial fix; that was
  narrowed on 2026-08-28 to loopback plus the tailnet address, because this
  laptop runs no host firewall. See "Binding and exposure" below.
- `pg_basebackup` copies the source's `pg_hba.conf` verbatim, so thinkpad's
  standby only ever had steel's bridge-subnet entry, not its own — meaning
  steel could never authenticate a replication connection *into* thinkpad
  during failback. Fixed by adding thinkpad's own bridge-subnet entry too
  (both entries now persist through re-seeds in either direction).

`scripts/failback-to-steel.sh` was also fixed: `podman-compose stop` leaves
containers around (just stopped), and `podman volume rm -f` can't force
through podman's container-dependency graph — it now removes the containers
explicitly before removing the volume, on both sides of the cutover.

## Binding and exposure

This laptop runs **no host firewall** — `ufw` is not installed, and
firewalld/nftables/iptables are all inactive (the only `nft` table present is
Tailscale's own, with `policy accept`). So a `0.0.0.0` bind here is genuinely
open to whatever wifi LAN the machine joins, not merely nominally so.

Both services are therefore bound deliberately:

- **Postgres publishes twice** — `127.0.0.1:5433` and `100.100.33.81:5433`.
  Loopback is how the sidecar reaches it (`ExecStartPre` probes
  `127.0.0.1:5433`, and `DATABASE_URL` uses `@localhost:5433`); the tailnet
  address is how steel reaches this box for failback's `pg_basebackup`.
  Binding *only* the tailnet IP would break the sidecar.
- **The sidecar binds `100.100.33.81:9145`.** uvicorn's `--host` takes a single
  address, and the reader has no authentication of its own, so it must not be
  on `0.0.0.0`.

**Failure mode this introduces:** if `tailscale0` is not up when podman
publishes the port, the tailnet bind fails and the db container will not
start (`restart: unless-stopped` keeps retrying). The same applies to the
sidecar. This matters because this is the box you turn to when things are
already broken — if it happens, drop the `100.100.33.81:5433:5432` line from
`docker-compose.replica.yml` and the `--host` from the unit to get running on
loopback, then put them back once the tailnet is up.

Closing the LAN exposure properly with `ufw` (which would also cover
mariadb `:3306`, photoprism `:2342` and tuwunel `:6167` on this box) is a
separate, still-outstanding job. Note `ufw` *does* work against rootless
podman — `rootlessport` is an ordinary userspace listener on the host, so
traffic hits the INPUT chain; the well-known rootful-Docker FORWARD-chain
bypass does not apply here.

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

## What degrades on thinkpad

The reader works, but it is not a byte-for-byte stand-in for steel:

| Dependency | On thinkpad | Effect |
|---|---|---|
| Ollama (`localhost:11434`) | **not installed** | embeddings unavailable; `embed_sim` degrades and ranking falls back to priority+recency. `embeddings.py` catches the failure and returns `None`, so it is genuinely fail-open. |
| Credence skin | `/usr/bin/julia` + `~/git/credence` both present | the ranker really does work here. |
| Invidious (`http://steel:5151`) | unreachable while steel is down | YouTube embeds degrade. |

## Incident record — 2026-08-26 to 2026-08-28

steel went offline 2026-08-26 07:00 UTC and stayed down. Nothing alerted; the
standby sat frozen at LSN `45/6A58F38` for **2 days 8 hours** while the WAL
receiver retried every ~135s, and rssfeed simply did not exist during that
time. Promotion is manual and nothing watches replication lag — that gap is
the real lesson here, and the alerting for it belongs in `~/git/ops` alongside
`watch-health.sh`, not in this repo.

`promote-standby.sh` was then run for real on 2026-08-28 and worked as
written. Verified after promotion: `pg_is_in_recovery()` = `f`, timeline
advanced 2 → 3, the reader served 200s, and a forced feed refresh fetched and
parsed cleanly.

Two things the real run exposed, both now fixed:

1. **`RSSFEED_REPLICATOR_PASSWORD` was not in thinkpad's keyring at all** —
   `failback-to-steel.sh` hard-exits without it, and thinkpad was by then
   holding the only copy of the data. It was recovered from the standby's
   `postgresql.auto.conf` (`pg_basebackup -R` leaves `primary_conninfo` there
   in plaintext) and stored. **The key must exist on both boxes**: failback
   runs on steel, but the re-seed step needs it here too.
2. The 2026-08-25 rehearsal had correctly opened the Postgres port beyond
   loopback so failback could reach it, but did so as `5433:5432` — every
   interface. On a laptop with no host firewall that is live LAN exposure, so
   it is now scoped to loopback + tailnet (see "Binding and exposure").

**Failback has not run for this outage yet.** The script itself was rehearsed
end-to-end on 2026-08-25 (see above), so it is not untested code — but that
rehearsal was a controlled one with writes drained and steel healthy, and its
`podman rm -f` fixes have never been exercised against a machine that was
genuinely down. The replication path into this box was re-verified on
2026-08-28 after the rebind: a `replication=database` connection to
`100.100.33.81:5433` as `replicator` authenticates and returns
`IDENTIFY_SYSTEM`.

Until failback runs, do **not** start steel's `rssfeed-sidecar.service`:
thinkpad is the only writable copy, and two live primaries is exactly the
split-brain this design exists to prevent.
