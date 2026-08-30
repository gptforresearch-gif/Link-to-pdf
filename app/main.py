"""
Link to PDF
Paste a link, get a printable PDF.
"""

import asyncio
import io
import os
import re
import subprocess
import tempfile
import time
import uuid
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import httpx
import mammoth
from PIL import Image, ImageFilter, ImageOps
import trafilatura
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
PDF_DIR = Path(os.environ.get("PDF_DIR", "/tmp/linkpdf"))
PDF_DIR.mkdir(parents=True, exist_ok=True)

FILE_LIFETIME_SECONDS = 24 * 60 * 60      # delete generated files after 24h
NAV_TIMEOUT_MS = 30_000                   # give slow sites time to load
MAX_PAGES = 1                             # one browser page at a time (512MB RAM)
MAX_READER_CHARS = 45_000                 # ~13 printed pages; beyond this, truncate
RESTART_EVERY = 10                        # relaunch Chromium periodically to free memory

# A heavy page can't be captured in full on a small server, so rather than fail we
# step down: the style asked for, then something lighter, then bare bones.
# Budgets are seconds, and must total well under the host's request timeout.
CASCADE = {
    "exact":  [("exact", 32), ("reader", 22), ("lite", 28)],
    "reader": [("reader", 35), ("lite", 28)],
}
DOWNGRADE_NOTE = {
    "reader": "That page was too heavy to copy in full, so this is the reading version.",
    "lite":   "That page was very heavy, so this is a simplified copy without pictures.",
}

# Ad, tracker and video traffic: useless in a PDF, and the main reason a heavy
# news page exhausts a small server. Blocking it cuts memory and time enormously.
BLOCKED_HOSTS = (
    "doubleclick.net", "googlesyndication", "googletagmanager", "google-analytics",
    "adservice.google", "adsystem", "amazon-adsystem", "adnxs.com", "criteo",
    "pubmatic", "rubiconproject", "openx.net", "taboola", "outbrain", "mgid.com",
    "smartadserver", "sharethrough", "teads.tv", "moatads", "zedo.com", "3lift.com",
    "casalemedia", "indexww.com", "sovrn.com", "bidswitch", "adform.net",
    "scorecardresearch", "chartbeat", "quantserve", "hotjar", "segment.io",
    "mixpanel", "amplitude.com", "clevertap", "moengage", "izooto", "onesignal",
    "pushengage", "vidoomy", "jwpsrv.com", "brightcove", "connatix", "youtube.com/embed",
    "facebook.net", "connect.facebook", "platform.twitter", "ads-twitter",
)
BLOCKED_TYPES = {"media", "websocket", "eventsource"}

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
  }}
  /* Keep a heading with its text, but not when headings run back-to-back
     (an index page), or every one lands on a page of its own. */
  h2:has(+ p), h3:has(+ p), h4:has(+ p) {{ page-break-after: avoid; }}
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
_renders = 0
_lock = asyncio.Semaphore(MAX_PAGES)


async def get_browser():
    """One Chromium instance, reused. Relaunched periodically and if it dies."""
    global _browser, _playwright, _renders
    if _browser is not None and _browser.is_connected() and _renders < RESTART_EVERY:
        return _browser
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:  # noqa: BLE001
            pass
        _browser = None
    _renders = 0
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
            "--disable-accelerated-2d-canvas",
            "--disable-software-rasterizer",
            "--disable-background-timer-throttling",
            "--renderer-process-limit=2",
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


def encrypt_pdf(data: bytes, password: str) -> bytes:
    """Lock the PDF with AES-256. Opening it then requires the password."""
    try:
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
        writer.encrypt(user_password=password, algorithm="AES-256")
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception:  # noqa: BLE001
        raise HTTPException(500, "Couldn't put a password on that PDF.")


def text_to_html(text: str, title: str, source: str = "typed text") -> str:
    """Blank lines separate paragraphs; single line breaks are kept."""
    blocks = re.split(r"\n\s*\n", text.strip())
    body = "".join(
        "<p>" + escape(b.strip()).replace("\n", "<br>") + "</p>"
        for b in blocks if b.strip()
    )
    return READER_TEMPLATE.format(
        base="about:blank",
        title=escape(title) if title else "Untitled note",
        source=source,
        date=time.strftime("%d %b %Y"),
        body=body or "<p></p>",
    )


async def render_html_pdf(html: str, size: str) -> bytes:
    """Render ready-made HTML. No web page is fetched, so this is cheap."""
    browser = await get_browser()
    global _renders
    _renders += 1
    context = await browser.new_context(viewport={"width": 1000, "height": 800})
    try:
        page = await context.new_page()
        await page.set_content(html, wait_until="load")
        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        return await page.pdf(
            format=PAGE_SIZES.get(size, "A4"),
            print_background=True,
            margin={"top": "18mm", "bottom": "18mm", "left": "18mm", "right": "18mm"},
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=(
                '<div style="width:100%;font-size:7pt;font-family:Helvetica;'
                'color:#8a939c;padding:0 18mm;text-align:right;">'
                '<span class="pageNumber"></span></div>'
            ),
        )
    finally:
        await context.close()


async def render_text_pdf(text: str, title: str, size: str, source: str = "typed text") -> bytes:
    return await render_html_pdf(text_to_html(text, title, source), size)


def _looks_like_index(html: str) -> bool:
    """A listing page is mostly headings and links with very little prose."""
    headings = len(re.findall(r"<h[1-6][\s>]", html, re.I))
    paras = re.findall(r"<p[\s>](.*?)</p>", html, re.I | re.S)
    prose = sum(len(re.sub(r"<[^>]+>", "", p).strip()) for p in paras)
    if headings >= 8 and prose < headings * 120:
        return True
    return len(paras) < 3 and headings > 3


PAGE_SIZES = {"A4": "A4", "Letter": "Letter", "Legal": "Legal"}


# ----------------------------------------------------------------------------
# The engine
# ----------------------------------------------------------------------------


async def render_pdf(url: str, mode: str, size: str) -> tuple[bytes, str, str]:
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


def make_blocker(mode: str):
    """Drop ads, trackers and video. In 'lite' mode drop images and fonts too."""
    heavy = {"image", "font", "media"} if mode == "lite" else set()

    async def block(route):
        req = route.request
        try:
            if req.resource_type in BLOCKED_TYPES or req.resource_type in heavy:
                return await route.abort()
            url = req.url.lower()
            if any(h in url for h in BLOCKED_HOSTS):
                return await route.abort()
            await route.continue_()
        except Exception:  # noqa: BLE001
            pass

    return block


async def _render_once(url: str, mode: str, size: str) -> tuple[bytes, str, str]:
    global _renders
    browser = await get_browser()
    _renders += 1
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 1000},
        locale="en-US",
        java_script_enabled=True,
        ignore_https_errors=True,
        bypass_csp=True,          # strict sites otherwise reject our print cleanup
        service_workers="block",
    )
    try:
        await context.route("**/*", make_blocker(mode))
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

        # Let late-loading content settle. Ad-heavy pages never truly go idle,
        # so this is a short courtesy wait, not a requirement.
        if mode != "lite":
            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
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
                # No article here (a homepage or index). Capture what's on screen
                # without the expensive scroll pass, which is what costs us.
                mode = "lite"
            elif _looks_like_index(extracted):
                # Mostly headlines with little prose: a listing page. Printing it
                # as an "article" gives dozens of pages of links, which is useless.
                mode = "lite"
            else:
                body = re.sub(r"</?doc[^>]*>", "", extracted).strip()
                # An index or homepage can extract into hundreds of pages of
                # headlines. Nobody wants to print that.
                if len(body) > MAX_READER_CHARS:
                    cut = body[:MAX_READER_CHARS]
                    cut = cut[: cut.rfind("</p>") + 4] or cut
                    body = cut + (
                        '<p style="margin-top:18pt;font-family:Helvetica;font-size:9pt;'
                        'color:#5b6672">[This page was very long and has been cut short '
                        'here. Open the original for the rest.]</p>'
                    )
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
                return pdf, title, "reader"

        # Exact mode: scroll through to trigger lazy-loaded images, tidy overlays.
        try:
            await page.add_style_tag(content=CLEANUP_CSS)
        except Exception:  # noqa: BLE001
            pass  # a locked-down site: capture it as-is rather than fail

        if mode != "lite":
            try:
                await page.evaluate(
                    """async () => {
                        const step = window.innerHeight;
                        const max = Math.min(document.body.scrollHeight, step * 12);
                        for (let y = 0; y < max; y += step) {
                            window.scrollTo(0, y);
                            await new Promise(r => setTimeout(r, 60));
                        }
                        window.scrollTo(0, 0);
                        await new Promise(r => setTimeout(r, 200));
                    }"""
                )
            except Exception:
                pass

            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
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
        return pdf, title, mode
    finally:
        await context.close()


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------


class MakeRequest(BaseModel):
    url: str
    mode: str = "exact"      # "exact" | "reader"
    size: str = "A4"         # "A4" | "Letter" | "Legal"
    password: str | None = None


class TextRequest(BaseModel):
    text: str
    title: str = ""
    size: str = "A4"
    password: str | None = None


def _store(pdf: bytes, name: str) -> str:
    file_id = uuid.uuid4().hex[:12]
    (PDF_DIR / f"{file_id}.pdf").write_bytes(pdf)
    (PDF_DIR / f"{file_id}.name").write_text(name, encoding="utf-8")
    return file_id


def _clean_password(pw: str | None) -> str | None:
    pw = (pw or "").strip()
    if not pw:
        return None
    if len(pw) > 128:
        raise HTTPException(400, "That password is too long.")
    return pw


@app.post("/api/text")
async def api_text(req: TextRequest):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "Type or paste some text first.")
    if len(text) > 200_000:
        raise HTTPException(400, "That's a lot of text. Trim it to about 200,000 characters.")
    size = req.size if req.size in PAGE_SIZES else "A4"
    password = _clean_password(req.password)

    sweep_old_files()
    async with _lock:
        try:
            pdf = await asyncio.wait_for(
                render_text_pdf(text, req.title.strip(), size), timeout=40
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "That took too long. Try shorter text.")
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            raise HTTPException(500, "Couldn't turn that text into a PDF.")

    pages = count_pdf_pages(pdf)
    if password:
        pdf = encrypt_pdf(pdf, password)

    stem = req.title.strip() or text[:40]
    name = safe_filename(stem, "https://typed.text")
    file_id = _store(pdf, name)
    return JSONResponse({
        "id": file_id,
        "filename": name,
        "title": req.title.strip() or "Typed text",
        "source": "typed text",
        "pages": pages,
        "kb": round(len(pdf) / 1024),
        "style": "text",
        "locked": bool(password),
        "note": None,
        "view": f"/f/{file_id}",
        "download": f"/d/{file_id}",
    })


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/api/make")
async def api_make(req: MakeRequest):
    url = normalise_url(req.url)
    mode = req.mode if req.mode in ("exact", "reader") else "exact"
    size = req.size if req.size in PAGE_SIZES else "A4"

    sweep_old_files()

    plan = CASCADE.get(mode, CASCADE["exact"])
    pdf = title = None
    used = mode
    last_error = None

    async with _lock:
        for attempt, budget in plan:
            try:
                pdf, title, effective = await asyncio.wait_for(
                    render_pdf(url, attempt, size), timeout=budget
                )
                used = effective
                break
            except asyncio.TimeoutError:
                last_error = "heavy"
                # A cancelled render leaves Chromium holding memory. Force a
                # fresh browser before the next attempt, or that one dies too.
                global _renders
                _renders = RESTART_EVERY
                continue
            except HTTPException as exc:
                # A bad link or dead site won't improve on retry.
                if exc.status_code < 500:
                    raise
                last_error = "server"
                continue
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "ERR_NAME_NOT_RESOLVED" in msg:
                    raise HTTPException(400, "That website doesn't exist. Check the spelling of the link.")
                last_error = "server"
                continue

    if pdf is None:
        if last_error == "heavy":
            raise HTTPException(
                504,
                "That page is too heavy for this server even simplified. "
                "It's usually an endless-scrolling homepage — try a link to a single article instead.",
            )
        raise HTTPException(500, "Couldn't turn that page into a PDF.")

    note = None
    if used != mode:
        if last_error == "heavy":
            note = DOWNGRADE_NOTE.get(used)
        elif used == "lite":
            note = ("That page is a list of headlines rather than an article, "
                    "so this is a plain copy of the page itself.")
        else:
            note = DOWNGRADE_NOTE.get(used)

    file_id = _store(pdf, safe_filename(title, url))
    pages = count_pdf_pages(pdf)
    password = _clean_password(req.password)
    if password:
        pdf = encrypt_pdf(pdf, password)
        (PDF_DIR / f"{file_id}.pdf").write_bytes(pdf)

    return JSONResponse(
        {
            "id": file_id,
            "filename": safe_filename(title, url),
            "title": title or urlparse(url).hostname,
            "source": urlparse(url).hostname,
            "pages": pages,
            "kb": round(len(pdf) / 1024),
            "style": used,
            "locked": bool(password),
            "note": note,
            "view": f"/f/{file_id}",
            "download": f"/d/{file_id}",
        }
    )


MAX_DOCX_BYTES = 12 * 1024 * 1024      # 12 MB: generous for text, keeps memory sane

# Word styles the converter doesn't recognise on its own. Without these, a
# document's own title and pull-quotes come out as ordinary body text.
DOCX_STYLE_MAP = """
p[style-name='Title'] => h1.doc-main:fresh
p[style-name='Subtitle'] => p.doc-sub:fresh
p[style-name='Quote'] => blockquote:fresh
p[style-name='Intense Quote'] => blockquote:fresh
p[style-name='Caption'] => p.doc-cap:fresh
r[style-name='Strong'] => strong
"""

DOCX_EXTRA_CSS = (
    "<style>"
    ".doc-sub{color:#5b6672;font-size:12pt;margin:-4pt 0 14pt;font-style:italic}"
    ".doc-cap{color:#5b6672;font-size:9pt;margin-top:-6pt}"
    "</style>"
)


@app.post("/api/docx")
async def api_docx(
    file: UploadFile = File(...),
    size: str = Form("A4"),
    password: str = Form(""),
):
    name = (file.filename or "").lower()
    if name.endswith(".doc"):
        raise HTTPException(
            400,
            "That's an older .doc file. Open it in Word and use Save As to make a .docx, then try again.",
        )
    if not name.endswith(".docx"):
        raise HTTPException(400, "That isn't a Word document. Choose a file ending in .docx.")

    data = await file.read()
    if not data:
        raise HTTPException(400, "That file is empty.")
    if len(data) > MAX_DOCX_BYTES:
        raise HTTPException(400, "That document is too big. The limit is 12 MB.")
    # A .docx is a zip; anything else here is mislabelled or corrupt.
    if not data.startswith(b"PK"):
        raise HTTPException(400, "That file isn't a real Word document, or it's damaged.")

    try:
        result = mammoth.convert_to_html(io.BytesIO(data), style_map=DOCX_STYLE_MAP)
        body = result.value or ""
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Couldn't read that Word document. It may be damaged or password-protected.")

    if not body.strip():
        raise HTTPException(400, "That document appears to be empty.")

    # If the document carries its own title, use it and take it out of the body
    # so the heading isn't printed twice.
    title = re.sub(r"\.docx$", "", file.filename or "Document", flags=re.I)
    own = re.search(r'<h1 class="doc-main">(.*?)</h1>', body, re.S | re.I)
    if own:
        plain = re.sub(r"<[^>]+>", "", own.group(1)).strip()
        if plain:
            title = plain
        body = body[: own.start()] + body[own.end():]
    # Reuse the reading-mode stylesheet so it prints like everything else.
    doc = READER_TEMPLATE.format(
        base="about:blank",
        title=escape(title),
        source="word document",
        date=time.strftime("%d %b %Y"),
        body=DOCX_EXTRA_CSS + body,
    )

    paper = size if size in PAGE_SIZES else "A4"
    pw = _clean_password(password)

    sweep_old_files()
    async with _lock:
        try:
            pdf = await asyncio.wait_for(render_html_pdf(doc, paper), timeout=45)
        except asyncio.TimeoutError:
            raise HTTPException(504, "That document took too long. Try a shorter one.")
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            raise HTTPException(500, "Couldn't turn that document into a PDF.")

    pages = count_pdf_pages(pdf)
    if pw:
        pdf = encrypt_pdf(pdf, pw)

    filename = safe_filename(title, "https://word.doc")
    file_id = _store(pdf, filename)
    return JSONResponse({
        "id": file_id,
        "filename": filename,
        "title": title,
        "source": "word document",
        "pages": pages,
        "kb": round(len(pdf) / 1024),
        "style": "docx",
        "locked": bool(pw),
        "note": None,
        "view": f"/f/{file_id}",
        "download": f"/d/{file_id}",
    })


# ----------------------------------------------------------------------------
# Voice notes
# ----------------------------------------------------------------------------

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
AUDIO_PASSCODE = os.environ.get("AUDIO_PASSCODE", "").strip()
AUDIO_MODEL = os.environ.get("AUDIO_MODEL", "openai/whisper-1").strip()

MAX_AUDIO_BYTES = 20 * 1024 * 1024     # the service itself refuses above 25 MB
AUDIO_TIMEOUT_S = 115                  # the service gives up upstream at 60s

AUDIO_TYPES = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
    ".wav": "audio/wav", ".webm": "audio/webm", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac",
    ".aac": "audio/aac", ".mpeg": "audio/mpeg", ".mpga": "audio/mpeg",
}

# Whisper takes a two-letter hint. Bhojpuri has no code it recognises, so it is
# sent as Hindi, which is the closest thing it has been trained on.
AUDIO_LANGS = {
    "auto": None, "en": "en", "hi": "hi", "gu": "gu", "bho": "hi",
}


@app.get("/api/features")
async def api_features():
    """Lets the page show the right thing instead of failing mysteriously."""
    return {"audio": bool(OPENROUTER_KEY), "audioPasscode": bool(AUDIO_PASSCODE)}


async def transcribe(data: bytes, filename: str, mime: str, lang: str | None) -> tuple[str, dict]:
    form = {"model": (None, AUDIO_MODEL)}
    if lang:
        form["language"] = (None, lang)
    files = {"file": (filename, data, mime), **form}
    async with httpx.AsyncClient(timeout=AUDIO_TIMEOUT_S) as client:
        r = await client.post(
            "https://openrouter.ai/api/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
            files=files,
        )
    if r.status_code == 401:
        raise HTTPException(502, "The transcription key was rejected. Check OPENROUTER_API_KEY on the server.")
    if r.status_code == 402:
        raise HTTPException(502, "The OpenRouter account is out of credit.")
    if r.status_code == 429:
        raise HTTPException(429, "Too many transcriptions at once. Wait a moment and try again.")
    if r.status_code >= 400:
        raise HTTPException(502, "The transcription service refused that file.")
    body = r.json()
    text = (body.get("text") or "").strip()
    return text, (body.get("usage") or {})


@app.post("/api/audio")
async def api_audio(
    file: UploadFile = File(...),
    language: str = Form("auto"),
    passcode: str = Form(""),
    size: str = Form("A4"),
    password: str = Form(""),
):
    if not OPENROUTER_KEY:
        raise HTTPException(
            503,
            "Voice notes aren't switched on. The server needs an OPENROUTER_API_KEY setting.",
        )
    if AUDIO_PASSCODE and passcode.strip() != AUDIO_PASSCODE:
        raise HTTPException(403, "Wrong passcode. Voice notes are limited to keep the bill down.")

    name = (file.filename or "voice-note").strip()
    ext = os.path.splitext(name.lower())[1]
    if ext not in AUDIO_TYPES:
        raise HTTPException(
            400,
            "That isn't an audio file we can read. Voice notes, MP3, M4A, WAV and OGG all work.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(400, "That audio file is empty.")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(400, "That recording is too big. The limit is 20 MB — about 40 minutes of voice note.")

    # WhatsApp voice notes arrive as .opus, which the service doesn't recognise
    # by name even though it reads the contents fine.
    send_name = re.sub(r"\.opus$", ".ogg", name, flags=re.I)

    try:
        text, usage = await asyncio.wait_for(
            transcribe(data, send_name, AUDIO_TYPES[ext], AUDIO_LANGS.get(language)),
            timeout=AUDIO_TIMEOUT_S + 5,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "That recording took too long to transcribe. Try a shorter one.")
    except HTTPException:
        raise
    except httpx.RequestError:
        raise HTTPException(502, "Couldn't reach the transcription service. Try again in a moment.")

    if not text:
        raise HTTPException(422, "No speech was found in that recording.")

    title = re.sub(r"\.[a-z0-9]+$", "", name, flags=re.I) or "Voice note"
    paper = size if size in PAGE_SIZES else "A4"
    pw = _clean_password(password)

    sweep_old_files()
    async with _lock:
        try:
            pdf = await asyncio.wait_for(
                render_text_pdf(text, title, paper, "voice note"), timeout=40
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "That transcript took too long to lay out.")
        except Exception:  # noqa: BLE001
            raise HTTPException(500, "Couldn't turn that transcript into a PDF.")

    pages = count_pdf_pages(pdf)
    if pw:
        pdf = encrypt_pdf(pdf, pw)

    filename = safe_filename(title, "https://voice.note")
    file_id = _store(pdf, filename)

    secs = usage.get("seconds")
    cost = usage.get("cost")
    bits = []
    if secs:
        bits.append(f"{int(secs) // 60}m {int(secs) % 60}s of audio")
    if cost:
        bits.append(f"cost ${float(cost):.4f}")
    if language == "bho":
        bits.append("Bhojpuri was transcribed using the Hindi model")

    return JSONResponse({
        "id": file_id,
        "filename": filename,
        "title": title,
        "source": "voice note",
        "pages": pages,
        "kb": round(len(pdf) / 1024),
        "style": "audio",
        "locked": bool(pw),
        "note": (" · ".join(bits) + ".") if bits else None,
        "words": len(text.split()),
        "view": f"/f/{file_id}",
        "download": f"/d/{file_id}",
    })


# ----------------------------------------------------------------------------
# Photographs of paper documents
# ----------------------------------------------------------------------------

MAX_SCAN_PAGES = 5                     # 0.1 CPU: more than this won't finish in time
MAX_SCAN_BYTES = 12 * 1024 * 1024      # per photo
SCAN_LONG_EDGE = 2000                  # OCR gains nothing above this, and costs time
SCAN_BUDGET_S = 105

SCAN_LANGS = {
    "eng": "eng", "hin": "hin", "guj": "guj",
    "eng+hin": "eng+hin", "eng+guj": "eng+guj",
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif", ".gif", ".tif", ".tiff"}


def prepare_scan(raw: bytes, enhance: bool) -> bytes:
    """Straighten the orientation, optionally clean it up, and shrink it."""
    im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(im)          # honour the camera's rotation flag
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if max(im.size) > SCAN_LONG_EDGE:
        ratio = SCAN_LONG_EDGE / max(im.size)
        im = im.resize((int(im.width * ratio), int(im.height * ratio)), Image.LANCZOS)
    if enhance:
        # Make a phone photo look like something off a scanner.
        im = ImageOps.grayscale(im)
        im = ImageOps.autocontrast(im, cutoff=2)
        im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=110, threshold=3))
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=82, optimize=True)
    return out.getvalue()


def ocr_page(jpeg: bytes, lang: str, searchable: bool) -> bytes:
    """One image in, a one-page PDF out. With OCR it carries an invisible
    text layer, so the words can be selected and searched."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "page.jpg"
        src.write_bytes(jpeg)
        if not searchable:
            im = Image.open(io.BytesIO(jpeg))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="PDF", resolution=150)
            return buf.getvalue()
        stem = Path(tmp) / "page"
        proc = subprocess.run(
            ["tesseract", str(src), str(stem), "-l", lang, "--psm", "1", "pdf"],
            capture_output=True, timeout=90,
        )
        made = Path(f"{stem}.pdf")
        if proc.returncode != 0 or not made.exists():
            raise HTTPException(500, "Couldn't read the text on that page.")
        return made.read_bytes()


@app.post("/api/scan")
async def api_scan(
    files: list[UploadFile] = File(...),
    language: str = Form("eng"),
    searchable: str = Form("1"),
    enhance: str = Form("1"),
    size: str = Form("A4"),
    password: str = Form(""),
):
    if not files:
        raise HTTPException(400, "Choose at least one photo.")
    if len(files) > MAX_SCAN_PAGES:
        raise HTTPException(
            400,
            f"That's {len(files)} photos. This free server can manage {MAX_SCAN_PAGES} at a time — "
            "do them in batches and merge the results from History.",
        )

    lang = SCAN_LANGS.get(language, "eng")
    want_text = searchable != "0"
    clean = enhance != "0"

    pages: list[bytes] = []
    for f in files:
        ext = os.path.splitext((f.filename or "").lower())[1]
        if ext not in IMAGE_EXT:
            raise HTTPException(400, f"'{f.filename}' isn't a photo we can read. Use JPG or PNG.")
        raw = await f.read()
        if not raw:
            raise HTTPException(400, f"'{f.filename}' is empty.")
        if len(raw) > MAX_SCAN_BYTES:
            raise HTTPException(400, f"'{f.filename}' is bigger than 12 MB.")
        pages.append(raw)

    loop = asyncio.get_running_loop()

    def build() -> tuple[bytes, int]:
        writer = PdfWriter()
        for raw in pages:
            try:
                prepped = prepare_scan(raw, clean)
            except Exception:  # noqa: BLE001
                raise HTTPException(400, "One of those photos couldn't be opened.")
            one = ocr_page(prepped, lang, want_text)
            reader = PdfReader(io.BytesIO(one))
            for pg in reader.pages:
                writer.add_page(pg)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue(), len(writer.pages)

    async with _lock:
        try:
            pdf, count = await asyncio.wait_for(
                loop.run_in_executor(None, build), timeout=SCAN_BUDGET_S
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                504,
                "Reading the text took too long. Try fewer photos, or switch off "
                "'make the text searchable' for a quicker plain scan.",
            )
        except HTTPException:
            raise
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Reading the text took too long on one page.")
        except Exception:  # noqa: BLE001
            raise HTTPException(500, "Couldn't turn those photos into a PDF.")

    pw = _clean_password(password)
    if pw:
        pdf = encrypt_pdf(pdf, pw)

    stem = re.sub(r"\.[a-z0-9]+$", "", files[0].filename or "scan", flags=re.I)
    filename = safe_filename(stem or "scan", "https://scan")
    file_id = _store(pdf, filename)

    return JSONResponse({
        "id": file_id,
        "filename": filename,
        "title": stem or "Scan",
        "source": "photo",
        "pages": count,
        "kb": round(len(pdf) / 1024),
        "style": "scan",
        "locked": bool(pw),
        "note": ("The text has been read, so you can select and search it."
                 if want_text else "Plain image scan — the text isn't selectable."),
        "view": f"/f/{file_id}",
        "download": f"/d/{file_id}",
    })


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


@app.post("/share-file")
async def share_file_fallback():
    """Reached only if the service worker isn't active. Send them to the app."""
    return RedirectResponse("/?shared=failed", status_code=303)


@app.get("/share-file")
async def share_file_get():
    return RedirectResponse("/", status_code=303)


@app.get("/share")
async def share_target():
    """Android hands shared links here. Serves the app; the page reads the link."""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
