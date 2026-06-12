/* JOGA Barbearia — Service Worker (PWA) */
const CACHE = 'barbearia-v1';
const ASSETS = ['/static/app.css', '/static/app.js', '/static/icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.pathname.startsWith('/api/') || url.origin !== location.origin) return;  // API/externos: rede direto

  // HTML/navegação: network-first (deploy novo aparece na hora; cache só offline)
  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    e.respondWith(fetch(req).catch(() => caches.match(req).then(c => c || caches.match('/'))));
    return;
  }
  // Estáticos: cache-first
  e.respondWith(caches.match(req).then(cached => cached || fetch(req).then(resp => {
    if (resp.ok) { const cp = resp.clone(); caches.open(CACHE).then(c => c.put(req, cp)); }
    return resp;
  })));
});
