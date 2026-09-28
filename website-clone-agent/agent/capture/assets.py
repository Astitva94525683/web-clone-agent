"""Download and store images, inline SVGs and web fonts for the generated site.

Everything is saved under ``site/public`` so the clone works offline and never
hot-links the original website.
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urljoin, urlparse

Fetcher = Callable[[str], Optional[tuple[bytes, str]]]

CT_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp",
    "image/gif": "gif", "image/svg+xml": "svg", "image/avif": "avif", "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico", "font/woff2": "woff2", "font/woff": "woff",
    "font/ttf": "ttf", "font/otf": "otf", "application/font-woff2": "woff2",
    "application/font-woff": "woff", "application/x-font-woff": "woff",
    "application/x-font-ttf": "ttf", "application/vnd.ms-fontobject": "eot",
}
IMG_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "svg", "avif", "ico"}
FONT_EXTS = {"woff2", "woff", "ttf", "otf"}
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 60 * 1024 * 1024
MAX_FONT_FILES = 24


def _hash(s: str | bytes, n: int = 10) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8", "ignore")
    return hashlib.sha1(s).hexdigest()[:n]


def _ext_for(url: str, content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in CT_EXT:
        return CT_EXT[ct]
    path = urlparse(url).path.lower()
    m = re.search(r"\.([a-z0-9]{2,5})$", path)
    if m:
        ext = m.group(1)
        return "jpg" if ext == "jpeg" else ext
    return "bin"


def _decode_data_uri(uri: str) -> Optional[tuple[bytes, str]]:
    m = re.match(r"data:([^,]*?),(.*)", uri, re.S)
    if not m:
        return None
    params = [p.strip() for p in m.group(1).split(";")]
    ct = params[0] or "application/octet-stream"
    payload = m.group(2)
    try:
        data = base64.b64decode(payload) if "base64" in params else unquote(payload).encode("utf-8")
    except Exception:
        return None
    return data, ct


class AssetStore:
    """Keeps a url -> local public path map and writes files to ``public/``."""

    def __init__(self, public_dir: Path, fetch: Fetcher):
        self.public = public_dir
        self.fetch = fetch
        self.map: dict[str, Optional[str]] = {}
        self.total_bytes = 0
        self.failed: list[str] = []
        (self.public / "assets").mkdir(parents=True, exist_ok=True)

    # ---- images --------------------------------------------------------
    def image(self, url: str) -> Optional[str]:
        """Return a local ``/assets/..`` path for an image URL (downloading once)."""
        if not url or url.startswith("blob:"):
            return None
        if url in self.map:
            return self.map[url]
        local = None
        try:
            got = _decode_data_uri(url) if url.startswith("data:") else self.fetch(url)
            if got:
                data, ct = got
                ext = _ext_for(url if not url.startswith("data:") else "", ct)
                if ext == "bin" and data[:5] in (b"<?xml", b"<svg ") or data.lstrip()[:4] == b"<svg":
                    ext = "svg"
                if ext in IMG_EXTS and 0 < len(data) <= MAX_IMAGE_BYTES and \
                        self.total_bytes + len(data) <= MAX_TOTAL_IMAGE_BYTES:
                    name = f"img-{_hash(url)}.{ext}"
                    (self.public / "assets" / name).write_bytes(data)
                    self.total_bytes += len(data)
                    local = f"/assets/{name}"
        except Exception:
            local = None
        if local is None:
            self.failed.append(url[:200])
        self.map[url] = local
        return local

    def svg(self, markup: str) -> Optional[str]:
        """Store serialised inline SVG markup as a file and return its public path."""
        if not markup:
            return None
        key = "svg:" + _hash(markup, 16)
        if key in self.map:
            return self.map[key]
        name = f"svg-{_hash(markup)}.svg"
        (self.public / "assets" / name).write_text(markup, encoding="utf-8")
        self.map[key] = f"/assets/{name}"
        return self.map[key]

    def raw(self, data: bytes, ext: str, key: str) -> str:
        name = f"shot-{_hash(key)}.{ext}"
        (self.public / "assets" / name).write_bytes(data)
        return f"/assets/{name}"

    def rewrite_css_urls(self, value: str) -> str:
        """Replace url(...) references in a CSS value with local asset paths."""

        def repl(m: re.Match) -> str:
            u = m.group(2)
            local = self.image(u)
            return f'url("{local}")' if local else m.group(0)

        return re.sub(r"url\((['\"]?)(.*?)\1\)", repl, value or "")


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
_FACE_RE = re.compile(r"@font-face\s*{([^}]*)}", re.I | re.S)


def _prop(block: str, name: str) -> str:
    m = re.search(rf"(?:^|;|\s){name}\s*:\s*([^;]+)", block, re.I)
    return m.group(1).strip() if m else ""


def parse_font_faces(css_text: str, base_url: str) -> list[dict]:
    faces = []
    for m in _FACE_RE.finditer(css_text or ""):
        block = m.group(1)
        faces.append({
            "family": _prop(block, "font-family").strip("'\" "),
            "weight": _prop(block, "font-weight") or "400",
            "style": _prop(block, "font-style") or "normal",
            "src": _prop(block, "src"),
            "unicodeRange": _prop(block, "unicode-range"),
            "base": base_url,
        })
    return faces


def clean_family(raw: str) -> str:
    """Normalise generated family names, e.g. next/font's ``__Inter_d65c78`` -> ``Inter``."""
    f = raw.strip().strip("'\"")
    m = re.match(r"^__(.+?)_[0-9a-f]{5,8}$", f)
    if m:
        f = m.group(1).replace("_", " ")
    return f


def first_family(stack: str) -> str:
    return clean_family((stack or "").split(",")[0])


GENERIC = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui", "ui-sans-serif",
           "ui-serif", "ui-monospace", "ui-rounded", "math", "emoji"}
SANS_FALLBACK = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'


def clean_stack(stack: str) -> str:
    """Normalise a computed font-family list, keeping the original generic fallback."""
    out: list[str] = []
    for part in (stack or "").split(","):
        name = clean_family(part)
        if not name or "fallback" in name.lower() or name in out:
            continue
        out.append(name)
    if not out:
        return SANS_FALLBACK
    quoted = [n if n.lower() in GENERIC or re.match(r"^[A-Za-z-]+$", n) else f'"{n}"' for n in out]
    if not any(n.lower() in GENERIC for n in out):
        quoted.append(SANS_FALLBACK)
    return ", ".join(quoted)


def _covers_basic_latin(unicode_range: str) -> bool:
    if not unicode_range:
        return True
    for part in unicode_range.split(","):
        part = part.strip().upper().removeprefix("U+")
        try:
            if "?" in part:
                lo, hi = int(part.replace("?", "0"), 16), int(part.replace("?", "F"), 16)
            elif "-" in part:
                a, b = part.split("-", 1)
                lo, hi = int(a, 16), int(b, 16)
            else:
                lo = hi = int(part, 16)
        except ValueError:
            continue
        if lo <= 0x41 <= hi:
            return True
    return False


def _pick_src(src: str, base: str) -> Optional[str]:
    urls = re.findall(r"url\((['\"]?)(.*?)\1\)\s*(?:format\((['\"]?)(.*?)\3\))?", src or "")
    if not urls:
        return None
    rank = {"woff2": 0, "woff": 1, "truetype": 2, "opentype": 3}

    def score(u):
        fmt = (u[3] or "").lower()
        if not fmt:
            ext = urlparse(u[1]).path.rsplit(".", 1)[-1].lower()
            fmt = {"ttf": "truetype", "otf": "opentype"}.get(ext, ext)
        return rank.get(fmt, 9)

    best = sorted(urls, key=score)[0][1]
    return best if best.startswith("data:") else urljoin(base, best)


def build_fonts(faces: list[dict], used_raw_families: set[str], public_dir: Path, fetch: Fetcher) -> tuple[str, list[dict]]:
    """Download the @font-face files actually used on the page.

    Returns (css_text for app/fonts.css, list of saved font descriptors).
    """
    used = {f.lower() for f in used_raw_families}
    (public_dir / "fonts").mkdir(parents=True, exist_ok=True)
    seen, css, saved = set(), [], []
    for face in faces:
        fam_raw = face.get("family", "")
        if fam_raw.lower() not in used or "fallback" in fam_raw.lower():
            continue
        if not _covers_basic_latin(face.get("unicodeRange", "")):
            continue
        url = _pick_src(face.get("src", ""), face.get("base", ""))
        if not url:
            continue
        key = (fam_raw.lower(), face.get("weight"), face.get("style"), url)
        if key in seen:
            continue
        seen.add(key)
        if len(saved) >= MAX_FONT_FILES:
            break
        got = _decode_data_uri(url) if url.startswith("data:") else fetch(url)
        if not got:
            continue
        data, ct = got
        ext = _ext_for(url if not url.startswith("data:") else "", ct)
        if ext not in FONT_EXTS:
            # some CDNs send fonts as application/octet-stream
            sig = data[:4]
            ext = {b"wOF2": "woff2", b"wOFF": "woff", b"\x00\x01\x00\x00": "ttf", b"OTTO": "otf"}.get(sig, "")
            if not ext:
                continue
        fam = clean_family(fam_raw)
        name = f"{re.sub(r'[^a-z0-9]+', '-', fam.lower())}-{_hash(url, 8)}.{ext}"
        (public_dir / "fonts" / name).write_bytes(data)
        fmt = {"woff2": "woff2", "woff": "woff", "ttf": "truetype", "otf": "opentype"}[ext]
        ur = face.get("unicodeRange")
        css.append(
            "@font-face {\n"
            f'  font-family: "{fam}";\n'
            f'  src: url("/fonts/{name}") format("{fmt}");\n'
            f"  font-weight: {face.get('weight') or '400'};\n"
            f"  font-style: {face.get('style') or 'normal'};\n"
            "  font-display: swap;\n"
            + (f"  unicode-range: {ur};\n" if ur else "")
            + "}\n"
        )
        saved.append({"family": fam, "raw": fam_raw, "weight": face.get("weight"), "file": f"/fonts/{name}"})
    return "\n".join(css), saved
