"""Stage 1 - Capture: load the page in headless Chromium and record everything
needed to rebuild it: DOM + computed styles at desktop and mobile widths,
full-page screenshots, images, SVGs and web fonts.

No LLM is used here; this stage is deterministic and free.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from ..config import settings
from ..events import Reporter
from .assets import AssetStore, build_fonts, parse_font_faces

EXTRACT_JS = (Path(__file__).parent / "extract.js").read_text(encoding="utf-8")

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
BLOCK_TITLES = re.compile(r"just a moment|attention required|access denied|are you a robot|verify you are human|security check", re.I)
NO_MOTION_CSS = """
*, *::before, *::after {
  animation-duration: 0s !important; animation-delay: 0s !important;
  transition-duration: 0s !important; transition-delay: 0s !important;
  scroll-behavior: auto !important; caret-color: transparent !important;
}
"""
CONSENT_BUTTON = re.compile(r"^(accept( all)?( cookies)?|allow all|i agree|agree|got it|ok(ay)?|continue)$", re.I)


class CaptureError(RuntimeError):
    """Raised with a user-facing message when a page cannot be captured."""


@dataclass
class Capture:
    url: str
    final_url: str
    title: str
    meta: dict
    desktop: dict            # raw extraction at desktop width
    mobile: dict             # raw extraction at mobile width
    desktop_png: Path
    mobile_png: Path
    assets: AssetStore
    fonts_css: str = ""
    fonts: list = field(default_factory=list)
    canvas_shots: dict = field(default_factory=dict)  # node id -> local asset path
    duration_s: float = 0.0


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise CaptureError("Please enter a website URL.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc or "." not in p.netloc and not p.netloc.startswith("localhost"):
        raise CaptureError(f"'{url}' does not look like a valid public URL.")
    return url


def _auto_scroll(page, max_steps: int = 40) -> None:
    """Scroll through the page to trigger lazy-loaded images and scroll animations."""
    last = -1
    for _ in range(max_steps):
        height = page.evaluate("document.documentElement.scrollHeight")
        y = page.evaluate("window.scrollY + window.innerHeight")
        if y >= height or y == last:
            break
        last = y
        page.mouse.wheel(0, int(page.viewport_size["height"] * 0.85))
        page.wait_for_timeout(180)
    page.wait_for_timeout(400)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)


def _settle(page) -> None:
    page.add_style_tag(content=NO_MOTION_CSS)
    page.evaluate("""() => { try { document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} }); } catch (e) {} }""")
    page.wait_for_timeout(250)


def _dismiss_consent(page) -> None:
    try:
        for btn in page.get_by_role("button").all()[:60]:
            try:
                txt = (btn.inner_text(timeout=300) or "").strip()
            except Exception:
                continue
            if CONSENT_BUTTON.match(txt) and btn.is_visible():
                btn.click(timeout=1200)
                page.wait_for_timeout(500)
                return
    except Exception:
        pass


def _screenshot(page, path: Path) -> None:
    h = min(page.evaluate("document.documentElement.scrollHeight"), settings.max_screenshot_height)
    w = page.viewport_size["width"]
    page.screenshot(path=str(path), full_page=True, clip={"x": 0, "y": 0, "width": w, "height": h}, animations="disabled")


def capture(url: str, out_dir: Path, public_dir: Path, reporter: Optional[Reporter] = None) -> Capture:
    from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout, sync_playwright

    rep = reporter or Reporter()
    url = normalize_url(url)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    responses: dict[str, tuple[bytes, str]] = {}
    css_texts: list[tuple[str, str]] = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=settings.headless, args=["--disable-blink-features=AutomationControlled"])
        except PWError as e:
            raise CaptureError(
                "Could not start Chromium. Run `playwright install chromium` once, then retry.\n" + str(e)[:300]
            )
        context = browser.new_context(
            viewport={"width": settings.desktop_width, "height": settings.desktop_height},
            user_agent=DESKTOP_UA, locale="en-US", ignore_https_errors=True, device_scale_factor=1,
        )
        page = context.new_page()

        def on_response(resp):
            try:
                ct = (resp.headers.get("content-type") or "").lower()
                rtype = resp.request.resource_type
                if resp.status >= 400:
                    return
                if rtype == "stylesheet" or "text/css" in ct:
                    css_texts.append((resp.url, resp.text()[:2_000_000]))
                elif rtype in ("image", "font") or ct.startswith(("image/", "font/")):
                    body = resp.body()
                    if len(body) <= 8 * 1024 * 1024:
                        responses[resp.url] = (body, ct)
            except Exception:
                pass

        page.on("response", on_response)

        rep.step("capture", f"Opening {url}")
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)
        except PWTimeout:
            browser.close()
            raise CaptureError(f"Timed out after {settings.nav_timeout_ms // 1000}s loading {url}.")
        except PWError as e:
            browser.close()
            msg = str(e).split("\n")[0]
            raise CaptureError(f"Could not open {url}: {msg}")
        if resp is not None and resp.status >= 400:
            rep.warn("capture", f"Server answered HTTP {resp.status}; continuing with what rendered.")
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except PWTimeout:
            rep.step("capture", "Network never went idle (analytics/streams); continuing.")

        title = page.title()
        body_text = page.evaluate("(document.body && document.body.innerText || '').slice(0, 2000)")
        if BLOCK_TITLES.search(title or "") or (len(body_text.strip()) < 40 and BLOCK_TITLES.search(body_text)):
            browser.close()
            raise CaptureError(f"{url} is showing a bot-protection page ('{title}'). Try another URL.")

        _dismiss_consent(page)
        rep.step("capture", "Scrolling page to load lazy images and animations")
        _auto_scroll(page)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except PWTimeout:
            pass
        _settle(page)

        page.evaluate(EXTRACT_JS)
        n = page.evaluate("() => window.__CA.tag()")
        rep.step("capture", f"Tagged {n} DOM elements; extracting desktop layout ({settings.desktop_width}px)")
        desktop = page.evaluate("() => window.__CA.extract('desktop')")
        meta = page.evaluate("() => window.__CA.pageMeta()")
        desktop_png = out_dir / "desktop.png"
        _screenshot(page, desktop_png)

        # Canvas/WebGL can't be rebuilt as markup: keep a bitmap of it instead.
        canvas_ids = [nid for nid, nd in desktop["nodes"].items()
                      if nd["tag"] == "canvas" and nd["rect"][2] >= 80 and nd["rect"][3] >= 80][:6]
        canvas_png: dict[str, bytes] = {}
        for nid in canvas_ids:
            try:
                canvas_png[nid] = page.locator(f'[data-ca="{nid}"]').screenshot(timeout=4000, animations="disabled")
            except Exception:
                pass

        rep.step("capture", f"Extracting mobile layout ({settings.mobile_width}px)")
        page.set_viewport_size({"width": settings.mobile_width, "height": settings.mobile_height})
        page.wait_for_timeout(900)
        _auto_scroll(page, max_steps=25)
        _settle(page)
        mobile = page.evaluate("() => window.__CA.extract('mobile')")
        mobile_png = out_dir / "mobile.png"
        _screenshot(page, mobile_png)
        final_url = page.url

        # ---- assets: reuse bytes seen on the network, otherwise fetch -----
        def fetch(u: str):
            if u in responses:
                return responses[u]
            try:
                r = context.request.get(u, timeout=15000, headers={"Referer": final_url})
                if r.ok:
                    body = r.body()
                    return body, r.headers.get("content-type", "")
            except Exception:
                return None
            return None

        store = AssetStore(public_dir, fetch)
        rep.step("capture", "Downloading images, icons and fonts")
        _collect_assets(desktop, store)
        _collect_assets(mobile, store)
        meta["icons_local"] = [x for x in (store.image(u) for u in (meta.get("icons") or [])[:3]) if x][:1]
        canvas_shots = {nid: store.raw(png, "png", f"canvas-{nid}-{url}") for nid, png in canvas_png.items()}

        faces = list(meta.get("fontFaces") or [])
        for css_url, text in css_texts:
            faces.extend(parse_font_faces(text, css_url))
        used = set()
        for nd in list(desktop["nodes"].values()) + list(mobile["nodes"].values()):
            fam = (nd["s"].get("font-family") or "").split(",")[0].strip().strip("'\"")
            if fam:
                used.add(fam)
        fonts_css, fonts = build_fonts(faces, used, public_dir, fetch)
        context.close()
        browser.close()

    cap = Capture(
        url=url, final_url=final_url, title=title, meta=meta, desktop=desktop, mobile=mobile,
        desktop_png=desktop_png, mobile_png=mobile_png, assets=store, fonts_css=fonts_css,
        fonts=fonts, canvas_shots=canvas_shots, duration_s=round(time.time() - t0, 1),
    )
    n_img = sum(1 for v in store.map.values() if v)
    rep.success("capture", f"Captured {len(desktop['nodes'])} visible elements, {n_img} assets, "
                           f"{len(fonts)} font files in {cap.duration_s}s")
    if store.failed:
        rep.warn("capture", f"{len(store.failed)} assets could not be downloaded (kept remote or dropped)")
    (out_dir / "raw.json").write_text(json.dumps({"url": url, "final_url": final_url, "meta": meta,
                                                   "desktop": desktop, "mobile": mobile}, ensure_ascii=False),
                                      encoding="utf-8")
    return cap


def _collect_assets(extraction: dict, store: AssetStore) -> None:
    """Download every image/background/svg referenced by visible nodes (records local paths on the node)."""
    for nd in extraction["nodes"].values():
        a, s = nd["a"], nd["s"]
        if nd["tag"] == "img" and a.get("src"):
            a["local"] = store.image(a["src"])
        elif nd["tag"] == "video" and a.get("poster"):
            a["poster_local"] = store.image(a["poster"])
        elif nd["tag"] == "svg" and a.get("svg"):
            a["local"] = store.svg(a["svg"])
            a.pop("svg", None)  # markup lives in the file now; keeps JSON small
        bg = s.get("background-image") or ""
        if "url(" in bg:
            s["background-image"] = store.rewrite_css_urls(bg)
