(() => {
  const $ = (id) => document.getElementById(id);

  const urlInput = $("url"), goBtn = $("goBtn"), statusEl = $("status");
  const formPanel = $("formPanel"), resultPanel = $("resultPanel");
  const historyPanel = $("historyPanel"), inkbar = $("inkbar"), modeHint = $("modeHint");

  let tab = "link", mode = "exact", size = "A4";
  let current = null, blobCache = null, lastUrl = "";

  const PREFS = "linkpdf-prefs", THEME = "linkpdf-theme";
  const HINTS = {
    exact: "Looks just like the page on screen — pictures, colours and all.",
    reader: "Just the article: no menus, no ads. Cleaner and fewer pages.",
  };

  /* ── dark mode ─────────────────────────────────────────────── */
  function applyTheme(dark) {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    const meta = $("themeColor");
    if (meta) meta.setAttribute("content", dark ? "#0e1216" : "#e9ecef");
  }
  $("themeBtn").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "dark";
    applyTheme(dark);
    try { localStorage.setItem(THEME, dark ? "dark" : "light"); } catch {}
  });
  // Follow the phone's setting until the user overrides it.
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    try { if (!localStorage.getItem(THEME)) applyTheme(e.matches); } catch {}
  });

  /* ── history, kept in this browser ─────────────────────────── */
  const DB = "linkpdf", STORE = "pdfs", KEEP = 50, KEEP_BYTES = 50 * 1024 * 1024;

  function db() {
    return new Promise((res, rej) => {
      const r = indexedDB.open(DB, 1);
      r.onupgradeneeded = () => {
        const d = r.result;
        if (!d.objectStoreNames.contains(STORE)) {
          d.createObjectStore(STORE, { keyPath: "key" }).createIndex("at", "at");
        }
      };
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  }
  function tx(store, fn, write) {
    return db().then((d) => new Promise((res, rej) => {
      const t = d.transaction(store, write ? "readwrite" : "readonly");
      const out = fn(t.objectStore(store));
      t.oncomplete = () => res(out && out.result !== undefined ? out.result : out);
      t.onerror = () => rej(t.error);
    }));
  }
  const histAll = () => tx(STORE, (s) => s.getAll()).then((r) => (r || []).sort((a, b) => b.at - a.at));
  const histPut = (rec) => tx(STORE, (s) => s.put(rec), true);
  const histDel = (key) => tx(STORE, (s) => s.delete(key), true);
  const histClear = () => tx(STORE, (s) => s.clear(), true);

  async function histSave(meta, blob) {
    try {
      await histPut({
        key: meta.id, at: Date.now(), title: meta.title, filename: meta.filename,
        source: meta.source, sourceUrl: meta.sourceUrl || "", style: meta.style,
        pages: meta.pages, locked: !!meta.locked, size: meta.paper || size, blob,
      });
      await histPrune();
      await histBadge();
    } catch { /* storage full or blocked: history is a bonus, not the job */ }
  }
  async function histPrune() {
    const all = await histAll();
    let total = all.reduce((n, r) => n + (r.blob ? r.blob.size : 0), 0);
    for (let i = KEEP; i < all.length; i++) { await histDel(all[i].key); }
    let i = 0;
    while (total > KEEP_BYTES && i < all.length) {
      const victim = all[all.length - 1 - i];
      if (!victim) break;
      total -= victim.blob ? victim.blob.size : 0;
      await histDel(victim.key);
      i++;
    }
  }
  async function histBadge() {
    try {
      const n = (await histAll()).length;
      const b = $("histCount");
      b.hidden = n === 0;
      b.textContent = n > 99 ? "99+" : String(n);
    } catch {}
  }

  function when(ts) {
    const d = new Date(ts), now = new Date();
    const same = d.toDateString() === now.toDateString();
    const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (same) return "Today " + time;
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return "Yesterday " + time;
    return d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
  }

  async function histRender() {
    const list = $("histList"), rows = await histAll();
    list.innerHTML = "";
    $("histEmpty").hidden = rows.length > 0;
    $("histTools").hidden = rows.length === 0;

    rows.forEach((r) => {
      const el = document.createElement("div");
      el.className = "hrow";
      const kb = r.blob ? Math.round(r.blob.size / 1024) : 0;
      const styleName = { exact: "Exact copy", reader: "Reading version", lite: "Simplified", text: "Typed text", merged: "Merged", edited: "Edited", docx: "Word document" }[r.style] || r.style;
      el.innerHTML =
        `<div class="hpick"><input type="checkbox" id="pick-${r.key}"><label for="pick-${r.key}">Select</label></div>
         <p class="htitle"></p>
         <p class="hmeta"></p>
         <div class="hacts">
           <button data-a="share">Share</button>
           <button data-a="open">Open</button>
           <button data-a="save">Save</button>
           <button data-a="del" class="del">Delete</button>
         </div>`;
      el.querySelector(".htitle").textContent = r.title || r.filename;
      el.querySelector(".hmeta").innerHTML =
        `${escapeHtml(r.source)} · ${when(r.at)} · ${r.pages || "?"}pp · ${kb}KB · ${escapeHtml(styleName)}` +
        (r.locked ? ' · <span class="lock">password</span>' : "");
      el.querySelector(".hacts").addEventListener("click", (e) => {
        const b = e.target.closest("button"); if (!b) return;
        histAction(b.dataset.a, r, b);
      });
      el.querySelector(".hpick input").addEventListener("change", paintMerge);
      list.appendChild(el);
    });
    paintMerge();
  }

  function picked() {
    return [...document.querySelectorAll(".hpick input:checked")]
      .map((c) => c.id.replace("pick-", ""));
  }
  function paintMerge() {
    const n = picked().length;
    const b = $("mergeBtn");
    b.hidden = n < 2;
    b.textContent = `Merge ${n} PDFs into one`;
  }

  /* ── merging ───────────────────────────────────────────────── */
  $("mergeBtn").addEventListener("click", async () => {
    const keys = picked();
    if (keys.length < 2) return;
    const btn = $("mergeBtn");
    btn.disabled = true; btn.textContent = "Merging…";
    try {
      const { PDFDocument } = await loadPdfLib();
      const all = await histAll();
      const chosen = keys.map((k) => all.find((r) => r.key === k)).filter(Boolean);
      if (chosen.some((r) => r.locked)) {
        throw new Error("Password-protected PDFs can't be merged. Unlock or remake them first.");
      }
      const out = await PDFDocument.create();
      for (const rec of chosen) {
        const src = await PDFDocument.load(await rec.blob.arrayBuffer());
        const pages = await out.copyPages(src, src.getPageIndices());
        pages.forEach((pg) => out.addPage(pg));
      }
      const bytes = await out.save();
      const blob = new Blob([bytes], { type: "application/pdf" });
      await finishLocal(blob, "merged.pdf", `${chosen.length} PDFs merged`, "merged");
    } catch (err) {
      setStatus((err && err.message) || "Couldn't merge those.", true);
      historyPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } finally {
      btn.disabled = false; paintMerge();
    }
  });

  /* ── a PDF made on this device (merged or edited) ──────────── */
  async function finishLocal(blob, filename, title, style) {
    const id = "local-" + Date.now().toString(36);
    const pages = await countPages(blob);
    current = {
      id, filename, title, source: "this device", pages,
      kb: Math.round(blob.size / 1024), style, locked: false,
      note: "Made on this device. Share and Download use this file; it has no web link.",
      view: null, download: null, localOnly: true,
    };
    blobCache = blob;
    await histSave({ ...current, sourceUrl: "", paper: size }, blob);
    await histRender();
    showResult(current);
  }

  async function countPages(blob) {
    try {
      const { PDFDocument } = await loadPdfLib();
      return (await PDFDocument.load(await blob.arrayBuffer())).getPageCount();
    } catch { return "?"; }
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function histAction(action, rec, btn) {
    if (!rec.blob) { flash(btn, "Gone"); return; }
    const file = new File([rec.blob], rec.filename, { type: "application/pdf" });
    if (action === "share") {
      try {
        if (navigator.canShare && navigator.canShare({ files: [file] })) {
          await navigator.share({ files: [file], title: rec.title });
        } else { flash(btn, "Not supported"); }
      } catch (e) { if (!e || e.name !== "AbortError") flash(btn, "Failed"); }
    } else if (action === "open") {
      const u = URL.createObjectURL(rec.blob);
      window.open(u, "_blank", "noopener");
      setTimeout(() => URL.revokeObjectURL(u), 60000);
    } else if (action === "save") {
      saveBlob(rec.blob, rec.filename); flash(btn, "Saved");
    } else if (action === "del") {
      await histDel(rec.key); await histRender(); await histBadge();
    }
  }

  function saveBlob(blob, filename) {
    const u = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = u; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(u), 10000);
  }

  $("historyBtn").addEventListener("click", async () => {
    const showing = !historyPanel.hidden;
    historyPanel.hidden = showing;
    if (!showing) { await histRender(); historyPanel.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
  });
  $("histClose").addEventListener("click", () => { historyPanel.hidden = true; });
  $("histClear").addEventListener("click", async () => {
    if (!confirm("Delete every saved PDF on this device? This can't be undone.")) return;
    await histClear(); await histRender(); await histBadge();
  });
  $("histExport").addEventListener("click", async () => {
    const rows = await histAll();
    const text = rows.map((r) =>
      `${new Date(r.at).toISOString().slice(0, 16).replace("T", " ")}\t${r.title}\t${r.sourceUrl || r.source}`
    ).join("\n");
    saveBlob(new Blob([text || "empty"], { type: "text/plain" }), "link-to-pdf-history.txt");
  });

  /* ── PDF libraries, loaded only when first needed ──────────── */
  let pdfLibP = null, pdfJsP = null;
  function loadPdfLib() {
    if (!pdfLibP) pdfLibP = new Promise((res, rej) => {
      const t = document.createElement("script");
      t.src = "/vendor/pdf-lib.min.js";
      t.onload = () => res(window.PDFLib); t.onerror = () => rej(new Error("lib"));
      document.head.appendChild(t);
    });
    return pdfLibP;
  }
  function loadPdfJs() {
    if (!pdfJsP) pdfJsP = import("/vendor/pdf.min.mjs").then((m) => {
      m.GlobalWorkerOptions.workerSrc = "/vendor/pdf.worker.min.mjs";
      return m;
    });
    return pdfJsP;
  }

  /* ── preferences ───────────────────────────────────────────── */
  function loadPrefs() {
    try {
      const p = JSON.parse(localStorage.getItem(PREFS) || "{}");
      if (p.mode === "exact" || p.mode === "reader") mode = p.mode;
      if (["A4", "Letter", "Legal"].includes(p.size)) size = p.size;
    } catch {}
  }
  function savePrefs() {
    try { localStorage.setItem(PREFS, JSON.stringify({ mode, size })); } catch {}
  }
  function paintChoices() {
    document.querySelectorAll("[data-mode]").forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle("on", on); b.setAttribute("aria-checked", on ? "true" : "false");
    });
    document.querySelectorAll("[data-size]").forEach((b) => {
      const on = b.dataset.size === size;
      b.classList.toggle("on", on); b.setAttribute("aria-checked", on ? "true" : "false");
    });
    modeHint.textContent = HINTS[mode];
  }

  /* ── controls ──────────────────────────────────────────────── */
  document.querySelectorAll(".seg").forEach((group) => {
    group.addEventListener("click", (e) => {
      const btn = e.target.closest("button"); if (!btn) return;
      group.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("on", b === btn);
        const attr = b.hasAttribute("aria-selected") ? "aria-selected" : "aria-checked";
        b.setAttribute(attr, b === btn ? "true" : "false");
      });
      if (btn.dataset.tab) {
        tab = btn.dataset.tab;
        $("linkTab").hidden = tab !== "link";
        $("textTab").hidden = tab !== "text";
        $("docxTab").hidden = tab !== "docx";
      }
      if (btn.dataset.mode) { mode = btn.dataset.mode; modeHint.textContent = HINTS[mode]; savePrefs(); }
      if (btn.dataset.size) { size = btn.dataset.size; savePrefs(); }
    });
  });

  $("pasteBtn").addEventListener("click", async () => {
    try {
      const t = await navigator.clipboard.readText();
      if (t) { urlInput.value = t.trim(); urlInput.focus(); }
      else setStatus("Nothing copied yet. Copy a link first.", true);
    } catch { urlInput.focus(); setStatus("Long-press the box and choose Paste.", false); }
  });

  $("pwShow").addEventListener("click", () => {
    const f = $("pw"), showing = f.type === "text";
    f.type = showing ? "password" : "text";
    $("pwShow").textContent = showing ? "Show" : "Hide";
  });

  $("bodyText").addEventListener("input", (e) => {
    const n = e.target.value.length;
    $("charCount").textContent = n.toLocaleString() + " character" + (n === 1 ? "" : "s");
  });

  urlInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); make(); } });
  goBtn.addEventListener("click", make);
  $("againBtn").addEventListener("click", reset);

  function setStatus(t, bad) { statusEl.textContent = t; statusEl.classList.toggle("bad", !!bad); }

  function explain(status, usedMode) {
    const lighter = usedMode === "exact"
      ? " Try Reading version — it's much lighter and usually gets through."
      : " Try again in a moment.";
    if (status === 502 || status === 503 || status === 504) return "That page was too heavy for the server." + lighter;
    if (status === 429) return "Too many at once. Wait a moment and try again.";
    if (status >= 500) return "The server had trouble with that page." + lighter;
    return "Couldn't make a PDF from that." + lighter;
  }

  /* ── the four-colour pass ──────────────────────────────────── */
  let inkTimer = null;
  const STAGES = ["Opening the page…", "Loading pictures…", "Laying out the paper…", "Printing to PDF…"];
  function startPress() {
    const inks = [...inkbar.querySelectorAll("i")]; let i = 0;
    setStatus(tab === "link" ? STAGES[0] : (tab === "docx" ? "Reading the document…" : "Setting the type…"), false);
    inkbar.classList.add("pressing"); inks[0].classList.add("wet");
    inkTimer = setInterval(() => {
      i += 1;
      if (i < inks.length) { inks[i].classList.add("wet"); if (tab === "link") setStatus(STAGES[i], false); }
      else if (i === inks.length + 3) setStatus("Still going — a slow page, or the server waking up.", false);
    }, 2600);
  }
  function stopPress() {
    clearInterval(inkTimer);
    inkbar.classList.remove("pressing");
    inkbar.querySelectorAll("i").forEach((n) => n.classList.remove("wet"));
  }

  /* ── make ──────────────────────────────────────────────────── */
  async function make() {
    const password = $("pw").value;
    let endpoint, payload, form = null;

    if (tab === "docx") {
      if (!docxFile) { setStatus("Choose a Word document first.", true); return; }
      if (!/\.docx$/i.test(docxFile.name)) {
        setStatus(/\.doc$/i.test(docxFile.name)
          ? "That's an older .doc file. Open it in Word, use Save As, and choose .docx."
          : "That isn't a Word document. Choose a file ending in .docx.", true);
        return;
      }
      form = new FormData();
      form.append("file", docxFile);
      form.append("size", size);
      form.append("password", password || "");
      endpoint = "/api/docx";
    }

    if (tab === "text" && !form) {
      const text = $("bodyText").value;
      if (!text.trim()) { setStatus("Type or paste some text first.", true); $("bodyText").focus(); return; }
      endpoint = "/api/text";
      payload = { text, size, password };
    } else if (!form) {
      const url = urlInput.value.trim();
      if (!url) { setStatus("Paste a link first.", true); urlInput.focus(); return; }
      lastUrl = url;
      endpoint = "/api/make";
      payload = { url, mode, size, password };
    }

    goBtn.disabled = true; goBtn.textContent = "Working…"; startPress();
    // If nothing has come back quickly, the server is almost certainly asleep.
    const wakeTimer = setTimeout(() => { $("waking").hidden = false; }, 4000);
    try {
      const res = form
        ? await fetch(endpoint, { method: "POST", body: form })
        : await fetch(endpoint, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
      const raw = await res.text();
      let data = null;
      if (raw) { try { data = JSON.parse(raw); } catch {} }
      if (!res.ok || !data || !data.id) throw new Error((data && data.detail) || explain(res.status, mode));

      current = data; blobCache = null;
      showResult(data);
      // Keep a copy on this device: server files vanish when it restarts.
      try {
        const blob = await getBlob();
        await histSave({ ...data, sourceUrl: tab === "text" ? "" : lastUrl, paper: size }, blob);
      } catch {}
    } catch (err) {
      const network = err instanceof TypeError ||
        /failed to fetch|networkerror|load failed/i.test((err && err.message) || "");
      setStatus(network
        ? "Couldn't reach the server. Check your connection, or it may still be waking up — try again in a moment."
        : (err && err.message) || "Something went wrong. Try again.", true);
    } finally {
      clearTimeout(wakeTimer); $("waking").hidden = true;
      stopPress(); goBtn.disabled = false; goBtn.textContent = "Make PDF";
      paintOnline();
    }
  }

  function showResult(d) {
    const styleName = { exact: "Exact copy", reader: "Reading version", lite: "Simplified", text: "Typed text", merged: "Merged", edited: "Edited", docx: "Word document" }[d.style] || "";
    $("fileName").textContent = d.filename;
    $("fileSpecs").textContent = `${d.pages} page${d.pages === 1 ? "" : "s"} · ${d.kb} KB · ${size} · ${styleName}`;
    const note = $("fileNote"); note.hidden = !d.note; note.textContent = d.note || "";
    $("lockTag").hidden = !d.locked;
    formPanel.hidden = true; historyPanel.hidden = true; resultPanel.hidden = false;
    setStatus("", false);
    resultPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    const local = !!d.localOnly;
    $("waBtn").hidden = local; $("mailBtn").hidden = local;
    $("printBtn").hidden = local;
    $("expiryLine") && ($("expiryLine").hidden = local);
    $("redoBtn").hidden = local || d.style === "text" || d.style === "merged" || d.style === "edited";
    $("redoBtn").textContent = d.style === "exact" ? "Redo as reading version" : "Redo as exact copy";
  }

  function reset() {
    resultPanel.hidden = true; formPanel.hidden = false;
    current = null; blobCache = null;
    urlInput.value = ""; $("bodyText").value = "";
    fileInput.value = ""; showFile(null);
    // A new document is a new decision: never carry a password over silently.
    $("pw").value = ""; $("pw").type = "password"; $("pwShow").textContent = "Show";
    $("charCount").textContent = "0 characters";
    document.querySelectorAll(".act").forEach((b) => b.classList.remove("done"));
    formPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function flash(btn, text) {
    const old = btn.textContent;
    btn.textContent = text; btn.classList.add("done");
    setTimeout(() => { btn.textContent = old; btn.classList.remove("done"); }, 1800);
  }

  async function getBlob() {
    if (blobCache) return blobCache;
    if (!current || !current.view) throw new Error("That file isn't available any more.");
    const r = await fetch(current.view);
    if (!r.ok) throw new Error("The file is no longer on the server.");
    blobCache = await r.blob();
    return blobCache;
  }

  /* ── result actions ────────────────────────────────────────── */
  $("shareBtn").addEventListener("click", async () => {
    try {
      const blob = await getBlob();
      const file = new File([blob], current.filename, { type: "application/pdf" });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], title: current.title });
        return;
      }
      if (navigator.share) { await navigator.share({ title: current.title, url: location.origin + current.view }); return; }
      setStatus("This browser can't open the share menu. Use Download instead.", true);
    } catch (err) {
      if (err && err.name === "AbortError") return;
      setStatus("Couldn't open the share menu. Use Download instead.", true);
    }
  });

  $("waBtn").addEventListener("click", () => {
    const msg = `${current.title}\n${location.origin + current.view}`;
    window.open("https://wa.me/?text=" + encodeURIComponent(msg), "_blank", "noopener");
  });

  $("mailBtn").addEventListener("click", () => {
    const subject = encodeURIComponent(current.title || "PDF");
    const body = encodeURIComponent(`${current.title}\n\n${location.origin + current.view}\n\n(Link works for about a day.)`);
    location.href = `mailto:?subject=${subject}&body=${body}`;
  });

  $("downloadBtn").addEventListener("click", async () => {
    try { saveBlob(await getBlob(), current.filename); flash($("downloadBtn"), "Saved"); }
    catch {
      if (!current.download) { setStatus("That file is no longer available.", true); return; }
      const a = document.createElement("a");
      a.href = current.download; a.download = current.filename;
      document.body.appendChild(a); a.click(); a.remove();
    }
  });

  $("printBtn").addEventListener("click", async () => {
    if (window.matchMedia("(pointer: coarse)").matches) {
      window.open(current.view, "_blank", "noopener"); return;
    }
    try {
      const src = URL.createObjectURL(await getBlob());
      const frame = document.createElement("iframe");
      frame.style.cssText = "position:fixed;width:0;height:0;border:0;opacity:0";
      frame.src = src;
      frame.onload = () => { frame.contentWindow.focus(); frame.contentWindow.print(); };
      document.body.appendChild(frame);
    } catch { window.open(current.view, "_blank", "noopener"); }
  });

  $("redoBtn").addEventListener("click", () => {
    if (!lastUrl) return;
    mode = mode === "exact" ? "reader" : "exact";
    savePrefs(); paintChoices();
    resultPanel.hidden = true; formPanel.hidden = false;
    tab = "link"; $("linkTab").hidden = false; $("textTab").hidden = true;
    urlInput.value = lastUrl;
    make();
  });

  /* ── page editor ───────────────────────────────────────────── */
  let editState = null;   // [{ index, rotate, cut }]

  $("editBtn").addEventListener("click", async () => {
    let blob;
    try { blob = await getBlob(); }
    catch { setStatus("That file is no longer available to edit.", true); return; }

    $("editBtn").disabled = true; $("editBtn").textContent = "Loading…";
    try {
      const pdfjs = await loadPdfJs();
      const doc = await pdfjs.getDocument({ data: await blob.arrayBuffer() }).promise;
      editState = Array.from({ length: doc.numPages }, (_, i) => ({ index: i, rotate: 0, cut: false }));
      resultPanel.hidden = true; $("editorPanel").hidden = false;
      await drawPages(doc);
      $("editorPanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch {
      setStatus("Couldn't open that PDF for editing. Password-protected files can't be edited.", true);
    } finally {
      $("editBtn").disabled = false; $("editBtn").textContent = "Edit pages";
    }
  });

  async function drawPages(doc) {
    const host = $("pageList");
    host.innerHTML = "";
    for (let i = 0; i < editState.length; i++) {
      const st = editState[i];
      const cell = document.createElement("div");
      cell.className = "pg" + (st.cut ? " cut" : "");
      const canvas = document.createElement("canvas");
      cell.appendChild(canvas);
      const num = document.createElement("p");
      num.className = "n"; num.textContent = `${i + 1} of ${editState.length}`;
      cell.appendChild(num);
      const ops = document.createElement("div");
      ops.className = "ops";
      ops.innerHTML = `<button data-o="up" title="Move earlier">↑</button>
                       <button data-o="rot" title="Rotate">⟳</button>
                       <button data-o="down" title="Move later">↓</button>
                       <button data-o="cut" class="x" title="Remove">✕</button>`;
      cell.appendChild(ops);
      host.appendChild(cell);

      const page = await doc.getPage(st.index + 1);
      const base = page.getViewport({ scale: 1 });
      const vp = page.getViewport({ scale: 150 / base.width, rotation: (base.rotation + st.rotate) % 360 });
      canvas.width = vp.width; canvas.height = vp.height;
      await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;

      ops.addEventListener("click", async (e) => {
        const op = e.target.closest("button")?.dataset.o;
        if (!op) return;
        if (op === "rot") st.rotate = (st.rotate + 90) % 360;
        if (op === "cut") st.cut = !st.cut;
        if (op === "up" && i > 0) [editState[i - 1], editState[i]] = [editState[i], editState[i - 1]];
        if (op === "down" && i < editState.length - 1) [editState[i + 1], editState[i]] = [editState[i], editState[i + 1]];
        await drawPages(doc);
        const kept = editState.filter((x) => !x.cut).length;
        $("editHint").textContent = kept === 0
          ? "Every page is marked for removal — keep at least one."
          : `${kept} page${kept === 1 ? "" : "s"} will be kept. Nothing changes until you tap Save.`;
      });
    }
  }

  $("editClose").addEventListener("click", () => {
    $("editorPanel").hidden = true; resultPanel.hidden = false; editState = null;
  });

  $("editSave").addEventListener("click", async () => {
    const keep = editState.filter((p) => !p.cut);
    if (!keep.length) { $("editHint").textContent = "Keep at least one page."; return; }
    const btn = $("editSave");
    btn.disabled = true; btn.textContent = "Saving…";
    try {
      const { PDFDocument, degrees } = await loadPdfLib();
      const src = await PDFDocument.load(await (await getBlob()).arrayBuffer());
      const out = await PDFDocument.create();
      const copied = await out.copyPages(src, keep.map((p) => p.index));
      copied.forEach((pg, i) => {
        if (keep[i].rotate) pg.setRotation(degrees((pg.getRotation().angle + keep[i].rotate) % 360));
        out.addPage(pg);
      });
      const blob = new Blob([await out.save()], { type: "application/pdf" });
      const name = (current.filename || "document.pdf").replace(/\.pdf$/i, "") + "-edited.pdf";
      $("editorPanel").hidden = true;
      editState = null;
      await finishLocal(blob, name, (current.title || "Document") + " (edited)", "edited");
    } catch (err) {
      $("editHint").textContent = "Couldn't save those changes. " + ((err && err.message) || "");
    } finally {
      btn.disabled = false; btn.textContent = "Save as a new PDF";
    }
  });

  /* ── arriving from the Android share sheet ─────────────────── */
  function pickLink(...cands) {
    for (const c of cands) {
      if (!c) continue;
      const hit = c.match(/https?:\/\/[^\s<>"]+/i);
      if (hit) return hit[0].replace(/[.,;:)\]]+$/, "");
      if (/^[a-z0-9.-]+\.[a-z]{2,}(\/|$)/i.test(c.trim())) return c.trim();
    }
    return "";
  }
  // A file arrived via the share sheet: pull it out of the handoff store and
  // drop the user straight into the Word tab with it ready to go.
  async function handleSharedFile() {
    try {
      const store = await caches.open("linkpdf-handoff");
      const res = await store.match("/__shared-file");
      if (!res) return false;
      await store.delete("/__shared-file");
      const blob = await res.blob();
      const name = decodeURIComponent(res.headers.get("X-Filename") || "document.docx");
      const file = new File([blob], name, {
        type: blob.type || "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      });

      pickTab("docx");
      showFile(file);
      if (!/\.docx$/i.test(name)) {
        setStatus(/\.doc$/i.test(name)
          ? "That's an older .doc file. Open it in Word, use Save As, and choose .docx."
          : "Only Word .docx files can be converted. Choose a different file.", true);
        return true;
      }
      make();
      return true;
    } catch {
      return false;
    }
  }

  function pickTab(which) {
    tab = which;
    document.querySelectorAll("[data-tab]").forEach((b) => {
      const on = b.dataset.tab === which;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    $("linkTab").hidden = which !== "link";
    $("textTab").hidden = which !== "text";
    $("docxTab").hidden = which !== "docx";
  }

  function handleShare() {
    const q = new URLSearchParams(location.search);

    if (q.get("shared") === "file") {
      history.replaceState(null, "", "/");
      handleSharedFile().then((ok) => {
        if (!ok) setStatus("That shared file didn't come through. Choose it here instead.", true);
      });
      return;
    }
    if (q.get("shared") === "empty" || q.get("shared") === "failed") {
      history.replaceState(null, "", "/");
      setStatus("That share didn't contain anything we could convert.", true);
      return;
    }

    if (!q.has("url") && !q.has("text") && !q.has("title")) return;
    const link = pickLink(q.get("url"), q.get("text"), q.get("title"));
    const shared = q.get("text") || "";
    history.replaceState(null, "", "/");
    $("pw").value = "";   // a shared link is a fresh document
    if (link) { urlInput.value = link; make(); return; }
    // Shared plain text with no link in it: send it to the text side instead.
    if (shared.trim()) {
      pickTab("text");
      $("bodyText").value = shared;
      $("charCount").textContent = shared.length.toLocaleString() + " characters";
      setStatus("That share had no link in it, so it's ready as text instead.", false);
      return;
    }
    setStatus("That share didn't contain anything to convert.", true);
  }

  /* ── install helpers ──────────────────────────────────────── */
  const ua = navigator.userAgent || "";
  const isAndroid = /Android/i.test(ua);
  const inAppBrowser =
    /FBAN|FBAV|FB_IAB|Instagram|WhatsApp|Line\/|Snapchat|Twitter|MicroMessenger|GSA\//i.test(ua) ||
    (isAndroid && /\bwv\b/.test(ua));
  const standalone = window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;

  if (inAppBrowser && !standalone) {
    $("browserAlert").hidden = false;
    $("copyForChrome").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(location.origin); flash($("copyForChrome"), "Copied"); }
      catch { prompt("Copy this, then open it in Chrome:", location.origin); }
    });
  }

  let installEvent = null;
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault(); installEvent = e;
    if (!standalone) $("installBtn").hidden = false;
  });
  $("installBtn").addEventListener("click", async () => {
    if (!installEvent) return;
    installEvent.prompt();
    const { outcome } = await installEvent.userChoice;
    installEvent = null;
    if (outcome === "accepted") $("installBtn").hidden = true;
  });
  window.addEventListener("appinstalled", () => {
    $("installBtn").hidden = true;
    setStatus("Installed. Open it once from your home screen, then it appears in your share menu.", false);
  });

  $("checkBtn").addEventListener("click", async () => {
    const box = $("checks");
    if (!box.hidden) { box.hidden = true; return; }
    box.hidden = false; box.innerHTML = "<div>Checking…</div>";
    const rows = [];
    const add = (ok, label) => rows.push({ ok, label });
    add(window.isSecureContext, "Secure connection");
    add(!inAppBrowser, inAppBrowser ? "Opened inside another app's browser" : "Opened in a real browser");
    add(/Chrome|Chromium/i.test(ua) && !/OPR|Firefox|SamsungBrowser/i.test(ua), "Using Chrome");
    add("serviceWorker" in navigator && !!(await navigator.serviceWorker.getRegistration()), "Background service running");
    let manifestOk = false;
    try {
      const m = await (await fetch("/manifest.json", { cache: "no-store" })).json();
      manifestOk = !!(m.share_target && m.share_target.action);
    } catch {}
    add(manifestOk, "Share-menu setting present");
    add(standalone, standalone ? "Running as an installed app" : "Not opened as an installed app");
    add(!!window.indexedDB, "History storage available");

    box.innerHTML = rows.map((r) =>
      `<div class="${r.ok ? "ok" : "no"}"><b>${r.ok ? "OK" : "NO"}</b><span>${escapeHtml(r.label)}</span></div>`).join("");

    let advice;
    if (inAppBrowser) advice = "You opened this by tapping a link inside another app (WhatsApp, Instagram and so on), which uses its own mini browser. Copy the address, open Chrome, paste it there, then install.";
    else if (!/Chrome|Chromium/i.test(ua) || /OPR|Firefox|SamsungBrowser/i.test(ua)) advice = "Only Chrome on Android can add an app to the share menu. Open this address in Chrome.";
    else if (!standalone) advice = "Tap the Install button above, or Chrome's ⋮ menu and choose Install app. If it only offers 'Add to Home screen', that makes a bookmark with no share menu entry — reload the page and try again.";
    else if (!manifestOk) advice = "The app couldn't read its settings. Reload the page while online, then reinstall.";
    else advice = "All good. If it's still missing, uninstall the icon, reload this page in Chrome, and install again.";
    const note = document.createElement("span");
    note.className = "fixit"; note.textContent = advice;
    box.appendChild(note);
  });

  /* ── Word file picking ─────────────────────────────────────── */
  let docxFile = null;
  const drop = $("drop"), fileInput = $("docxFile");

  function showFile(f) {
    docxFile = f || null;
    setStatus("", false);   // a new choice clears any previous complaint
    if (!f) {
      drop.classList.remove("has");
      $("dropMain").textContent = "Choose a .docx file";
      $("dropSub").textContent = "Or drag one here";
      return;
    }
    drop.classList.add("has");
    $("dropMain").textContent = f.name;
    $("dropSub").textContent = (f.size / 1024 < 1024)
      ? Math.round(f.size / 1024) + " KB — tap Make PDF"
      : (f.size / 1048576).toFixed(1) + " MB — tap Make PDF";
  }

  fileInput.addEventListener("change", (e) => showFile(e.target.files[0]));

  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => {
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) showFile(f);
  });

  /* ── offline ───────────────────────────────────────────────── */
  function paintOnline() {
    const off = !navigator.onLine;
    $("offlineAlert").hidden = !off;
    goBtn.disabled = off;
    goBtn.textContent = off ? "No connection" : "Make PDF";
    if (off) setStatus("", false);
  }
  window.addEventListener("online", paintOnline);
  window.addEventListener("offline", paintOnline);
  $("offlineHistory").addEventListener("click", async () => {
    historyPanel.hidden = false;
    await histRender();
    historyPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  /* ── start ─────────────────────────────────────────────────── */
  loadPrefs();
  paintChoices();
  histBadge();
  paintOnline();
  handleShare();

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
  }
})();
