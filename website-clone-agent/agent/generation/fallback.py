"""Deterministic section renderer: captured tree -> TSX, no LLM.

Used (1) in offline/mock mode, (2) when the LLM fails for a section even after
repairs - so the generated site *always* builds and every section is present.
Output is valid, typed TSX with the same Tailwind classes as the reference.
"""
from __future__ import annotations

import json
import re

from ..analysis.outline import VOID, VNode

BLOCK = {"div", "section", "header", "footer", "nav", "main", "aside", "article", "ul", "ol", "li", "h1",
         "h2", "h3", "h4", "h5", "h6", "p", "form", "table", "blockquote", "figure", "dl", "details", "pre",
         "address", "hr"}
BOOL_ATTRS = {"autoPlay", "muted", "loop", "playsInline"}
SAFE_TEXT = re.compile(r"^[\w\s.,!?'’‘“”\-–—:;()&%$#@/+*=|~^·•…©®™€£¥°]*$")


def _fix_nesting(v: VNode, in_p: bool = False, in_a: bool = False, in_button: bool = False) -> None:
    """Avoid invalid DOM nesting that React reports as hydration errors."""
    for k in v.elements:
        if in_p and k.tag in BLOCK:
            k.tag = "span"
        if k.tag == "a" and (in_a or in_button):
            k.tag = "span"
            k.attrs.pop("href", None)
            k.attrs.pop("target", None)
            k.attrs.pop("rel", None)
        if k.tag == "button" and (in_a or in_button):
            k.tag = "span"
            k.attrs.pop("type", None)
        if k.tag in ("form",) and in_a:
            k.tag = "div"
        _fix_nesting(k, in_p or k.tag == "p", in_a or k.tag == "a", in_button or k.tag == "button")
    if v.tag == "p" and any(k.tag in BLOCK for k in v.elements):
        v.tag = "div"
    if v.tag == "table" and any(k.tag == "tr" for k in v.elements):
        rows = [k for k in v.kids if isinstance(k, VNode) and k.tag == "tr"]
        v.kids = [k for k in v.kids if not (isinstance(k, VNode) and k.tag == "tr")] + [VNode("tbody", "", {}, rows)]


def _attr(k: str, val) -> str:
    if val is True:
        return k
    if isinstance(val, int):
        return f"{k}={{{val}}}"
    s = str(val)
    if '"' in s or "\n" in s or "{" in s or "}" in s or "\\" in s:
        return f"{k}={{{json.dumps(s, ensure_ascii=False)}}}"
    return f'{k}="{s}"'


def _open(v: VNode, extra: dict | None = None) -> str:
    parts = [v.tag]
    if v.cls:
        parts.append(_attr("className", v.cls.replace('"', "'")))
    attrs = dict(v.attrs)
    if extra:
        attrs.update(extra)
    if v.tag == "img" and "alt" not in attrs:
        attrs["alt"] = ""
    if v.tag == "iframe" and "title" not in attrs:
        attrs["title"] = "Embedded content"
    for k, val in attrs.items():
        if val in (None, "", False):
            if not (k == "alt" and v.tag == "img"):
                continue
        parts.append(_attr(k, val))
    return " ".join(parts)


def _text(t: str) -> str:
    if t.strip() and SAFE_TEXT.match(t) and t == t.strip() and "  " not in t:
        return t
    return "{" + json.dumps(t, ensure_ascii=False) + "}"


def render(v: VNode, indent: int, extra: dict | None = None) -> str:
    pad = "  " * indent
    if v.tag in VOID:
        return f"{pad}<{_open(v, extra)} />"
    has_text = any(isinstance(k, str) and k.strip() for k in v.kids)
    if not v.kids:
        return f"{pad}<{_open(v, extra)}></{v.tag}>"
    if has_text:
        # mixed content on one line keeps the exact spacing between words and inline elements
        inner = "".join(_text(k) if isinstance(k, str) else render(k, 0).strip() for k in v.kids)
        return f"{pad}<{_open(v, extra)}>{inner}</{v.tag}>"
    lines = [f"{pad}<{_open(v, extra)}>"]
    for k in v.kids:
        if isinstance(k, str):
            if k.strip():
                lines.append(pad + "  " + _text(k))
        else:
            lines.append(render(k, indent + 1))
    lines.append(f"{pad}</{v.tag}>")
    return "\n".join(lines)


def root_tag(kind: str, typ: str) -> str:
    if kind == "header" or typ == "navbar":
        return "header"
    if kind == "footer" or typ == "footer":
        return "footer"
    return "section"


def render_component(name: str, slug: str, root: VNode, kind: str = "section", typ: str = "content") -> str:
    root = VNode(root.tag, root.cls, dict(root.attrs), list(root.kids), root.src_id)
    if root.tag in ("div", "span", "main", "article", "aside"):
        root.tag = root_tag(kind, typ)
    _fix_nesting(root, root.tag == "p", root.tag == "a", root.tag == "button")
    jsx = render(root, 2, {"id": slug})
    return (
        f"// {name}: generated deterministically from the captured layout (no LLM).\n"
        f"export default function {name}() {{\n"
        "  return (\n"
        f"{jsx}\n"
        "  );\n"
        "}\n"
    )


def placeholder_component(name: str, slug: str, title: str = "") -> str:
    """Last-resort component when even the captured tree is unusable."""
    t = json.dumps(title or name, ensure_ascii=False)
    return (
        f"export default function {name}() {{\n"
        "  return (\n"
        f'    <section id="{slug}" className="mx-auto max-w-site px-4 py-16 lg:px-8">\n'
        f'      <h2 className="font-heading text-[32px] font-semibold">{{{t}}}</h2>\n'
        "    </section>\n"
        "  );\n"
        "}\n"
    )
