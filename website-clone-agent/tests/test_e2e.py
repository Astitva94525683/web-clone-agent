"""End-to-end tests on a local fixture site (no internet, no API key needed).

1. offline mode: deterministic fallback renderer for every section
2. scripted LLM: exercises the real LLM code path - sanitising, tsc repair loop,
   fallback when the model returns no code, and multi-operation modifications.

Requires Node.js 20.9+ and Playwright's Chromium. Slow (~2-4 min).
"""
from __future__ import annotations

import functools
import http.server
import shutil
import threading
from pathlib import Path

import pytest

from agent.config import settings

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")


@pytest.fixture(scope="module")
def site_url():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/landing.html"
    server.shutdown()


def _sections(project):
    return {s["name"]: s for s in project.manifest["sections"]}


def test_offline_clone_and_edits(site_url, monkeypatch):
    from agent.events import Reporter
    from agent.llm import LLM
    from agent.modification.modify import modify, undo
    from agent.pipeline import clone, previews

    monkeypatch.setattr(settings, "provider", "mock")
    llm = LLM()
    assert llm.is_mock
    res = clone(site_url, Reporter(), llm=llm, preview=True)
    try:
        assert res.ok, res.error
        secs = res.project.manifest["sections"]
        assert len(secs) >= 5
        assert secs[0]["kind"] == "header" and secs[-1]["kind"] == "footer"
        assert all(s["source"] == "fallback" for s in secs)
        assert res.scores["desktop"]["score"] > 70, res.scores
        assert res.scores["runtime_ok"]
        page = (res.project.site / "app" / "page.tsx").read_text()
        assert all(s["name"] in page for s in secs)
        # offline edits
        r1 = modify(res.project, "Change the primary color to blue", llm, Reporter())
        assert r1.ok and "app/globals.css" in r1.changed_files
        assert "--color-primary: #2563eb" in (res.project.site / "app/globals.css").read_text()
        r2 = modify(res.project, "Make the navbar sticky", llm, Reporter())
        assert r2.ok
        assert "sticky top-0" in (res.project.site / "components/sections/Navbar.tsx").read_text()
        assert undo(res.project) is not None
        assert "sticky top-0" not in (res.project.site / "components/sections/Navbar.tsx").read_text()
    finally:
        previews().stop_all()


def test_llm_path_with_scripted_model(site_url, monkeypatch):
    from agent.events import Reporter
    from agent.modification.modify import modify
    from agent.pipeline import clone, previews
    from tests.fakes import ScriptedLLM

    monkeypatch.setattr(settings, "refine_enabled", False)
    llm = ScriptedLLM()
    res = clone(site_url, Reporter(), llm=llm, preview=False)
    assert res.ok, res.error
    secs = _sections(res.project)
    assert list(secs)[:3] == ["Navbar", "Hero", "FeatureGrid"]
    assert secs["Navbar"]["source"] == "llm"
    nav = (res.project.site / "components/sections/Navbar.tsx").read_text()
    assert nav.startswith('"use client";')                      # sanitizer added directive
    assert "Circle as HamburgerIconThatDoesNotExist" in nav      # unknown icon replaced
    assert 'class="' not in nav
    assert secs["Pricing"]["source"] == "fallback"               # no code -> deterministic fallback
    val = res.project.read_json("validation.json")
    assert "components/sections/FeatureGrid.tsx" in val["repaired"]  # tsc error -> LLM repair
    assert val["build_ok"] is True
    stages = [c["stage"] for c in res.project.read_json("llm_calls.json")]
    assert "analyze" in stages and "repair:FeatureGrid.tsx" in stages
    assert llm.saw_image  # analysis sends a screenshot to multimodal models

    # multi-operation natural-language edit
    r = modify(res.project, "Make it blue, sticky nav, add testimonials, remove pricing", llm, Reporter())
    assert r.ok, r.error
    names = [s["name"] for s in res.project.manifest["sections"]]
    assert "CustomerStories" in names and "Pricing" not in names
    assert names.index("CustomerStories") == names.index("FeatureGrid") + 1
    assert "sticky top-0" in (res.project.site / "components/sections/Navbar.tsx").read_text()
    assert "#2563eb" in (res.project.site / "app/globals.css").read_text()
    assert not (res.project.site / "components/sections/Pricing.tsx").exists()
    previews().stop_all()
