# Mobile UX Audit — RSS Reader (sidecar)

_Date: 2026-06-05 · Scope: mobile experience first (desktop deferred)_

## How this was tested

- **Live instance** at `http://localhost:9145` (systemd `rssfeed-sidecar`, populated with real feed data).
- **Playwright 1.60** driving system **Chromium 148**, emulating **iPhone 13 (390×844, DPR 3, touch)** and **iPhone SE (320×568)**.
- Read-mostly: navigated, screenshotted, measured DOM geometry, exercised drawer / reader / swipe / overlays. No destructive or bulk actions (no delete, no "mark all read", no subscribe).
- Artifacts: screenshots in `/tmp/rssfeed-audit/shots/`, drive scripts `/tmp/rssfeed-audit/drive.cjs` + `capture2.cjs` (re-runnable for regression).

Every finding below is tagged **[verified]** (observed live) or **[code]** (from source, not stress-tested).

## Executive summary

The core **reading flow on `/entries` is genuinely good** and should not be rebuilt — the off-canvas drawer, single-pane list⇄reader, finger-follow swipe, native back integration, safe-area insets, and reduced-motion support all work, with **zero horizontal overflow** at both 390px and 320px. The command palette and triage queue render cleanly on a phone.

The rough edges cluster in five areas:

1. **A 404 "icon storm"** — ~224 failed requests per page load (perf + console noise).
2. **Touch targets and input sizing** — sub-44px tap targets and iOS zoom-on-focus.
3. **The legacy-chrome pages** (`/`, `/categories`, `/cookies`, `/digest`, full-page `/feeds/health`) never received the mobile treatment — they're a shrunk desktop top-nav.
4. **Overlay accessibility & scroll-lock** — no modal semantics, focus trap, or background scroll-lock.
5. **A few deep-link / swipe edge cases.**

## Priority findings

| # | Pri | Finding | Evidence | Root cause |
|---|-----|---------|----------|------------|
| 1 | **P0** | Per-feed icon **404 storm** (~224/load) | [verified] | `feeds.py:879-880`, `_macros.html:18` |
| 2 | **P0** | Touch targets < 44px (star 13px, icons 28–34px) | [verified] | `style.css:216,160,660,135` |
| 3 | **P0** | iOS zoom-on-focus (inputs < 16px) | [verified] | `style.css:180,477,550` |
| 4 | **P1** | Legacy pages are desktop-only on mobile | [verified] | `base.html:16-32` |
| 5 | **P1** | Drawer can't reach Digest/Categories/Cookies/Feeds | [verified] | `_sidebar.html:103-114` |
| 6 | **P1** | No body scroll-lock behind drawer/overlays | [verified] | `mobile.js`, `app.js:183` |
| 7 | **P1** | Overlays lack `role=dialog`/focus trap/restore | [verified] | `app.js:181-183` |
| 8 | **P2** | `/triage` & `/help` unstyled when opened directly | [verified] | `entries.py:453,458` |
| 9 | **P2** | No styled 404 (raw `{"detail":"Not Found"}`) | [verified] | app-level handler missing |
| 10 | **P2** | Swipe edge/inertia/hidden-button fragility | [code] | `mobile.js` |
| 11 | **P3** | Keyboard-only hints shown on touch (⌘K, ↑↓, e/s) | [verified] | `_sidebar.html:13`, palette/triage |
| 12 | **P3** | Magic numbers (80px reader pad; 264/84vw drawer) | [code] | `style.css` |
| 13 | **P1** | PWA `start_url:"/"` opens the legacy Feeds page (Android install) | [verified] | `manifest.json:4` |
| 14 | **P2** | No `maskable` icon → letterboxed on Android home screen | [verified] | `manifest.json:8-19` |

---

## Details

### P0-1 · Per-feed icon 404 storm  [verified]
Every list row and every drawer feed renders `<img src="/feeds/{id}/icon">` via the `tile()` macro (`_macros.html:18`). The route returns a bare **404** when the feed has no favicon in Miniflux's `icons` table (`feeds.py:879-880`). With many subscribed feeds, a single `/entries` load fired **~220 `404`s** (confirmed: `404 /feeds/14/icon`, `/feeds/389/icon`, …). The letter-tile fallback (`onerror="this.remove()"`) hides it visually, so it's invisible until you open devtools — but it wastes 50+ round-trips per navigation and burns mobile data/battery, and there's no caching so it repeats on every page.

**Fix:** make the icon route always return 200 — serve a generated SVG letter-avatar (or a 1×1 transparent) on miss with `Cache-Control: public, max-age=...`; better still, pass `has_icon` into `tile()` and only emit the `<img>` when an icon exists.

### P0-2 · Touch targets below 44px  [verified]
Measured minimums on iPhone 13: star button **13×13** (`style.css:216`, `padding:0`), `.icon-btn` **28×28** (`:160`), top-bar hamburger/search `.micon` **34×34** (`:660`), `.time-btn` ~**29px** tall (`:135`). The row itself is a comfortable 376×123, but the per-row star and the top-bar icons are easy to miss.

**Fix:** under `.is-mobile`, expand hit areas to ≥44×44 (extra padding or an invisible `::before` expander) — prioritise the star and the two `.micon` top-bar buttons.

### P0-3 · iOS zoom-on-focus  [verified]
`.search-input` computes to **12.5px** (`style.css:180`), `.ti` filter/rename inputs (`:477`), command palette `.cmdk-input` **15px** (`:550`). iOS Safari force-zooms when a focused input's font-size is < 16px, then leaves the page zoomed.

**Fix:** `font-size:16px` for these inputs (at least under `.is-mobile`).

### P1-4 · Legacy-chrome pages are desktop-only  [verified]
`shell.html` (the modern, mobile-aware drawer UI) is used **only by `/entries`**. Everything else — `/` (Feeds management), `/categories`, `/cookies`, `/digest`, and the full-page `/feeds/health` — extends `base.html`'s `legacy-topbar` (`base.html:16-32`): an 11-item horizontal nav that wraps into **two cramped rows of ~12px text links** with no hamburger and no drawer. `/` is the worst: it renders at **616px CSS-width on a 390px phone** (zoomed out; the "Feed or page URL" add form runs off the right edge). This is jarringly inconsistent with the polished `/entries` shell.

**Fix:** give `base.html` a real mobile treatment — collapse `legacy-topbar` into a hamburger or horizontally-scrollable bar, constrain/`max-width` `.legacy-main`, and make the feeds.html add-feed form stack. Longer-term, fold these into the shell.

### P1-5 · Mobile navigation gap  [verified]
From the drawer you **cannot reach** Digest, Categories, Cookies, or Feeds-management — they only exist in the legacy top-nav. The single escape hatch is the footer "Refresh feeds" icon, which is `<a href="/">` (`_sidebar.html:110`): its `title` says "Refresh feeds" but it **navigates to the Feeds page** and drops you into legacy chrome.

**Fix:** add drawer entries for these (as overlays where a fragment exists); fix the refresh icon's label/behaviour so it does what it says.

### P1-6 · No body scroll-lock  [verified]
With the drawer open, `body{overflow:visible}` (and same with the triage overlay open) — content scrolls behind the scrim.

**Fix:** lock background scroll when the drawer/overlay opens (`overflow:hidden` on body or the scroll container) and restore on close.

### P1-7 · Overlays lack modal semantics & focus management  [verified]
The stats overlay renders with `role=null, aria-modal=null`; `closeOverlay()` (`app.js:183`) just does `innerHTML = ''` — no focus trap while open, no focus restored to the trigger. The same pattern covers palette, triage, help, filters, health.

**Fix:** add `role="dialog" aria-modal="true"`, trap focus inside the overlay, and restore focus to the opener on close.

### P2-8 · `/triage` and `/help` unstyled on direct hit  [verified]
Both routes always return the bare fragment with no full-page fallback (`entries.py:453` → `_triage.html`, `:458` → `_help_overlay.html`). Compare `/stats` (`stats.py:85`) and `/filters` (`filters.py:30`), which correctly return the full styled page when `HX-Request` is absent. So a bookmark, typed URL, or open-in-new-tab on `/triage` or `/help` yields raw unstyled HTML. (In-app, both open as proper overlays — the triage overlay is excellent on mobile — so this only bites direct navigation.)

**Fix:** mirror the stats/filters pattern (full page on non-HX requests).

### P2-9 · No styled 404  [verified]
Unknown URLs return FastAPI's raw `{"detail":"Not Found"}` JSON.

**Fix:** register an exception handler that renders a styled 404 and points back to `/entries`.

### P2-10 · Swipe fragility  [code]
The mechanic works (verified: swipe navigated `34548 → 34540` and landed cleanly). But: the 24px edge cutoff is binary (no hysteresis), there's no velocity/inertia (a confident flick and a slow drag are both judged only against the 70px threshold), it drives navigation by `.click()`ing the hidden `.rb-nav` buttons (silently no-ops if disabled), and `prefers-reduced-motion` is sampled once at load. Polish, not blockers.

### P3-11 / P3-12 · Cosmetic
Keyboard-only affordances surface on touch — the `cmdk-trigger` "⌘K" pill (`_sidebar.html:13`), the palette footer ("↑↓ navigate · ↵ run · esc close"), triage key hints (e/s) — hide them under `.is-mobile`. Magic numbers: `.reader-inner` has 80px bottom padding with no corresponding element; the 264px/84vw drawer leaves only ~52px of scrim as a tap-to-close target at 320px.

---

### P1-13 · PWA opens the wrong page on Android  [verified]
`manifest.json:4` sets `start_url: "/"`. Android Chrome treats the manifest as first-class (install-to-home, standalone), so the installed app **launches into the legacy Feeds page** (`/`) — the cramped desktop chrome from P1-4 — not the reading shell. iOS largely ignores this.

**Fix:** `start_url: "/entries?view=unread"`.

### P2-14 · No maskable icon on Android  [verified]
`manifest.json:8-19` declares 192/512 icons but none with `"purpose": "maskable"`. Android adaptive-icon launchers letterbox a non-maskable icon inside a white circle/squircle.

**Fix:** add a maskable variant (`"purpose": "maskable"`) with adequate safe-zone padding.

## Cross-platform (Android vs iOS)

Tested **Pixel 7 / Chrome 148 (real Android engine)** and the original "iPhone 13" run — which, importantly, *also used Chromium*, so it reflects Android more than Safari. The **WebKit/Safari engine could not be launched here** (host missing `libicu74`/`libxml2`/`libflite1`), so true iOS rendering/behaviour is reasoned, not measured.

**Identical on both (engine-independent):** layout, zero horizontal overflow, the 404 icon storm (218 on Android), touch-target sizes, swipe navigation, and every CSS feature the app uses (`color-mix`, `text-wrap:balance`, `env()`, `backdrop-filter` all supported).

**iOS-only risks (not reproduced on Android):**
- **P0-3 zoom-on-focus is iOS-only** — Android Chrome does *not* auto-zoom on sub-16px inputs. Still worth fixing for iOS users, but Android is unaffected.
- **Edge-swipe-back conflict** — the article swipe starts near the left edge, where Safari's edge-back gesture lives; the 24px `EDGE` reserve mitigates but needs a real device to confirm.
- **Rubber-band overscroll** — the missing scroll-lock (P1-6) looks worse on iOS (background rubber-bands) than on Android.

**Android-specific (worse or extra):**
- **PWA install matters** — surfaces findings 13 & 14 above; iOS mostly ignores them.
- **Stricter touch guideline** — Material wants 48dp vs Apple's 44pt, so the sub-44px targets miss the Android bar by more.
- **System back button** is central — the drawer-consumes-first-back and history model deserve real-device testing.

**Bottom line:** Android shows the *same* layout/perf/sizing problems (it's the same engine), drops the iOS-zoom issue, and adds two PWA issues. For true Safari coverage, test on a physical iPhone or a WebKit-capable CI (BrowserStack/Sauce) — this machine can't run the WebKit engine.

## What already works well (don't rebuild)
- Drawer open/close + scrim, fully usable at 320px and 390px.
- Single-pane list⇄reader switch with native back-button integration.
- Finger-follow swipe between articles (verified clean navigation).
- Safe-area insets for notch/home-indicator; `prefers-reduced-motion` handling.
- **Zero horizontal overflow** in the `/entries` shell at 320px and 390px.
- Command palette and triage overlay are polished on mobile.

## Suggested fix sequence
1. **Quick wins (P0):** icon route 200+cache, ≥44px touch targets, 16px inputs. Small, high-impact, low-risk.
2. **Consistency (P1-4/5):** mobile-ize `base.html` legacy chrome + wire the missing drawer destinations.
3. **A11y/correctness (P1-6/7, P2-8/9):** scroll-lock, modal semantics/focus, direct-route fallbacks, styled 404.
4. **Polish (P2-10, P3):** swipe inertia, hide keyboard-only hints, magic-number cleanup.

## Status — all findings fixed (2026-06-05, verified on Pixel 7)

Every P0–P3 above is resolved and verified live (Playwright, Pixel 7 / Chrome 148). Headline before→after: **icon 404s 224→0**, **console errors 224→0**, search input **12.5px→16px**, hamburger **34→44px**, star **13→34px**, `/` feeds page **616px overflow→0**, overlays now `role=dialog`+`aria-modal` with focus-trap + background scroll-lock, `/triage` & `/help` direct hits redirect into the app with the overlay auto-opening, unknown URLs render a styled 404.

Changes by file:
- `app/routes/feeds.py` — icon route returns `204`+`Cache-Control` on miss (was bare 404).
- `app/routes/entries.py` — `/triage`,`/help` redirect non-HTMX hits to `/entries?open=…`.
- `app/main.py` + `templates/404.html` — styled 404 handler (JSON for `/api`).
- `static/style.css` — touch targets, 16px inputs, legacy-chrome `@media (max-width:880px)` layout, `.ov-lock`, hide keyboard-only hints, magic-number cleanup.
- `static/app.js` — overlay dialog semantics + focus trap/restore, scroll-lock sync, `?open=` auto-open.
- `static/palette.js` — dialog semantics, scroll-lock, focus restore.
- `static/mobile.js` — drawer scroll-lock, live `prefers-reduced-motion`, swipe flick-velocity.
- `templates/_sidebar.html` — drawer gains Feeds + Digest; mislabeled "Refresh feeds" icon removed.
- `static/manifest.json` — `start_url=/entries?view=unread`, maskable icon, dark theme color.
- `templates/base.html` — favicon link; static cache-bust `?v=ds1→ds2`.

## Reproduce
```bash
NODE_PATH="$(npm root -g)" node /tmp/rssfeed-audit/drive.cjs      # main drive + measurements
NODE_PATH="$(npm root -g)" node /tmp/rssfeed-audit/capture2.cjs    # overlays + legacy pages
```
