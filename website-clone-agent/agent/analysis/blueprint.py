"""Stage 2 - Analyze: combine the deterministic measurements with one cheap
LLM call that *understands* the page (section roles, names, layout intent,
responsive behaviour, interactivity). Output: ``blueprint.json``.
"""
from __future__ import annotations

import base64
import io
import re
from typing import Optional

from ..capture.dom import Node, Section, assign_names
from ..config import settings
from ..events import Reporter
from ..llm import LLM, LLMError, LLMUnavailable
from .. import prompts
from .stylemap import to_hex

TYPE_BY_NAME = {
    "Navbar": "navbar", "Hero": "hero", "Footer": "footer", "Pricing": "pricing", "Testimonials": "testimonials",
    "Faq": "faq", "Features": "features", "Logos": "logos", "Stats": "stats", "Contact": "contact",
    "Newsletter": "newsletter", "Team": "team", "Blog": "blog", "Cta": "cta",
}
VALID_TYPES = set(TYPE_BY_NAME.values()) | {"content", "gallery", "other"}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def pascal(name: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", name or "")
    out = "".join(p[:1].upper() + p[1:] for p in parts)
    if not out or not out[0].isalpha():
        out = "Section" + out
    return out[:40]


def kebab(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


def nav_links(sections: list[Section]) -> list[str]:
    for s in sections:
        if s.kind == "header":
            return [a.text(40) for a in s.node.walk() if a.tag in ("a", "button") and a.text(40)][:14]
    return []


def section_summary(s: Section, outline_excerpt: str) -> dict:
    n: Node = s.node
    heads = [k.text(120) for k in n.walk() if k.tag in ("h1", "h2", "h3")][:4]
    walk = list(n.walk())
    counts = {
        "links": sum(1 for k in walk if k.tag == "a"),
        "buttons": sum(1 for k in walk if k.tag == "button"),
        "images": sum(1 for k in walk if k.tag in ("img", "svg", "picture", "video")),
        "inputs": sum(1 for k in walk if k.tag in ("input", "textarea", "select")),
    }
    bg = to_hex((n.style("d") or {}).get("background-color"))
    return {
        "index": s.index, "guess": s.name, "kind": s.kind,
        "y": n.rect[1], "height": n.rect[3], "background": bg,
        "headings": heads, "text": n.text(260), "counts": counts,
        "hidden_on_mobile": n.m is None,
        "markup_excerpt": outline_excerpt[:1400],
    }


def _screenshot_part(path, max_h: int = 2600) -> Optional[str]:
    try:
        from PIL import Image

        im = Image.open(path)
        w = 720
        im = im.resize((w, int(im.height * w / im.width)))
        im = im.crop((0, 0, w, min(im.height, max_h))).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def heuristic_blueprint(site: dict, sections: list[Section]) -> dict:
    assign_names(sections)
    out = []
    for s in sections:
        base = re.sub(r"\d+$", "", s.name)
        heads = [k.text(80) for k in s.node.walk() if k.tag in ("h1", "h2", "h3")][:2]
        out.append({
            "index": s.index, "name": s.name, "type": TYPE_BY_NAME.get(base, "content"),
            "description": ("; ".join(heads) or s.node.text(120)),
            "responsive": "stacks vertically on mobile", "interactive": "mobile menu" if s.kind == "header" else "",
        })
    return {"site_name": site.get("title", "").split("|")[0].split("–")[0].strip()[:40],
            "summary": site.get("description", "")[:200], "style_notes": "", "tokens": {},
            "sections": out, "source": "heuristic"}


def build_blueprint(llm: LLM, site: dict, tokens: dict, sections: list[Section], excerpts: list[str],
                    desktop_png=None, reporter: Optional[Reporter] = None) -> dict:
    rep = reporter or Reporter()
    fallback = heuristic_blueprint(site, sections)  # also assigns heuristic names used as hints
    summaries = [section_summary(s, ex) for s, ex in zip(sections, excerpts)]
    tok_view = {k: tokens[k] for k in ("theme", "colors", "fonts", "radius", "container", "typeScale")}
    user_text = prompts.analyze_user(site, tok_view, nav_links(sections), summaries)
    content: object = user_text
    if settings.vision and desktop_png:
        img = _screenshot_part(desktop_png)
        if img:
            content = [{"type": "text", "text": user_text}, {"type": "image_url", "image_url": {"url": img}}]
    messages = [{"role": "system", "content": prompts.ANALYZE_SYSTEM}, {"role": "user", "content": content}]
    try:
        try:
            data = llm.chat_json(messages, stage="analyze", fast=True, max_tokens=10000)
        except LLMError as e:
            if isinstance(e, LLMUnavailable) or isinstance(content, str):
                raise
            rep.warn("analyze", "Model rejected the screenshot - retrying text-only")
            messages[1]["content"] = user_text
            data = llm.chat_json(messages, stage="analyze", fast=True, max_tokens=10000)
    except LLMUnavailable:
        rep.step("analyze", "No LLM configured - using heuristic section analysis")
        return fallback
    except (LLMError, ValueError) as e:
        rep.warn("analyze", f"LLM analysis failed ({str(e)[:120]}); using heuristic analysis")
        return fallback

    # ---- validate + merge with the heuristic fallback ----------------------
    by_index = {}
    for item in data.get("sections", []) if isinstance(data, dict) else []:
        try:
            by_index[int(item.get("index"))] = item
        except (TypeError, ValueError):
            continue
    used: set[str] = set()
    merged = []
    for fb in fallback["sections"]:
        item = by_index.get(fb["index"], {})
        name = pascal(item.get("name") or fb["name"])
        base, i = name, 2
        while name in used or name in ("Page", "Layout", "Button", "Container", "Image", "Link"):
            name, i = f"{base}{i}", i + 1
        used.add(name)
        typ = item.get("type") if item.get("type") in VALID_TYPES else fb["type"]
        merged.append({
            "index": fb["index"], "name": name, "type": typ,
            "description": str(item.get("description") or fb["description"])[:400],
            "responsive": str(item.get("responsive") or fb["responsive"])[:300],
            "interactive": str(item.get("interactive") or "")[:200],
        })
    token_fix = {k: v for k, v in (data.get("tokens") or {}).items() if isinstance(v, str) and HEX.match(v)}
    bp = {
        "site_name": str(data.get("site_name") or fallback["site_name"])[:60],
        "summary": str(data.get("summary") or fallback["summary"])[:300],
        "style_notes": str(data.get("style_notes") or "")[:500],
        "tokens": token_fix, "sections": merged, "source": "llm",
    }
    return bp
