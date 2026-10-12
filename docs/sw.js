// Service worker: keeps the app's own files on the phone so it opens without signal.
// Network first for the app shell: every load asks the server for fresh files (bypassing the browser
// cache) and keeps a copy; when there is no network, the kept copy is used. So a new deploy reaches the
// phone on its next online start, with no version bump needed.
// Calls to GitHub (or any other origin) and anything that is not a GET are never touched.
const CACHE = "focus-shell";
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
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL.map((url) => new Request(url, { cache: "reload" }))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

async function networkFirst(req) {
  const cache = await caches.open(CACHE);
  try {
    // Fetch by URL: a navigation Request cannot be copied with new options.
    const res = await fetch(req.url, { cache: "no-store", credentials: "same-origin" });
    if (res && res.redirected && req.mode === "navigate") return Response.redirect(res.url, 302);
    if (res && res.ok && res.type === "basic" && !res.redirected) {
      // Store under the URL without its query, so "./?api=..." and "./" share one copy.
      const url = new URL(req.url);
      url.search = "";
      await cache.put(url.href, res.clone());
    }
    return res;
  } catch (err) {
    const hit = await cache.match(req, { ignoreSearch: true });
    if (hit) return hit;
    throw err;
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // GitHub API and test servers: straight to the network
  event.respondWith(networkFirst(req));
});
