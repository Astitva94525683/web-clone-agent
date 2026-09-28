"""Turn a captured section (DOM subtree) into a compact, Tailwind-annotated
outline - the "reference markup" the LLM recreates.

Steps: build a simplified virtual tree (``VNode``) -> prune invisible/empty
wrappers -> serialise as indented pseudo-HTML, compressing repeated siblings
(cards, logos, list items) so large sections stay within a token budget.
The same ``VNode`` tree also feeds the deterministic fallback renderer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional, Union
from urllib.parse import urlparse

from ..capture.dom import Node
from .stylemap import TokenMap, classes_for

VOID = {"img", "br", "input", "hr", "source"}
ALLOWED_TAGS = {
    "div", "section", "header", "footer", "nav", "main", "aside", "article", "h1", "h2", "h3", "h4",
    "h5", "h6", "p", "span", "a", "ul", "ol", "li", "img", "button", "form", "input", "textarea",
    "select", "option", "label", "strong", "b", "em", "i", "small", "br", "hr", "blockquote", "figure",
    "figcaption", "video", "iframe", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "code", "pre", "sup",
    "sub", "dl", "dt", "dd", "time", "address", "mark", "u", "s", "details", "summary",
}
VISUAL_KEYS = ("bg-", "border", "shadow-", "rounded", "w-[", "h-[")
TABLE_STRUCT = {"table", "thead", "tbody", "tfoot", "tr", "colgroup"}
INLINE = {"span", "a", "strong", "b", "em", "i", "small", "label", "button", "code", "sup", "sub", "time",
          "mark", "u", "s", "abbr"}
EMBED_OK = re.compile(r"youtube\.com|youtube-nocookie\.com|player\.vimeo\.com|google\.com/maps")

VKid = Union["VNode", str]


@dataclass
class VNode:
    tag: str
    cls: str = ""
    attrs: dict = field(default_factory=dict)
    kids: list = field(default_factory=list)  # VNode | str
    src_id: str = ""

    @property
    def elements(self) -> list["VNode"]:
        return [k for k in self.kids if isinstance(k, VNode)]

    def text(self) -> str:
        out = []
        for k in self.kids:
            out.append(k if isinstance(k, str) else " " + k.text() + " ")
        return re.sub(r"\s+", " ", "".join(out)).strip()

    def has_media(self) -> bool:
        return self.tag in ("img", "video", "iframe", "input", "textarea", "select") or any(
            k.has_media() for k in self.elements)


class OutlineBuilder:
    def __init__(self, tokens: TokenMap, site_url: str, body: Optional[Node] = None,
                 canvas_shots: Optional[dict] = None, text_limit: int = 600):
        self.tokens = tokens
        self.host = (urlparse(site_url).netloc or "").lower().removeprefix("www.")
        self.body = body
        self.canvas = canvas_shots or {}
        self.text_limit = text_limit

    # ---- links / media ------------------------------------------------
    def href(self, url: str) -> str:
        if not url or url.startswith("javascript:"):
            return "#"
        if url.startswith(("mailto:", "tel:")):
            return url
        p = urlparse(url)
        host = p.netloc.lower().removeprefix("www.")
        if not host or host == self.host:
            return f"#{p.fragment}" if p.fragment else "#"
        return url

    # ---- build ----------------------------------------------------------
    def build(self, node: Node, root: bool = True) -> Optional[VNode]:
        parent_styles = None
        if root and self.body is not None:
            parent_styles = (self.body.style("d"), self.body.style("m"))
        return self._build(node, parent_styles)

    def _build(self, n: Node, parent_styles=None) -> Optional[VNode]:
        a = n.attrs
        cls = classes_for(n, self.tokens, parent_styles)
        tag = n.tag

        # media --------------------------------------------------------------
        if tag == "svg":
            if not a.get("local"):
                return None
            return VNode("img", cls, {"src": a["local"], "alt": a.get("aria-label", "")}, src_id=n.id)
        if tag == "img":
            src = a.get("local") or (a.get("src") if (a.get("src") or "").startswith("http") else None)
            if not src:
                return None
            return VNode("img", cls, {"src": src, "alt": a.get("alt", "")}, src_id=n.id)
        if tag == "canvas":
            shot = self.canvas.get(n.id)
            return VNode("img", cls, {"src": shot, "alt": ""}, src_id=n.id) if shot else None
        if tag == "picture":
            for k in n.elements:
                if k.tag == "img":
                    return self._build(k)
            return None
        if tag == "video":
            attrs = {"src": a.get("src", ""), "autoPlay": True, "muted": True, "loop": True, "playsInline": True}
            if a.get("poster_local"):
                attrs["poster"] = a["poster_local"]
            if not attrs["src"].startswith("http"):
                if not attrs.get("poster"):
                    return None
                return VNode("img", cls, {"src": attrs["poster"], "alt": ""}, src_id=n.id)
            return VNode("video", cls, attrs, src_id=n.id)
        if tag == "iframe":
            src = a.get("src", "")
            if EMBED_OK.search(src):
                return VNode("iframe", cls, {"src": src, "title": a.get("title", "Embedded content")}, src_id=n.id)
            return VNode("div", cls + " bg-surface", {}, src_id=n.id)

        if tag not in ALLOWED_TAGS:
            tag = "span" if n.style("d") and (n.style("d") or {}).get("display", "").startswith("inline") else "div"

        attrs: dict = {}
        if tag == "a":
            attrs["href"] = self.href(a.get("href", ""))
            if attrs["href"].startswith("http"):
                attrs["target"] = "_blank"
                attrs["rel"] = "noreferrer"
        for k in ("aria-label", "placeholder", "type", "title"):
            if a.get(k) and (k != "type" or tag in ("input", "button")):
                attrs[k] = a[k]
        if tag in ("input", "textarea") and a.get("value") and not a.get("placeholder"):
            attrs["placeholder"] = a["value"]
        if tag in ("td", "th"):
            for src_k, jsx_k in (("colspan", "colSpan"), ("rowspan", "rowSpan")):
                try:
                    n_ = int(a.get(src_k, "1"))
                except ValueError:
                    n_ = 1
                if n_ > 1:
                    attrs[jsx_k] = n_
        if tag == "button" and "type" not in attrs:
            attrs["type"] = "button"

        v = VNode(tag, cls, attrs, src_id=n.id)
        if tag == "select":
            v.attrs["aria-label"] = v.attrs.get("aria-label") or a.get("selected") or "Select"
            v.kids = [VNode("option", "", {}, [a.get("selected") or ""])]
            return v
        for k in n.kids:
            if isinstance(k, str):
                t = k if len(k) <= self.text_limit else k[: self.text_limit] + "…"
                v.kids.append(t)
            else:
                child = self._build(k)
                if child is not None:
                    v.kids.append(child)
        return self._simplify(v)

    def _simplify(self, v: VNode) -> Optional[VNode]:
        # unwrap unstyled inline spans into their parent
        kids: list = []
        for k in v.kids:
            if isinstance(k, VNode) and k.tag == "span" and not k.cls and not k.attrs:
                kids.extend(k.kids)
            else:
                kids.append(k)
        # merge adjacent strings
        merged: list = []
        for k in kids:
            if isinstance(k, str) and merged and isinstance(merged[-1], str):
                merged[-1] += k
            else:
                merged.append(k)
        # trim whitespace at block edges (inline elements keep it: "text <span> (x)</span>")
        if v.tag not in INLINE:
            if merged and isinstance(merged[0], str):
                merged[0] = merged[0].lstrip()
            if merged and isinstance(merged[-1], str):
                merged[-1] = merged[-1].rstrip()
        else:
            merged = [re.sub(r"\s+", " ", k) if isinstance(k, str) else k for k in merged]
        v.kids = [k for k in merged if not (isinstance(k, str) and not k)]
        if v.tag in TABLE_STRUCT:  # whitespace text inside tables breaks React hydration
            v.kids = [k for k in v.kids if not (isinstance(k, str) and not k.strip())]

        if v.tag in VOID:
            return v
        has_text = bool(v.text())
        visual = any(key in v.cls for key in VISUAL_KEYS)
        keep_empty = ("br", "hr", "input", "td", "th", "tr", "tbody", "thead", "tfoot")  # table cells shape layout
        if not has_text and not v.has_media() and not visual and v.tag not in keep_empty:
            return None
        # collapse pure wrappers: <div> with one element child and nothing of its own
        els = v.elements
        trivial = not v.cls.replace("w-full", "").strip()
        if v.tag in ("div", "span") and trivial and not v.attrs and len(v.kids) == 1 and len(els) == 1:
            return els[0]
        return v


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------
def _attr_str(v: VNode) -> str:
    parts = []
    if v.cls:
        parts.append(f'class="{v.cls}"')
    for k, val in v.attrs.items():
        if val is True:
            continue
        parts.append(f'{k}="{str(val)[:300]}"')
    return (" " + " ".join(parts)) if parts else ""


def _sig(v: VKid, depth: int = 0) -> str:
    if isinstance(v, str):
        return "t"
    if depth > 3:
        return v.tag
    return v.tag + "(" + v.cls + ")[" + ",".join(_sig(k, depth + 1) for k in v.kids[:8]) + "]"


def _content(v: VNode) -> dict:
    texts, imgs, links = [], [], []

    def walk(x: VKid):
        if isinstance(x, str):
            if x.strip():
                texts.append(x.strip()[:200])
            return
        if x.tag == "img":
            imgs.append(x.attrs.get("src", ""))
        if x.tag == "a" and x.attrs.get("href") not in (None, "#"):
            links.append(x.attrs["href"])
        for k in x.kids:
            walk(k)

    walk(v)
    out = {"texts": texts}
    if imgs:
        out["imgs"] = imgs
    if links:
        out["links"] = links
    return out


def to_outline(v: VNode, indent: int = 0, compress_after: int = 3) -> str:
    pad = " " * indent
    if v.tag in VOID:
        return f"{pad}<{v.tag}{_attr_str(v)}>"
    inline_only = all(isinstance(k, str) or (not k.elements and k.tag not in ("div", "section", "ul", "ol"))
                      for k in v.kids) and len(v.text()) < 160 and len(v.kids) <= 6
    if inline_only:
        inner = "".join(k if isinstance(k, str) else to_outline(k, 0, compress_after) for k in v.kids)
        return f"{pad}<{v.tag}{_attr_str(v)}>{inner}</{v.tag}>"
    lines = [f"{pad}<{v.tag}{_attr_str(v)}>"]
    kids = v.kids
    i = 0
    while i < len(kids):
        k = kids[i]
        if isinstance(k, str):
            if k.strip():
                lines.append(f"{pad} {k.strip()}")
            i += 1
            continue
        # detect a run of structurally identical siblings
        j = i + 1
        sig = _sig(k)
        while j < len(kids) and not isinstance(kids[j], str) and _sig(kids[j]) == sig:
            j += 1
        run = j - i
        if run > compress_after:
            for x in kids[i:i + 2]:
                lines.append(to_outline(x, indent + 1, compress_after))
            rest = [_content(x) for x in kids[i + 2:j]]
            lines.append(f"{pad} <!-- {len(rest)} more items with the same markup as the item above; their content: -->")
            lines.append(f"{pad} <!-- {json.dumps(rest, ensure_ascii=False)} -->")
            i = j
            continue
        lines.append(to_outline(k, indent + 1, compress_after))
        i += 1
    lines.append(f"{pad}</{v.tag}>")
    return "\n".join(lines)


def outline_for(v: VNode, max_chars: int) -> str:
    """Serialise with progressively stronger compression until it fits the budget."""
    out = to_outline(v, 0, 3)
    if len(out) <= max_chars:
        return out
    out = to_outline(v, 0, 2)
    if len(out) <= max_chars:
        return out
    return out[:max_chars] + "\n<!-- outline truncated: recreate the remaining content in the same style -->"
