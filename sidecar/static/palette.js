/* ============================================================
   COMMAND PALETTE (⌘K) — fuzzy launcher over actions, views,
   saved searches, feeds and recent articles. Fed by /api/palette.
   ============================================================ */
(function () {
  'use strict';
  var GROUPS = ['Actions', 'Views', 'Searches', 'Feeds', 'Articles'];
  var GLYPH = { Actions: '⚡', Views: '▦', Searches: '⌕', Feeds: '•', Articles: '›' };
  var cache = null, cacheAt = 0;

  function slot() { return document.getElementById('overlay-slot'); }
  function esc(s) { return (s || '').replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

  function fuzzy(text, q) {
    if (!q) return 0;
    text = text.toLowerCase(); q = q.toLowerCase();
    var ti = 0, qi = 0, score = 0, streak = 0;
    while (ti < text.length && qi < q.length) {
      if (text[ti] === q[qi]) { qi++; streak++; score += streak + ((ti === 0 || text[ti - 1] === ' ') ? 3 : 0); }
      else streak = 0;
      ti++;
    }
    return qi === q.length ? score : -1;
  }

  function fetchItems() {
    if (cache && Date.now() - cacheAt < 60000) return Promise.resolve(cache);
    return fetch('/api/palette').then(function (r) { return r.json(); })
      .then(function (d) { cache = d.items || []; cacheAt = Date.now(); return cache; })
      .catch(function () { return cache || []; });
  }

  function open() {
    var s = slot();
    if (!s || s.querySelector('.cmdk-scrim')) return;
    var prevFocus = document.activeElement;
    s.innerHTML =
      '<div class="cmdk-scrim">' +
        '<div class="cmdk">' +
          '<div class="cmdk-input-wrap"><span class="cmdk-ic">⌕</span>' +
            '<input class="cmdk-input" id="cmdk-input" placeholder="Type a command, feed, or article…" autocomplete="off" spellcheck="false">' +
            '<span class="cmdk-esc">esc</span></div>' +
          '<div class="cmdk-list" id="cmdk-list"></div>' +
          '<div class="cmdk-foot"><span><span class="kbd-mini">↑</span><span class="kbd-mini">↓</span> navigate</span>' +
            '<span><span class="kbd-mini">↵</span> run</span><span><span class="kbd-mini">esc</span> close</span></div>' +
        '</div>' +
      '</div>';

    var scrim = s.querySelector('.cmdk-scrim');
    if (scrim) { scrim.setAttribute('role', 'dialog'); scrim.setAttribute('aria-modal', 'true'); }
    document.body.classList.add('ov-lock');

    var input = document.getElementById('cmdk-input');
    var list = document.getElementById('cmdk-list');
    var items = [], results = [], sel = 0;

    function compute(q) {
      if (!q) {
        var def = [];
        ['Actions', 'Views', 'Searches'].forEach(function (g) { def = def.concat(items.filter(function (i) { return i.group === g; })); });
        def = def.concat(items.filter(function (i) { return i.group === 'Feeds'; }).slice(0, 6));
        def = def.concat(items.filter(function (i) { return i.group === 'Articles'; }).slice(0, 4));
        return def;
      }
      return items.map(function (i) { return { i: i, s: fuzzy(i.label + ' ' + (i.sub || ''), q) }; })
        .filter(function (x) { return x.s >= 0; }).sort(function (a, b) { return b.s - a.s; })
        .slice(0, 12).map(function (x) { return x.i; });
    }
    function ordered() { var out = []; GROUPS.forEach(function (g) { out = out.concat(results.filter(function (r) { return r.group === g; })); }); return out; }

    function render() {
      results = compute(input.value.trim());
      if (!results.length) { list.innerHTML = '<div class="cmdk-empty">No matches for “' + esc(input.value.trim()) + '”</div>'; return; }
      var html = '', idx = -1;
      GROUPS.forEach(function (g) {
        var gi = results.filter(function (r) { return r.group === g; });
        if (!gi.length) return;
        html += '<div class="cmdk-group"><div class="cmdk-group-label">' + g + '</div>';
        gi.forEach(function (r) {
          idx++;
          html += '<button type="button" class="cmdk-item' + (idx === sel ? ' on' : '') + '" data-i="' + idx + '">' +
            '<span class="cmdk-item-ic">' + (GLYPH[g] || '›') + '</span>' +
            '<span class="cmdk-item-label">' + esc(r.label) + '</span>' +
            (r.sub ? '<span class="cmdk-item-sub">' + esc(r.sub) + '</span>' : '') +
            (r.kbd ? '<span class="cmdk-item-kbd">' + esc(r.kbd) + '</span>' : '') +
            '</button>';
        });
        html += '</div>';
      });
      list.innerHTML = html;
    }

    function updateSel() {
      Array.prototype.forEach.call(list.querySelectorAll('.cmdk-item'), function (el) {
        var on = parseInt(el.dataset.i, 10) === sel;
        el.classList.toggle('on', on);
        if (on) el.scrollIntoView({ block: 'nearest' });
      });
    }
    function move(d) { var ord = ordered(); if (!ord.length) return; sel = Math.max(0, Math.min(ord.length - 1, sel + d)); updateSel(); }
    function close() {
      var sl = slot(); if (sl) sl.innerHTML = '';
      document.body.classList.remove('ov-lock');
      if (prevFocus && prevFocus.focus) { try { prevFocus.focus(); } catch (_) {} }
    }

    function run(item) {
      close();
      if (!item) return;
      if (item.act === 'triage') { if (window.ReaderApp) window.ReaderApp.openTriage(); return; }
      if (item.act === 'help') { if (window.htmx) window.htmx.ajax('GET', '/help', { target: '#overlay-slot', swap: 'innerHTML' }); return; }
      if (item.act === 'theme') { var b = document.querySelector('[data-act=theme]'); if (b) b.click(); return; }
      if (item.url) {
        var target = '#' + (item.target || 'list-col');
        if (window.htmx) {
          window.htmx.ajax('GET', item.url, { target: target, swap: 'innerHTML' });
          if ((item.target === 'list-col' || item.target === 'reader-col') && window.history) history.pushState(null, '', item.url);
        } else { window.location.href = item.url; }
      }
    }

    input.addEventListener('input', function () { sel = 0; render(); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || (e.key === 'n' && e.ctrlKey)) { e.preventDefault(); e.stopPropagation(); move(1); }
      else if (e.key === 'ArrowUp' || (e.key === 'p' && e.ctrlKey)) { e.preventDefault(); e.stopPropagation(); move(-1); }
      else if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); run(ordered()[sel]); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
    });
    list.addEventListener('click', function (e) { var b = e.target.closest('.cmdk-item'); if (b) run(ordered()[parseInt(b.dataset.i, 10)]); });
    list.addEventListener('mousemove', function (e) { var b = e.target.closest('.cmdk-item'); if (b) { var i = parseInt(b.dataset.i, 10); if (i !== sel) { sel = i; updateSel(); } } });

    fetchItems().then(function (its) { items = its; render(); input.focus(); });
  }

  document.addEventListener('palette:open', open);
})();
