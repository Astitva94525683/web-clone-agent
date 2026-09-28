"""Convert computed CSS styles into Tailwind CSS v4 utility classes.

This is the deterministic "translation layer" of the agent: it turns what the
browser measured into compact, exact class hints (``text-[48px] leading-[1.1]
bg-primary``). The LLM then only has to *structure* the component instead of
guessing measurements, which is both more accurate and far cheaper.

``classes_for(node)`` returns a responsive class string: mobile-first base
classes plus ``lg:`` overrides taken from the desktop capture.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..capture.assets import GENERIC, clean_family, first_family

INLINE_TAGS = {
    "a", "span", "img", "strong", "b", "em", "i", "small", "label", "svg", "code", "abbr",
    "sup", "sub", "br", "video", "iframe", "select", "textarea", "time", "mark", "u", "s",
    "input", "button", "picture", "canvas",
}
REPLACED = {"img", "svg", "video", "canvas", "iframe", "picture", "input", "textarea", "select"}
INHERITED = ["color", "font-family", "font-size", "font-weight", "font-style", "line-height",
             "letter-spacing", "text-transform", "text-align", "white-space"]
WEIGHTS = {100: "thin", 200: "extralight", 300: "light", 400: "normal", 500: "medium",
           600: "semibold", 700: "bold", 800: "extrabold", 900: "black"}
BP = "lg"  # desktop breakpoint prefix (>=1024px). Base classes = mobile.


# ---------------------------------------------------------------------------
# value helpers
# ---------------------------------------------------------------------------
def px(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    m = re.match(r"^(-?[\d.]+)px$", str(v).strip())
    return float(m.group(1)) if m else None


def num(n: float) -> str:
    r = round(n, 2)
    return str(int(r)) if abs(r - int(r)) < 0.01 else f"{r:g}"


def space(prefix: str, value: float) -> str:
    """Tailwind v4 spacing scale (4px steps, dynamic) with arbitrary fallback."""
    neg = value < 0
    v = abs(value)
    if abs(v * 2 - round(v * 2)) < 0.01 and abs((v / 4) * 2 - round((v / 4) * 2)) < 0.01:
        cls = f"{prefix}-{num(v / 4)}"
    else:
        cls = f"{prefix}-[{num(v)}px]"
    return ("-" + cls) if neg else cls


def arb(value: str) -> str:
    """Escape a CSS value for use inside Tailwind's [...] syntax."""
    v = value.strip().replace('"', "'")
    v = re.sub(r",\s+", ",", v)
    return v.replace(" ", "_")


UNSAFE_ARB = re.compile(r"[<>{}\\`]|data:|'")


def safe_arb(value: str) -> Optional[str]:
    """Like arb() but returns None for values Tailwind/CSS parsers may choke on."""
    v = value.strip()
    # quotes around url() paths are optional - drop them
    v = re.sub(r"url\((['\"])([^'\"()]+)\1\)", r"url(\2)", v)
    if UNSAFE_ARB.search(v) or len(v) > 400 or v.count("(") != v.count(")"):
        return None
    return arb(v)


def parse_rgba(c: Optional[str]) -> Optional[tuple[int, int, int, float]]:
    if not c:
        return None
    m = re.match(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+%?))?\s*\)", c)
    if not m:
        return None
    a = m.group(4)
    alpha = 1.0 if a is None else (float(a[:-1]) / 100 if a.endswith("%") else float(a))
    return int(float(m.group(1))), int(float(m.group(2))), int(float(m.group(3))), alpha


def to_hex(c: Optional[str]) -> Optional[str]:
    """rgba() -> #rrggbb / #rrggbbaa. Returns None for fully transparent colours."""
    rgba = parse_rgba(c)
    if rgba is None:
        return None
    r, g, b, a = rgba
    if a <= 0.01:
        return None
    h = f"#{r:02x}{g:02x}{b:02x}"
    if a < 0.995:
        h += f"{round(a * 255):02x}"
    return h


def hex_close(a: str, b: str, tol: int = 3) -> bool:
    if len(a) != len(b):
        return False
    try:
        return all(abs(int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) <= tol for i in range(1, len(a), 2))
    except ValueError:
        return False



def _grid_place(start: str, end: str, prefix: str) -> str:
    """grid-column/row start+end -> Tailwind placement classes."""
    start, end = (start or "auto").strip(), (end or "auto").strip()
    is_int = lambda v: re.match(r"^-?\d+$", v) is not None
    if end.startswith("span "):
        n = end.split()[1]
        return (f"{prefix}-start-{start} " if is_int(start) else "") + f"{prefix}-span-{n}"
    if start.startswith("span "):
        return f"{prefix}-span-{start.split()[1]}"
    if is_int(start) and is_int(end):
        if start == "1" and end == "-1":
            return f"{prefix}-span-full"
        return f"{prefix}-[{start}/{end}]"
    if is_int(start):
        return f"{prefix}-start-{start}"
    return ""

# ---------------------------------------------------------------------------
# context: design tokens used to turn raw colours/fonts into semantic classes
# ---------------------------------------------------------------------------
@dataclass
class TokenMap:
    colors: dict = field(default_factory=dict)  # token name -> hex
    fonts: dict = field(default_factory=dict)   # token name -> family (clean)
    radius: dict = field(default_factory=dict)  # token name -> px value
    container: Optional[int] = None
    link_internal: Optional[str] = None  # origin of the captured site

    ORDER = {
        "text": ["foreground", "muted-foreground", "primary", "primary-foreground", "accent", "background", "surface"],
        "bg": ["background", "surface", "primary", "accent", "foreground", "primary-foreground"],
        "border": ["border", "primary", "foreground", "background", "surface"],
    }

    def color(self, prefix: str, hex_: str, on_primary: bool = False) -> str:
        order = self.ORDER.get(prefix, list(self.colors))
        if on_primary and prefix == "text":
            order = ["primary-foreground"] + order
        for name in order + [n for n in self.colors if n not in order]:
            val = self.colors.get(name)
            if val and hex_close(hex_.lower(), val.lower()):
                return f"{prefix}-{name}"
        return f"{prefix}-[{hex_}]"

    def font(self, family: str, stack: str = "") -> Optional[str]:
        fam = family.lower()
        for name, val in self.fonts.items():
            if val and val.lower() == fam:
                return None if name == "body" else f"font-{name}"
        names = []
        for part in (stack or family).split(","):
            n = re.sub(r"[^A-Za-z0-9 _-]", "", clean_family(part)).strip()
            if n and "fallback" not in n.lower() and n not in names:
                names.append(n)
        if not names:
            return None
        return "font-[" + ",".join(n if n.lower() in GENERIC else f"'{arb(n)}'" for n in names[:3]) + "]"

    def rounded(self, value: float, prefix: str = "rounded") -> str:
        for name, val in self.radius.items():
            if val is not None and abs(val - value) < 0.5 and value > 0:
                return f"{prefix}-{name}"
        return f"{prefix}-[{num(value)}px]"


# ---------------------------------------------------------------------------
# per-viewport style -> {property-group: class}
# ---------------------------------------------------------------------------
def _box(prefix: str, t: float, r: float, b: float, l: float) -> list[str]:
    if t == r == b == l:
        return [space(prefix, t)] if t else []
    out = []
    if t == b and l == r:
        if t:
            out.append(space(prefix + "y", t))
        if l:
            out.append(space(prefix + "x", l))
        return out
    for side, v in (("t", t), ("r", r), ("b", b), ("l", l)):
        if v:
            out.append(space(prefix + side, v))
    return out


def style_classes(node, vp: str, tokens: TokenMap, parent_style: Optional[dict]) -> dict:
    """Return {group: class-string} for one node at one viewport."""
    rec = node.d if vp == "d" else node.m
    if rec is None:
        return {}
    s, rect, a = rec["s"], rec["rect"], rec.get("a", {})
    tag = node.tag
    ps = parent_style or {}
    out: dict[str, str] = {}
    # visually hidden (screen-reader only) text: keep it accessible, never visible
    clip = (s.get("clip") or "").replace(" ", "")
    if (rect[2] <= 1.5 and rect[3] <= 1.5 and s.get("overflow-x") == "hidden") \
            or clip.startswith(("rect(0px,0px,0px,0px)", "rect(0,0,0,0)", "rect(1px,1px,1px,1px)")) \
            or "inset(50%)" in (s.get("clip-path") or "") or "inset(100%)" in (s.get("clip-path") or ""):
        return {"sr": "sr-only"}

    # ---- display ------------------------------------------------------
    disp = s.get("display", "")
    default_inline = tag in INLINE_TAGS
    dmap = {"flex": "flex", "inline-flex": "inline-flex", "grid": "grid", "inline-grid": "inline-grid",
            "inline-block": "inline-block", "contents": "contents", "table": "table"}
    media = tag in ("img", "svg", "video", "canvas", "iframe")
    if media and disp in ("inline", "inline-block"):
        out["display"] = disp  # Tailwind's preflight makes media display:block
    elif disp in dmap:
        out["display"] = dmap[disp]
    elif disp == "block" and default_inline and not media:
        out["display"] = "block"
    elif disp == "inline" and not default_inline:
        out["display"] = "inline"
    elif disp == "list-item" and tag != "li":
        out["display"] = "list-item"

    is_flex = disp in ("flex", "inline-flex")
    is_grid = disp in ("grid", "inline-grid")
    if is_flex:
        fd = s.get("flex-direction", "row")
        out["flex-dir"] = {"column": "flex-col", "row-reverse": "flex-row-reverse",
                           "column-reverse": "flex-col-reverse"}.get(fd, "flex-row")
        if s.get("flex-wrap") == "wrap":
            out["flex-wrap"] = "flex-wrap"
    if is_flex or is_grid:
        jc = s.get("justify-content", "normal")
        jm = {"center": "justify-center", "flex-end": "justify-end", "end": "justify-end",
              "space-between": "justify-between", "space-around": "justify-around",
              "space-evenly": "justify-evenly"}
        if jc in jm:
            out["justify"] = jm[jc]
        ai = s.get("align-items", "normal")
        am = {"center": "items-center", "flex-start": "items-start", "start": "items-start",
              "flex-end": "items-end", "end": "items-end", "baseline": "items-baseline"}
        if ai in am:
            out["items"] = am[ai]
        rg, cg = px(s.get("row-gap")), px(s.get("column-gap"))
        rg = rg or 0
        cg = cg or 0
        if rg and rg == cg:
            out["gap"] = space("gap", rg)
        else:
            g = []
            if cg:
                g.append(space("gap-x", cg))
            if rg:
                g.append(space("gap-y", rg))
            if g:
                out["gap"] = " ".join(g)
    if is_grid:
        cols = [px(t) for t in (s.get("grid-template-columns") or "").split() if px(t) is not None]
        if cols:
            if max(cols) - min(cols) <= 2:
                out["grid-cols"] = f"grid-cols-{len(cols)}"
            else:
                mn = min(cols) or 1
                out["grid-cols"] = "grid-cols-[" + "_".join(f"{num(round(c / mn, 1))}fr" for c in cols) + "]"
    # grid / flex child
    pdisp = ps.get("display", "")
    if pdisp in ("grid", "inline-grid"):
        for axis, prefix in (("column", "col"), ("row", "row")):
            place = _grid_place(s.get(f"grid-{axis}-start", "auto"), s.get(f"grid-{axis}-end", "auto"), prefix)
            if place:
                out[f"{prefix}-span"] = place
    if pdisp in ("flex", "inline-flex"):
        grow, shrink = s.get("flex-grow"), s.get("flex-shrink")
        if grow == "1" and s.get("flex-basis") in ("0%", "0px"):
            out["flex"] = "flex-1"
        elif grow == "1":
            out["flex"] = "grow"
        fb = s.get("flex-basis", "auto")
        if fb not in ("auto", "0%", "0px", "content", "") and "flex" not in out:
            if fb == "100%":
                out["basis"] = "basis-full"
            elif re.match(r"^[\d.]+(%|px)$", fb):
                out["basis"] = f"basis-[{fb}]"
        if shrink == "0" and tag not in REPLACED:
            out["shrink"] = "shrink-0"
        asf = s.get("align-self", "auto")
        if asf in ("center", "flex-start", "flex-end", "stretch"):
            out["self"] = {"center": "self-center", "flex-start": "self-start",
                           "flex-end": "self-end", "stretch": "self-stretch"}[asf]

    # ---- position -----------------------------------------------------
    pos = s.get("position", "static")
    if pos in ("relative", "absolute", "fixed", "sticky"):
        out["position"] = pos
        if pos != "relative":
            ins = []
            t, r_, b, l = (px(s.get(k)) for k in ("top", "right", "bottom", "left"))
            if pos == "sticky":
                if t is not None:
                    ins.append(space("top", t))
            else:
                if l is not None and r_ is not None and l == 0 and r_ == 0:
                    ins.append("inset-x-0")
                elif l is not None and r_ is not None:
                    ins.append(space("left", l) if abs(l) <= abs(r_) else space("right", r_))
                if t is not None and b is not None and t == 0 and b == 0:
                    ins.append("inset-y-0")
                elif t is not None and b is not None:
                    ins.append(space("top", t) if abs(t) <= abs(b) else space("bottom", b))
            if ins:
                out["inset"] = " ".join(ins)
    z = s.get("z-index", "auto")
    is_item = ps.get("display", "") in ("flex", "inline-flex", "grid", "inline-grid")
    if (pos in ("relative", "absolute", "fixed", "sticky") or is_item) and z not in ("auto", "0") \
            and re.match(r"^-?\d+$", z):
        out["z"] = f"z-[{min(int(z), 9999)}]"  # flex/grid items honour z-index even when static

    # ---- box model ------------------------------------------------------
    pads = [px(s.get(f"padding-{k}")) or 0 for k in ("top", "right", "bottom", "left")]
    p_cls = _box("p", *pads)
    if p_cls:
        out["padding"] = " ".join(p_cls)
    margins = [px(s.get(f"margin-{k}")) or 0 for k in ("top", "right", "bottom", "left")]
    mt, mr, mb, ml = margins
    parent_rect = None
    if node.parent is not None:
        prec = node.parent.d if vp == "d" else node.parent.m
        parent_rect = prec["rect"] if prec else None
    centered = False
    auto_side = ""
    if ml > 0 and abs(ml - mr) <= 1 and parent_rect and disp not in ("inline", "inline-block", "inline-flex"):
        centered = True
        ml = mr = 0
    elif ps.get("display", "") in ("flex", "inline-flex") and ps.get("flex-direction", "row").startswith("row"):
        # margin-left:auto in a flex row resolves to a big px value -> restore "ml-auto"
        if ml > 32 and mr < 32:
            auto_side, ml = "ml-auto", 0
        elif mr > 32 and ml < 32:
            auto_side, mr = "mr-auto", 0
    m_cls = _box("m", mt, mr, mb, ml)
    if centered:
        m_cls.append("mx-auto")
    if auto_side:
        m_cls.append(auto_side)
    if m_cls:
        out["margin"] = " ".join(m_cls)

    w, h = rect[2], rect[3]
    maxw = px(s.get("max-width"))
    if maxw and maxw < 3000:
        cont = tokens.container
        out["max-w"] = "max-w-site" if cont and abs(maxw - cont) < 1 else f"max-w-[{num(maxw)}px]"
        if centered or (parent_rect and w >= maxw - 1):
            out["w"] = "w-full"
    elif tag in REPLACED or (not node.kids and (s.get("background-color") not in (None, "", "rgba(0, 0, 0, 0)")
                                              or s.get("border-top-style") not in (None, "none")
                                              or s.get("background-image", "none") != "none"
                                              or s.get("box-shadow", "none") != "none")):
        pw = parent_rect[2] if parent_rect else None
        pp = px((ps or {}).get("padding-left")) or 0
        pr = px((ps or {}).get("padding-right")) or 0
        vw = 1440 if vp == "d" else 390
        if pw and abs(w - (pw - pp - pr)) <= 2 and w >= 0.5 * vw:
            out["w"] = "w-full"  # genuinely fluid (hero images, full-width blocks)
        elif w:
            # fixed size (logos, icons); max-w-full keeps it from overflowing small screens
            out["w"] = f"w-[{num(w)}px]" + (" max-w-full" if tag in ("img", "video", "iframe", "canvas") else "")
        nw, nh = a.get("nw"), a.get("nh")
        if tag == "img" and nw and nh and w and abs(h - w * nh / nw) <= 2:
            out["h"] = "h-auto"
        elif h:
            out["h"] = f"h-[{num(h)}px]"
    elif pdisp in ("flex", "inline-flex") and ps.get("flex-direction", "row").startswith("row") \
            and parent_rect and disp in ("block", "flex", "grid") and tag not in ("a", "button", "span", "label"):
        inner_w = parent_rect[2] - (px(ps.get("padding-left")) or 0) - (px(ps.get("padding-right")) or 0)
        if inner_w > 0 and "flex" not in out and "basis" not in out:
            ratio = w / inner_w
            if ratio >= 0.97 and w >= 100:
                out["w"] = "w-full"  # flex items shrink-to-fit unless told to fill
            elif w >= 160 and 0.2 <= ratio < 0.97 and len(node.parent.elements) >= 2:
                out["w"] = f"w-[{num(round(100 * ratio, 1))}%]"
    elif disp in ("inline-flex", "inline-block", "inline-grid") and parent_rect and tag not in REPLACED \
            and tag not in ("a", "button", "span", "label"):
        inner_w = parent_rect[2] - (px(ps.get("padding-left")) or 0) - (px(ps.get("padding-right")) or 0)
        if inner_w > 0 and w / inner_w >= 0.97 and w >= 100:
            out["w"] = "w-full"  # inline-level boxes shrink-to-fit otherwise
    minh = px(s.get("min-height"))
    if minh and minh > 0:
        vh = 900 if vp == "d" else 844
        out["min-h"] = "min-h-screen" if abs(minh - vh) <= 2 else f"min-h-[{num(minh)}px]"
    elif "h" not in out and tag not in REPLACED and disp not in ("inline", "contents") and h >= 24 \
            and not node.own_text():
        # Taller than its in-flow children -> the original had an explicit height (navbars, heroes).
        kids = [(k.d if vp == "d" else k.m) for k in node.elements]
        kids = [r for r in kids if r and r["s"].get("position") not in ("absolute", "fixed")]
        if kids:
            top_r = min(kids, key=lambda r: r["rect"][1])
            bot_r = max(kids, key=lambda r: r["rect"][1] + r["rect"][3])
            extent = (bot_r["rect"][1] + bot_r["rect"][3]) - top_r["rect"][1]
            extent += (px(top_r["s"].get("margin-top")) or 0) + (px(bot_r["s"].get("margin-bottom")) or 0)
            extent += pads[0] + pads[2] + (px(s.get("border-top-width")) or 0) + (px(s.get("border-bottom-width")) or 0)
            if h - extent > 12:
                out["min-h"] = f"min-h-[{num(h)}px]"

    # ---- typography (only when it differs from the parent: inheritance) --
    def changed(prop: str) -> bool:
        return s.get(prop) != ps.get(prop) or parent_style is None

    col = to_hex(s.get("color"))
    if col and changed("color"):
        own_bg = to_hex(s.get("background-color")) or ""
        on_primary = bool(own_bg) and hex_close(own_bg, tokens.colors.get("primary", "") or "#zzzzzz")
        out["color"] = tokens.color("text", col, on_primary=on_primary)
    ff = s.get("font-family", "")
    if ff and changed("font-family"):
        fc = tokens.font(first_family(ff), ff)
        if fc:
            out["font"] = fc
    fs = px(s.get("font-size"))
    if fs and changed("font-size"):
        out["text-size"] = f"text-[{num(fs)}px]"
    fw = s.get("font-weight", "400")
    if changed("font-weight") and fw:
        try:
            wv = int(float(fw))
            out["weight"] = f"font-{WEIGHTS[wv]}" if wv in WEIGHTS else f"font-[{wv}]"
        except ValueError:
            pass
    lh = px(s.get("line-height"))
    if lh and fs and (changed("line-height") or changed("font-size")):
        out["leading"] = f"leading-[{num(round(lh / fs, 2))}]"
    ls = px(s.get("letter-spacing"))
    if ls and fs and changed("letter-spacing"):
        out["tracking"] = f"tracking-[{num(round(ls / fs, 3))}em]"
    tt = s.get("text-transform", "none")
    if tt != "none" and changed("text-transform"):
        out["transform"] = {"uppercase": "uppercase", "lowercase": "lowercase", "capitalize": "capitalize"}.get(tt, "")
    ta = s.get("text-align", "start")
    if changed("text-align") and ta in ("center", "right", "justify", "left", "start", "end"):
        out["text-align"] = {"center": "text-center", "right": "text-right", "end": "text-right",
                             "justify": "text-justify"}.get(ta, "text-left")
        if out["text-align"] == "text-left" and parent_style is not None and \
                ps.get("text-align") in (None, "start", "left"):
            out.pop("text-align")
    if s.get("font-style") == "italic" and changed("font-style"):
        out["italic"] = "italic"
    td = s.get("text-decoration-line", "none")
    if "underline" in td:
        out["decoration"] = "underline"
    elif "line-through" in td:
        out["decoration"] = "line-through"
    if s.get("white-space") in ("nowrap", "pre") and changed("white-space"):
        out["whitespace"] = "whitespace-nowrap" if s.get("white-space") == "nowrap" else "whitespace-pre"

    # ---- visuals --------------------------------------------------------
    bg = to_hex(s.get("background-color"))
    if bg:
        out["bg"] = tokens.color("bg", bg)
    bgi = s.get("background-image", "none")
    bgi_arb = safe_arb(bgi) if bgi and bgi != "none" else None
    if bgi_arb:
        out["bg-image"] = f"bg-[{bgi_arb}]"
        if "url(" in bgi:
            size = s.get("background-size", "auto")
            extra = []
            if size in ("cover", "contain"):
                extra.append(f"bg-{size}")
            if "50%" in (s.get("background-position") or ""):
                extra.append("bg-center")
            if s.get("background-repeat", "").startswith("no-repeat"):
                extra.append("bg-no-repeat")
            if extra:
                out["bg-image"] += " " + " ".join(extra)
    bw = [px(s.get(f"border-{k}-width")) or 0 for k in ("top", "right", "bottom", "left")]
    bst = [s.get(f"border-{k}-style", "none") for k in ("top", "right", "bottom", "left")]
    bw = [w_ if st not in ("none", "hidden") else 0 for w_, st in zip(bw, bst)]
    if any(bw):
        sides = ["t", "r", "b", "l"]
        if len(set(bw)) == 1:
            out["border"] = "border" if bw[0] == 1 else f"border-[{num(bw[0])}px]"
        else:
            out["border"] = " ".join(
                (f"border-{sd}" if v == 1 else f"border-{sd}-[{num(v)}px]") for sd, v in zip(sides, bw) if v)
        side_idx = next(i for i, v in enumerate(bw) if v)
        bcol = to_hex(s.get(f"border-{['top', 'right', 'bottom', 'left'][side_idx]}-color"))
        if bcol:
            out["border"] += " " + tokens.color("border", bcol)
        if bst[side_idx] in ("dashed", "dotted"):
            out["border"] += f" border-{bst[side_idx]}"
    radii = [px(s.get(f"border-{k}-radius")) or 0 for k in ("top-left", "top-right", "bottom-right", "bottom-left")]
    if any(radii):
        if len(set(radii)) == 1:
            r0 = radii[0]
            out["rounded"] = "rounded-full" if r0 >= 999 or (h and r0 >= min(w, h) / 2 - 0.5) else tokens.rounded(r0)
        else:
            out["rounded"] = " ".join(
                tokens.rounded(v, f"rounded-{c}") for c, v in zip(("tl", "tr", "br", "bl"), radii) if v)
    sh = s.get("box-shadow", "none")
    sh_arb = safe_arb(sh) if sh and sh != "none" and len(sh) < 300 else None
    if sh_arb:
        out["shadow"] = f"shadow-[{sh_arb}]"
    op = s.get("opacity", "1")
    try:
        if float(op) < 0.99:
            out["opacity"] = f"opacity-[{num(round(float(op), 2))}]"
    except ValueError:
        pass
    ox, oy = s.get("overflow-x", "visible"), s.get("overflow-y", "visible")
    if ox == oy == "hidden" or ox == oy == "clip":
        out["overflow"] = "overflow-hidden"
    elif ox in ("auto", "scroll"):
        out["overflow"] = "overflow-x-auto"
    elif ox == "hidden":
        out["overflow"] = "overflow-x-hidden"
    of = s.get("object-fit", "fill")
    if of in ("cover", "contain") and tag in ("img", "video"):
        out["object"] = f"object-{of}"
    bf = s.get("backdrop-filter", "none")
    m = re.search(r"blur\(([\d.]+)px\)", bf or "")
    if m:
        out["backdrop"] = f"backdrop-blur-[{num(float(m.group(1)))}px]"
    lst = s.get("list-style-type", "none")
    if tag in ("ul", "ol") and lst in ("disc", "decimal", "circle", "square"):
        out["list"] = "list-disc" if lst in ("disc", "circle", "square") else "list-decimal"
    return out


# ---------------------------------------------------------------------------
# responsive merge
# ---------------------------------------------------------------------------
RESET = {
    "display": "block", "flex-dir": "flex-row", "justify": "justify-start", "items": "items-stretch",
    "gap": "gap-0", "grid-cols": "grid-cols-1", "padding": "p-0", "margin": "m-0", "w": "w-auto",
    "h": "h-auto", "max-w": "max-w-none", "min-h": "min-h-0", "text-align": "text-left",
    "position": "static", "inset": "inset-auto", "flex": "flex-initial", "col-span": "col-auto", "row-span": "row-auto",
    "flex-wrap": "flex-nowrap", "whitespace": "whitespace-normal", "transform": "normal-case",
    "shrink": "shrink", "self": "self-auto", "rounded": "rounded-none", "border": "border-0",
    "shadow": "shadow-none", "overflow": "overflow-visible", "bg-image": "bg-none",
}


def classes_for(node, tokens: TokenMap, parent_styles: Optional[tuple] = None) -> str:
    """Mobile-first responsive class string for a node.

    ``parent_styles`` overrides the (desktop, mobile) styles the node inherits
    from - used for section roots, which inherit from <body> in the clone.
    """
    parent = node.parent
    if parent_styles is not None:
        pd, pm = parent_styles
    else:
        pd = parent.style("d") if parent is not None else None
        pm = parent.style("m") if parent is not None else None
    D = style_classes(node, "d", tokens, pd) if node.d else None
    M = style_classes(node, "m", tokens, pm) if node.m else None

    if D is not None and M is None:  # desktop only
        if parent is not None and parent.m is None and parent.d is not None and parent_styles is None:
            return " ".join(v for v in D.values() if v)  # whole subtree is desktop-only
        disp = D.get("display", "block")
        base = [f"{BP}:{c}" for k, v in D.items() for c in v.split() if k != "display"]
        return " ".join(["hidden", f"{BP}:{disp}"] + base)
    if M is not None and D is None:  # mobile only
        if parent is not None and parent.d is None and parent.m is not None and parent_styles is None:
            return " ".join(v for v in M.values() if v)
        return " ".join([v for v in M.values() if v] + [f"{BP}:hidden"])
    if D is None or M is None:
        return ""
    out = [v for v in M.values() if v]
    for key in list(dict.fromkeys(list(M) + list(D))):
        dv, mv = D.get(key), M.get(key)
        if dv == mv:
            continue
        if dv:
            out.extend(f"{BP}:{c}" for c in dv.split())
        elif key in RESET:
            out.append(f"{BP}:{RESET[key]}")
    return " ".join(c for c in out if c)
