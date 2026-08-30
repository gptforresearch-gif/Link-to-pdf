const SHELL = "linkpdf-v8";
const HANDOFF = "linkpdf-handoff";   // holds a shared file between share and app

const FILES = [
  "/", "/styles.css", "/app.js",
  "/vendor/pdf-lib.min.js", "/vendor/pdf.min.mjs", "/vendor/pdf.worker.min.mjs",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(SHELL)
      .then((c) => Promise.allSettled(FILES.map((f) => c.add(f))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        // Keep the handoff store; it may hold a file mid-share.
        keys.filter((k) => k !== SHELL && k !== HANDOFF).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // ── Android handing us a share ────────────────────────────────
  // A file-capable share target must be POST, so links and files both
  // arrive here. Sort out which, stash anything large, and send the app
  // a plain URL it can act on.
  if (e.request.method === "POST" && url.pathname === "/share-file") {
    e.respondWith((async () => {
      let dest = "/?shared=empty";
      try {
        const form = await e.request.formData();
        const file = form.get("file");
        const text = form.get("text") || form.get("url") || form.get("title") || "";

        if (file && file.size) {
          // A Response body can't be a File, so keep the name in a header.
          const store = await caches.open(HANDOFF);
          await store.put("/__shared-file", new Response(file, {
            headers: {
              "Content-Type": file.type || "application/octet-stream",
              "X-Filename": encodeURIComponent(file.name || "document.docx"),
            },
          }));
          dest = "/?shared=file";
        } else if (String(text).trim()) {
          dest = "/?text=" + encodeURIComponent(text);
        }
      } catch {
        dest = "/?shared=failed";
      }
      return Response.redirect(new URL(dest, self.location.origin).href, 303);
    })());
    return;
  }

  if (e.request.method !== "GET" || url.origin !== location.origin) return;

  // Android reads these when installing; a stale copy breaks the share entry.
  if (url.pathname === "/manifest.json" || url.pathname.startsWith("/icon-")) return;

  // Generated PDFs and the API always go to the network.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/f/") ||
      url.pathname.startsWith("/d/")) return;

  // The app itself: cache first, so it opens instantly while the free server
  // wakes, and works with no connection at all.
  e.respondWith(
    caches.match(e.request, { ignoreSearch: url.pathname === "/" || url.pathname === "/share" })
      .then((hit) => {
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
