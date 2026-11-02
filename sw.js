// Service worker minimal : rend l'appli installable et garde la page en secours si le réseau (ou le service qui se réveille) ne répond pas
const CACHE = 'tcl-localisation-v1';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())
));
self.addEventListener('fetch', e => {
    const u = new URL(e.request.url);
    // API (temps réel), cartes et autres sites : réseau direct, jamais mis en cache
    if (e.request.method !== 'GET' || u.origin !== location.origin || u.pathname.startsWith('/api/')) return;
    e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(CACHE).then(x => x.put(e.request, c)); return r; }).catch(() => caches.match(e.request)));
});
