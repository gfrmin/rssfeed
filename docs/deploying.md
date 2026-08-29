# Deploying

The stack is three pieces, and only two of them are containers:

| Piece | Runs as | Why |
|---|---|---|
| Postgres | container (`docker-compose.yml`) | shared by Miniflux and the sidecar |
| Miniflux | container (`docker-compose.yml`) | owns feeds, entries, read/star state |
| the sidecar | **on the host** | needs `uv`, and — optionally — a local Chromium for paywall logins and a local Ollama for embeddings |

The sidecar is deliberately **not** a compose service. It is a thin app over a
database that two things share, and running it on the host keeps the optional
extras (`playwright install chromium`, an Ollama on `localhost:11434`, a Credence
ranker) reachable without wiring each one through a container boundary. It also
means you can restart the reader without touching the data.

## Prerequisites

- Docker (or Podman) with Compose
- [uv](https://docs.astral.sh/uv/) — the sidecar's dependencies are managed with it,
  and the system Python has none of them

## Running it as a service

`systemd/rssfeed-sidecar.service` is an example **user** unit. Symlink rather than
copy, so a `git pull` keeps it current:

```bash
ln -sfn ~/git/rssfeed/systemd/rssfeed-sidecar.service \
  ~/.config/systemd/user/rssfeed-sidecar.service
systemctl --user daemon-reload
systemctl --user enable --now rssfeed-sidecar
```

To survive logout and start at boot, user services need lingering:

```bash
loginctl enable-linger "$USER"
```

Don't run `./run-sidecar.sh` at the same time — they fight over the port.

## Where it listens, and why the default is loopback

`RSSFEED_BIND` defaults to `127.0.0.1`. **The reader has no authentication of its
own.** Anyone who can reach the port can read your subscriptions, your article
archive, and use any site logins you have stored. So the default is the safe one,
and widening it should be a decision you make on purpose:

- **A VPN address** (WireGuard, Tailscale, ZeroTier) is the good option — reachable
  from your other machines, not from the internet. Set `RSSFEED_BIND` to that
  interface's address.
- **Behind a reverse proxy** that does the authenticating (basic auth, an OIDC
  forward-auth, Authelia) — bind loopback and let the proxy be the only listener.
- **`0.0.0.0` only if you have a host firewall you trust.** A laptop that binds
  `0.0.0.0` publishes the reader to every network it later joins.

If you bind to a VPN interface, be aware it may not exist yet at boot. Add a
readiness wait as a drop-in rather than editing the unit:

```ini
# ~/.config/systemd/user/rssfeed-sidecar.service.d/wait-for-vpn.conf
[Service]
ExecStartPre=/bin/bash -c 'for i in $(seq 60); do ip addr show wg0 2>/dev/null | grep -q "inet " && exit 0; sleep 2; done; exit 1'
```

This matters more than it looks: in a **user** systemd manager,
`network-online.target` is inert — it reports inactive and has no dependencies — so
`After=network-online.target` orders against nothing at all. An explicit wait is the
only thing that works.

## Customising without editing tracked files

Two levers, both survive `git pull`:

- **`.env`** (gitignored) for values — see `.env.example`.
- **A drop-in** at `~/.config/systemd/user/rssfeed-sidecar.service.d/*.conf` for unit
  settings. Note that `ExecStartPre=` in a drop-in *appends* to the list, while
  `ExecStart=` is a list you must clear with an empty `ExecStart=` before setting
  your own.

## Backups

Everything that matters is in Postgres — entries, snapshots, per-feed config,
engagement events, ranker weights, embeddings and stored site cookies. Back up the
`pgdata` volume, or take periodic `pg_dump`s. The sidecar keeps no state of its own
on disk beyond `.env` and the optional login-recipes file.
