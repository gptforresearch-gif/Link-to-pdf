const SHELL = "linkpdf-v4";

// Everything the app needs to open with no server at all.
const FILES = [
  "/", "/styles.css", "/app.js",
  "/vendor/pdf-lib.min.js", "/vendor/pdf.min.mjs", "/vendor/pdf.worker.min.mjs",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(SHELL)
      // Vendor files are large; don't fail the whole install if one misses.
      .then((c) => Promise.allSettled(FILES.map((f) => c.add(f))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;

  // Android reads these when installing the app; a stale copy breaks the
  // share-menu entry, so they always come from the network.
  if (url.pathname === "/manifest.json" || url.pathname.startsWith("/icon-")) return;

  // Generated PDFs and the API are never cached.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/f/") ||
      url.pathname.startsWith("/d/")) return;

  // Everything else — the app itself — comes from the cache first. This is what
  // lets the app open instantly while the free server is still waking up, and
  // what makes it work with no connection at all. A fresh copy is fetched in
  // the background for next time.
  e.respondWith(
    caches.match(e.request, { ignoreSearch: url.pathname === "/share" }).then((hit) => {
      const fresh = fetch(e.request)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(SHELL).then((c) => c.put(e.request, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => hit || caches.match("/"));
      return hit || fresh;
    })
  );
});
