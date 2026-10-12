// Service worker: keeps the app's own files on the phone so it opens without signal.
// Cache-first for the app shell. Calls to GitHub (or any other origin) are never cached.
// Change VERSION whenever a file in docs/ changes, so phones pick up the new files.
const VERSION = "focus-v2.0.0";
const SHELL = [
  "./",
  "./index.html",
  "./app.css",
  "./app.js",
  "./core.js",
  "./logic.js",
  "./store.js",
  "./manifest.webmanifest",
  "./icons/icon-180.png",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // GitHub API and test servers: straight to the network
  event.respondWith(
    caches.open(VERSION).then((cache) => cache.match(req, { ignoreSearch: true }).then((hit) => hit || fetch(req))),
  );
});
