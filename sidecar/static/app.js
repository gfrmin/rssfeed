/* ============================================================
   RSS Sidecar — client runtime for the three-pane shell.
   Vanilla JS + HTMX. No framework.
   ============================================================ */
(function () {
  'use strict';

  var html = document.documentElement;
  var body = document.body;

  // We drive the history stack ourselves (see below), so turn off htmx's.
  if (window.htmx) window.htmx.config.historyEnabled = false;

  /* ---------- theme ---------- */
  function applyTheme(t) { html.setAttribute('data-theme', t); localStorage.setItem('theme', t); }
  applyTheme(localStorage.getItem('theme') || 'dark');
  function toggleTheme() { applyTheme(html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'); }

  /* ---------- mobile detection ----------
     The breakpoint is owned by CSS (@media max-width:879px). JS mirrors it into the
     is-mobile class for behavioural code (drawer/swipe/reading-mode) via the SAME media
     query — not window.innerWidth, which a wide article element can inflate past the
     threshold (a horizontal-overflow page reports innerWidth > viewport), silently
     dropping the phone back into the three-pane desktop layout on a deep-linked article. */
  var mobileMQ = window.matchMedia('(max-width: 879px)');
  function onEntryUrl() { return /^\/entries\/\d+$/.test(location.pathname); }
  function syncMobile() {
    var mobile = mobileMQ.matches;
    body.classList.toggle('is-mobile', mobile);
    // A direct load / refresh of /entries/{id} renders the full shell; on mobile we must
    // show the reader pane, not the (empty, background-filled) list. Re-checked on every
    // breakpoint change so a rotate/resize across the threshold self-corrects too.
    if (mobile && onEntryUrl()) body.classList.add('mobile-reading');
  }
  syncMobile();
  if (mobileMQ.addEventListener) mobileMQ.addEventListener('change', syncMobile);
  else window.addEventListener('resize', syncMobile);   // legacy fallback

  /* ---------- delegated controls ---------- */
  document.addEventListener('click', function (e) {
    var act = e.target.closest('[data-act]');
    if (act) {
      if (act.dataset.act === 'theme') toggleTheme();
      return;
    }
    // Diff overlay: Unified/Split toggle (both rendered server-side).
    var dm = e.target.closest('.diff-modeseg .seg-btn');
    if (dm) {
      var panel = dm.closest('.ov-panel');
      if (panel) panel.setAttribute('data-diffmode', dm.dataset.diffmode);
      Array.prototype.forEach.call(dm.parentNode.querySelectorAll('.seg-btn'), function (b) { b.classList.toggle('on', b === dm); });
    }
  });

  /* ============================================================
     LIST: keyboard navigation + selection over .erow rows
     ============================================================ */
  var selectedIdx = -1;

  function listRows() {
    var lc = document.getElementById('list-col');
    return lc ? Array.prototype.slice.call(lc.querySelectorAll('.erow')) : [];
  }

  function selectRow(idx, opts) {
    var rows = listRows();
    if (!rows.length) return;
    idx = Math.max(0, Math.min(rows.length - 1, idx));
    rows.forEach(function (r) { r.classList.remove('sel'); });
    selectedIdx = idx;
    var row = rows[idx];
    row.classList.add('sel');
    row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    updateStatusPos(idx + 1, rows.length);
    if (opts && opts.open) openRow(row);
  }

  function openRow(row) {
    if (!row) return;
    // HTMX-enabled rows load the reader pane; fall back to navigation.
    if (window.htmx) { window.htmx.trigger(row, 'reader:open'); }
    var href = row.getAttribute('data-href');
    if (!window.htmx && href) window.location.href = href;
    markRowRead(row, true);
  }

  function markRowRead(row, read) {
    if (!row) return;
    row.classList.toggle('read', read);
    row.classList.toggle('unread', !read);
    var dot = row.querySelector('.erow-unread-dot');
    if (read && dot) dot.remove();
  }

  function postAction(id, action) {
    if (!id) return Promise.resolve();
    return fetch('/entries/' + id + '/' + action, { method: 'POST' }).catch(function () {});
  }

  /* ---------- engagement signals (Part B) ----------
     Quality-of-attention only. Plain reads (swipe / mark-all) are NEVER sent. */
  function sendEvent(id, payload) {
    if (!id) return;
    var url = '/entries/' + id + '/event';
    var body = JSON.stringify(payload);
    // sendBeacon survives navigation/unload — important for dwell flushes.
    if (navigator.sendBeacon) {
      try { navigator.sendBeacon(url, new Blob([body], { type: 'application/json' })); return; } catch (_) {}
    }
    fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body, keepalive: true }).catch(function () {});
  }
  function logOpenOriginal(id, feedId) {
    sendEvent(id, { signal: 'open_original', value: 1, feed_id: feedId || null });
  }
  function postThumb(id, dir, feedId) {
    if (!id) return;
    fetch('/entries/' + id + '/thumb', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir: dir, feed_id: feedId || null }),
    }).catch(function () {});
  }

  /* Dwell: how long a reader pane stays open before moving on. A deliberate
     read lingers (>= threshold, logged); a swipe-past is brief (dropped). */
  var DWELL_MIN = 4;                      // seconds; server re-checks this too
  var dwell = { id: null, feedId: null, since: 0 };
  function flushDwell() {
    if (!dwell.id) return;
    var secs = (Date.now() - dwell.since) / 1000;
    var id = dwell.id, feedId = dwell.feedId;
    dwell = { id: null, feedId: null, since: 0 };
    if (secs >= DWELL_MIN) sendEvent(id, { signal: 'dwell', value: secs, feed_id: feedId || null });
  }
  function startDwell(id, feedId) {
    flushDwell();
    if (!id) return;
    dwell = { id: id, feedId: feedId || null, since: Date.now() };
  }
  function startDwellFromReader() {
    var r = document.querySelector('#reader-col .reader[data-entry-id]') ||
            document.querySelector('#reader-col [data-entry-id]');
    if (r) startDwell(r.getAttribute('data-entry-id'), r.getAttribute('data-feed-id'));
  }

  function toggleRead(row) {
    if (!row) return;
    var id = row.dataset.entryId;
    var isUnread = row.classList.contains('unread');
    postAction(id, isUnread ? 'mark-read' : 'mark-unread');
    markRowRead(row, isUnread);
  }

  function toggleStar(row) {
    if (!row) return;
    var id = row.dataset.entryId;
    postAction(id, 'toggle-star');
    var btn = row.querySelector('.erow-star');
    if (btn) btn.classList.toggle('on');
  }

  function markAllVisible() {
    var rows = listRows();
    var ids = rows.map(function (r) { return parseInt(r.dataset.entryId, 10); }).filter(Boolean);
    if (!ids.length) return;
    fetch('/entries/mark-all-read', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entry_ids: ids }),
    }).then(function () { rows.forEach(function (r) { markRowRead(r, true); }); }).catch(function () {});
  }

  /* row click → select; HTMX handles the reader load via the row's own trigger */
  document.addEventListener('click', function (e) {
    // Inline hide (✕ on a feed-page author/tag): htmx posts the mute on the button
    // itself; swallow the click here so it doesn't also open the row's reader.
    if (e.target.closest('.erow-hide')) { e.stopImmediatePropagation(); return; }
    var star = e.target.closest('.erow-star');
    if (star) {
      e.preventDefault(); e.stopPropagation();
      toggleStar(star.closest('.erow'));
      return;
    }
    var row = e.target.closest('.erow');
    if (row) {
      var rows = listRows();
      var idx = rows.indexOf(row);
      if (idx >= 0) selectRow(idx);
      openRow(row);
    }
    var markAll = e.target.closest('.mark-all');
    if (markAll) { e.preventDefault(); markAllVisible(); }

    // reader-bar "Original" link → log open-original (lets the link proceed).
    var orig = e.target.closest('.rb-original');
    if (orig) {
      var rsec = orig.closest('.reader');
      logOpenOriginal(orig.dataset.entryId, rsec && rsec.getAttribute('data-feed-id'));
      return;
    }

    // reader-bar mark read / star / thumb (no HTML swap; toggle in place)
    var rb = e.target.closest('.rb-act[data-raction]');
    if (rb) {
      e.preventDefault();
      var id = rb.dataset.entryId;
      if (rb.dataset.raction === 'read') {
        var nowRead = rb.textContent.trim().indexOf('Mark read') === 0;
        postAction(id, nowRead ? 'mark-read' : 'mark-unread');
        rb.textContent = nowRead ? 'Mark unread' : 'Mark read';
        syncListRowRead(id, nowRead);
      } else if (rb.dataset.raction === 'star') {
        postAction(id, 'toggle-star');
        rb.classList.toggle('on');
      } else if (rb.dataset.raction === 'thumb') {
        var rsec2 = rb.closest('.reader');
        postThumb(id, rb.dataset.dir, rsec2 && rsec2.getAttribute('data-feed-id'));
        // Visual: the two thumbs are mutually exclusive within this session.
        var bar = rb.closest('.rb-actions');
        if (bar) Array.prototype.forEach.call(bar.querySelectorAll('.rb-thumb'), function (b) { b.classList.toggle('on', b === rb); });
      }
    }
  });

  function syncListRowRead(id, read) {
    var row = document.querySelector('.erow[data-entry-id="' + id + '"]');
    if (row) markRowRead(row, read);
  }

  /* ============================================================
     OVERLAYS (feed-settings / diff) loaded as fragments into #overlay-slot.
     ============================================================ */
  function overlaySlot() { return document.getElementById('overlay-slot'); }
  function overlayOpen() { var s = overlaySlot(); return s && s.children.length > 0; }

  // Background scroll-lock while a drawer or overlay is up.
  function syncScrollLock() {
    body.classList.toggle('ov-lock', body.classList.contains('drawer-open') || overlayOpen());
  }

  // Focus management + dialog semantics for overlays (a11y).
  var lastFocused = null;
  function focusables(root) {
    return Array.prototype.slice.call(root.querySelectorAll(
      'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea,[tabindex]'
    )).filter(function (el) { return el.tabIndex !== -1 && el.offsetParent !== null; });
  }
  function onTrapKeydown(e) {
    if (e.key !== 'Tab') return;
    var s = overlaySlot(); if (!s) return;
    var f = focusables(s); if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
  function markOverlayA11y() {
    var s = overlaySlot(); if (!s) return;
    var scrim = s.querySelector('.ov-scrim');
    if (!scrim || scrim.getAttribute('role') === 'dialog') return;
    scrim.setAttribute('role', 'dialog');
    scrim.setAttribute('aria-modal', 'true');
    if (lastFocused === null) lastFocused = document.activeElement;
    document.removeEventListener('keydown', onTrapKeydown, true);
    document.addEventListener('keydown', onTrapKeydown, true);
    var f = focusables(s);
    if (f.length) f[0].focus();
    syncScrollLock();
  }
  function closeOverlay() {
    var s = overlaySlot(); if (s) s.innerHTML = '';
    document.removeEventListener('keydown', onTrapKeydown, true);
    syncScrollLock();
    if (lastFocused && lastFocused.focus) { try { lastFocused.focus(); } catch (_) {} }
    lastFocused = null;
  }

  document.addEventListener('click', function (e) {
    var s = overlaySlot();
    if (!s || !s.children.length) return;
    if (e.target.classList.contains('ov-scrim') || e.target.closest('[data-close]')) {
      closeOverlay();
    }
  });

  /* ============================================================
     MOBILE READING STATE + HISTORY-DRIVEN NAVIGATION
     htmx's own history is disabled (above); we own push/pop so the Android
     back gesture moves reader→list→view predictably and keeps list scroll.
     ============================================================ */
  function isMobile() { return body.classList.contains('is-mobile'); }
  function enterReading() { if (isMobile()) body.classList.add('mobile-reading'); }
  function exitReading() { body.classList.remove('mobile-reading'); }

  window.ReaderApp = {
    closeOverlay: closeOverlay,
    isMobile: isMobile, enterReading: enterReading, exitReading: exitReading,
    syncScrollLock: syncScrollLock, markOverlayA11y: markOverlayA11y,
  };

  var navRestoring = false;
  function curUrl() { return location.pathname + location.search; }
  function restoreInto(target, url) {
    navRestoring = true;
    // Send an explicit HX-Target so the server returns the correct styled pane
    // fragment (the server now refuses to emit a bare fragment without it).
    var ctx = { target: target, swap: 'innerHTML', headers: { 'HX-Target': target.replace(/^#/, '') } };
    var p = window.htmx ? window.htmx.ajax('GET', url, ctx) : null;
    return Promise.resolve(p).then(function () { navRestoring = false; }, function () { navRestoring = false; });
  }

  // Own the URL stack (replaces the now-disabled hx-push-url).
  document.body.addEventListener('htmx:afterSettle', function (e) {
    if (navRestoring) return;
    var d = e.detail || {}, tgt = e.target; if (!tgt) return;
    var id = tgt.id;
    if (id !== 'list-col' && id !== 'reader-col') return;
    var rc = d.requestConfig || {};
    if (rc.verb && rc.verb.toLowerCase() !== 'get') return;               // only navigational GETs
    var url = null;
    if (d.xhr && d.xhr.responseURL) { try { var u = new URL(d.xhr.responseURL); url = u.pathname + u.search; } catch (_) {} }
    if (!url) url = rc.path;
    if (!url) return;
    var elt = rc.elt;
    // A programmatic backfill (hx-trigger="load", e.g. the list pane filled in
    // on a deep-linked entry page) is NOT a user navigation and must never drive
    // the URL stack — otherwise refreshing /entries/{id} gets its URL silently
    // overwritten with the backfilled feed URL.
    var fromLoad = (rc.triggeringEvent && rc.triggeringEvent.type === 'load') ||
                   (elt && elt.id === 'entry-list-backfill');
    if (fromLoad) return;
    var fromNav = elt && elt.closest && elt.closest('.rb-nav');           // prev/next or swipe
    var fromSearch = elt && elt.id === 'list-search-input';
    if (id === 'reader-col' && isMobile() && fromNav) history.replaceState({ p: id }, '', url);
    else if (id === 'list-col' && fromSearch) history.replaceState({ p: id }, '', url);
    else if (url !== curUrl()) history.pushState({ p: id }, '', url);
  });

  window.addEventListener('popstate', function () {
    // An open mobile drawer swallows the first back press.
    if (body.classList.contains('drawer-open')) { body.classList.remove('drawer-open'); syncScrollLock(); return; }
    var m = location.pathname.match(/^\/entries\/(\d+)$/);
    if (m) {
      if (isMobile()) enterReading();
      var cur = document.querySelector('#reader-col [data-entry-id]');
      if (cur && cur.getAttribute('data-entry-id') === m[1]) return;       // already shown
      restoreInto('#reader-col', curUrl());
    } else {
      flushDwell();                                                        // leaving the reader for the list ends dwell
      if (isMobile()) {
        exitReading();
        var lc = document.getElementById('list-col');
        if (lc && lc.querySelector('.erow')) return;                       // list still here → instant, scroll kept
      }
      restoreInto('#list-col', curUrl());
    }
  });

  /* ============================================================
     GLOBAL KEYBOARD
     ============================================================ */
  document.addEventListener('keydown', function (e) {
    var tag = e.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    if (overlayOpen()) {
      if (e.key === 'Escape') { e.preventDefault(); closeOverlay(); }
      return;
    }

    var rows = listRows();
    var row = rows[selectedIdx];
    switch (e.key) {
      case 'j': e.preventDefault(); selectRow(selectedIdx + 1); break;
      case 'k': e.preventDefault(); selectRow(selectedIdx - 1); break;
      case 'o':
      case 'Enter': if (row) { e.preventDefault(); openRow(row); } break;
      case 'v': if (row && row.dataset.url) { window.open(row.dataset.url, '_blank'); logOpenOriginal(row.dataset.entryId, row.dataset.feedId); } break;
      case 'm': if (row) { e.preventDefault(); toggleRead(row); } break;
      case 's': if (row) { e.preventDefault(); toggleStar(row); } break;
      case 'r': e.preventDefault(); markAllVisible(); break;
      case '/': e.preventDefault(); { var si = document.getElementById('list-search-input'); if (si) si.focus(); } break;
      case 'Escape': rows.forEach(function (r) { r.classList.remove('sel'); }); selectedIdx = -1; break;
    }
  });

  /* ============================================================
     STATUS BAR
     ============================================================ */
  function updateStatusPos(pos, total) {
    var el = document.getElementById('sb-pos');
    if (el) el.textContent = total ? (pos + ' / ' + total) : '';
  }

  /* ============================================================
     SWIPE (mobile) — list rows
     ============================================================ */
  var SWIPE = 70;
  function bindSwipe() {
    if (!body.classList.contains('is-mobile')) return;
    listRows().forEach(function (row) {
      if (row.__swipe) return; row.__swipe = true;
      var sx = 0, sy = 0;
      row.addEventListener('touchstart', function (e) { sx = e.touches[0].clientX; sy = e.touches[0].clientY; }, { passive: true });
      row.addEventListener('touchend', function (e) {
        var dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
        if (Math.abs(dx) < SWIPE || Math.abs(dy) > Math.abs(dx)) return;
        if (dx < 0) { postAction(row.dataset.entryId, 'mark-read'); markRowRead(row, true); }
        else { toggleStar(row); }
      }, { passive: true });
    });
  }

  /* ============================================================
     UNREAD POLLING (title badge)
     ============================================================ */
  function pollUnread() {
    if (document.hidden) return;   // don't poll a backgrounded tab / installed PWA
    fetch('/api/new-count').then(function (r) { return r.json(); }).then(function (d) {
      var c = d.count || 0;
      var base = document.title.replace(/^\(\d+\)\s*/, '');
      document.title = c > 0 ? '(' + c + ') ' + base : base;
    }).catch(function () {});
  }

  /* ============================================================
     INIT + HTMX re-binding
     ============================================================ */
  function initList() {
    selectedIdx = -1;
    bindSwipe();
    updateStatusPos(0, listRows().length);
  }

  document.addEventListener('DOMContentLoaded', function () {
    initList();
    startDwellFromReader();   // deep-linked /entries/{id} starts the dwell clock
    pollUnread();
    setInterval(pollUnread, 60000);
    // Refresh the badge when foregrounded; flush dwell when backgrounded.
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) flushDwell(); else pollUnread();
    });
    window.addEventListener('pagehide', flushDwell);
    if ('serviceWorker' in navigator) {
      // Register at root scope so the worker controls the whole app (it's served
      // from /static/ but the server sends Service-Worker-Allowed:/). Drop any
      // stale /static/-scoped registration left over from before this change.
      navigator.serviceWorker.getRegistrations().then(function (regs) {
        regs.forEach(function (r) { if (/\/static\/$/.test(r.scope)) r.unregister(); });
      }).catch(function () {});
      navigator.serviceWorker.register('/static/sw.js', { scope: '/' }).catch(function () {});
    }
  });

  // Re-bind whenever the list pane is swapped in by HTMX.
  document.body.addEventListener('htmx:afterSwap', function (e) {
    var t = e.target;
    if (t && (t.id === 'list-col' || (t.closest && t.closest('#list-col')))) { flushDwell(); initList(); }
    if (t && t.id === 'reader-col') { startDwellFromReader(); if (isMobile()) enterReading(); }
    if (t && t.id === 'overlay-slot') markOverlayA11y();
  });
})();
