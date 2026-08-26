(() => {
  const $ = (id) => document.getElementById(id);

  const urlInput = $("url");
  const goBtn = $("goBtn");
  const statusEl = $("status");
  const formPanel = $("formPanel");
  const resultPanel = $("resultPanel");
  const inkbar = $("inkbar");
  const modeHint = $("modeHint");

  let mode = "exact";
  let size = "A4";
  let current = null;   // { id, filename, view, download, ... }
  let blobCache = null;
  let lastUrl = "";

  const PREFS = "linkpdf-prefs";

  function loadPrefs() {
    try {
      const p = JSON.parse(localStorage.getItem(PREFS) || "{}");
      if (p.mode === "exact" || p.mode === "reader") mode = p.mode;
      if (["A4", "Letter", "Legal"].includes(p.size)) size = p.size;
    } catch { /* first run, or storage blocked */ }
  }

  function savePrefs() {
    try { localStorage.setItem(PREFS, JSON.stringify({ mode, size })); } catch { /* ignore */ }
  }

  function paintChoices() {
    document.querySelectorAll("[data-mode]").forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle("on", on);
      b.setAttribute("aria-checked", on ? "true" : "false");
    });
    document.querySelectorAll("[data-size]").forEach((b) => {
      const on = b.dataset.size === size;
      b.classList.toggle("on", on);
      b.setAttribute("aria-checked", on ? "true" : "false");
    });
    modeHint.textContent = HINTS[mode];
  }

  const HINTS = {
    exact: "Looks just like the page on screen — pictures, colours and all.",
    reader: "Just the article: no menus, no ads. Cleaner and fewer pages.",
  };

  // ── segmented controls ────────────────────────────────────────
  document.querySelectorAll(".seg").forEach((group) => {
    group.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      group.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("on", b === btn);
        b.setAttribute("aria-checked", b === btn ? "true" : "false");
      });
      if (btn.dataset.mode) {
        mode = btn.dataset.mode;
        modeHint.textContent = HINTS[mode];
      }
      if (btn.dataset.size) size = btn.dataset.size;
      savePrefs();
    });
  });

  // ── paste button ──────────────────────────────────────────────
  $("pasteBtn").addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) { urlInput.value = text.trim(); urlInput.focus(); }
      else setStatus("Nothing copied yet. Copy a link first.", true);
    } catch {
      urlInput.focus();
      setStatus("Long-press the box and choose Paste.", false);
    }
  });

  urlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); make(); }
  });

  goBtn.addEventListener("click", make);
  $("againBtn").addEventListener("click", reset);

  function explain(status, usedMode) {
    const lighter = usedMode === "exact"
      ? " Try Reading version — it's much lighter and usually gets through."
      : " Try again in a moment.";
    if (status === 502 || status === 503 || status === 504) {
      return "That page was too heavy for the server." + lighter;
    }
    if (status === 429) return "Too many at once. Wait a moment and try again.";
    if (status >= 500) return "The server had trouble with that page." + lighter;
    return "Couldn't make a PDF from that link." + lighter;
  }

  function setStatus(text, bad) {
    statusEl.textContent = text;
    statusEl.classList.toggle("bad", !!bad);
  }

  // ── the four-colour pass ──────────────────────────────────────
  let inkTimer = null;
  const STAGES = [
    "Opening the page…",
    "Loading pictures…",
    "Laying out the paper…",
    "Printing to PDF…",
  ];

  function startPress() {
    const inks = [...inkbar.querySelectorAll("i")];
    let i = 0;
    setStatus(STAGES[0], false);
    inkbar.classList.add("pressing");
    inks[0].classList.add("wet");
    inkTimer = setInterval(() => {
      i += 1;
      if (i < inks.length) {
        inks[i].classList.add("wet");
        setStatus(STAGES[i], false);
      } else if (i === inks.length + 3) {
        setStatus("Still going — a slow page, or the server waking up.", false);
      }
    }, 2600);
  }

  function stopPress() {
    clearInterval(inkTimer);
    inkbar.classList.remove("pressing");
    inkbar.querySelectorAll("i").forEach((n) => n.classList.remove("wet"));
  }

  // ── make the PDF ──────────────────────────────────────────────
  async function make() {
    const url = urlInput.value.trim();
    if (!url) { setStatus("Paste a link first.", true); urlInput.focus(); return; }
    lastUrl = url;

    goBtn.disabled = true;
    goBtn.textContent = "Working…";
    startPress();

    try {
      const res = await fetch("/api/make", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, mode, size }),
      });

      // The server may die mid-render on a heavy page and send nothing back,
      // so never assume the reply is valid JSON.
      const raw = await res.text();
      let data = null;
      if (raw) { try { data = JSON.parse(raw); } catch { /* not JSON */ } }

      if (!res.ok || !data || !data.id) {
        throw new Error((data && data.detail) || explain(res.status, mode));
      }
      current = data;
      blobCache = null;
      showResult(data);
    } catch (err) {
      // A dropped connection surfaces as a TypeError like "Failed to fetch".
      const network =
        err instanceof TypeError ||
        /failed to fetch|networkerror|load failed/i.test((err && err.message) || "");
      setStatus(
        network
          ? "Couldn't reach the server. Check your connection, or it may still be waking up — try again in a moment."
          : (err && err.message) || "Something went wrong. Try again.",
        true
      );
    } finally {
      stopPress();
      goBtn.disabled = false;
      goBtn.textContent = "Make PDF";
    }
  }

  function showResult(d) {
    $("fileName").textContent = d.filename;
    $("fileSpecs").textContent = `${d.pages} page${d.pages === 1 ? "" : "s"} · ${d.kb} KB · ${size} · ${d.source}`;
    formPanel.hidden = true;
    resultPanel.hidden = false;
    setStatus("", false);
    resultPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    $("shareBtn").hidden = !navigator.canShare;
    $("redoBtn").textContent =
      mode === "exact" ? "Redo as reading version" : "Redo as exact copy";
  }

  // Regenerate the same link in the other style, one tap.
  $("redoBtn").addEventListener("click", () => {
    if (!lastUrl) return;
    mode = mode === "exact" ? "reader" : "exact";
    savePrefs();
    paintChoices();
    resultPanel.hidden = true;
    formPanel.hidden = false;
    urlInput.value = lastUrl;
    make();
  });

  function reset() {
    resultPanel.hidden = true;
    formPanel.hidden = false;
    current = null;
    blobCache = null;
    urlInput.value = "";
    urlInput.focus();
    document.querySelectorAll(".act").forEach((b) => b.classList.remove("done"));
  }

  function flash(btn, text) {
    const old = btn.textContent;
    btn.textContent = text;
    btn.classList.add("done");
    setTimeout(() => { btn.textContent = old; btn.classList.remove("done"); }, 1800);
  }

  async function getBlob() {
    if (blobCache) return blobCache;
    const r = await fetch(current.view);
    blobCache = await r.blob();
    return blobCache;
  }

  // ── actions ───────────────────────────────────────────────────
  $("downloadBtn").addEventListener("click", () => {
    const a = document.createElement("a");
    a.href = current.download;
    a.download = current.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    flash($("downloadBtn"), "Saved");
  });

  $("printBtn").addEventListener("click", async () => {
    const touch = window.matchMedia("(pointer: coarse)").matches;
    if (touch) {
      window.open(current.view, "_blank", "noopener");
      return;
    }
    try {
      const blob = await getBlob();
      const src = URL.createObjectURL(blob);
      const frame = document.createElement("iframe");
      frame.style.cssText = "position:fixed;width:0;height:0;border:0;opacity:0";
      frame.src = src;
      frame.onload = () => {
        frame.contentWindow.focus();
        frame.contentWindow.print();
      };
      document.body.appendChild(frame);
    } catch {
      window.open(current.view, "_blank", "noopener");
    }
  });

  $("shareBtn").addEventListener("click", async () => {
    try {
      const blob = await getBlob();
      const file = new File([blob], current.filename, { type: "application/pdf" });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], title: current.title });
        return;
      }
      await navigator.share({ title: current.title, url: location.origin + current.view });
    } catch (err) {
      if (err && err.name === "AbortError") return;
      flash($("shareBtn"), "Not supported");
    }
  });

  $("linkBtn").addEventListener("click", async () => {
    const link = location.origin + current.view;
    try {
      await navigator.clipboard.writeText(link);
      flash($("linkBtn"), "Copied");
    } catch {
      prompt("Copy this link:", link);
    }
  });

  // ── arriving from the Android share sheet ─────────────────────
  function pickLink(...candidates) {
    for (const c of candidates) {
      if (!c) continue;
      const hit = c.match(/https?:\/\/[^\s<>"]+/i);
      if (hit) return hit[0].replace(/[.,;:)\]]+$/, "");
      if (/^[a-z0-9.-]+\.[a-z]{2,}(\/|$)/i.test(c.trim())) return c.trim();
    }
    return "";
  }

  function handleShare() {
    const q = new URLSearchParams(location.search);
    if (!q.has("url") && !q.has("text") && !q.has("title")) return false;
    // Android puts the link in whichever field the sending app chose.
    const link = pickLink(q.get("url"), q.get("text"), q.get("title"));
    // Clean the address bar so a refresh doesn't fire this again.
    history.replaceState(null, "", "/");
    if (!link) {
      setStatus("That share didn't contain a link. Paste one below.", true);
      return false;
    }
    urlInput.value = link;
    make();
    return true;
  }

  loadPrefs();
  paintChoices();
  handleShare();

  // ── making sure it installs properly ──────────────────────────
  const ua = navigator.userAgent || "";
  const isAndroid = /Android/i.test(ua);
  const inAppBrowser =
    /FBAN|FBAV|FB_IAB|Instagram|WhatsApp|Line\/|Snapchat|Twitter|MicroMessenger|GSA\//i.test(ua) ||
    (isAndroid && /\bwv\b/.test(ua));
  const standalone =
    window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;

  // Opened from inside WhatsApp, Instagram etc: installing from here silently
  // produces a bookmark with no share-menu entry, so say so plainly.
  if (inAppBrowser && !standalone) {
    $("browserAlert").hidden = false;
    $("copyForChrome").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(location.origin);
        flash($("copyForChrome"), "Copied — now paste in Chrome");
      } catch {
        prompt("Copy this, then open it in Chrome:", location.origin);
      }
    });
  }

  // Chrome fires this only when a genuine app install is possible.
  let installEvent = null;
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    installEvent = e;
    if (!standalone) $("installBtn").hidden = false;
  });

  $("installBtn").addEventListener("click", async () => {
    if (!installEvent) return;
    installEvent.prompt();
    const { outcome } = await installEvent.userChoice;
    installEvent = null;
    if (outcome === "accepted") {
      $("installBtn").hidden = true;
    }
  });

  window.addEventListener("appinstalled", () => {
    $("installBtn").hidden = true;
    setStatus("Installed. Open it once from your home screen, then it appears in your share menu.", false);
  });

  // ── self-diagnosis ────────────────────────────────────────────
  $("checkBtn").addEventListener("click", async () => {
    const box = $("checks");
    if (!box.hidden) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = "<div>Checking…</div>";

    const rows = [];
    const add = (ok, label) => rows.push({ ok, label });

    add(window.isSecureContext, "Secure connection");
    add(!inAppBrowser, inAppBrowser ? "Opened inside another app's browser" : "Opened in a real browser");
    add(/Chrome|Chromium/i.test(ua) && !/OPR|Firefox|SamsungBrowser/i.test(ua),
        "Using Chrome");
    add("serviceWorker" in navigator && !!(await navigator.serviceWorker.getRegistration()),
        "Background service running");

    let manifestOk = false;
    try {
      const r = await fetch("/manifest.json", { cache: "no-store" });
      const m = await r.json();
      manifestOk = !!(m.share_target && m.share_target.action);
    } catch { /* offline */ }
    add(manifestOk, "Share-menu setting present");
    add(standalone, standalone ? "Running as an installed app" : "Not opened as an installed app");

    box.innerHTML = rows
      .map((r) => `<div class="${r.ok ? "ok" : "no"}"><b>${r.ok ? "OK" : "NO"}</b><span>${r.label}</span></div>`)
      .join("");

    // Say what to actually do about it.
    let advice = "";
    if (inAppBrowser) {
      advice = "You opened this by tapping a link inside another app (WhatsApp, Instagram and so on), which uses its own mini browser. Copy the address, open Chrome, paste it there, then install.";
    } else if (!/Chrome|Chromium/i.test(ua) || /OPR|Firefox|SamsungBrowser/i.test(ua)) {
      advice = "Only Chrome on Android can add an app to the share menu. Open this address in Chrome.";
    } else if (!standalone) {
      advice = "Tap the Install button above, or Chrome's ⋮ menu and choose Install app. If it only offers 'Add to Home screen', that makes a bookmark with no share menu entry — reload the page and try again.";
    } else if (!manifestOk) {
      advice = "The app couldn't read its settings. Reload the page while online, then reinstall.";
    } else {
      advice = "All good. If it's still missing, uninstall the icon, reload this page in Chrome, and install again.";
    }
    const note = document.createElement("span");
    note.className = "fixit";
    note.textContent = advice;
    box.appendChild(note);
  });

  // ── install as an app ─────────────────────────────────────────
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () =>
      navigator.serviceWorker.register("/sw.js").catch(() => {})
    );
  }
})();
