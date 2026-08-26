const SHELL = "linkpdf-v2";
const FILES = ["/", "/styles.css", "/app.js"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;

  // Never touch these. Android reads the manifest and icons when installing the
  // app; a stale cached copy is what stops it registering as a share target.
  if (url.pathname === "/manifest.json" || url.pathname.startsWith("/icon-")) return;

  // Generated PDFs and the API always go straight to the network.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/f/") ||
      url.pathname.startsWith("/d/") || url.pathname.startsWith("/share")) return;

  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(SHELL).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match("/")))
  );
});
