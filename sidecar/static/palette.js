/* Command palette — ⌘K / Ctrl+K.
 *
 * Loaded on every page, including the management pages that have no reader
 * shell. It builds nothing on the server and fetches nothing: the feed list
 * comes out of whatever the current page already rendered (the sidebar on
 * reader pages, the table on /feeds), marked with data-palette-feed. That
 * keeps 500-odd feeds out of every page payload and means the palette can
 * never disagree with what is on screen.
 */
(function () {
  'use strict';

  // Static destinations. Every href here is checked against the app's own
  // route table by tests/test_palette.py — a renamed route fails there rather
  // than leaving a dead entry in the menu.
  var VIEWS = [
    { label: 'Unread',          href: '/entries?view=unread',  hint: 'articles you have not read' },
    { label: 'All articles',    href: '/entries?view=all',     hint: 'read and unread together' },
    { label: 'Read',            href: '/entries?view=read',    hint: 'what you have got through' },
    { label: 'Starred',         href: '/entries?view=starred', hint: 'kept for later' },
    { label: 'Changed',         href: '/entries?view=changed', hint: 'articles edited after publication' },
    { label: 'Needs attention', href: '/triage',               hint: 'feeds grouped by what is wrong' },
    { label: 'Manage feeds',    href: '/',                     hint: 'the full list, filters and bulk actions' },
    { label: 'Cookies',         href: '/cookies',              hint: 'stored logins for paywalled sites' }
  ];

  var MAX_ROWS = 60;      // enough to scroll, few enough to render instantly
  var el = null, input = null, list = null, items = [], active = 0;

  /* ---------------- what there is to jump to ---------------- */

  function fromDom(selector, kind) {
    var seen = {}, out = [];
    Array.prototype.forEach.call(document.querySelectorAll(selector), function (node) {
      var key = node.getAttribute('data-palette-feed') || node.getAttribute('href');
      if (!key || seen[key]) return;
      var label = (node.getAttribute('data-palette-label') || node.textContent || '').trim();
      if (!label) return;
      seen[key] = 1;
      out.push({ label: label, href: node.getAttribute('href'), kind: kind,
                 feedId: node.getAttribute('data-palette-feed'),
                 hint: node.getAttribute('data-palette-hint') || '' });
    });
    return out;
  }

  function everything() {
    return VIEWS.map(function (v) { return { label: v.label, href: v.href, hint: v.hint, kind: 'view' }; })
      .concat(fromDom('[data-palette-cause]', 'cause'))
      .concat(fromDom('[data-palette-feed]', 'feed'));
  }

  /* ---------------- matching ---------------- */

  // Rank by where the match lands: a name that starts with what you typed is
  // almost always the one you meant, and a word boundary beats mid-word.
  function score(label, q) {
    var l = label.toLowerCase(), i = l.indexOf(q);
    if (i < 0) return -1;
    if (i === 0) return 0;
    return /[\s\W]/.test(l.charAt(i - 1)) ? 1 : 2;
  }

  function filter(q) {
    q = q.trim().toLowerCase();
    var all = everything();
    if (!q) return all.slice(0, MAX_ROWS);
    var hits = [];
    all.forEach(function (it, idx) {
      var s = score(it.label, q);
      if (s >= 0) hits.push({ item: it, s: s, idx: idx });
    });
    hits.sort(function (a, b) { return a.s - b.s || a.idx - b.idx; });
    return hits.slice(0, MAX_ROWS).map(function (h) { return h.item; });
  }

  /* ---------------- rendering ---------------- */

  var KIND_LABEL = { view: 'view', cause: 'triage', feed: 'feed' };

  function draw() {
    list.innerHTML = '';
    if (!items.length) {
      list.innerHTML = '<div class="pal-none">Nothing matches.</div>';
      return;
    }
    items.forEach(function (it, i) {
      var row = document.createElement('div');
      row.className = 'pal-row' + (i === active ? ' on' : '');
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', i === active ? 'true' : 'false');
      row.dataset.i = i;
      var kind = document.createElement('span');
      kind.className = 'pal-kind pal-kind-' + it.kind;
      kind.textContent = KIND_LABEL[it.kind] || it.kind;
      var label = document.createElement('span');
      label.className = 'pal-label';
      label.textContent = it.label;
      row.appendChild(kind);
      row.appendChild(label);
      if (it.hint) {
        var hint = document.createElement('span');
        hint.className = 'pal-hint';
        hint.textContent = it.hint;
        row.appendChild(hint);
      }
      list.appendChild(row);
    });
    var on = list.querySelector('.pal-row.on');
    if (on && on.scrollIntoView) on.scrollIntoView({ block: 'nearest' });
  }

  function build() {
    el = document.createElement('div');
    el.className = 'pal-scrim';
    el.id = 'palette';
    el.innerHTML =
      '<div class="pal" role="dialog" aria-modal="true" aria-label="Command palette">' +
        '<input class="pal-input" type="text" autocomplete="off" spellcheck="false" ' +
               'placeholder="Jump to a feed, a view, or a cause…" aria-controls="pal-list">' +
        '<div class="pal-list" id="pal-list" role="listbox"></div>' +
        '<div class="pal-foot">' +
          '<span><kbd class="kbd">↑↓</kbd> move</span>' +
          '<span><kbd class="kbd">↵</kbd> open</span>' +
          '<span><kbd class="kbd">⇧↵</kbd> feed settings</span>' +
          '<span><kbd class="kbd">esc</kbd> close</span>' +
        '</div>' +
      '</div>';
    document.body.appendChild(el);
    input = el.querySelector('.pal-input');
    list = el.querySelector('.pal-list');

    input.addEventListener('input', function () {
      items = filter(input.value); active = 0; draw();
    });
    el.addEventListener('click', function (e) {
      if (e.target === el) { close(); return; }
      var row = e.target.closest('.pal-row');
      if (row) { active = Number(row.dataset.i); go(e.shiftKey); }
    });
    input.addEventListener('keydown', onKey);
  }

  /* ---------------- opening, closing, going ---------------- */

  var lastFocused = null;

  function open() {
    if (!el) build();
    lastFocused = document.activeElement;
    el.classList.add('on');
    document.body.classList.add('ov-lock');
    input.value = '';
    items = filter(''); active = 0; draw();
    input.focus();
  }

  function close() {
    if (!el) return;
    el.classList.remove('on');
    document.body.classList.remove('ov-lock');
    if (lastFocused && lastFocused.focus) { try { lastFocused.focus(); } catch (_) {} }
    lastFocused = null;
  }

  function isOpen() { return !!el && el.classList.contains('on'); }

  // Shift+Enter on a feed goes to its settings rather than its articles. That
  // is the route the reader never had: from a feed you can see is broken to
  // the page that can fix it, without going through an article it has not got.
  function go(settings) {
    var it = items[active];
    if (!it) return;
    var href = (settings && it.feedId) ? '/feeds/' + it.feedId : it.href;
    if (!href) return;
    close();
    window.location.href = href;
  }

  function move(delta) {
    if (!items.length) return;
    active = (active + delta + items.length) % items.length;
    draw();
  }

  function onKey(e) {
    switch (e.key) {
      case 'Escape':    e.preventDefault(); close(); break;
      case 'ArrowDown': e.preventDefault(); move(1); break;
      case 'ArrowUp':   e.preventDefault(); move(-1); break;
      case 'Enter':     e.preventDefault(); go(e.shiftKey); break;
    }
  }

  // Touch has no ⌘K. Every page chrome carries a visible trigger, and it is
  // the only way in on a phone.
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-palette-open]')) { e.preventDefault(); open(); }
  });

  // Bound in the capture phase so it beats the reader's own single-key
  // bindings and works from inside a search box, where ⌘K is most wanted.
  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      isOpen() ? close() : open();
    }
  }, true);
})();
