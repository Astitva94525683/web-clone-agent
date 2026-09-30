"""On-disk layout of one cloned website.

workspace/
  package.json + node_modules/     shared Node runtime (installed once, reused by every site)
  projects/<slug>/
    site/                          the generated Next.js app (plain, standalone project)
    data/                          agent metadata: capture, analysis, manifest, logs, versions
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .config import settings


def slugify(url: str) -> str:
    host = urlparse(url).netloc or url
    host = host.lower().removeprefix("www.")
    path = urlparse(url).path.strip("/")
    base = host + ("-" + path if path else "")
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:48]
    return slug or "site"


@dataclass
class Project:
    slug: str
    root: Path

    # ---- paths ---------------------------------------------------------
    @property
    def site(self) -> Path:
        return self.root / "site"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def capture_dir(self) -> Path:
        return self.data / "capture"

    @property
    def validation_dir(self) -> Path:
        return self.data / "validation"

    @property
    def versions_dir(self) -> Path:
        return self.data / "versions"

    @property
    def sections_dir(self) -> Path:
        return self.site / "components" / "sections"

    # ---- json helpers --------------------------------------------------
    def read_json(self, name: str, default: Any = None) -> Any:
        p = self.data / name
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.data / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return p

    @property
    def manifest(self) -> dict:
        return self.read_json("manifest.json", {}) or {}

    def save_manifest(self, manifest: dict) -> None:
        manifest["updated"] = time.time()
        self.write_json("manifest.json", manifest)

    def append_llm_calls(self, calls: list[dict]) -> None:
        log = self.read_json("llm_calls.json", []) or []
        log.extend(calls)
        self.write_json("llm_calls.json", log)

    # ---- lifecycle -----------------------------------------------------
    @classmethod
    def create(cls, url: str) -> "Project":
        base = slugify(url)
        projects = settings.workspace / "projects"
        projects.mkdir(parents=True, exist_ok=True)
        slug, i = base, 2
        while (projects / slug).exists():
            slug, i = f"{base}-{i}", i + 1
        proj = cls(slug=slug, root=projects / slug)
        for d in (proj.site, proj.capture_dir, proj.validation_dir, proj.versions_dir):
            d.mkdir(parents=True, exist_ok=True)
        return proj

    @classmethod
    def load(cls, slug: str) -> "Project":
        root = settings.workspace / "projects" / slug
        if not root.exists():
            raise FileNotFoundError(f"No project named '{slug}' in {root.parent}")
        return cls(slug=slug, root=root)

    @classmethod
    def list_all(cls) -> list["Project"]:
        projects = settings.workspace / "projects"
        if not projects.exists():
            return []
        out = [cls(slug=p.name, root=p) for p in projects.iterdir() if (p / "data").exists()]
        return sorted(out, key=lambda p: p.root.stat().st_mtime, reverse=True)

    def section_file(self, name: str) -> Path:
        return self.sections_dir / f"{name}.tsx"

    def rel(self, path: Path) -> str:
        return path.relative_to(self.site).as_posix()

    def file_at(self, rel_path: str) -> Optional[Path]:
        """Resolve a site-relative path, refusing anything outside the site folder."""
        p = (self.site / rel_path).resolve()
        if self.site.resolve() not in p.parents and p != self.site.resolve():
            return None
        return p
