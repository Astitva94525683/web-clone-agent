"""All prompt templates in one place.

Prompts are split into a *stable prefix* (rules + site context, identical for
every section of a site) and a small *variable suffix* (one section). DeepSeek
caches repeated prefixes automatically, so sections 2..N pay the cached rate
for most of their input tokens.
"""
from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
ANALYZE_SYSTEM = """You are a senior frontend architect. You receive a structured capture of a live
website (design tokens measured from computed styles, navigation, and a summary of every
page section). Plan a faithful React/Next.js recreation of it.

Reply with ONE JSON object and nothing else, using exactly this shape:
{
  "site_name": "short brand name",
  "summary": "one sentence: what this website is",
  "style_notes": "2-3 sentences on visual style: light/dark, density, corner radius, typography, imagery",
  "tokens": {"primary": "#hex", "accent": "#hex"},
  "sections": [
    {"index": 0, "name": "Navbar", "type": "navbar",
     "description": "content + desktop layout (columns, alignment, where media sits)",
     "responsive": "how it changes on mobile",
     "interactive": "mobile menu / tabs / accordion / carousel, or empty string"}
  ]
}
Rules:
- One entry per input section, same indexes, same order.
- "name": unique PascalCase React component name describing the section (Navbar, Hero, LogoCloud,
  FeatureGrid, Testimonials, Pricing, Faq, CallToAction, Footer ...).
- "type": one of navbar, hero, logos, features, stats, testimonials, pricing, faq, cta, content,
  gallery, team, blog, contact, newsletter, footer, other.
- "tokens": only include keys you want to correct (primary = main brand/CTA colour). Hex only.
- Be concrete and brief; no marketing language."""


def analyze_user(site: dict, tokens: dict, nav: list, sections: list) -> str:
    return (
        f"URL: {site['url']}\nTitle: {site.get('title', '')}\nMeta description: {site.get('description', '')}\n\n"
        f"Measured design tokens:\n{json.dumps(tokens, indent=1)}\n\n"
        f"Navigation links (header): {json.dumps(nav, ensure_ascii=False)}\n\n"
        f"Sections (top to bottom):\n{json.dumps(sections, indent=1, ensure_ascii=False)}"
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
GENERATE_RULES = """You are an expert frontend engineer. You recreate ONE section of an existing website
as a production-quality React component for a Next.js 16 (App Router) + TypeScript + Tailwind CSS v4 project.

You get REFERENCE MARKUP captured from the live site. Its classes are exact Tailwind translations of the
browser's computed styles, written mobile-first: base classes = mobile (390px), `lg:` classes = desktop
(1440px). Elements with `hidden lg:...` exist only on desktop; `... lg:hidden` only on mobile.

Hard rules (the file is type-checked and built automatically):
1. Output exactly ONE ```tsx code block containing the complete file, nothing else.
2. `export default function {Name}()` - no props. Put repeated content (links, cards, logos, plans, FAQ
   items) in typed `const` arrays at the top of the file and render them with `.map()` (with `key`).
3. First line `"use client";` ONLY if you use React hooks or event handlers (e.g. a mobile menu toggle).
4. Allowed imports only: "react", "next/link", "@/components/ui" (Button, Container), and "lucide-react"
   for simple UI icons (Menu, X, ChevronDown, ArrowRight, Check, Plus, Minus, Star, Play, Search).
   Nothing else. Do not import CSS.
5. Use plain <img> (not next/image) with the exact `src` paths from the reference and meaningful `alt`.
   Never invent image URLs. For decorative visuals you cannot reproduce, use a CSS gradient/solid block.
6. Colours: use the theme token classes when the reference uses them (bg-primary, text-foreground,
   text-muted-foreground, bg-surface, border-border, bg-background, text-primary-foreground, bg-accent);
   keep other exact values as arbitrary classes (text-[#8a8f98]). Fonts: font-heading / font-body / exact
   arbitrary families from the reference. Radii: rounded-button / rounded-card when referenced.
7. Keep the reference's measurements (font sizes, spacing, max widths, gaps, radii) - that is what makes
   the clone accurate. Simplify only meaningless wrapper divs, absolute-position hacks and animation markup.
8. Must be responsive and never overflow horizontally on a 390px screen. Honour `lg:` differences.
   If the navbar collapses on mobile, implement a working hamburger toggle with useState.
9. Root element: a semantic tag (header/section/footer/nav) with id="{slug}".
10. Keep ALL visible text content exactly (you may drop duplicated/animated copies). Links use the
    hrefs given. Buttons get type="button".
11. Valid TSX only: className (not class), self-closing void tags, style objects not strings, escape
    `{`, `}` and `>` in text, no <html>/<body>, no styled-jsx, no `any`."""


def site_context(site: dict, tokens: dict, blueprint: dict) -> str:
    colors = tokens.get("colors", {})
    fonts = tokens.get("fonts", {})
    return (
        "\n\nSITE CONTEXT\n"
        f"Site: {blueprint.get('site_name') or site.get('title', '')} - {blueprint.get('summary', '')}\n"
        f"Style: {blueprint.get('style_notes', '')}\n"
        "Theme tokens available as Tailwind classes (defined in app/globals.css):\n"
        + "\n".join(f"  {k}: {v}" for k, v in colors.items())
        + f"\n  font-heading: {fonts.get('heading')}  |  font-body (default on <body>): {fonts.get('body')}"
        + (f"  |  font-mono: {fonts.get('mono')}" if fonts.get("mono") else "")
        + f"\n  rounded-button: {tokens.get('radius', {}).get('button')}px  |  rounded-card: "
          f"{tokens.get('radius', {}).get('card')}px"
        + (f"\n  max-w-site: {tokens.get('container')}px (main content width)" if tokens.get("container") else "")
        + "\n\nShared UI components (import { Button, Container } from \"@/components/ui\"):\n"
          "  <Container className?>  -> mx-auto w-full max-w-site px-4 lg:px-8 wrapper\n"
          "  <Button href? variant=\"primary\"|\"secondary\"|\"outline\"|\"ghost\" size=\"sm\"|\"md\"|\"lg\" className?>"
          " -> renders <a> when href is set, else <button>. Use when a button matches the token style; "
          "otherwise write exact classes.\n"
    )


def generate_user(name: str, slug: str, spec: dict, outline: str) -> str:
    return (
        f"Recreate this section as `components/sections/{name}.tsx`.\n"
        f"Component name: {name}\nRoot id: {slug}\n"
        f"Section type: {spec.get('type', 'content')}\n"
        f"What it is: {spec.get('description', '')}\n"
        f"Mobile behaviour: {spec.get('responsive', '')}\n"
        f"Interactivity: {spec.get('interactive', '') or 'none'}\n\n"
        f"REFERENCE MARKUP:\n{outline}"
    )


# Mode suffixes are appended AFTER the shared rules + site context so every call of a
# site shares the same cached prompt prefix.
REPAIR_SUFFIX = """

MODE: FIXING a file that failed validation. Keep the design and content identical; change only what
is needed to fix the errors. Output the complete corrected file in ONE ```tsx block."""


def repair_user(path: str, code: str, errors: str) -> str:
    return f"File: {path}\n\nErrors:\n{errors}\n\nCurrent file:\n```tsx\n{code}\n```"


REFINE_USER = """The component below renders noticeably different from the original section.
Measured differences (original vs. your render at the same width):
{diffs}

Adjust spacing/sizes/layout so it matches the reference markup more closely. Keep all content.
Output the complete corrected file in ONE ```tsx block.

REFERENCE MARKUP:
{outline}

CURRENT FILE:
```tsx
{code}
```"""


# ---------------------------------------------------------------------------
# Modification
# ---------------------------------------------------------------------------
PLAN_SYSTEM = """You are the planning step of an AI website editor. The website is a Next.js + Tailwind v4
project whose page is an ordered list of section components. Theme colours/fonts/radii are CSS variables in
app/globals.css (used via classes like bg-primary, text-foreground, font-heading, rounded-button).

Turn the user's instruction into the SMALLEST set of operations. Reply with ONE JSON object:
{
  "summary": "what you will change, one sentence",
  "operations": [
    {"op": "update_tokens", "changes": {"--color-primary": "#2563eb"}},
    {"op": "edit_section", "section": "Navbar", "instruction": "precise change for this file"},
    {"op": "add_section", "name": "Testimonials", "after": "Features", "instruction": "what to build"},
    {"op": "remove_section", "section": "Pricing"},
    {"op": "move_section", "section": "Faq", "after": "Hero"}
  ]
}
Rules:
- Colour/font/radius changes that apply site-wide -> update_tokens (only existing variable names;
  also update the matching -foreground colour if contrast would break).
- "section" and "after" must be existing section names from the list (after may be "START").
- New section names: unique PascalCase. "Replace X with Y" = edit_section on X with a full rewrite
  instruction (keep its name).
- If the instruction is impossible or unrelated to the website, return {"summary": "...", "operations": []}."""


def plan_user(instruction: str, sections: list, tokens_css: str) -> str:
    return (
        f"Instruction: {instruction}\n\n"
        f"Sections in page order:\n{json.dumps(sections, indent=1, ensure_ascii=False)}\n\n"
        f"Current theme variables (app/globals.css):\n{tokens_css}"
    )


EDIT_SUFFIX = """

MODE: EDITING an existing section component according to an instruction. Apply exactly the requested
change, keep everything else (content, design, structure) unchanged unless the instruction says otherwise.
Output the complete updated file in ONE ```tsx block."""


def edit_user(path: str, code: str, instruction: str) -> str:
    return f"File: {path}\nInstruction: {instruction}\n\nCurrent file:\n```tsx\n{code}\n```"


def add_section_user(name: str, slug: str, instruction: str, style_example: str) -> str:
    return (
        f"Create a NEW section component `components/sections/{name}.tsx` (component name {name}, "
        f"root id=\"{slug}\").\nWhat to build: {instruction}\n\n"
        "There is no reference markup. Match the visual language of this existing section from the same site "
        "(spacing, typography, colours, container width):\n"
        f"```tsx\n{style_example}\n```\n"
        "Write realistic, specific placeholder copy (no lorem ipsum). No external images."
    )
