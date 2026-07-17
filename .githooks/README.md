# Git hooks — structural PII guard

This is a **public** repo, and it fronts a personal RSS reader: the tree must never
carry personal data. These version-controlled hooks block committing or pushing
content shaped like real personal data. Activate them once per clone:

    git config core.hooksPath .githooks

(Git ≥2.9. Scripts need the executable bit; if a clone dropped it, run
`chmod +x .githooks/pre-commit .githooks/pre-push`.)

`pii_check.py` enforces an **allowlist of safe shapes** — only `@example.*` emails
(plus `pii-allow.txt`), no checksum-valid Israeli IDs, no passport/mobile shapes,
no filesystem paths outside `pii-path-allow.txt`, and real machine paths (derived
live from `$HOME`) rejected outright. It supplements this with a **private
denylist** of shapeless things (names, employers, private domains) read from
`$LIFE_AGENT_KB/pii-patterns.txt`, which is never stored in any repo. Because the
shapes are an allowlist, this catches *novel* PII by construction rather than only
known values. Output reports `path:line: kind` and never echoes the matched value.

```
python3 .githooks/pii_check.py                  # scan the whole tracked tree
python3 .githooks/pii_check.py --shapes-only    # no private list needed
```

Without `LIFE_AGENT_KB` the hooks **fail closed** (exit 2, refuse to scan blind) —
by design. Export it, or use `--shapes-only` for an ad-hoc scan.

Exempt a reviewed false positive with an inline `PII-OK` marker on that line. Use
it sparingly: it exempts the *whole line from every rule*, including the denylist,
so prefer a `pii-path-allow.txt` entry when the issue is only a path shape.

## Provenance

`pii_check.py` is copied **verbatim** from `life-agent/.githooks/pii_check.py` so
the two stay diffable — improve it there and re-copy, rather than editing here.
Only the two `.txt` allowlists are repo-specific; the private denylist is shared
across repos via `$LIFE_AGENT_KB`, so one list protects all of them.

## What's tuned for this repo

This is a web app, so most path-shaped strings in the tree are **HTTP routes**
(`/entries/{id}/related`, `/feeds`, `/v1/...`), not filesystem paths. They trip the
path heuristic purely by having two segments, so the route namespaces are
allowlisted in `pii-path-allow.txt`. Untuned, the guard reported 137 findings here;
tuned, it reports 0 — with the real machine-path rule still active.
