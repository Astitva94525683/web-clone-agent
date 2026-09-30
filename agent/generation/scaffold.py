"""Deterministic project scaffold: every file that does NOT need an LLM.

package.json, configs, layout, theme tokens (globals.css), fonts, shared UI
components and the page composition are all written from templates. This
removes the most common sources of build errors and costs zero tokens.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

VERSIONS = {
    "next": "16.3.6",
    "react": "19.3.0",
    "react-dom": "19.3.0",
    "lucide-react": "1.48.0",
    "typescript": "5.9.3",
    "@types/react": "19.3.0",
    "@types/react-dom": "19.3.0",
    "@types/node": "22.15.0",
    "tailwindcss": "4.3.3",
    "@tailwindcss/postcss": "4.3.3",
}
DEPS = ["next", "react", "react-dom", "lucide-react"]
TEMPLATES = Path(__file__).parent / "templates"

SANS = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'


def package_json(name: str) -> dict:
    return {
        "name": re.sub(r"[^a-z0-9-]", "-", name.lower())[:60] or "cloned-site",
        "version": "0.1.0",
        "private": True,
        "scripts": {"dev": "next dev", "build": "next build", "start": "next start", "typecheck": "tsc --noEmit"},
        "dependencies": {k: VERSIONS[k] for k in DEPS},
        "devDependencies": {k: v for k, v in VERSIONS.items() if k not in DEPS},
    }


TSCONFIG = {
    "compilerOptions": {
        "target": "ES2017", "lib": ["dom", "dom.iterable", "esnext"], "allowJs": False, "skipLibCheck": True,
        "strict": True, "noEmit": True, "esModuleInterop": True, "module": "esnext",
        "moduleResolution": "bundler", "resolveJsonModule": True, "isolatedModules": True,
        "jsx": "react-jsx", "incremental": True, "plugins": [{"name": "next"}], "paths": {"@/*": ["./*"]},
    },
    "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts", ".next/dev/types/**/*.ts"],
    "exclude": ["node_modules"],
}

NEXT_CONFIG = """import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
// When run inside the clone agent's workspace, dependencies live in a shared
// node_modules three levels up; standalone, this is a normal Next.js app.
const shared = path.resolve(here, "../../..");
const useShared = !fs.existsSync(path.join(here, "node_modules")) && fs.existsSync(path.join(shared, "node_modules", "next"));

/** @type {import('next').NextConfig} */
const nextConfig = {
  images: { unoptimized: true },
  agentRules: false,
  devIndicators: false,
  ...(useShared ? { turbopack: { root: shared } } : {}),
};

export default nextConfig;
"""

POSTCSS = 'export default { plugins: { "@tailwindcss/postcss": {} } };\n'
NEXT_ENV = '/// <reference types="next" />\n/// <reference types="next/image-types/global" />\n'
GITIGNORE = "node_modules\n.next\nout\n*.tsbuildinfo\nnext-env.d.ts\n"


def _font_stack(family: str | None, fallback: str) -> str:
    if not family:
        return fallback
    return f'"{family}", {fallback}'


def theme_css(tokens: dict, body: dict) -> str:
    """app/globals.css - Tailwind v4 theme tokens as CSS variables."""
    c = tokens["colors"]
    f = tokens["fonts"]
    r = tokens.get("radius", {})
    lines = ['@import "tailwindcss";', '@import "./fonts.css";', "", "/* Design tokens measured from the original site. */", "@theme {"]
    for k, v in c.items():
        lines.append(f"  --color-{k}: {v};")
    stacks = tokens.get("fontStacks") or {}
    lines.append(f"  --font-heading: {stacks.get('heading') or _font_stack(f.get('heading'), SANS)};")
    lines.append(f"  --font-body: {stacks.get('body') or _font_stack(f.get('body'), SANS)};")
    if f.get("mono"):
        lines.append(f"  --font-mono: {stacks.get('mono') or _font_stack(f.get('mono'), MONO)};")
    lines.append(f"  --radius-button: {float(r.get('button') or 8):g}px;")
    lines.append(f"  --radius-card: {float(r.get('card') or 12):g}px;")
    if tokens.get("container"):
        lines.append(f"  --container-site: {tokens['container']}px;")
    else:
        lines.append("  --container-site: 1200px;")
    lines.append("}")
    fs = body.get("font-size") or "16px"
    lh = body.get("line-height") or "normal"
    lines += [
        "",
        "@layer base {",
        "  html { scroll-behavior: smooth; }",
        "  body {",
        "    background-color: var(--color-background);",
        "    color: var(--color-foreground);",
        "    font-family: var(--font-body);",
        f"    font-size: {fs};",
        f"    line-height: {lh};",
        "    -webkit-font-smoothing: antialiased;",
        "  }",
        "}",
        "",
    ]
    return "\n".join(lines)


def layout_tsx(title: str, description: str, lang: str, icon: str | None) -> str:
    meta = {"title": title or "Cloned site", "description": description or ""}
    if icon:
        meta["icons"] = {"icon": icon}
    return (
        'import type { Metadata } from "next";\n'
        'import type { ReactNode } from "react";\n'
        'import "./globals.css";\n\n'
        f"export const metadata: Metadata = {json.dumps(meta, ensure_ascii=False, indent=2)};\n\n"
        "export default function RootLayout({ children }: { children: ReactNode }) {\n"
        "  return (\n"
        f'    <html lang="{(lang or "en")[:10]}">\n'
        "      <body>{children}</body>\n"
        "    </html>\n"
        "  );\n"
        "}\n"
    )


def wrapper_class(sec: dict) -> str:
    """Outer spacing that the original page applied *around* this section."""
    cls = []
    gm, gd = sec.get("gap_m", 0) or 0, sec.get("gap_d", 0) or 0
    im, idd = sec.get("inset_m", 0) or 0, sec.get("inset_d", 0) or 0
    if gm > 2:
        cls.append(f"pt-[{gm}px]")
    if gd > 2 and gd != gm:
        cls.append(f"lg:pt-[{gd}px]")
    elif gd <= 2 < gm:
        cls.append("lg:pt-0")
    if im > 2:
        cls.append(f"px-[{im}px]")
    if idd > 2 and idd != im:
        cls.append(f"lg:px-[{idd}px]")
    elif idd <= 2 < im:
        cls.append("lg:px-0")
    return " ".join(cls) if cls else "contents"


def page_tsx(sections: list[dict]) -> str:
    """app/page.tsx - composed deterministically from the manifest (never LLM-written)."""
    imports = "\n".join(f'import {s["name"]} from "@/components/sections/{s["name"]}";' for s in sections)
    body = "\n".join(
        f'      <div data-section="{s["name"]}" className="{wrapper_class(s)}">\n        <{s["name"]} />\n      </div>'
        for s in sections
    )
    return (
        f"{imports}\n\n"
        "export default function Home() {\n"
        "  return (\n"
        '    <main className="min-h-screen overflow-x-clip">\n'
        f"{body}\n"
        "    </main>\n"
        "  );\n"
        "}\n"
    )


def write_scaffold(site_dir: Path, name: str, tokens: dict, body_style: dict, meta: dict, fonts_css: str) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "app").mkdir(exist_ok=True)
    (site_dir / "components" / "sections").mkdir(parents=True, exist_ok=True)
    (site_dir / "components" / "ui").mkdir(parents=True, exist_ok=True)
    (site_dir / "lib").mkdir(exist_ok=True)
    (site_dir / "public").mkdir(exist_ok=True)

    (site_dir / "package.json").write_text(json.dumps(package_json(name), indent=2) + "\n", encoding="utf-8")
    (site_dir / "tsconfig.json").write_text(json.dumps(TSCONFIG, indent=2) + "\n", encoding="utf-8")
    (site_dir / "next.config.mjs").write_text(NEXT_CONFIG, encoding="utf-8")
    (site_dir / "postcss.config.mjs").write_text(POSTCSS, encoding="utf-8")
    (site_dir / "next-env.d.ts").write_text(NEXT_ENV, encoding="utf-8")
    (site_dir / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (site_dir / "app" / "globals.css").write_text(theme_css(tokens, body_style), encoding="utf-8")
    (site_dir / "app" / "fonts.css").write_text(fonts_css or "/* no web fonts captured */\n", encoding="utf-8")
    icon = next((i for i in meta.get("icons_local", []) if i), None)
    (site_dir / "app" / "layout.tsx").write_text(
        layout_tsx(meta.get("title", ""), meta.get("description", ""), meta.get("lang", "en"), icon),
        encoding="utf-8")
    for sub in ("ui", "lib"):
        for f in (TEMPLATES / sub).iterdir():
            dest = site_dir / ("components/ui" if sub == "ui" else "lib") / f.name
            shutil.copyfile(f, dest)  # binary copy keeps UTF-8
    (site_dir / "README.md").write_text(
        f"# {name}\n\nGenerated by the AI Website Clone Agent from {meta.get('url', '')}.\n\n"
        "```bash\nnpm install\nnpm run dev\n```\n\n"
        "- `app/globals.css` - theme tokens (colours, fonts, radii)\n"
        "- `components/sections/*` - one component per page section\n"
        "- `components/ui/*` - shared Button/Container\n",
        encoding="utf-8",
    )


def write_page(site_dir: Path, sections: list[dict]) -> None:
    (site_dir / "app" / "page.tsx").write_text(page_tsx(sections), encoding="utf-8")


# ---- token edits (used by the modification engine) -------------------------
def update_theme_vars(css: str, changes: dict) -> tuple[str, list[str]]:
    """Replace ``--var: value;`` entries inside the @theme block. Returns (css, applied)."""
    applied = []
    for var, value in changes.items():
        var = var if var.startswith("--") else f"--{var}"
        value = str(value).strip().rstrip(";")
        if not re.match(r"^[#\w\s,.'\"()%-]+$", value):
            continue
        pat = re.compile(rf"({re.escape(var)}\s*:\s*)([^;]+)(;)")
        if pat.search(css):
            css = pat.sub(lambda m: m.group(1) + value + m.group(3), css, count=1)
            applied.append(var)
        elif var.startswith(("--color-", "--font-", "--radius-")):
            css = css.replace("@theme {", f"@theme {{\n  {var}: {value};", 1)
            applied.append(var)
    return css, applied
