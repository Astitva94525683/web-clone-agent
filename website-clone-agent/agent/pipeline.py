"""The agent: URL -> Capture -> Analyze -> Generate -> Validate -> Preview (+ Refine).

This module only orchestrates; each stage lives in its own package.
"""
from __future__ import annotations

import concurrent.futures as cf
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import prompts
from .analysis.blueprint import build_blueprint, kebab
from .analysis.outline import OutlineBuilder, outline_for
from .analysis.stylemap import TokenMap, px
from .analysis.tokens import derive_tokens
from .capture.browser import CaptureError, capture
from .capture.dom import Section, build_tree, find_sections
from .config import settings
from .events import Reporter
from .generation.codefix import extract_code, sanitize
from .generation.fallback import placeholder_component
from .generation.scaffold import write_page, write_scaffold
from .generation.sections import SectionJob, generate_sections, repair_section
from .llm import LLM, LLMError, LLMUnavailable
from .modification import history
from .project import Project
from .validation import visual
from .validation.checks import attribute_errors, check_page
from .validation.repair import validate_and_repair
from .validation.runtime import NodeRuntime, PreviewManager, RuntimeErrorNode

_runtime: Optional[NodeRuntime] = None
_previews: Optional[PreviewManager] = None
_lock = threading.RLock()


def runtime() -> NodeRuntime:
    global _runtime
    with _lock:
        if _runtime is None:
            _runtime = NodeRuntime()
        return _runtime


def previews() -> PreviewManager:
    global _previews
    with _lock:
        if _previews is None:
            _previews = PreviewManager(runtime())
        return _previews


@dataclass
class CloneResult:
    project: Project
    ok: bool
    preview_url: Optional[str] = None
    scores: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
def _spacing(sections: list[Section]) -> dict:
    """Vertical gaps / horizontal insets the page applied around each section."""
    out = {s.index: {} for s in sections}
    for vp, vw in (("d", settings.desktop_width), ("m", settings.mobile_width)):
        prev_bottom = 0.0
        for s in sections:
            rec = s.node.d if vp == "d" else s.node.m
            if rec is None:
                continue
            st = rec["s"]
            x, y, w, h = rec["rect"]
            if st.get("position") in ("fixed", "absolute"):
                continue
            mt = px(st.get("margin-top")) or 0
            gap = y - prev_bottom - mt
            out[s.index][f"gap_{vp}"] = int(gap) if gap > 2 else 0
            prev_bottom = y + h + (px(st.get("margin-bottom")) or 0)
            self_centering = st.get("max-width", "none") != "none" or (px(st.get("margin-left")) or 0) > 0
            right = vw - (x + w)
            if x > 2 and abs(right - x) <= 4 and not self_centering:
                out[s.index][f"inset_{vp}"] = int(x)
    return out


def _load_fallback(project: Project, rel: str) -> Optional[str]:
    name = rel.rsplit("/", 1)[-1].removesuffix(".tsx")
    fb = project.data / "fallback" / f"{name}.tsx"
    current = (project.site / rel).read_text(encoding="utf-8") if (project.site / rel).exists() else ""
    if rel.startswith("components/sections/"):
        if fb.exists() and fb.read_text(encoding="utf-8") != current:
            return fb.read_text(encoding="utf-8")
        slug = kebab(name)
        return placeholder_component(name, slug, name)
    return None


def site_system(project: Project) -> str:
    """Rules + site context used for every code-writing call of this site (cache-friendly prefix)."""
    p = project.data / "system_prompt.txt"
    return p.read_text(encoding="utf-8") if p.exists() else prompts.GENERATE_RULES


def make_repair_fn(llm: LLM, project: Project):
    nm = str(runtime().node_modules)
    system = site_system(project)

    def fn(rel: str, code: str, errors: str) -> str:
        return repair_section(llm, rel, code, errors, nm, system)

    return fn


# ---------------------------------------------------------------------------
def clone(url: str, reporter: Optional[Reporter] = None, llm: Optional[LLM] = None,
          preview: bool = True, refine: Optional[bool] = None) -> CloneResult:
    rep = reporter or Reporter()
    llm = llm or LLM()
    rt = runtime()
    timings: dict[str, float] = {}
    t_start = time.time()
    rep.step("capture", f"LLM: {llm.label}")

    # Install the shared Node runtime in the background while we capture (first run only).
    if not rt.ready:
        rep.step("capture", "First run: installing the shared Next.js runtime in the background")
    pool = cf.ThreadPoolExecutor(max_workers=1)
    rt_future = pool.submit(rt.ensure, Reporter())  # silent: UI sinks must only be called from this thread

    project = Project.create(url)
    manifest = {"url": url, "slug": project.slug, "created": time.time(), "llm": llm.label, "sections": []}
    project.save_manifest(manifest)

    try:
        # ---- 1. capture ------------------------------------------------------
        t0 = time.time()
        cap = capture(url, project.capture_dir, project.site / "public", rep)
        timings["capture"] = round(time.time() - t0, 1)
        manifest["final_url"] = cap.final_url
        manifest["title"] = cap.title

        # ---- 2. analyze ------------------------------------------------------
        t0 = time.time()
        rep.step("analyze", "Building responsive DOM tree and splitting it into sections")
        body = build_tree(cap.desktop, cap.mobile)
        sections = find_sections(body, settings.desktop_width, settings.max_sections)
        if not sections:
            raise CaptureError("No visible content sections were found on this page.")
        tokens = derive_tokens(body, cap.desktop["page"], [s.node for s in sections])
        rep.step("analyze", f"Found {len(sections)} sections; palette primary {tokens['colors']['primary']}, "
                            f"fonts {tokens['fonts'].get('heading')} / {tokens['fonts'].get('body')}")
        tmap = TokenMap(colors=tokens["colors"], fonts=tokens["fonts"], radius=tokens["radius"],
                        container=tokens.get("container"))
        builder = OutlineBuilder(tmap, cap.final_url, body, cap.canvas_shots)
        short_builder = OutlineBuilder(tmap, cap.final_url, body, cap.canvas_shots, text_limit=100)
        excerpts = []
        for s in sections:
            v = short_builder.build(s.node)
            excerpts.append(outline_for(v, 1400) if v else "")
        site = {"url": cap.final_url, "title": cap.title, "description": cap.meta.get("description", "")}
        rep.step("analyze", "Asking the LLM to interpret layout, roles and responsive behaviour")
        bp = build_blueprint(llm, site, tokens, sections, excerpts, cap.desktop_png, rep)
        for k, v in (bp.get("tokens") or {}).items():
            if k in tokens["colors"]:
                tokens["colors"][k] = v
        for s, spec in zip(sections, bp["sections"]):
            s.name = spec["name"]
        project.write_json("tokens.json", tokens)
        project.write_json("blueprint.json", bp)
        timings["analyze"] = round(time.time() - t0, 1)
        rep.success("analyze", "Sections: " + ", ".join(f"{s.name}" for s in sections))

        # ---- 3. generate -----------------------------------------------------
        t0 = time.time()
        icons_local = cap.meta.get("icons_local") or []
        meta = {"title": cap.title, "description": cap.meta.get("description", ""), "lang": cap.meta.get("lang"),
                "url": cap.final_url, "icons_local": icons_local}
        write_scaffold(project.site, project.slug, tokens, cap.desktop["nodes"]["0"]["s"], meta, cap.fonts_css)
        spacing = _spacing(sections)
        (project.data / "outlines").mkdir(exist_ok=True)
        (project.data / "fallback").mkdir(exist_ok=True)
        jobs, sec_meta = [], []
        from .generation.fallback import render_component

        for s, spec in zip(sections, bp["sections"]):
            v = builder.build(s.node)
            outline = outline_for(v, settings.max_section_outline_chars) if v else ""
            slug = kebab(s.name)
            (project.data / "outlines" / f"{s.name}.html").write_text(outline, encoding="utf-8")
            fb = render_component(s.name, slug, v, s.kind, spec["type"]) if v else placeholder_component(s.name, slug)
            (project.data / "fallback" / f"{s.name}.tsx").write_text(fb, encoding="utf-8")
            jobs.append(SectionJob(s.name, slug, s.kind, spec, outline, v))
            m_rec = s.node.m
            sec_meta.append({
                "name": s.name, "file": f"components/sections/{s.name}.tsx", "kind": s.kind,
                "type": spec["type"], "description": spec["description"], "slug": slug,
                "orig_box_d": s.node.rect if s.node.d else None,
                "orig_box_m": m_rec["rect"] if m_rec else None,
                **spacing.get(s.index, {}),
            })
        system = prompts.GENERATE_RULES + prompts.site_context(site, tokens, bp)
        (project.data / "system_prompt.txt").write_text(system, encoding="utf-8")
        mode = "offline fallback renderer" if llm.is_mock else f"{llm.cfg.model} ({settings.max_concurrency} in parallel)"
        rep.step("generate", f"Generating {len(jobs)} section components with {mode}")
        if not rt_future.done():
            rep.step("generate", "Waiting for the Next.js runtime install to finish...")
        rt_future.result()  # re-raises RuntimeErrorNode with an actionable message
        nm = str(rt.node_modules)

        def done(res):
            label = {"llm": "generated", "fallback": "rendered (fallback)", "placeholder": "placeholder"}[res.source]
            extra = f" - {res.error}" if res.error else ""
            rep.step("generate", f"{res.name}: {label}{extra}", "warning" if res.error else "info")

        results = generate_sections(llm, system, jobs, nm, done)
        for res, meta_s in zip(results, sec_meta):
            (project.site / meta_s["file"]).write_text(res.code, encoding="utf-8")
            meta_s["source"] = res.source
        manifest["sections"] = sec_meta
        manifest["site_name"] = bp.get("site_name")
        project.save_manifest(manifest)
        write_page(project.site, sec_meta)
        timings["generate"] = round(time.time() - t0, 1)
        rep.success("generate", f"Wrote {len(results)} components + page, layout, theme and UI kit")

        # ---- 4. validate -----------------------------------------------------
        t0 = time.time()
        report = validate_and_repair(project, llm, rt, make_repair_fn(llm, project),
                                     lambda rel: _load_fallback(project, rel), rep)
        for f in report.fell_back:
            for m_ in manifest["sections"]:
                if m_["file"] == f:
                    m_["source"] = "fallback"
        project.save_manifest(manifest)
        project.write_json("validation.json", report.to_dict())
        timings["validate"] = round(time.time() - t0, 1)
        if not report.ok:
            rep.error("validate", "The site does not build yet - see validation.json")

        history.snapshot(project, "Initial generation", kind="generate")

        # ---- 5. preview + runtime check + visual score (+ refine) ----------
        result = CloneResult(project=project, ok=report.ok)
        if preview and report.ok:
            t0 = time.time()
            result.preview_url, result.scores = preview_and_score(project, llm, rep, refine=refine)
            timings["preview"] = round(time.time() - t0, 1)
        timings["total"] = round(time.time() - t_start, 1)
        _save_run(project, llm, rep, timings, result)
        rep.success("preview" if preview else "validate",
                    f"Done in {timings['total']}s · LLM cost ${llm.summary()['cost_usd']:.4f}")
        return result
    except (CaptureError, RuntimeErrorNode, LLMError) as e:
        rep.error("capture" if isinstance(e, CaptureError) else "validate", str(e))
        timings["total"] = round(time.time() - t_start, 1)
        res = CloneResult(project=project, ok=False, error=str(e))
        _save_run(project, llm, rep, timings, res)
        return res
    except Exception as e:  # unexpected bug: keep the traceback with the project for debugging
        import traceback

        (project.data / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        rep.error("validate", f"Unexpected error: {type(e).__name__}: {str(e)[:300]} (details in data/error.txt)")
        timings["total"] = round(time.time() - t_start, 1)
        res = CloneResult(project=project, ok=False, error=f"{type(e).__name__}: {e}")
        _save_run(project, llm, rep, timings, res)
        return res
    finally:
        pool.shutdown(wait=False)


def _save_run(project: Project, llm: LLM, rep: Reporter, timings: dict, result: CloneResult) -> None:
    summary = llm.summary()
    project.append_llm_calls(llm.drain())
    runs = project.read_json("runs.json", []) or []
    runs.append({"kind": "clone", "ok": result.ok, "error": result.error, "timings": timings,
                 "llm": summary, "scores": result.scores, "events": rep.as_dicts()})
    project.write_json("runs.json", runs)


# ---------------------------------------------------------------------------
def preview_and_score(project: Project, llm: LLM, rep: Reporter, refine: Optional[bool] = None) -> tuple[str, dict]:
    pm = previews()
    rep.step("preview", "Starting local preview (next dev)")
    srv = pm.start(project.slug, project.site, project.data)
    rep.success("preview", f"Preview running at {srv.url}")
    scores = runtime_check_and_score(project, srv.url, llm, rep)
    do_refine = settings.refine_enabled if refine is None else refine
    if do_refine and not llm.is_mock and scores.get("sections"):
        if refine_sections(project, llm, rep, scores["sections"]):
            scores = runtime_check_and_score(project, srv.url, llm, rep)
    return srv.url, scores


def runtime_check_and_score(project: Project, url: str, llm: LLM, rep: Reporter, repair: bool = True) -> dict:
    rep.step("validate", "Runtime check in headless Chromium (desktop + mobile)")
    rr = check_page(url, project.validation_dir)
    names = [s["name"] for s in project.manifest.get("sections", [])]
    srv = previews().get(project.slug)
    if rr.problems and repair:
        evidence = rr.problems + ([srv.log_tail(6000)] if srv else [])
        blamed = attribute_errors(evidence, names)
        rep.warn("validate", f"Runtime problems: {rr.problems[0][:160]}")
        for name, errs in blamed.items():
            rel = f"components/sections/{name}.tsx"
            path = project.site / rel
            fixed = False
            if not llm.is_mock:
                try:
                    code = repair_section(llm, rel, path.read_text(encoding="utf-8"), "\n".join(errs[:5])[-4000:],
                                          str(runtime().node_modules), site_system(project))
                    path.write_text(code, encoding="utf-8")
                    fixed = runtime().typecheck(project.site).ok
                    rep.step("validate", f"LLM repaired runtime error in {name}")
                except (LLMError, LLMUnavailable) as e:
                    rep.warn("validate", f"Runtime repair failed for {name}: {str(e)[:100]}")
            if not fixed:
                fb = _load_fallback(project, rel)
                if fb:
                    path.write_text(fb, encoding="utf-8")
                    rep.warn("validate", f"{name}: switched to deterministic fallback after a runtime error")
        if blamed:
            time.sleep(3)
            rr = check_page(url, project.validation_dir)
    for w in rr.warnings:
        rep.warn("validate", w)
    if rr.ok:
        rep.success("validate", "No runtime errors" + (f" ({len(rr.console_errors)} console warnings)" if rr.console_errors else ""))
    project.write_json("runtime.json", rr.to_dict())

    scores: dict = {"runtime_ok": rr.ok}
    for vp in ("desktop", "mobile"):
        gen = rr.screenshots.get(vp)
        orig = project.capture_dir / f"{vp}.png"
        if gen:
            page_score = visual.compare_pages(orig, gen)
            if page_score:
                scores[vp] = page_score
            visual.side_by_side(orig, gen, project.validation_dir / f"compare-{vp}.png")
    orig_boxes = {s["name"]: s.get("orig_box_d") for s in project.manifest.get("sections", [])}
    if rr.screenshots.get("desktop"):
        scores["sections"] = visual.section_scores(project.capture_dir / "desktop.png", rr.screenshots["desktop"],
                                                   orig_boxes, rr.boxes.get("desktop", []))
    if "desktop" in scores:
        rep.success("validate", f"Visual similarity: desktop {scores['desktop']['score']}"
                    + (f", mobile {scores['mobile']['score']}" if "mobile" in scores else "") + " / 100")
    project.write_json("scores.json", scores)
    return scores


def refine_sections(project: Project, llm: LLM, rep: Reporter, section_scores: dict) -> bool:
    """Re-prompt only the worst-matching LLM sections with measured differences (bounded cost)."""
    manifest = project.manifest
    by_name = {s["name"]: s for s in manifest.get("sections", [])}
    cands = []
    for name, sc in section_scores.items():
        meta = by_name.get(name)
        if not meta or meta.get("source") != "llm":
            continue
        if sc["score"] < 72 or sc["height_ratio"] < 0.75:
            cands.append((sc["score"] * sc["height_ratio"], name, sc))
    cands.sort()
    cands = cands[: settings.refine_max_sections]
    if not cands:
        rep.step("validate", "All sections are close to the original - no refinement needed")
        return False
    rep.step("validate", "Refining weakest sections: " + ", ".join(c[1] for c in cands))
    changed = False
    nm = str(runtime().node_modules)
    for _, name, sc in cands:
        rel = f"components/sections/{name}.tsx"
        path = project.site / rel
        before = path.read_text(encoding="utf-8")
        outline_path = project.data / "outlines" / f"{name}.html"
        outline = outline_path.read_text(encoding="utf-8") if outline_path.exists() else ""
        diffs = (f"- desktop height: original {sc['orig_h']}px, yours {sc['gen_h']}px\n"
                 f"- structural similarity {sc['ssim']:.2f} (1.0 = identical), colour similarity {sc['color']:.2f}")
        msg = [{"role": "system", "content": site_system(project)},
               {"role": "user", "content": prompts.REFINE_USER.format(diffs=diffs, outline=outline[:20000], code=before)}]
        try:
            code = extract_code(llm.chat(msg, stage=f"refine:{name}", max_tokens=16000))
        except (LLMError, LLMUnavailable) as e:
            rep.warn("validate", f"Refine failed for {name}: {str(e)[:100]}")
            continue
        if not code:
            continue
        code, _ = sanitize(code, name, nm)
        path.write_text(code, encoding="utf-8")
        if runtime().typecheck(project.site).ok:
            changed = True
            rep.step("validate", f"Refined {name}")
        else:
            path.write_text(before, encoding="utf-8")
            rep.warn("validate", f"Refined {name} did not type-check - kept previous version")
    if changed:
        history.snapshot(project, "Visual refinement", kind="refine")
    return changed
