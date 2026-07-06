/* HeliNav Service Worker — オフライン対応
 * app-shell: cache-first / OSM tiles: cache-first(+network fill) / data: stale-while-revalidate
 */
const SHELL = 'helinav-shell-v6';
const TILES = 'helinav-tiles';
const APP_FILES = [
  './',
  './index.html',
  './manifest.webmanifest',
  './css/app.css',
  './js/app.js',
  './lib/leaflet/leaflet.js',
  './lib/leaflet/leaflet.css',
  './lib/leaflet/images/marker-icon.png',
  './lib/leaflet/images/marker-icon-2x.png',
  './lib/leaflet/images/marker-shadow.png',
  './lib/leaflet/images/layers.png',
  './lib/leaflet/images/layers-2x.png',
  './data/airports.geojson',
  './data/heliports.geojson',
  './data/navaids.geojson',
  './data/powerlines.geojson',
  './data/airspace.geojson',
  './icons/icon-192.png',
  './icons/icon-512.png',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(APP_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== SHELL && k !== TILES).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // OSM タイル: cache-first、無ければ取得してキャッシュ
  if (/tile\.openstreetmap\.org/.test(url.host) || /\/\d+\/\d+\/\d+\.png$/.test(url.pathname)) {
    e.respondWith(
      caches.open(TILES).then(async cache => {
        const hit = await cache.match(req);
        if (hit) return hit;
        try {
          const res = await fetch(req, { mode: 'cors' });
          if (res.ok) cache.put(req, res.clone());
          return res;
        } catch (_) {
          return hit || Response.error();
        }
      })
    );
    return;
  }

  // Overpass 等の外部 API はキャッシュしない(ネットワーク直)
  if (url.host.includes('overpass') || url.host.includes('aviationweather')) return;

  // アプリシェル/データ: cache-first + バックグラウンド更新
  e.respondWith(
    caches.match(req).then(hit => {
      const net = fetch(req).then(res => {
        if (res && res.ok && url.origin === self.location.origin) {
          caches.open(SHELL).then(c => c.put(req, res.clone()));
        }
        return res;
      }).catch(() => hit);
      return hit || net;
    })
  );
});
