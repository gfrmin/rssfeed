# Design System — rssfeed (sidecar reader)

The contract for the reader's UI. Read it before changing a template or `style.css`; a change that
conflicts with it is a bug unless this file changes in the same PR. The look was chosen by the owner
on 2026-05-31 (a Claude Design prototype, "as much as possible like the one I just showed you") and
ported into `sidecar/static/style.css`; this file codifies it rather than redesigning it. Journeys in
`.claude/ux/journeys/` say what each screen is for; `.claude/ux/site.md` says how to run and audit it.

## Tokens

Defined once in `sidecar/static/style.css` (`:root, [data-theme="dark"]` and `[data-theme="light"]`) and
exposed to Tailwind through `@theme` in `sidecar/static/input.css`. **Use the variable, never its value.**

### Color

| token | role | rule |
|---|---|---|
| `--bg`, `--panel`, `--panel-2`, `--raised`, `--hover` | surfaces, darkest to lightest | a surface is never the only thing separating two regions: pair it with `--border` |
| `--border`, `--border-2` | hairlines, control outlines | |
| `--text` | body and titles; also "unread" (`--unread`) | |
| `--muted` | secondary text: meta lines, excerpts | must pass 4.5:1 on `--panel` in both themes |
| `--faint` | tertiary: separators, disabled, read titles (`--read`) | decoration and read state only; never the only carrier of information a reader needs |
| `--accent` (+`--accent-hover`) | the one amber: current selection, unread dot, primary action, star | at most one accent-filled control per region |
| `--danger` | failing feeds, destructive actions | always with an icon or word |
| `--add`, `--del` (+`-bg`), `--ctx` | diff lines only | never used for health or ranking: red/green is unreadable to the owner's colour-blind-safe requirement unless paired with `+`/`−` |
| `--link` | links inside article bodies only | chrome links use `--text`/`--muted` |
| `--tile-bg`, `--tile-fg` | feed initial tiles | |

Dark is the default theme; light must be equally complete (every token redefined).

### Typography

- **UI**: `"Geist", system-ui, …` at 13px base (`body`), 1.5 line height. Sizes from `@theme`
  (`--text-2xs` … `--text-article`) or the existing literal steps in `style.css`; no new ad-hoc sizes.
- **Labels and data**: `"Geist Mono", ui-monospace, …` (`.mono`, `.mlabel`) for counts, timestamps, section
  labels. `.mlabel` uppercase is for Latin-script chrome only.
- **Reading**: `.art-body` 15.5px / 1.72, paragraphs 17px apart, headings 18.5/16.5px. The article is the
  only place long text appears; it gets the largest type in the app.
- Counts and times use tabular figures.

### Spacing & layout

- Spacing follows the literal steps already in `style.css` (multiples of ~2px around 4/6/8/12/16/24);
  match the neighbouring component rather than inventing a step.
- **Desktop (≥880px)**: three panes — sidebar (views, feeds by priority), list, reader.
- **Mobile (<880px)**: one pane at a time — drawer ⇄ list ⇄ reader — owned by CSS
  (`@media (max-width: 879px)`); `app.js` mirrors it to `is-mobile` via `matchMedia`, never `innerWidth`.
  Every route renders this way when loaded directly, not only after an htmx swap.
- Phone widths 360–412px are the primary test widths; no page scrolls sideways at 360.

### Motion

- Hover/state transitions ~0.1s; overlays fade/pop ~0.15s; finger-follow swipe tracks the finger.
- Under `prefers-reduced-motion: reduce` everything above is instant (already in `style.css`).
- Nothing animates to signal success alone; the new state must be visible when the motion is skipped.

## Signature element

**The reading flow**: open a feed, read a post at full article size, swipe to the next. Dense chrome,
generous article. Boldness is spent on the article pane and the swipe; everything else stays quiet,
monochrome and small so the text wins.

## Banned patterns

Each is a finding when seen. Project-specific bans cite the owner's words (journeys README, prompt dates).

- [ ] **Native dialogs**: no `alert()`, `confirm()`, `prompt()`, `hx-confirm`. Confirm inline, in the page
      (owner, 2026-04-21). Three `hx-confirm` remain at the time of writing (`site.md` lists them).
- [ ] **Silent actions**: no button whose effect is invisible until reload. Every POST re-renders its control
      or region with the new state within a second, or shows progress, then the result or a reason
      ("fetch full text doesn't do anything", "refresh now does nothing"; 2026-04 to 2026-06).
- [ ] **Raw responses in the UI**: no JSON (`{"ok":true}`), stack traces or Miniflux error strings as the
      primary message. A cause in plain words first; raw text one disclosure away.
- [ ] **Hue-only meaning**: no state carried by colour alone — health, ranking, thumbs, diff. Icon + word
      (+ ▲/▼ or +/−) (2026-06-20).
- [ ] **Settings on the reading surface**: per-feed configuration lives in feed settings, not in the list or
      reader ("set and forget per feed", 2026-03-31).
- [ ] **Two UIs for one thing**: one feed-settings surface, whether opened as overlay or by URL.
- [ ] **Dead ends**: no screen that shows a problem without a route to its fix (a health dot with no path
      to the feed's settings).
- [ ] **Mislabelled state**: a label must match what the system will do ("OFF" while still proxied, #58;
      "stale" meaning last poll when the reader asks about last post).
- [ ] **Equal weight for one-off tools**: OPML import/export never sits beside "Add feed" at the same weight
      (2026-04-11).
- [ ] **Unescaped or double-escaped text**: no `&#39;` in previews, no markup rendered as text in titles.
- [ ] **Accent as decoration**: amber marks selection, unread and the primary action; not backgrounds,
      borders or headings.
- [ ] **Gradients** except the existing warmth meter bar, which must also carry a number.
- [ ] Default browser-blue links or focus rings in chrome; unstyled native controls.
- [ ] Spinner-only loading where the previous content could stay readable.
- [ ] Apple-only assumptions: test on Android/Chromium emulation (2026-06-05).

## State matrix requirements

Every view defines loading, empty, error and populated. The specific triggers come from the journeys'
`failure_modes`; the generic four still apply.

| view (route) | loading | empty | error | populated — must show first |
|---|---|---|---|---|
| Feed list (`/entries?feed_id=`) | rows paint before counts finish (500+ feeds) | "All read" + one tap to show read posts | feed failing → health line says broken since when, links to its cause | feed name, health line, newest unread title |
| Ranked view (`/entries?view=unread`) | list stays; order says "not personalised" when the ranker is down | "All caught up" | ranker down → priority + recency, labelled | ≥3 titles with feed names; no single feed fills the first screen |
| Article (`/entries/{id}`) | previous article never shown as if it were the new one | feed gave no body → say so, offer fetch | fetch failed → reason + next step (login, proxy), feed text still readable; fetched text empty → shown as empty with "Show original RSS" | source label (feed text / full article vN) and first paragraph; direct load = same layout as in-app |
| Diff (`/entries/{id}/diff`) | — | one version → "no changes stored" | label and diff never disagree | first changed passage, marked by +/− not colour alone |
| Triage (`/triage`, `/triage/{bucket}`) | — | "Nothing needs attention" | — | causes in plain words, count per cause, the feeds, the remedy with its cost |
| Feed settings (`/feeds/{id}`) | control shows busy | — | a remedy that did not work says so with the new error | full text, priority, paused, proxy state in words |
| Feeds (`/`) | table paints progressively | no feeds → "Add feed" field and a hint | subscribe failure → cause (already subscribed, not a feed, blocked) | add-feed field above the table |

## Accessibility floor

- **WCAG 2.2 AA**, checked against a live seeded instance by `accessibility-evaluator` (axe-core), not by
  reading templates.
- Contrast 4.5:1 text / 3:1 UI in **both** themes, including `--muted` on every surface it sits on.
- Targets ≥24×24 CSS px everywhere (2.5.8); the reader's primary mobile actions (back, next/previous,
  star, more/less, fetch) ≥44×44.
- Every interactive element reachable and visible on keyboard focus, with a focus style using `--accent`;
  `j/k/o/v/m/s/r` and ⌘K keep working.
- Overlays are dialogs: labelled, focus moved in and trapped, Escape closes, background scroll locked.
- Article bodies keep their language and direction: an RTL article renders `dir="rtl"` with its punctuation
  in place; list rows of an RTL feed align titles to the start of their own direction.
- Colour-blind safe as in Banned patterns.

## Verification

- Local server: `make seed-up` (seeded instance, invented content) → `http://127.0.0.1:9147/`; `make seed-down`.
- Audit: the global `ux-loop` skill with `.claude/ux/site.md`; ledger at `docs/ux/findings.yaml`.
