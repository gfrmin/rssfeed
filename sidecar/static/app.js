/* ============================================================
   RSS Sidecar — client runtime for the three-pane shell.
   Vanilla JS + HTMX. No framework.
   ============================================================ */
(function () {
  'use strict';

  var html = document.documentElement;
  var body = document.body;

  /* ---------- theme ---------- */
  function applyTheme(t) { html.setAttribute('data-theme', t); localStorage.setItem('theme', t); }
  applyTheme(localStorage.getItem('theme') || 'dark');
  function toggleTheme() { applyTheme(html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'); }

  /* ---------- layout (triptych / stacked) ---------- */
  function applyLayout(l) {
    var app = document.getElementById('app');
    if (app) app.classList.toggle('layout-stacked', l === 'stacked');
    localStorage.setItem('layout', l);
  }
  applyLayout(localStorage.getItem('layout') || 'triptych');
  function toggleLayout() { applyLayout(localStorage.getItem('layout') === 'stacked' ? 'triptych' : 'stacked'); }

  /* ---------- mobile detection ---------- */
  function syncMobile() { body.classList.toggle('is-mobile', window.innerWidth < 880); }
  syncMobile();
  window.addEventListener('resize', syncMobile);

  /* ---------- footer tool buttons + density + tag cloud (delegated) ---------- */
  function applyDensity(d) {
    var sc = document.getElementById('list-scroll');
    if (sc) { sc.classList.remove('dens-compact', 'dens-normal', 'dens-expanded'); sc.classList.add('dens-' + d); }
    document.querySelectorAll('#density-seg .seg-btn').forEach(function (b) {
      b.classList.toggle('on', b.dataset.density === d);
    });
    localStorage.setItem('density', d);
  }

  document.addEventListener('click', function (e) {
    var act = e.target.closest('[data-act]');
    if (act) {
      var a = act.dataset.act;
      if (a === 'theme') toggleTheme();
      else if (a === 'layout') toggleLayout();
      else if (a === 'triage') openTriage();
      else if (a === 'palette') openPalette();
      return;
    }
    var dens = e.target.closest('[data-density]');
    if (dens) { applyDensity(dens.dataset.density); return; }
    var tc = e.target.closest('#tagcloud-toggle');
    if (tc) { var cloud = document.getElementById('tagcloud'); if (cloud) cloud.classList.toggle('hidden'); }
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

    // reader-bar mark read / star (no HTML swap; toggle in place)
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
      }
    }
  });

  function syncListRowRead(id, read) {
    var row = document.querySelector('.erow[data-entry-id="' + id + '"]');
    if (row) markRowRead(row, read);
  }

  /* ============================================================
     OVERLAYS (health/stats/filters/feed-settings/diff/help/triage)
     loaded as fragments into #overlay-slot.
     ============================================================ */
  function overlaySlot() { return document.getElementById('overlay-slot'); }
  function overlayOpen() { var s = overlaySlot(); return s && s.children.length > 0; }
  function closeOverlay() { var s = overlaySlot(); if (s) s.innerHTML = ''; }

  document.addEventListener('click', function (e) {
    var s = overlaySlot();
    if (!s || !s.children.length) return;
    if (e.target.classList.contains('ov-scrim') || e.target.classList.contains('tri-scrim') ||
        e.target.classList.contains('cmdk-scrim') || e.target.closest('[data-close]')) {
      closeOverlay();
    }
  });

  function openTriage() {
    if (window.htmx) window.htmx.ajax('GET', '/triage', { target: '#overlay-slot', swap: 'innerHTML' });
  }
  function openPalette() { document.dispatchEvent(new CustomEvent('palette:open')); }
  window.ReaderApp = { openTriage: openTriage, openPalette: openPalette, closeOverlay: closeOverlay };

  /* ============================================================
     GLOBAL KEYBOARD
     ============================================================ */
  document.addEventListener('keydown', function (e) {
    var tag = e.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) {
      if ((e.key === 'k' || e.key === 'K') && (e.metaKey || e.ctrlKey)) { e.preventDefault(); openPalette(); }
      return;
    }

    // Triage owns its own keyboard while open; the global handler steps back.
    if (document.querySelector('.tri-scrim')) {
      if (e.key === 'Escape') closeOverlay();
      return;
    }
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
      case 'v': if (row && row.dataset.url) window.open(row.dataset.url, '_blank'); break;
      case 'm': if (row) { e.preventDefault(); toggleRead(row); } break;
      case 's': if (row) { e.preventDefault(); toggleStar(row); } break;
      case 'r': e.preventDefault(); markAllVisible(); break;
      case 'q': e.preventDefault(); openTriage(); break;
      case '/': e.preventDefault(); { var si = document.getElementById('list-search-input'); if (si) si.focus(); } break;
      case '?': e.preventDefault(); if (window.htmx) window.htmx.ajax('GET', '/help', { target: '#overlay-slot', swap: 'innerHTML' }); break;
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
    applyDensity(localStorage.getItem('density') || 'normal');
    bindSwipe();
    updateStatusPos(0, listRows().length);
  }

  document.addEventListener('DOMContentLoaded', function () {
    initList();
    pollUnread();
    setInterval(pollUnread, 60000);
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/static/sw.js').catch(function () {});
  });

  // Re-bind whenever the list pane is swapped in by HTMX.
  document.body.addEventListener('htmx:afterSwap', function (e) {
    var t = e.target;
    if (t && (t.id === 'list-col' || (t.closest && t.closest('#list-col')))) initList();
    // On mobile the panes stack, so bring the freshly-loaded reader into view.
    if (t && t.id === 'reader-col' && body.classList.contains('is-mobile')) {
      t.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });
})();
