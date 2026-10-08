// The phone app's service worker: keeps the app shell on the phone so the feed opens at
// once, docked or not. Shell files always come from this cache, so a launch never waits on
// the network (away from home the Angler's address often hangs rather than fails). Posts and
// images live in IndexedDB (device-store.js), so /api/ and /media/ are never touched here.
//
// How a new release reaches the phone: the Angler serves this file with VERSION set to a
// hash of web/device/ and web/shared/ (angler/server.py), so changing any shell file changes
// this script. On a launch where the Angler answers, the browser spots that in the background
// and installs this worker under a new cache, which then takes over; the next launch runs the
// new shell. A failed install (offline, a 502 from a proxy) changes nothing: the old worker
// and its cache stay. Served some other way, VERSION stays 'dev': bump it by hand per release.
const VERSION = 'dev';
const SHELL = 'baitbook-shell-' + VERSION;
const FILES = ['index.html', 'device.css', 'device-store.js', 'device.js', 'manifest.webmanifest', 'icon.svg',
  'icon-192.png', 'icon-512.png', 'icon-maskable-512.png', 'apple-touch-icon.png',
  '/shared/theme.css', '/shared/mark.svg', '/shared/api.js', '/shared/reef.js', '/shared/fonts/fonts.css',
  '/shared/fonts/fraunces-latin.woff2', '/shared/fonts/fraunces-latin-ext.woff2',
  '/shared/fonts/fraunces-italic-latin.woff2', '/shared/fonts/fraunces-italic-latin-ext.woff2',
  '/shared/fonts/manrope-latin.woff2', '/shared/fonts/manrope-latin-ext.woff2',
  '/shared/fonts/jetbrains-mono-latin.woff2', '/shared/fonts/jetbrains-mono-latin-ext.woff2'];
const INDEX = new URL('index.html', location).href;

self.addEventListener('install', (e) => {
  // cache: 'reload' skips the HTTP cache, so a new version never stores yesterday's files.
  // addAll fails on any error status, and a failed install leaves the old worker in charge.
  e.waitUntil(caches.open(SHELL)
    .then((c) => c.addAll(FILES.map((f) => new Request(f, { cache: 'reload' }))))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  // drop the other versions' shells, then take over the open pages
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith('baitbook-shell-') && k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const req = e.request, url = new URL(req.url);
  // only this origin's files: the API and images (maybe at another address) go straight through
  if (req.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/') || url.pathname.startsWith('/media/')) return;
  e.respondWith(caches.open(SHELL).then(async (cache) => {
    // opening the app: the cached page, whatever the URL's query
    if (req.mode === 'navigate') return (await cache.match(req, { ignoreSearch: true })) || (await cache.match(INDEX)) || fetch(req);
    const hit = await cache.match(req);
    if (hit) return hit;
    // not in the shell: the network, kept only if it's a good response from here (never an
    // error page); offline it fails like any missing file instead of getting index.html
    const res = await fetch(req);
    if (res.ok && res.type === 'basic') e.waitUntil(cache.put(req, res.clone()));
    return res;
  }));
});
