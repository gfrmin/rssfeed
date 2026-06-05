/* ============================================================
   MOBILE — single-pane presentation of the three-pane DOM:
   off-canvas drawer sidebar, list⇄reader, finger-follow swipe
   between articles. Back/forward is owned by app.js (history shim);
   here we just drive drawer + swipe + the top-bar title.
   ============================================================ */
(function () {
  'use strict';
  var body = document.body;
  var RMQ = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function reduce() { return !!(RMQ && RMQ.matches); }   // checked live, not sampled once at load
  function isMobile() { return body.classList.contains('is-mobile'); }
  function enterReading() { if (isMobile()) body.classList.add('mobile-reading'); }
  function exitReading() { body.classList.remove('mobile-reading'); }

  /* ---- Drawer (sidebar) — a history entry so the back gesture closes it ---- */
  function openDrawer() {
    if (body.classList.contains('drawer-open')) return;
    body.classList.add('drawer-open');
    if (window.ReaderApp) window.ReaderApp.syncScrollLock();
    history.pushState({ drawer: 1 }, '');
  }
  function closeDrawer() { if (body.classList.contains('drawer-open')) history.back(); }  // app.js popstate drops the class
  function toggleDrawer() { body.classList.contains('drawer-open') ? closeDrawer() : openDrawer(); }

  document.addEventListener('click', function (e) {
    if (e.target.closest('#mtop-menu')) { e.preventDefault(); toggleDrawer(); return; }
    if (e.target.closest('#drawer-scrim')) { closeDrawer(); return; }
    if (e.target.closest('#mback')) { e.preventDefault(); history.back(); return; }
    if (!isMobile()) return;
    if (e.target.closest('#list-col .erow')) enterReading();
    if (e.target.closest('#sidebar a, #sidebar .nav-row, #sidebar .search-link, #sidebar .time-btn')) closeDrawer();
  });

  /* ---- Top-bar title ---- */
  function mtitle() { return document.getElementById('mtitle'); }
  function syncTitleFromList() { var el = mtitle(), t = document.querySelector('#list-col .list-title'); if (el && t) el.textContent = t.textContent.trim(); }
  function syncTitleFromReader() { var el = mtitle(), f = document.querySelector('#reader-col .art-feed'); if (el && f) el.textContent = f.textContent.trim(); }

  /* ---- Swipe between articles: translate #reader-col .reader directly ---- */
  var EDGE = 24, REVEAL = 28, THRESH = 70;
  var sx = 0, sy = 0, active = false, horiz = false, dx = 0, st = 0;

  function readerSection() { return document.querySelector('#reader-col .reader'); }
  function ensureHints() {
    var rc = document.getElementById('reader-col');
    if (!rc || rc.querySelector('.mswipe-hint')) return;
    var lh = document.createElement('div'); lh.className = 'mswipe-hint left'; lh.textContent = '‹ Newer'; lh.style.opacity = 0;
    var rh = document.createElement('div'); rh.className = 'mswipe-hint right'; rh.textContent = 'Older ›'; rh.style.opacity = 0;
    rc.appendChild(lh); rc.appendChild(rh);
  }
  function setHints(d) {
    var lh = document.querySelector('#reader-col .mswipe-hint.left'), rh = document.querySelector('#reader-col .mswipe-hint.right');
    if (lh) lh.style.opacity = d > REVEAL ? Math.min(0.95, d / THRESH) : 0;
    if (rh) rh.style.opacity = d < -REVEAL ? Math.min(0.95, -d / THRESH) : 0;
  }
  function setX(d, anim) { var r = readerSection(); if (r) { r.style.transition = anim || 'none'; r.style.transform = d ? 'translateX(' + d + 'px)' : ''; } }
  function clearSwipe() { setX(0, reduce() ? 'none' : 'transform .18s ease-out'); setHints(0); }

  document.addEventListener('touchstart', function (e) {
    active = false;
    if (!isMobile() || !body.classList.contains('mobile-reading')) return;
    var rc = document.getElementById('reader-col');
    if (!rc || !rc.contains(e.target)) return;
    var x = e.touches[0].clientX;
    if (x < EDGE || x > window.innerWidth - EDGE) return;   // leave edges for the system back gesture
    ensureHints();
    sx = x; sy = e.touches[0].clientY; active = true; horiz = false; dx = 0; st = Date.now();
  }, { passive: true });

  document.addEventListener('touchmove', function (e) {
    if (!active) return;
    var ddx = e.touches[0].clientX - sx, ddy = e.touches[0].clientY - sy;
    if (!horiz) {
      if (Math.abs(ddx) > 10 && Math.abs(ddx) > Math.abs(ddy) * 1.3) horiz = true;
      else if (Math.abs(ddy) > 10) { active = false; return; }   // vertical → let it scroll
    }
    if (horiz) { dx = ddx; if (!reduce()) setX(dx); setHints(dx); }
  }, { passive: true });

  document.addEventListener('touchend', function () {
    if (!active) return; active = false;
    if (!horiz) return;
    var navs = document.querySelectorAll('#reader-col .rb-nav');   // [0]=prev/newer, [1]=next/older
    var dt = Date.now() - st, vel = dt > 0 ? Math.abs(dx) / dt : 0;       // px/ms
    var go = Math.abs(dx) > THRESH || (Math.abs(dx) > 34 && vel > 0.5);   // committed drag OR quick flick
    if (go && dx > 0 && navs[0] && !navs[0].disabled) { if (!reduce()) setX(window.innerWidth, 'transform .15s ease-in'); setHints(0); navs[0].click(); }
    else if (go && dx < 0 && navs[1] && !navs[1].disabled) { if (!reduce()) setX(-window.innerWidth, 'transform .15s ease-in'); setHints(0); navs[1].click(); }
    else clearSwipe();
  }, { passive: true });

  /* ---- React to pane swaps ---- */
  document.body.addEventListener('htmx:afterSwap', function (e) {
    var t = e.target; if (!t) return;
    if (t.id === 'list-col') { if (isMobile()) exitReading(); syncTitleFromList(); }
    if (t.id === 'reader-col' && isMobile()) { ensureHints(); syncTitleFromReader(); }
  });

  window.addEventListener('resize', function () {
    if (!isMobile()) { body.classList.remove('drawer-open'); exitReading(); setX(0); }
  });

  document.addEventListener('DOMContentLoaded', function () {
    syncTitleFromList();
    if (isMobile() && document.querySelector('#reader-col .art-title')) { enterReading(); syncTitleFromReader(); }
  });
})();
