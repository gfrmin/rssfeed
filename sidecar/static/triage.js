/* ============================================================
   TRIAGE QUEUE — drives the server-rendered .tri card deck.
   Owns its own keyboard (capture phase) while mounted.
   ============================================================ */
(function () {
  'use strict';
  function q(sel, root) { return (root || document).querySelector(sel); }
  function qa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function initTriage(scope) {
    var tri = q('.tri', scope);
    if (!tri || tri.__init) return;
    tri.__init = true;

    var cards = qa('.tri-card', tri);
    var total = cards.length;
    var idx = 0;
    var acts = {};                       // entryId -> 'read' | 'star' | 'skip'

    var deck = q('#tri-deck', tri);
    var done = q('#tri-done', tri);
    var foot = q('#tri-foot', tri);
    var posEl = q('#tri-pos', tri);
    var fill = q('#tri-progfill', tri);
    var back = q('#tri-back', tri);

    function post(id, action) { fetch('/entries/' + id + '/' + action, { method: 'POST' }).catch(function () {}); }
    function syncListRow(id) {
      var r = document.querySelector('.erow[data-entry-id="' + id + '"]');
      if (r) { r.classList.add('read'); r.classList.remove('unread'); var d = r.querySelector('.erow-unread-dot'); if (d) d.remove(); }
    }
    function close() { var slot = document.getElementById('overlay-slot'); if (slot) slot.innerHTML = ''; }
    function curCard() { return cards[idx]; }

    function render() {
      cards.forEach(function (c, i) {
        c.hidden = (i !== idx);
        c.classList.remove('out-left', 'out-right');
        if (i === idx) c.classList.add('in'); else c.classList.remove('in');
      });
      if (back) back.disabled = idx <= 0;
      if (posEl) posEl.textContent = idx >= total ? total : (idx + 1);
      if (fill) fill.style.width = (total ? (idx / total) * 100 : 100) + '%';
      if (idx >= total) finish();
    }

    function finish() {
      if (deck) deck.style.display = 'none';
      if (foot) foot.hidden = true;
      if (!done) return;
      done.hidden = false;
      var read = 0, star = 0, skip = 0;
      Object.keys(acts).forEach(function (k) { var a = acts[k]; if (a === 'read') read++; else if (a === 'star') star++; else skip++; });
      var t = q('#tri-done-title', tri), s = q('#tri-done-sub', tri), tally = q('#tri-tally', tri);
      if (total === 0) {
        if (t) t.textContent = 'Nothing to triage';
        if (s) s.textContent = "You're already at inbox zero on unread.";
        if (tally) tally.hidden = true;
      } else {
        if (t) t.textContent = 'Queue cleared';
        if (s) s.textContent = 'You triaged ' + total + ' article' + (total > 1 ? 's' : '') + '.';
        if (tally) { tally.hidden = false; q('#tally-read', tri).textContent = read; q('#tally-star', tri).textContent = star; q('#tally-skip', tri).textContent = skip; }
      }
    }

    function advance(dir) {
      var card = curCard();
      if (card) card.classList.add(dir < 0 ? 'out-left' : 'out-right');
      setTimeout(function () { idx = Math.min(total, idx + 1); render(); }, 160);
    }
    function doRead() { var c = curCard(); if (!c) return; var id = c.dataset.entryId; post(id, 'mark-read'); acts[id] = 'read'; syncListRow(id); advance(1); }
    function doStar() { var c = curCard(); if (!c) return; var id = c.dataset.entryId; post(id, 'toggle-star'); acts[id] = 'star'; advance(1); }
    function doSkip() { var c = curCard(); if (!c) return; var id = c.dataset.entryId; if (!acts[id]) acts[id] = 'skip'; advance(1); }
    function doBack() { if (idx <= 0) return; idx = Math.max(0, idx - 1); render(); }
    function doOpen() {
      var c = curCard(); if (!c) return;
      var href = c.dataset.href;
      close();
      if (window.htmx) { window.htmx.ajax('GET', href, { target: '#reader-col', swap: 'innerHTML' }); if (window.history) history.pushState(null, '', href); }
      else { window.location.href = href; }
    }

    var b;
    if ((b = q('#tri-read', tri))) b.onclick = doRead;
    if ((b = q('#tri-star', tri))) b.onclick = doStar;
    if ((b = q('#tri-skip', tri))) b.onclick = doSkip;
    if ((b = q('#tri-back', tri))) b.onclick = doBack;
    if ((b = q('#tri-open', tri))) b.onclick = doOpen;

    function onKey(e) {
      if (!document.querySelector('.tri-scrim')) return;
      var tag = e.target.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      var k = e.key;
      var handled = true;
      if (k === 'Escape') close();
      else if (k === 'o' || k === 'Enter') doOpen();
      else if (k === 's') doStar();
      else if (k === 'e' || k === 'm') doRead();
      else if (k === ' ' || k === 'ArrowRight' || k === 'j') doSkip();
      else if (k === 'ArrowLeft' || k === 'k') doBack();
      else handled = false;
      if (handled) { e.preventDefault(); e.stopPropagation(); }
    }
    window.addEventListener('keydown', onKey, true);

    // Tear down the key handler once the overlay is cleared.
    var slot = document.getElementById('overlay-slot');
    if (slot) {
      var mo = new MutationObserver(function () {
        if (!document.body.contains(tri)) { window.removeEventListener('keydown', onKey, true); mo.disconnect(); }
      });
      mo.observe(slot, { childList: true });
    }

    render();
  }

  document.body.addEventListener('htmx:afterSwap', function (e) {
    if (e.target && e.target.id === 'overlay-slot') initTriage(e.target);
  });
  document.addEventListener('DOMContentLoaded', function () { if (q('.tri-scrim')) initTriage(document); });
})();
