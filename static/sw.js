// ── Animeku.ID Service Worker ──────────────────────────
const CACHE_NAME = 'animeku-v1';

// Aset statis yang di-cache saat install
const STATIC_ASSETS = [
  '/',
  '/static/css/main.css',
  '/static/js/main.js',
  '/static/img/favicon.png',
  '/static/img/icon_192.png',
  '/static/img/icon_512.png',
  '/static/manifest.json',
];

// ── Install: cache aset statis ──
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(STATIC_ASSETS).catch(() => {
        // Lanjut meski ada yang gagal di-cache
      });
    })
  );
  self.skipWaiting();
});

// ── Activate: hapus cache lama ──
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ── Fetch: Network first, fallback to cache ──
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip non-GET & non-HTTP requests
  if (request.method !== 'GET') return;
  if (!url.protocol.startsWith('http')) return;

  // Skip Supabase API calls (jangan di-cache)
  if (url.hostname.includes('supabase.co')) return;

  // Skip external CDN (fonts, ads, dll)
  if (!url.hostname.includes('animeku') && url.hostname !== location.hostname) return;

  event.respondWith(
    fetch(request)
      .then(response => {
        // Cache response statis yang berhasil
        if (response.ok && url.pathname.startsWith('/static/')) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, clone));
        }
        return response;
      })
      .catch(() => {
        // Offline fallback: coba dari cache
        return caches.match(request).then(cached => {
          if (cached) return cached;
          // Fallback ke halaman utama jika navigate
          if (request.mode === 'navigate') {
            return caches.match('/');
          }
        });
      })
  );
});
