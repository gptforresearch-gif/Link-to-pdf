"""
Link to PDF
Paste a link, get a printable PDF.
"""

import asyncio
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import trafilatura
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright
from pydantic import BaseModel

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
PDF_DIR = Path(os.environ.get("PDF_DIR", "/tmp/linkpdf"))
PDF_DIR.mkdir(parents=True, exist_ok=True)

FILE_LIFETIME_SECONDS = 24 * 60 * 60      # delete generated files after 24h
NAV_TIMEOUT_MS = 45_000                   # give slow sites time to load
MAX_PAGES = 1                             # one browser page at a time (512MB RAM)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Overlays that ruin a printed page: cookie walls, newsletter popups, chat bubbles,
# and sticky headers that would otherwise repeat on every single PDF page.
CLEANUP_CSS = """
  *[style*="position: fixed"], *[style*="position:fixed"] { position: static !important; }
  [class*="cookie" i], [id*="cookie" i],
  [class*="consent" i], [id*="consent" i],
  [class*="gdpr" i], [id*="gdpr" i],
  [class*="newsletter-popup" i], [class*="paywall" i],
  [class*="subscribe-modal" i], [class*="interstitial" i],
  [aria-label*="cookie" i], [role="dialog"], [role="alertdialog"],
  [class*="onetrust" i], #onetrust-banner-sdk, #onetrust-consent-sdk,
  [class*="sticky" i], [class*="floating" i],
  [class*="back-to-top" i], [class*="scroll-to-top" i],
  iframe[src*="doubleclick"], iframe[src*="googlesyndication"],
  ins.adsbygoogle {
      display: none !important;
  }
  header, nav, [class*="navbar" i], [class*="header" i] {
      position: static !important;
  }
  html, body { overflow: visible !important; height: auto !important; }
"""

READER_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><base href="{base}">
<style>
  @page {{ margin: 0; }}
  body {{
    font-family: Georgia, "Times New Roman", serif;
    font-size: 11.5pt;
    line-height: 1.62;
    color: #14181f;
    margin: 0;
    padding: 0;
  }}
  .sheet {{ padding: 0 4mm; }}
  .doc-head {{
    border-bottom: 1.5pt solid #14181f;
    padding-bottom: 10pt;
    margin-bottom: 20pt;
  }}
  h1.doc-title {{
    font-family: "Helvetica Neue", Arial, sans-serif;
    font-size: 21pt;
    line-height: 1.18;
    font-weight: 700;
    margin: 0 0 8pt;
    letter-spacing: -0.4pt;
  }}
  .doc-meta {{
    font-family: "Courier New", monospace;
    font-size: 8pt;
    text-transform: uppercase;
    letter-spacing: 0.9pt;
    color: #5b6672;
    word-break: break-all;
  }}
  h2, h3, h4 {{
    font-family: "Helvetica Neue", Arial, sans-serif;
    line-height: 1.3;
    margin: 20pt 0 7pt;
    page-break-after: avoid;
  }}
  h2 {{ font-size: 14pt; }}
  h3 {{ font-size: 12pt; }}
  p {{ margin: 0 0 11pt; orphans: 3; widows: 3; }}
  a {{ color: #14181f; text-decoration: underline; }}
  img {{ max-width: 100%; height: auto; page-break-inside: avoid; margin: 10pt 0; }}
  figure {{ margin: 12pt 0; }}
  figcaption {{
    font-family: "Helvetica Neue", Arial, sans-serif;
    font-size: 8.5pt; color: #5b6672; margin-top: 4pt;
  }}
  blockquote {{
    margin: 12pt 0; padding-left: 12pt;
    border-left: 2pt solid #c3c9cf; color: #3d4752;
  }}
  pre, code {{ font-family: "Courier New", monospace; font-size: 9.5pt; }}
  pre {{ background: #f1f3f5; padding: 8pt; overflow-wrap: break-word; white-space: pre-wrap; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 9.5pt; margin: 12pt 0; }}
  td, th {{ border: 0.5pt solid #c3c9cf; padding: 4pt 6pt; text-align: left; }}
  ul, ol {{ margin: 0 0 11pt; padding-left: 18pt; }}
  li {{ margin-bottom: 4pt; }}
</style></head>
<body><div class="sheet">
  <div class="doc-head">
    <h1 class="doc-title">{title}</h1>
    <div class="doc-meta">{source} &nbsp;·&nbsp; saved {date}</div>
  </div>
  {body}
</div></body></html>"""


# ----------------------------------------------------------------------------
# App + shared browser
# ----------------------------------------------------------------------------

app = FastAPI(title="Link to PDF", docs_url=None, redoc_url=None)

_browser = None
_playwright = None
_lock = asyncio.Semaphore(MAX_PAGES)


async def get_browser():
    """One Chromium instance, reused. Restarted if it ever dies."""
    global _browser, _playwright
    if _browser is not None and _browser.is_connected():
        return _browser
    if _playwright is None:
        _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-features=IsolateOrigins,site-per-process,TranslateUI",
            "--mute-audio",
            "--no-first-run",
            "--js-flags=--max-old-space-size=256",
        ]
    )
    return _browser


@app.on_event("shutdown")
async def _shutdown():
    global _browser, _playwright
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def sweep_old_files():
    cutoff = time.time() - FILE_LIFETIME_SECONDS
    for f in PDF_DIR.glob("*.pdf"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def normalise_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(400, "Paste a link first.")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    if re.search(r"\s", raw):
        raise HTTPException(400, "A link can't contain spaces. Check what you pasted.")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "That doesn't look like a web link.")
    host = parsed.hostname or ""
    # Must look like a real domain: something.something
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9\-._]*[a-z0-9])?", host, re.I) or "." not in host:
        raise HTTPException(400, "That doesn't look like a web address. Links look like example.com/page.")
    # Block requests aimed at the server's own network.
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local"):
        raise HTTPException(400, "That address can't be reached.")
    if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.)", host):
        raise HTTPException(400, "That address can't be reached.")
    return raw


def safe_filename(title: str, url: str) -> str:
    stem = title or urlparse(url).hostname or "page"
    stem = re.sub(r"[^\w\s-]", "", stem).strip()
    stem = re.sub(r"[\s_]+", "-", stem)[:60].strip("-")
    return (stem or "page") + ".pdf"


def count_pdf_pages(data: bytes) -> int:
    return max(1, len(re.findall(rb"/Type\s*/Page[^s]", data)))


PAGE_SIZES = {"A4": "A4", "Letter": "Letter", "Legal": "Legal"}


# ----------------------------------------------------------------------------
# The engine
# ----------------------------------------------------------------------------


async def render_pdf(url: str, mode: str, size: str) -> tuple[bytes, str]:
    """Render once; if the browser died between requests, relaunch and try again."""
    global _browser
    try:
        return await _render_once(url, mode, size)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        if "closed" not in str(exc).lower():
            raise
        try:
            if _browser:
                await _browser.close()
        except Exception:  # noqa: BLE001
            pass
        _browser = None
        return await _render_once(url, mode, size)


async def _render_once(url: str, mode: str, size: str) -> tuple[bytes, str]:
    browser = await get_browser()
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 1600},
        locale="en-US",
        java_script_enabled=True,
        ignore_https_errors=True,
        bypass_csp=True,          # strict sites otherwise reject our print cleanup
        service_workers="block",
    )
    try:
        page = await context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "ERR_NAME_NOT_RESOLVED" in msg:
                raise HTTPException(400, "That website doesn't exist. Check the spelling of the link.")
            if "ERR_CERT" in msg or "SSL" in msg:
                raise HTTPException(502, "That website's security certificate is broken.")
            if "ERR_CONNECTION_REFUSED" in msg or "ERR_CONNECTION_CLOSED" in msg:
                raise HTTPException(502, "That website refused the connection.")
            if "ERR_" in msg and "TIMED_OUT" not in msg:
                raise HTTPException(502, "That website couldn't be reached.")
            raise HTTPException(
                504, "That page took too long to open. It may be down or blocking us."
            )

        # Let late-loading content settle.
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        title = (await page.title()) or ""

        if mode == "reader":
            html = await page.content()
            extracted = trafilatura.extract(
                html,
                output_format="html",
                include_images=True,
                include_links=True,
                include_tables=True,
                favor_precision=False,
            )
            if not extracted or len(extracted) < 200:
                # Nothing article-like on this page: fall back to the exact copy.
                mode = "exact"
            else:
                body = re.sub(r"</?doc[^>]*>", "", extracted).strip()
                # The extractor often repeats the headline as the first heading.
                first = re.match(r"\s*<h[1-3][^>]*>(.*?)</h[1-3]>", body, re.S | re.I)
                if first:
                    plain = re.sub(r"<[^>]+>", "", first.group(1)).strip().lower()
                    if plain and plain in title.strip().lower():
                        body = body[first.end():].lstrip()
                doc = READER_TEMPLATE.format(
                    base=url,
                    title=title or urlparse(url).hostname,
                    source=urlparse(url).hostname,
                    date=time.strftime("%d %b %Y"),
                    body=body,
                )
                await page.set_content(doc, wait_until="load")
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                pdf = await page.pdf(
                    format=PAGE_SIZES.get(size, "A4"),
                    print_background=True,
                    margin={"top": "16mm", "bottom": "16mm", "left": "16mm", "right": "16mm"},
                    display_header_footer=True,
                    header_template="<div></div>",
                    footer_template=(
                        '<div style="width:100%;font-size:7pt;font-family:Helvetica;'
                        'color:#8a939c;padding:0 16mm;display:flex;'
                        'justify-content:space-between;">'
                        f'<span>{urlparse(url).hostname}</span>'
                        '<span class="pageNumber"></span></div>'
                    ),
                )
                return pdf, title

        # Exact mode: scroll through to trigger lazy-loaded images, tidy overlays.
        try:
            await page.add_style_tag(content=CLEANUP_CSS)
        except Exception:  # noqa: BLE001
            pass  # a locked-down site: capture it as-is rather than fail
        try:
            await page.evaluate(
                """async () => {
                    const step = window.innerHeight;
                    const max = Math.min(document.body.scrollHeight, 40000);
                    for (let y = 0; y < max; y += step) {
                        window.scrollTo(0, y);
                        await new Promise(r => setTimeout(r, 90));
                    }
                    window.scrollTo(0, 0);
                    await new Promise(r => setTimeout(r, 300));
                }"""
            )
        except Exception:
            pass

        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass

        await page.emulate_media(media="screen")
        pdf = await page.pdf(
            format=PAGE_SIZES.get(size, "A4"),
            print_background=True,
            margin={"top": "10mm", "bottom": "12mm", "left": "8mm", "right": "8mm"},
            scale=0.78,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=(
                '<div style="width:100%;font-size:7pt;font-family:Helvetica;'
                'color:#8a939c;padding:0 10mm;display:flex;'
                'justify-content:space-between;">'
                f'<span>{urlparse(url).hostname}</span>'
                '<span class="pageNumber"></span></div>'
            ),
        )
        return pdf, title
    finally:
        await context.close()


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------


class MakeRequest(BaseModel):
    url: str
    mode: str = "exact"      # "exact" | "reader"
    size: str = "A4"         # "A4" | "Letter" | "Legal"


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/api/make")
async def api_make(req: MakeRequest):
    url = normalise_url(req.url)
    mode = req.mode if req.mode in ("exact", "reader") else "exact"
    size = req.size if req.size in PAGE_SIZES else "A4"

    sweep_old_files()

    async with _lock:
        try:
            pdf, title = await render_pdf(url, mode, size)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "ERR_NAME_NOT_RESOLVED" in msg:
                raise HTTPException(400, "That website couldn't be found. Check the link.")
            if "ERR_CONNECTION" in msg or "ERR_ABORTED" in msg:
                raise HTTPException(502, "That website refused the connection.")
            raise HTTPException(500, "Couldn't turn that page into a PDF. Try the other style.")

    file_id = uuid.uuid4().hex[:12]
    (PDF_DIR / f"{file_id}.pdf").write_bytes(pdf)
    (PDF_DIR / f"{file_id}.name").write_text(safe_filename(title, url), encoding="utf-8")

    return JSONResponse(
        {
            "id": file_id,
            "filename": safe_filename(title, url),
            "title": title or urlparse(url).hostname,
            "source": urlparse(url).hostname,
            "pages": count_pdf_pages(pdf),
            "kb": round(len(pdf) / 1024),
            "view": f"/f/{file_id}",
            "download": f"/d/{file_id}",
        }
    )


def _lookup(file_id: str) -> tuple[Path, str]:
    if not re.fullmatch(r"[a-f0-9]{12}", file_id):
        raise HTTPException(404, "Not found")
    path = PDF_DIR / f"{file_id}.pdf"
    if not path.exists():
        raise HTTPException(404, "This PDF has expired. Make it again.")
    name_file = PDF_DIR / f"{file_id}.name"
    name = name_file.read_text(encoding="utf-8") if name_file.exists() else "page.pdf"
    return path, name


@app.get("/f/{file_id}")
async def view_pdf(file_id: str):
    path, name = _lookup(file_id)
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


@app.get("/d/{file_id}")
async def download_pdf(file_id: str):
    path, name = _lookup(file_id)
    return FileResponse(path, media_type="application/pdf", filename=name)


@app.get("/share")
async def share_target():
    """Android hands shared links here. Serves the app; the page reads the link."""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
