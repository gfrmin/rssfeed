# rssfeed journeys

One file per job the owner (the only user) uses rssfeed for. Read by the global `ux-loop`
skill and its critics; schema in the `ux-journeys` skill, extra keys in `ux-loop`.

## Where the evidence comes from

- **The owner's own words**: their Claude Code prompt history for this project
  (Claude Code's `history.jsonl` on the owner's machines, project `rssfeed`, 2026-03-30 to
  2026-10-18, ~600 prompts). It is private and not in this repo. Each journey's `source`
  names the prompt dates; quotes are trimmed and **de-identified**: no feed, site or
  publication names, because the repo is public and a reading list is personal.
- **Behaviour**: aggregate counts from the production database (Miniflux `entries`,
  sidecar `engagement_events`) and the sidecar's access log. Counts only, never titles.
  Snapshot taken 2026-10-18 is quoted in each file; `signals:` re-measure it.

Requests show what the owner wanted at the time; counts show what they do now. Where the
two disagree, the journey says so instead of picking one.

## The journeys

| file | job | strength |
|---|---|---|
| `read-one-feed-through.yaml` | read a favourite feed's new posts, newest first, swiping on a phone | strongest: 89% of reads |
| `trust-the-full-article.yaml` | read the whole article as published, not the feed's teaser | the founding need |
| `diagnose-a-quiet-feed.yaml` | find out why a feed stopped and fix it | asked 4+ times |
| `set-up-a-feed-once.yaml` | configure a feed once (full text, paid login, priority) and forget it | asked often |
| `repoint-a-moved-feed.yaml` | give a feed its new URL without losing its history | one burst, 2026-04-20/21 |
| `subscribe-to-a-new-source.yaml` | add a site, feed URL or not | moderate |
| `find-worth-reading-across-feeds.yaml` | see the best of everything unread, ordered for me | wanted; little used |
| `teach-what-i-like.yaml` | tell the reader what I liked so the ordering improves | **kill signal met**: no ratings in 60 days |
| `see-how-an-article-changed.yaml` | check whether and how a post changed after publication | wanted; route unused |

Not journeys: AI summaries and custom summary prompts (built 2026-04, removed in the
2026-06-19 "back to basics" cleanup the owner asked for); OPML import (one-off migration).

## Cross-cutting facts every journey inherits

- **Device**: an Android phone (Brave) over a VPN is the main reader; the
  desktop is for setup. The owner uses no Apple hardware (2026-06-05) — emulate Android.
- **Colour**: must work for red/green colour-blind readers (2026-06-20).
- **No native dialogs**: `alert`/`confirm` popups were rejected (2026-04-21) — say it on the page.
- **Every button answers**: the most repeated complaint across all journeys is a click that
  changes nothing visible ("fetch full text doesn't do anything", "refresh now does nothing",
  "login button does nothing", "confirm… nothing happens"; 2026-03-31 to 2026-06-26).
