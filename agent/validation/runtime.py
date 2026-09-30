"""Local Node.js runtime: one shared node_modules for every generated site,
type-checking, production builds and dev-server (preview) processes.

Installing Next.js once (instead of per site) makes each new clone start in
seconds instead of a minute, and keeps disk usage flat.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..config import settings
from ..events import Reporter
from ..generation.scaffold import VERSIONS

IS_WIN = sys.platform.startswith("win")


class RuntimeErrorNode(RuntimeError):
    pass


def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd + ".cmd") if IS_WIN and shutil.which(cmd + ".cmd") else shutil.which(cmd)


@dataclass
class CheckError:
    file: str
    line: int
    col: int
    message: str

    def __str__(self) -> str:
        return f"{self.file}({self.line},{self.col}): {self.message}"


@dataclass
class BuildResult:
    ok: bool
    errors: list = field(default_factory=list)  # list[CheckError]
    output: str = ""
    seconds: float = 0.0


class NodeRuntime:
    def __init__(self, workspace: Optional[Path] = None):
        self.ws = (workspace or settings.workspace).resolve()
        self.node_modules = self.ws / "node_modules"
        self._install_lock = threading.Lock()

    # ------------------------------------------------------------------
    def check_node(self) -> str:
        node = _which("node")
        if not node:
            raise RuntimeErrorNode("Node.js is not installed (need v20.9+). Install it from nodejs.org.")
        out = subprocess.run([node, "--version"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace").stdout.strip()
        m = re.match(r"v(\d+)\.(\d+)", out)
        if m and (int(m.group(1)), int(m.group(2))) < (20, 9):
            raise RuntimeErrorNode(f"Node.js {out} is too old; Next.js 16 needs v20.9 or newer.")
        return out

    @property
    def ready(self) -> bool:
        pkg = self.node_modules / "next" / "package.json"
        if not pkg.exists():
            return False
        try:
            return json.loads(pkg.read_text(encoding="utf-8"))["version"] == VERSIONS["next"] and \
                (self.node_modules / "@tailwindcss" / "postcss").exists()
        except (OSError, ValueError, KeyError):
            return False

    def ensure(self, reporter: Optional[Reporter] = None) -> None:
        """Install the shared runtime once (idempotent, thread-safe)."""
        rep = reporter or Reporter()
        with self._install_lock:
            if self.ready:
                return
            self.check_node()
            npm = _which("npm")
            if not npm:
                raise RuntimeErrorNode("npm was not found on PATH.")
            self.ws.mkdir(parents=True, exist_ok=True)
            pkg = {"name": "clone-agent-runtime", "private": True,
                   "dependencies": {k: v for k, v in VERSIONS.items()}}
            (self.ws / "package.json").write_text(json.dumps(pkg, indent=2), encoding="utf-8")
            rep.step("validate", "Installing shared Next.js runtime (first run only, ~30-60s)")
            t0 = time.time()
            proc = subprocess.run([npm, "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=self.ws,
                                  capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
            if proc.returncode != 0:
                raise RuntimeErrorNode("npm install failed:\n" + (proc.stderr or proc.stdout)[-1500:])
            rep.success("validate", f"Runtime installed in {time.time() - t0:.0f}s")

    # ------------------------------------------------------------------
    def _node(self, script: str, args: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
        node = _which("node") or "node"
        env = dict(os.environ, NEXT_TELEMETRY_DISABLED="1", NODE_OPTIONS=os.environ.get("NODE_OPTIONS", ""))
        try:
            p = subprocess.run([node, str(self.node_modules / script)] + args, cwd=cwd, capture_output=True,
                               text=True, timeout=timeout, env=env, encoding="utf-8", errors="replace")
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired as e:
            return 124, f"timed out after {timeout}s\n{(e.stdout or '')}"

    def typecheck(self, site: Path) -> BuildResult:
        t0 = time.time()
        code, out = self._node("typescript/bin/tsc", ["--noEmit", "-p", ".", "--pretty", "false"], site, 180)
        errors = []
        for m in re.finditer(r"^(.+?\.tsx?)\((\d+),(\d+)\): error (TS\d+: .+)$", out, re.M):
            errors.append(CheckError(m.group(1).replace("\\", "/"), int(m.group(2)), int(m.group(3)), m.group(4)))
        ok = code == 0
        if not ok and not errors:
            errors.append(CheckError("?", 0, 0, out.strip()[-800:]))
        return BuildResult(ok, errors, out[-6000:], round(time.time() - t0, 1))

    def build(self, site: Path) -> BuildResult:
        t0 = time.time()
        code, out = self._node("next/dist/bin/next", ["build"], site, 420)
        errors = []
        if code != 0:
            seen = set()
            for m in re.finditer(r"\./((?:components|app|lib)/[\w./-]+\.tsx?)(?::(\d+):(\d+))?", out):
                f = m.group(1)
                if f in seen:
                    continue
                seen.add(f)
                # message = the few lines after the file reference
                tail = out[m.end(): m.end() + 700].strip()
                errors.append(CheckError(f, int(m.group(2) or 0), int(m.group(3) or 0), tail[:600]))
            for m in re.finditer(r"^(.+?\.tsx?)\((\d+),(\d+)\): error (TS\d+: .+)$", out, re.M):
                errors.append(CheckError(m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)))
            if not errors:
                errors.append(CheckError("?", 0, 0, out.strip()[-1500:]))
        return BuildResult(code == 0, errors, out[-8000:], round(time.time() - t0, 1))


# ---------------------------------------------------------------------------
# Dev servers (local preview)
# ---------------------------------------------------------------------------
def _port_answers(port: int) -> bool:
    """True if anything accepts connections on this port (IPv4 or IPv6 localhost)."""
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue  # e.g. no IPv6 on this machine
    return False


def free_port(start: int, taken: set | None = None) -> int:
    """First port nothing is listening on. Binding alone is not enough: on Windows a bind to
    127.0.0.1 succeeds even while another server (e.g. an older preview) listens on [::]."""
    for port in range(start, start + 200):
        if taken and port in taken or _port_answers(port):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeErrorNode("No free port found for the preview server.")


@dataclass
class DevServer:
    slug: str
    port: int
    proc: subprocess.Popen
    log_path: Path

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        return self.proc.poll() is None

    def log_tail(self, n: int = 4000) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-n:]
        except OSError:
            return ""


class PreviewManager:
    """Keeps one `next dev` process per project; survives Streamlit reruns."""

    def __init__(self, runtime: NodeRuntime):
        self.runtime = runtime
        self.servers: dict[str, DevServer] = {}
        atexit.register(self.stop_all)

    def start(self, slug: str, site: Path, log_dir: Path, timeout: int = 90) -> DevServer:
        srv = self.servers.get(slug)
        if srv and srv.alive():
            return srv
        port = free_port(settings.preview_port_start, {s.port for s in self.servers.values() if s.alive()})
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "dev-server.log"
        log = open(log_path, "w", encoding="utf-8")
        node = _which("node") or "node"
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WIN else {"start_new_session": True}
        proc = subprocess.Popen(
            [node, str(self.runtime.node_modules / "next/dist/bin/next"), "dev", "-p", str(port), "-H", "127.0.0.1"],
            cwd=site, stdout=log, stderr=subprocess.STDOUT, env=dict(os.environ, NEXT_TELEMETRY_DISABLED="1"),
            **kwargs,
        )
        srv = DevServer(slug, port, proc, log_path)
        self.servers[slug] = srv
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not srv.alive():
                self.servers.pop(slug, None)
                raise RuntimeErrorNode("Preview server exited:\n" + srv.log_tail(1500))
            try:
                with urllib.request.urlopen(srv.url, timeout=60):
                    pass
            except urllib.error.HTTPError:
                pass  # server is up; the runtime check will report the page error
            except Exception:
                time.sleep(0.8)
                continue
            # Only trust the answer if it came from *our* process (never a leftover server).
            time.sleep(0.5)
            if not srv.alive():
                self.servers.pop(slug, None)
                raise RuntimeErrorNode("Preview server exited:\n" + srv.log_tail(1500))
            return srv
        self.stop(slug)
        raise RuntimeErrorNode("Preview server did not respond in time:\n" + srv.log_tail(1500))

    def get(self, slug: str) -> Optional[DevServer]:
        srv = self.servers.get(slug)
        return srv if srv and srv.alive() else None

    def stop(self, slug: str) -> None:
        srv = self.servers.pop(slug, None)
        if not srv:
            return
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(srv.proc.pid)], capture_output=True)
            else:
                os.killpg(os.getpgid(srv.proc.pid), signal.SIGTERM)
            srv.proc.wait(timeout=10)
        except Exception:
            try:
                srv.proc.kill()
            except Exception:
                pass

    def stop_all(self) -> None:
        for slug in list(self.servers):
            self.stop(slug)
