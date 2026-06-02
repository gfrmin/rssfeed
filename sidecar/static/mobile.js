/* ============================================================
   MOBILE — re-presents the three-pane DOM as a single-pane app:
   off-canvas drawer sidebar, list⇄reader swap, swipe between
   articles. Driven by state classes on <body> (.is-mobile is
   toggled in app.js at <880px).
   ============================================================ */
(function () {
  'use strict';
  var body = document.body;
  function isMobile() { return body.classList.contains('is-mobile'); }

  function openDrawer() { body.classList.add('drawer-open'); }
  function closeDrawer() { body.classList.remove('drawer-open'); }
  function toggleDrawer() { body.classList.toggle('drawer-open'); }
  function enterReading() { if (isMobile()) body.classList.add('mobile-reading'); }
  function exitReading() { body.classList.remove('mobile-reading'); }

  function syncTitle() {
    var el = document.getElementById('mtitle');
    if (!el) return;
    var t = document.querySelector('#list-col .list-title');
    if (t) el.textContent = t.textContent.trim();
  }

  document.addEventListener('click', function (e) {
    if (e.target.closest('#mtop-menu')) { e.preventDefault(); toggleDrawer(); return; }
    if (e.target.closest('#drawer-scrim')) { closeDrawer(); return; }
    if (e.target.closest('#mback')) { e.preventDefault(); exitReading(); return; }
    if (!isMobile()) return;
    // Opening an article → switch to the reader pane straight away.
    if (e.target.closest('#list-col .erow')) { enterReading(); }
    // Picking any sidebar destination closes the drawer.
    if (e.target.closest('#sidebar a, #sidebar .nav-row, #sidebar .search-link, #sidebar .time-btn')) { closeDrawer(); }
  });

  document.body.addEventListener('htmx:afterSwap', function (e) {
    var t = e.target;
    if (!t) return;
    if (t.id === 'reader-col' && isMobile()) enterReading();
    if (t.id === 'list-col') { exitReading(); syncTitle(); }
  });

  /* Swipe left/right in the reader → next/previous article (drives the
     existing .rb-nav buttons so URL + state stay correct). */
  var SWIPE = 60, sx = null, sy = null;
  document.addEventListener('touchstart', function (e) {
    if (!isMobile() || !body.classList.contains('mobile-reading')) return;
    var rc = document.getElementById('reader-col');
    if (!rc || !rc.contains(e.target)) { sx = null; return; }
    sx = e.touches[0].clientX; sy = e.touches[0].clientY;
  }, { passive: true });
  document.addEventListener('touchend', function (e) {
    if (sx === null) return;
    var dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
    sx = null;
    if (Math.abs(dx) < SWIPE || Math.abs(dy) > Math.abs(dx)) return;
    var navs = document.querySelectorAll('#reader-col .rb-nav');
    if (dx > 0 && navs[0] && !navs[0].disabled) navs[0].click();        // → newer / previous
    else if (dx < 0 && navs[1] && !navs[1].disabled) navs[1].click();   // → older / next
  }, { passive: true });

  window.addEventListener('resize', function () { if (!isMobile()) { closeDrawer(); exitReading(); } });

  document.addEventListener('DOMContentLoaded', function () {
    syncTitle();
    // Deep-linked article on mobile: start in the reader, not the list.
    if (isMobile() && document.querySelector('#reader-col .art-title')) enterReading();
  });
})();
