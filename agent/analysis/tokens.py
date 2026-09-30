"""Derive design tokens (palette, fonts, radii, container width, type scale)
from the captured computed styles. Deterministic and free.

The LLM later only *confirms/renames* these; generation then uses token
classes (``bg-primary``) so a request like "make the primary colour blue"
becomes a one-line change in ``globals.css``.
"""
from __future__ import annotations

import colorsys
from collections import Counter
from typing import Optional

from ..capture.assets import clean_stack, first_family
from ..capture.dom import Node
from .stylemap import parse_rgba, px, to_hex


def _rgb(hex_: str) -> tuple[float, float, float]:
    return tuple(int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore


def saturation(hex_: str) -> float:
    r, g, b = _rgb(hex_)
    _, l, s = colorsys.rgb_to_hls(r, g, b)
    return s if 0.08 < l < 0.92 else 0.0


def luminance(hex_: str) -> float:
    def ch(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(hex_)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hue(hex_: str) -> float:
    r, g, b = _rgb(hex_)
    return colorsys.rgb_to_hls(r, g, b)[0] * 360


def _opaque(c: Optional[str]) -> Optional[str]:
    rgba = parse_rgba(c)
    if not rgba or rgba[3] < 0.9:
        return None
    return to_hex(c)


def _is_button(n: Node) -> bool:
    s = n.style("d")
    if not s:
        return False
    if n.tag not in ("a", "button") and n.attrs.get("role") != "button":
        return False
    w, h = n.rect[2], n.rect[3]
    return 24 <= h <= 72 and 40 <= w <= 420 and bool(n.text(40))


def derive_tokens(body: Node, page: dict, sections_nodes: list[Node]) -> dict:
    text_colors: Counter = Counter()
    bg_area: Counter = Counter()
    btn_bg: Counter = Counter()
    btn_fg: Counter = Counter()
    border_colors: Counter = Counter()
    btn_radius: Counter = Counter()
    card_radius: Counter = Counter()
    heading_fonts: Counter = Counter()
    body_fonts: Counter = Counter()
    mono_fonts: Counter = Counter()
    maxw: Counter = Counter()
    sizes: dict[str, Counter] = {k: Counter() for k in ("h1", "h2", "h3", "p")}
    stacks: dict[str, str] = {}

    for n in body.walk():
        s = n.style("d")
        if not s:
            continue
        w, h = n.rect[2], n.rect[3]
        own = n.own_text()
        fam = first_family(s.get("font-family", ""))
        stacks.setdefault(fam, s.get("font-family", ""))
        if own:
            c = _opaque(s.get("color"))
            fs = px(s.get("font-size")) or 16
            if c:
                text_colors[c] += len(own) * (fs / 16)
            if n.tag in ("h1", "h2", "h3"):
                heading_fonts[fam] += len(own) + 20
            elif n.tag in ("code", "pre", "kbd"):
                mono_fonts[fam] += len(own)
            else:
                body_fonts[fam] += len(own)
        bg = _opaque(s.get("background-color"))
        if bg:
            bg_area[bg] += w * h
        if _is_button(n):
            if bg:
                btn_bg[bg] += 1
                fg = _opaque(s.get("color"))
                if fg:
                    btn_fg[(bg, fg)] += 1
            r = px(s.get("border-top-left-radius"))
            if r is not None:
                btn_radius[min(r, 9999)] += 1
        elif (bg or s.get("border-top-style") not in (None, "none")) and w * h > 30000 and w < 1200:
            r = px(s.get("border-top-left-radius"))
            if r:
                card_radius[r] += 1
        if s.get("border-top-style") not in (None, "none") or s.get("border-bottom-style") not in (None, "none"):
            for side in ("top", "bottom"):
                if s.get(f"border-{side}-style") not in (None, "none") and (px(s.get(f"border-{side}-width")) or 0) > 0:
                    c = _opaque(s.get(f"border-{side}-color"))
                    if c:
                        border_colors[c] += max(w, h)
        mw = px(s.get("max-width"))
        if mw and 800 <= mw <= 1600:
            maxw[round(mw)] += 1
        if n.tag in sizes and own:
            sizes[n.tag][px(s.get("font-size")) or 0] += 1
        elif n.tag in ("p", "li") and own:
            sizes["p"][px(s.get("font-size")) or 0] += 1

    background = _opaque(page.get("bodyBg")) or _opaque(page.get("htmlBg"))
    if not background:
        # the biggest painted area is the de-facto page background
        background = bg_area.most_common(1)[0][0] if bg_area else "#ffffff"
    foreground = _opaque(page.get("bodyColor")) or (text_colors.most_common(1)[0][0] if text_colors else "#111111")
    if contrast(foreground, background) < 3 and text_colors:
        foreground = max(text_colors, key=lambda c: (contrast(c, background) > 4.5, text_colors[c]))

    # primary brand colour: most used saturated button background, else saturated text/link colour
    sat_btn = [(c, k) for c, k in btn_bg.most_common() if saturation(c) > 0.25]
    sat_text = [(c, k) for c, k in text_colors.most_common() if saturation(c) > 0.35]
    sat_bg = [(c, k) for c, k in bg_area.most_common() if saturation(c) > 0.3]
    if sat_btn:
        primary = sat_btn[0][0]
    elif btn_bg:
        primary = btn_bg.most_common(1)[0][0]
    elif sat_text:
        primary = sat_text[0][0]
    elif sat_bg:
        primary = sat_bg[0][0]
    else:
        primary = foreground
    if primary.lower() == background.lower() and btn_bg:
        others = [c for c, _ in btn_bg.most_common() if c.lower() != background.lower()]
        primary = others[0] if others else foreground
    fg_on_primary = [fg for (bg, fg), _ in btn_fg.most_common() if bg == primary]
    primary_fg = fg_on_primary[0] if fg_on_primary else (
        "#ffffff" if contrast("#ffffff", primary) >= contrast("#000000", primary) else "#000000")

    accent = None
    for c, _ in sat_btn + sat_text + sat_bg:
        if c != primary and abs(hue(c) - hue(primary)) > 25:
            accent = c
            break
    accent = accent or primary

    surface = None
    for c, _ in bg_area.most_common(8):
        if c.lower() != background.lower() and c.lower() != primary.lower():
            surface = c
            break
    if surface is None:
        surface = background

    muted = None
    for c, _ in text_colors.most_common(8):
        if c.lower() not in (foreground.lower(), primary.lower(), primary_fg.lower()) and contrast(c, background) >= 2.2:
            muted = c
            break
    muted = muted or foreground
    border = border_colors.most_common(1)[0][0] if border_colors else ("#e5e7eb" if luminance(background) > 0.5 else "#27272a")

    def pick_font(counter: Counter, fallback: str) -> str:
        return counter.most_common(1)[0][0] if counter else fallback

    body_font = first_family(page.get("bodyFont", "")) or pick_font(body_fonts, "Inter")
    if body_fonts:
        body_font = pick_font(body_fonts, body_font)
    heading_font = pick_font(heading_fonts, body_font)
    mono_font = pick_font(mono_fonts, "")

    def mode(counter: Counter, default):
        return counter.most_common(1)[0][0] if counter else default

    palette = [c for c, _ in (text_colors + bg_area).most_common(14)]
    tokens = {
        "theme": "dark" if luminance(background) < 0.2 else "light",
        "colors": {
            "background": background,
            "foreground": foreground,
            "primary": primary,
            "primary-foreground": primary_fg,
            "accent": accent,
            "surface": surface,
            "muted-foreground": muted,
            "border": border,
        },
        "fonts": {"heading": heading_font, "body": body_font, **({"mono": mono_font} if mono_font else {})},
        "fontStacks": {"heading": clean_stack(stacks.get(heading_font) or heading_font),
                       "body": clean_stack(stacks.get(body_font) or page.get("bodyFont", "") or body_font),
                       **({"mono": clean_stack(stacks.get(mono_font) or mono_font)} if mono_font else {})},
        "radius": {"button": mode(btn_radius, 8.0), "card": mode(card_radius, 12.0)},
        "container": mode(maxw, None),
        "typeScale": {k: mode(v, None) for k, v in sizes.items()},
        "palette": palette,
    }
    return tokens
