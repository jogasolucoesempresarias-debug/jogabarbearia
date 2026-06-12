/* JOGA Barbearia — Service Worker (PWA)
   Estratégia NETWORK-FIRST: sempre busca a versão nova da rede e atualiza o cache;
   o cache só é usado como fallback quando está offline. Evita servir JS/CSS velho. */
const CACHE = 'barbearia-v2';
const ASSETS = ['/static/app.css', '/static/app.js', '/static/icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).catch(() => {}));
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
  if (url.origin !== location.origin || url.pathname.startsWith('/api/')) return;  // API/externos: rede direto

  // Network-first: tenta a rede, atualiza o cache; se offline, cai pro cache.
  e.respondWith(
    fetch(req).then(resp => {
      if (resp && resp.ok) { const cp = resp.clone(); caches.open(CACHE).then(c => c.put(req, cp)); }
      return resp;
    }).catch(() => caches.match(req).then(c => c || (req.mode === 'navigate' ? caches.match('/') : undefined)))
  );
});
