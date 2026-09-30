"""Version history for a generated site: snapshot, diff, undo.

Only source folders are snapshotted (app/, components/, lib/) plus the
manifest - assets never change - so snapshots are tiny and instant.
"""
from __future__ import annotations

import difflib
import json
import shutil
import time
from pathlib import Path
from typing import Optional

from ..project import Project

TRACKED = ("app", "components", "lib")


def _files(root: Path) -> dict[str, Path]:
    out = {}
    for d in TRACKED:
        base = root / d
        if base.exists():
            for p in base.rglob("*"):
                if p.is_file() and p.suffix in (".tsx", ".ts", ".css", ".json"):
                    out[p.relative_to(root).as_posix()] = p
    return out


def versions(project: Project) -> list[dict]:
    return project.read_json("history.json", []) or []


def current_version(project: Project) -> int:
    v = versions(project)
    return v[-1]["version"] if v else 0


def snapshot(project: Project, message: str, kind: str = "edit", extra: Optional[dict] = None) -> int:
    hist = versions(project)
    n = (hist[-1]["version"] + 1) if hist else 1
    dest = project.versions_dir / f"v{n:03d}"
    if dest.exists():
        shutil.rmtree(dest)
    for rel, src in _files(project.site).items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    (dest / "manifest.json").write_text(json.dumps(project.manifest, indent=2), encoding="utf-8")
    hist.append({"version": n, "message": message, "kind": kind, "ts": time.time(), **(extra or {})})
    project.write_json("history.json", hist)
    return n


def restore(project: Project, version: int) -> None:
    src = project.versions_dir / f"v{version:03d}"
    if not src.exists():
        raise FileNotFoundError(f"Version {version} not found")
    snap = _files(src)
    for rel, path in _files(project.site).items():
        if rel not in snap:
            path.unlink()
    for rel, path in snap.items():
        target = project.site / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    mf = src / "manifest.json"
    if mf.exists():
        project.save_manifest(json.loads(mf.read_text(encoding="utf-8")))


def diff_versions(project: Project, a: int, b: Optional[int] = None, context: int = 2) -> str:
    """Unified diff between version a and version b (default: working tree)."""
    ra = project.versions_dir / f"v{a:03d}"
    rb = project.versions_dir / f"v{b:03d}" if b else project.site
    fa, fb = _files(ra), _files(rb)
    chunks = []
    for rel in sorted(set(fa) | set(fb)):
        ta = fa[rel].read_text(encoding="utf-8").splitlines(keepends=True) if rel in fa else []
        tb = fb[rel].read_text(encoding="utf-8").splitlines(keepends=True) if rel in fb else []
        if ta != tb:
            chunks.append("".join(difflib.unified_diff(ta, tb, f"a/{rel}", f"b/{rel}", n=context)))
    return "\n".join(chunks)


def changed_files(project: Project, a: int, b: Optional[int] = None) -> list[str]:
    ra = project.versions_dir / f"v{a:03d}"
    rb = project.versions_dir / f"v{b:03d}" if b else project.site
    fa, fb = _files(ra), _files(rb)
    return [rel for rel in sorted(set(fa) | set(fb))
            if (rel not in fa) or (rel not in fb) or fa[rel].read_bytes() != fb[rel].read_bytes()]
