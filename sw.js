/* La Cochette Dorée — service worker (hors-ligne) */
const VERSION = 'cochette-v1';
const PRECACHE = [
  './', './index.html', './manifest.json', './cochette-boot.js',
  './app.py', './cochette.db',
  './vendor/chart.umd.js',
  './icons/icon-180.png', './icons/icon-192.png', './icons/icon-512.png',
  './pyodide/pyodide.mjs', './pyodide/pyodide.asm.mjs', './pyodide/pyodide.asm.wasm',
  './pyodide/python_stdlib.zip', './pyodide/pyodide-lock.json',
  './wheels/markupsafe-3.0.3-py3-none-any.whl',
  './wheels/itsdangerous-2.2.0-py3-none-any.whl',
  './wheels/blinker-1.9.0-py3-none-any.whl',
  './wheels/click-8.4.2-py3-none-any.whl',
  './wheels/jinja2-3.1.6-py3-none-any.whl',
  './wheels/werkzeug-3.1.8-py3-none-any.whl',
  './wheels/flask-3.1.3-py3-none-any.whl'
];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k)))
  ).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.includes('/api/')) return; // interceptés côté page
  e.respondWith(
    caches.match(e.request, {ignoreSearch: true}).then(hit =>
      hit || fetch(e.request).then(resp => {
        const copy = resp.clone();
        caches.open(VERSION).then(c => c.put(e.request, copy));
        return resp;
      }).catch(() => e.request.mode === 'navigate' ? caches.match('./index.html') : undefined)
    )
  );
});
