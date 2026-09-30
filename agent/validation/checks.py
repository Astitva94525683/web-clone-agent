"""Runtime validation of the generated site in a real browser.

Loads the preview at desktop and mobile widths and records: uncaught page
errors, console errors (hydration, React warnings), an empty page, horizontal
overflow on mobile, per-section bounding boxes, and full-page screenshots
used for the visual-similarity score.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings

IGNORE_CONSOLE = re.compile(
    r"React DevTools|Download the React|\[HMR\]|\[Fast Refresh\]|favicon|Failed to load resource|"
    r"net::ERR_|third-party cookie|preload|Content Security Policy", re.I)

SECTION_BOXES_JS = """() => Array.from(document.querySelectorAll('[data-section]')).map(w => {
  const el = getComputedStyle(w).display === 'contents' ? (w.firstElementChild || w) : w;
  const r = el.getBoundingClientRect();
  return {name: w.getAttribute('data-section'), x: Math.round(r.left + scrollX), y: Math.round(r.top + scrollY),
          w: Math.round(r.width), h: Math.round(r.height)};
})"""


@dataclass
class RuntimeReport:
    ok: bool = True
    page_errors: list = field(default_factory=list)
    console_errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    boxes: dict = field(default_factory=dict)  # viewport -> list of section boxes
    screenshots: dict = field(default_factory=dict)  # viewport -> path

    @property
    def problems(self) -> list[str]:
        return self.page_errors + self.console_errors

    def to_dict(self) -> dict:
        return {"ok": self.ok, "page_errors": self.page_errors, "console_errors": self.console_errors,
                "warnings": self.warnings, "boxes": self.boxes,
                "screenshots": {k: str(v) for k, v in self.screenshots.items()}}


def check_page(url: str, out_dir: Path, screenshots: bool = True) -> RuntimeReport:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    rep = RuntimeReport()
    viewports = {"desktop": (settings.desktop_width, settings.desktop_height),
                 "mobile": (settings.mobile_width, settings.mobile_height)}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for name, (w, h) in viewports.items():
            ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
            page = ctx.new_page()
            page.on("pageerror", lambda e: rep.page_errors.append(str(e)[:600]))
            page.on("console", lambda m: rep.console_errors.append(m.text[:600])
                    if m.type == "error" and not IGNORE_CONSOLE.search(m.text) else None)
            try:
                resp = page.goto(url, wait_until="networkidle", timeout=90000)
                if resp is not None and resp.status >= 500:
                    rep.page_errors.append(f"HTTP {resp.status} from the preview server")
            except Exception as e:
                rep.page_errors.append(f"Could not load preview: {str(e)[:300]}")
                ctx.close()
                continue
            page.wait_for_timeout(1200)
            info = page.evaluate("""() => ({
                text: (document.body.innerText || '').trim().length,
                overflow: document.documentElement.scrollWidth - window.innerWidth,
                nextError: !!document.querySelector('nextjs-portal') && (() => {
                    const b = document.querySelector('nextjs-portal').shadowRoot;
                    return !!(b && b.querySelector('[data-next-badge][data-error="true"], [data-nextjs-dialog]'));
                })(),
            })""")
            if info["text"] < 20:
                rep.page_errors.append(f"{name}: page rendered (almost) no text - likely a render error")
            if info["nextError"]:
                rep.page_errors.append(f"{name}: Next.js error overlay is showing")
            if name == "mobile" and info["overflow"] > 2:
                rep.warnings.append(f"mobile: page is {info['overflow']}px wider than the screen (horizontal scroll)")
            rep.boxes[name] = page.evaluate(SECTION_BOXES_JS)
            if screenshots:
                path = out_dir / f"generated-{name}.png"
                full_h = min(page.evaluate("document.documentElement.scrollHeight"), settings.max_screenshot_height)
                page.screenshot(path=str(path), full_page=True, clip={"x": 0, "y": 0, "width": w, "height": full_h})
                rep.screenshots[name] = path
            ctx.close()
        browser.close()
    rep.page_errors = list(dict.fromkeys(rep.page_errors))
    rep.console_errors = list(dict.fromkeys(rep.console_errors))[:20]
    rep.ok = not rep.page_errors
    return rep


def attribute_errors(errors: list[str], section_names: list[str]) -> dict[str, list[str]]:
    """Best-effort mapping of runtime errors to section components (via component stacks)."""
    out: dict[str, list[str]] = {}
    for e in errors:
        for name in section_names:
            if re.search(rf"\b{re.escape(name)}\b", e):
                out.setdefault(name, []).append(e)
                break
    return out
