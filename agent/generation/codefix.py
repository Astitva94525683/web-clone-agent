"""Deterministic clean-up of LLM-written TSX before it is type-checked.

Each fix here is a common, mechanical LLM mistake that would otherwise cost a
build + an LLM repair round-trip. Fixing it with code is instant and free.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Optional

HOOKS = re.compile(r"\buse(State|Effect|Ref|Reducer|Memo|Callback|LayoutEffect|Transition|Id)\s*[(<]")
HANDLERS = re.compile(r"\bon(Click|Change|Submit|Mouse\w*|Key\w*|Focus|Blur|Input|Toggle|Scroll)\s*=\s*\{")
ALLOWED_MODULES = ("react", "next/link", "next/image", "lucide-react", "@/components/ui", "@/lib/cn")


def extract_code(text: str) -> Optional[str]:
    """Pull the TSX source out of a model reply (largest fenced block wins)."""
    blocks = re.findall(r"```(?:tsx|jsx|typescript|ts|javascript|js|react)?[ \t]*\n(.*?)```", text or "", re.S)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    # unterminated fence (truncated output) -> take everything after it
    m = re.search(r"```(?:tsx|jsx|typescript|ts)?[ \t]*\n(.*)$", text or "", re.S)
    if m and "export default" in m.group(1):
        return m.group(1).strip() + "\n"
    if text and "export default" in text:
        return text.strip() + "\n"
    return None


@functools.lru_cache(maxsize=4)
def lucide_icons(node_modules: str) -> frozenset:
    d = Path(node_modules) / "lucide-react" / "dist" / "lucide-react.d.ts"
    try:
        src = d.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    names = set()
    for block in re.findall(r"export\s*{([^}]*)}", src):
        for entry in block.split(","):
            entry = entry.strip()
            if not entry:
                continue
            names.add(entry.split(" as ")[-1].strip())
    return frozenset(names)


def fix_lucide(code: str, icons: frozenset) -> tuple[str, list[str]]:
    """Replace icon names that don't exist in the installed lucide-react with a safe icon."""
    if not icons:
        return code, []
    m = re.search(r'import\s*{([^}]*)}\s*from\s*["\']lucide-react["\'];?', code)
    if not m:
        return code, []
    fixed, keep = [], []
    for part in m.group(1).split(","):
        part = part.strip()
        if not part:
            continue
        orig, _, alias = part.partition(" as ")
        orig, alias = orig.strip(), alias.strip()
        local = alias or orig
        if orig in icons or part.startswith("type "):
            keep.append(part)
        else:
            fixed.append(orig)
            keep.append(f"Circle as {local}" if local != "Circle" else "Circle")
    if not fixed:
        return code, []
    keep = list(dict.fromkeys(keep))
    code = code[: m.start()] + "import { " + ", ".join(keep) + ' } from "lucide-react";' + code[m.end():]
    return code, fixed


def sanitize(code: str, component: str, node_modules: Optional[str] = None) -> tuple[str, list[str]]:
    """Apply mechanical fixes. Returns (code, list of notes about what changed)."""
    notes: list[str] = []
    code = code.replace("\r\n", "\n")
    # 1. stray CSS imports / html wrappers
    new = re.sub(r'^\s*import\s+["\'][^"\']+\.css["\'];?\s*$', "", code, flags=re.M)
    if new != code:
        notes.append("removed CSS import")
        code = new
    # 2. class= / for= -> className= / htmlFor= (JSX attribute position only)
    new = re.sub(r'(<[A-Za-z][\w.]*\b[^<>]*?\s)class=(["{])', r"\1className=\2", code)
    new = re.sub(r'(<label\b[^<>]*?\s)for=(["{])', r"\1htmlFor=\2", new)
    if new != code:
        notes.append("class->className")
        code = new
    # 3. "use client" directive placement
    needs_client = bool(HOOKS.search(code) or HANDLERS.search(code))
    has_client = re.search(r'^\s*["\']use client["\'];?', code, re.M)
    if has_client and not code.lstrip().startswith(("'use client'", '"use client"')):
        code = re.sub(r'^\s*["\']use client["\'];?\s*$\n?', "", code, flags=re.M)
        has_client = None
        needs_client = True
    if needs_client and not has_client:
        code = '"use client";\n\n' + code.lstrip()
        notes.append("added 'use client'")
    # 4. default export
    if not re.search(r"export\s+default\b", code):
        m = re.search(r"(?:function|const)\s+([A-Z]\w*)", code)
        if m:
            code = code.rstrip() + f"\n\nexport default {m.group(1)};\n"
            notes.append("added default export")
    # 5. React namespace usage without import (React.ReactNode etc.)
    if re.search(r"\bReact\.", code) and not re.search(r"import\s+(\*\s+as\s+)?React\b", code):
        code = re.sub(r'^("use client";\n+)?', lambda m: (m.group(1) or "") + 'import * as React from "react";\n', code, count=1)
        notes.append("added React import")
    # 6. lucide icon names
    if node_modules:
        code, bad = fix_lucide(code, lucide_icons(node_modules))
        if bad:
            notes.append("replaced unknown icons: " + ", ".join(bad))
    return code, notes


def disallowed_imports(code: str) -> list[str]:
    mods = re.findall(r'^\s*import[^;]*?from\s*["\']([^"\']+)["\']', code, re.M)
    mods += re.findall(r'^\s*import\s*["\']([^"\']+)["\']', code, re.M)
    return [m for m in mods if not m.startswith(ALLOWED_MODULES) and not m.startswith("@/components/")]


_PATCH = re.compile(r"<{5,9} ?SEARCH\s*?\n(.*?)\n?={5,9}\s*?\n(.*?)\n?>{5,9} ?REPLACE", re.S)


def apply_patch(code: str, reply: str) -> Optional[str]:
    """Apply SEARCH/REPLACE edit blocks from a model reply. Returns None if there are no blocks
    or any block cannot be located (exactly, or ignoring indentation)."""
    blocks = _PATCH.findall(reply or "")
    if not blocks:
        return None
    for search, replace in blocks:
        if search and code.count(search) == 1:
            code = code.replace(search, replace, 1)
            continue
        # tolerate indentation / trailing-space differences: match line by line on stripped text
        lines, want = code.split("\n"), [ln.strip() for ln in search.strip("\n").split("\n")]
        if not want or not any(want):
            return None
        hits = [i for i in range(len(lines) - len(want) + 1)
                if [ln.strip() for ln in lines[i:i + len(want)]] == want]
        if len(hits) != 1:
            return None
        i = hits[0]
        indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        new_lines = [indent + ln.strip() if ln.strip() else ln for ln in replace.strip("\n").split("\n")] if replace.strip() else []
        lines[i:i + len(want)] = new_lines
        code = "\n".join(lines)
    return code
