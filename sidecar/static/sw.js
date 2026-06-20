// Service worker for the RSS sidecar.
//
// Registered with an explicit root scope ({scope:'/'} in app.js, backed by the
// Service-Worker-Allowed:/ header on this file) so it actually controls the app
// pages — a worker served from /static/ defaults to /static/ scope and would
// never intercept /entries navigations or their asset requests at all.
//
// Asset strategy is stale-while-revalidate keyed by PATH (query stripped) so the
// versioned URLs the page requests (style.css?v=ds2, app.js?v=ds2, …) resolve from
// the precached base file and survive the network being down. This is the fix for
// "no CSS when reopened later/offline": previously the worker either didn't run on
// app pages or cache-missed on the ?v= query and the styles failed with the network.

const CACHE_NAME = 'rss-sidecar-v11';

// Everything base.html loads under /static/.
const STATIC_ASSETS = [
  '/static/style.css',
  '/static/tailwind.css',
  '/static/app.js',
  '/static/htmx.min.js',
  '/static/mobile.js',
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      // Cache each individually so one failure doesn't abort the whole precache.
      Promise.all(
        STATIC_ASSETS.map((url) =>
          cache.add(url).catch((err) => console.warn('[sw] precache failed', url, err))
        )
      )
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return; // same-origin only

  // Static assets: stale-while-revalidate, keyed by PATH ONLY so style.css?v=ds2
  // matches the precached style.css. Cache wins instantly; refresh in background;
  // when the network is down the cached asset still serves.
  if (url.pathname.startsWith('/static/')) {
    const cacheKey = url.origin + url.pathname;
    event.respondWith(
      caches.open(CACHE_NAME).then((cache) =>
        cache.match(cacheKey).then((cached) => {
          const network = fetch(request)
            .then((resp) => {
              if (resp && resp.ok) cache.put(cacheKey, resp.clone());
              return resp;
            })
            .catch(() => cached);
          return cached || network;
        })
      )
    );
    return;
  }

  // Full-page navigations: network-first (always fresh online), fall back to the
  // last cached page when offline → real offline reading. htmx fragment requests
  // send Accept: */* and mode != "navigate", so they never enter this branch; only
  // genuine document loads are cached.
  const accept = request.headers.get('accept') || '';
  if (request.mode === 'navigate' || accept.includes('text/html')) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response && response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
          }
          return response;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  // Everything else (API, SSE, image proxy): network only.
});
