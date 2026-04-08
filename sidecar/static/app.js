/* === RSS Sidecar — Client-side enhancements === */

(function () {
  'use strict';

  /* --- Mobile nav toggle --- */
  const navToggle = document.getElementById('nav-toggle');
  const navLinks = document.getElementById('nav-links');
  if (navToggle && navLinks) {
    navToggle.addEventListener('click', () => navLinks.classList.toggle('hidden'));
  }

  /* --- Theme toggle --- */
  const theme = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', theme);

  const themeBtn = document.getElementById('theme-toggle');
  if (themeBtn) {
    themeBtn.addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme');
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('theme', next);
    });
  }

  /* --- Help modal --- */
  const helpBtn = document.getElementById('help-toggle');
  const helpModal = document.getElementById('help-modal');
  if (helpBtn && helpModal) {
    helpBtn.addEventListener('click', () => helpModal.classList.toggle('hidden'));
    helpModal.addEventListener('click', (e) => {
      if (e.target === helpModal) helpModal.classList.add('hidden');
    });
  }

  /* --- View mode toggle --- */
  const entryList = document.getElementById('entry-list');
  const savedView = localStorage.getItem('viewMode') || 'normal';

  function applyView(mode) {
    if (!entryList) return;
    entryList.classList.remove('view-compact', 'view-normal', 'view-expanded');
    entryList.classList.add('view-' + mode);
    localStorage.setItem('viewMode', mode);
    document.querySelectorAll('.view-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.view === mode);
    });
  }
  applyView(savedView);

  document.querySelectorAll('.view-btn').forEach(btn => {
    btn.addEventListener('click', () => applyView(btn.dataset.view));
  });

  /* --- Scroll position memory --- */
  const scrollKey = 'scroll_' + location.pathname + location.search;
  const savedScroll = sessionStorage.getItem(scrollKey);
  if (savedScroll) {
    window.scrollTo(0, parseInt(savedScroll, 10));
  }
  let scrollTimer;
  window.addEventListener('scroll', () => {
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {
      sessionStorage.setItem(scrollKey, window.scrollY.toString());
    }, 200);
  });

  /* --- Keyboard shortcuts --- */
  const rows = document.querySelectorAll('.entry-row');
  let selectedIdx = -1;

  function selectRow(idx) {
    if (idx < 0 || idx >= rows.length) return;
    rows.forEach(r => r.classList.remove('selected'));
    selectedIdx = idx;
    rows[idx].classList.add('selected');
    rows[idx].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  document.addEventListener('keydown', (e) => {
    // Ignore when typing in inputs
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) {
      if (e.key === 'Escape') e.target.blur();
      return;
    }

    const row = rows[selectedIdx];
    const entryId = row ? row.dataset.entryId : null;

    switch (e.key) {
      case 'j':
        selectRow(selectedIdx + 1);
        break;
      case 'k':
        selectRow(selectedIdx - 1);
        break;
      case 'o':
      case 'Enter':
        if (row) {
          const link = row.querySelector('a');
          if (link) link.click();
        }
        break;
      case 'v':
        if (row) {
          const url = row.dataset.url;
          if (url) window.open(url, '_blank');
        }
        break;
      case 'm':
        if (entryId) {
          const isUnread = row.classList.contains('unread');
          const action = isUnread ? 'mark-read' : 'mark-unread';
          fetch(`/entries/${entryId}/${action}`, { method: 'POST' })
            .then(() => {
              row.classList.toggle('unread');
              row.classList.toggle('read');
            });
        }
        break;
      case 's':
        if (entryId) {
          fetch(`/entries/${entryId}/toggle-star`, { method: 'POST' });
        }
        break;
      case 'r':
        document.getElementById('mark-all-read-btn')?.click();
        break;
      case '/':
        e.preventDefault();
        document.getElementById('search-input')?.focus();
        break;
      case '?':
        helpModal?.classList.toggle('hidden');
        break;
      case 'Escape':
        helpModal?.classList.add('hidden');
        rows.forEach(r => r.classList.remove('selected'));
        selectedIdx = -1;
        break;
    }
  });

  /* --- Mark all read --- */
  const markAllBtn = document.getElementById('mark-all-read-btn');
  if (markAllBtn) {
    markAllBtn.addEventListener('click', () => {
      const ids = Array.from(rows).map(r => parseInt(r.dataset.entryId)).filter(Boolean);
      if (!ids.length) return;
      fetch('/entries/mark-all-read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entry_ids: ids }),
      }).then(() => {
        rows.forEach(r => {
          r.classList.remove('unread');
          r.classList.add('read');
        });
      });
    });
  }

  /* --- Swipe gestures (mobile) --- */
  let touchStartX = 0;
  let touchStartY = 0;
  const SWIPE_THRESHOLD = 80;

  rows.forEach(row => {
    row.addEventListener('touchstart', (e) => {
      touchStartX = e.touches[0].clientX;
      touchStartY = e.touches[0].clientY;
    }, { passive: true });

    row.addEventListener('touchend', (e) => {
      const dx = e.changedTouches[0].clientX - touchStartX;
      const dy = e.changedTouches[0].clientY - touchStartY;
      if (Math.abs(dx) < SWIPE_THRESHOLD || Math.abs(dy) > Math.abs(dx)) return;

      const entryId = row.dataset.entryId;
      if (!entryId) return;

      if (dx < 0) {
        // Swipe left → mark read
        fetch(`/entries/${entryId}/mark-read`, { method: 'POST' });
        row.classList.remove('unread');
        row.classList.add('read');
        row.style.transition = 'opacity 0.3s';
        row.style.opacity = '0.5';
        setTimeout(() => { row.style.opacity = '1'; }, 500);
      } else {
        // Swipe right → toggle star
        fetch(`/entries/${entryId}/toggle-star`, { method: 'POST' });
      }
    }, { passive: true });
  });

  /* --- Swipe navigation on entry detail page --- */
  const detailArticle = document.querySelector('article[data-prev-entry], article[data-next-entry]');
  if (detailArticle) {
    let detailStartX = 0;
    let detailStartY = 0;

    detailArticle.addEventListener('touchstart', (e) => {
      detailStartX = e.touches[0].clientX;
      detailStartY = e.touches[0].clientY;
    }, { passive: true });

    detailArticle.addEventListener('touchend', (e) => {
      const dx = e.changedTouches[0].clientX - detailStartX;
      const dy = e.changedTouches[0].clientY - detailStartY;
      if (Math.abs(dx) < SWIPE_THRESHOLD || Math.abs(dy) > Math.abs(dx)) return;

      const target = dx > 0
        ? detailArticle.dataset.prevEntry   // swipe right → newer
        : detailArticle.dataset.nextEntry;  // swipe left  → older
      if (target) {
        detailArticle.style.transition = 'transform 0.2s ease-out, opacity 0.2s';
        detailArticle.style.transform = `translateX(${dx > 0 ? '100%' : '-100%'})`;
        detailArticle.style.opacity = '0';
        setTimeout(() => { window.location.href = '/entries/' + target; }, 200);
      }
    }, { passive: true });
  }

  /* --- Mark read on click --- */
  document.querySelectorAll('.entry-row .entry-link').forEach(link => {
    link.addEventListener('click', () => {
      const row = link.closest('.entry-row');
      if (!row || !row.classList.contains('unread')) return;
      const entryId = row.dataset.entryId;
      if (!entryId) return;
      fetch(`/entries/${entryId}/mark-read`, { method: 'POST' });
      row.classList.remove('unread');
      row.classList.add('read');
    });
  });

  /* --- Unread count polling (title badge) --- */
  let lastCount = null;
  function pollUnreadCount() {
    fetch('/api/new-count')
      .then(r => r.json())
      .then(data => {
        const count = data.count || 0;
        const baseTitle = document.title.replace(/^\(\d+\)\s*/, '');
        document.title = count > 0 ? `(${count}) ${baseTitle}` : baseTitle;

        // Browser notification if count increased
        if (lastCount !== null && count > lastCount && Notification.permission === 'granted') {
          new Notification('RSS Reader', {
            body: `${count - lastCount} new unread article(s)`,
            icon: '/static/icon-192.png',
          });
        }
        lastCount = count;
      })
      .catch(() => {});
  }

  // Request notification permission
  if ('Notification' in window && Notification.permission === 'default') {
    // Don't ask immediately, wait for user interaction
    document.addEventListener('click', function askOnce() {
      Notification.requestPermission();
      document.removeEventListener('click', askOnce);
    }, { once: true });
  }

  // Poll every 60 seconds
  pollUnreadCount();
  setInterval(pollUnreadCount, 60000);

  /* --- Service Worker registration --- */
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
  }
})();
