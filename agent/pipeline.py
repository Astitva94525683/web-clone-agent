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
from .generation.sections import SectionJob, SectionResult, fallback_code, generate_sections, llm_section, repair_section
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


def make_repair_fn(llm: LLM, project: Project, deadline: Optional[float] = None):
    """Repair callback for validate_and_repair; with a deadline, repairs stop when time is up."""
    nm = str(runtime().node_modules)
    system = site_system(project)

    def fn(rel: str, code: str, errors: str) -> str:
        left = None if deadline is None else deadline - time.time()
        return repair_section(llm, rel, code, errors, nm, system, max_seconds=left)

    return fn


# ---------------------------------------------------------------------------
def clone(url: str, reporter: Optional[Reporter] = None, llm: Optional[LLM] = None,
          preview: bool = True, refine: Optional[bool] = None, mode: Optional[str] = None) -> CloneResult:
    """mode "fast" (default): measured components first, the LLM only improves the weakest sections.
    mode "full": the LLM rewrites every section. In both, an AI version is kept only if it scores better."""
    rep = reporter or Reporter()
    llm = llm or LLM()
    llm.reset_pause()
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
        full = (mode or settings.generation_mode) == "full" and not llm.is_mock
        if not rt_future.done():
            rep.step("generate", "Waiting for the Next.js runtime install to finish...")
        rt_future.result()  # re-raises RuntimeErrorNode with an actionable message
        nm = str(rt.node_modules)

        if full and not preview:
            # Nothing to measure against: the LLM writes every section directly.
            rep.step("generate", f"Generating {len(jobs)} section components with {llm.cfg.model} "
                                 f"({settings.max_concurrency} in parallel)")

            def done(res):
                label = {"llm": "generated", "fallback": "rendered (fallback)", "placeholder": "placeholder"}[res.source]
                extra = f" - {res.error}" if res.error else ""
                rep.step("generate", f"{res.name}: {label}{extra}", "warning" if res.error else "info")

            results = generate_sections(llm, system, jobs, nm, done)
        else:
            # Measured components first: instant, free, and the accuracy baseline the AI must beat.
            rep.step("generate", f"Building {len(jobs)} section components from the measured layout (no AI needed)")
            results = [SectionResult(j.name, *fallback_code(j), []) for j in jobs]
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
            result.preview_url, result.scores = preview_and_score(
                project, llm, rep, refine=refine, jobs=None if (full and not preview) else jobs, rewrite_all=full)
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
def preview_and_score(project: Project, llm: LLM, rep: Reporter, refine: Optional[bool] = None,
                      jobs: Optional[list] = None, rewrite_all: bool = False) -> tuple[str, dict]:
    pm = previews()
    rep.step("preview", "Starting local preview (next dev)")
    srv = pm.start(project.slug, project.site, project.data)
    rep.success("preview", f"Preview running at {srv.url}")
    scores = runtime_check_and_score(project, srv.url, llm, rep)
    use_ai = rewrite_all or (settings.refine_enabled if refine is None else refine)
    if use_ai and _endpoint_too_slow(llm):
        rep.warn("validate", "The AI did not answer the page analysis in time, so AI rewrites are skipped for this "
                             "site (they would only add minutes). The measured components are kept.")
        use_ai = False
    if use_ai and jobs and not llm.is_mock and scores.get("sections"):
        scores = improve_sections(project, llm, rep, srv.url, scores, jobs, rewrite_all)
    return srv.url, scores


def runtime_check_and_score(project: Project, url: str, llm: LLM, rep: Reporter, repair: bool = True,
                            deadline: Optional[float] = None) -> dict:
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
            left = None if deadline is None else deadline - time.time()
            if not llm.is_mock and (left is None or left > 20):
                try:
                    code = repair_section(llm, rel, path.read_text(encoding="utf-8"), "\n".join(errs[:5])[-4000:],
                                          str(runtime().node_modules), site_system(project), max_seconds=left)
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


def _endpoint_too_slow(llm: LLM) -> bool:
    """True when this run's page analysis already timed out: further calls would time out too."""
    return any(c.stage.startswith("analyze") and not c.ok and c.error in ("time limit", "APITimeoutError",
                                                                          "APIConnectionError")
               for c in llm.calls)


def improve_sections(project: Project, llm: LLM, rep: Reporter, url: str, scores: dict, jobs: list,
                     rewrite_all: bool) -> dict:
    """Let the LLM (re)write sections, then keep each AI version only if it scores better.

    rewrite_all=False: only the weakest sections (lowest similarity) get a targeted fix.
    rewrite_all=True:  every section is rewritten by the LLM (cleaner code, much slower).
    """
    manifest = project.manifest
    sec_scores = scores.get("sections") or {}
    budget = settings.ai_time_budget * (3 if rewrite_all else 1)
    deadline = time.time() + budget
    if rewrite_all:
        targets = list(jobs)
    else:
        weak = sorted((sc["score"] * sc["height_ratio"], name) for name, sc in sec_scores.items()
                      if sc["score"] < settings.refine_below or sc["height_ratio"] < 0.8)
        # A whole-page-sized component is too slow for the AI to rewrite and rarely comes back better.
        too_big = [n for _, n in weak if project.section_file(n).exists()
                   and len(project.section_file(n).read_text(encoding="utf-8")) > settings.refine_max_chars]
        if too_big:
            rep.step("validate", "Too large for a quick AI rewrite, keeping the measured version: " + ", ".join(too_big))
        names = [n for _, n in weak if n not in too_big][: settings.refine_max_sections]
        targets = [j for j in jobs if j.name in names]
    if not targets:
        rep.success("validate", "No section needs an AI rewrite - keeping the measured components")
        return scores
    rep.step("validate", ("AI is rewriting every section" if rewrite_all else "AI is improving the weakest sections")
             + f" with {llm.cfg.model} (time limit {round(budget / 60, 1):g} min): " + ", ".join(j.name for j in targets))
    nm = str(runtime().node_modules)
    system = site_system(project)
    paths = {j.name: project.section_file(j.name) for j in targets}
    before = {n: p.read_text(encoding="utf-8") for n, p in paths.items()}

    def work(job) -> tuple[str, Optional[str], str]:
        left = deadline - time.time()
        if left < 15:
            return job.name, None, "time limit reached"
        try:
            if rewrite_all:
                return job.name, llm_section(llm, system, job, nm, max_seconds=left).code, ""
            sc = sec_scores[job.name]
            diffs = (f"- desktop height: original {sc['orig_h']}px, yours {sc['gen_h']}px\n"
                     f"- structural similarity {sc['ssim']:.2f} (1.0 = identical), colour similarity {sc['color']:.2f}")
            msg = [{"role": "system", "content": system},
                   {"role": "user", "content": prompts.REFINE_USER.format(
                       diffs=diffs, outline=job.outline[:20000], code=before[job.name])}]
            code = extract_code(llm.chat(msg, stage=f"refine:{job.name}", max_tokens=16000, max_seconds=left))
            if not code:
                return job.name, None, "no code in the reply"
            return job.name, sanitize(code, job.name, nm)[0], ""
        except (LLMError, LLMUnavailable) as e:
            return job.name, None, str(e)[:160]

    with cf.ThreadPoolExecutor(max_workers=max(1, settings.max_concurrency)) as pool:
        futures = [pool.submit(work, j) for j in targets]
        results = [f.result() for f in cf.as_completed(futures)]  # UI updates stay on this thread
    changed = []
    for name, code, err in results:
        if code:
            paths[name].write_text(code, encoding="utf-8")
            changed.append(name)
            rep.step("validate", f"{name}: AI version written")
        else:
            rep.warn("validate", f"{name}: AI rewrite failed ({err}) - keeping the measured version")
    if not changed:
        return scores

    # Type-check (+ LLM repair); a file that stays broken goes back to its previous version.
    # (no time left -> zero repair rounds: a broken file simply goes back to its measured version)
    rounds = 2 if deadline - time.time() > 60 else 0
    report = validate_and_repair(project, llm, runtime(), make_repair_fn(llm, project, deadline),
                                 lambda rel: before.get(rel.rsplit("/", 1)[-1].removesuffix(".tsx")),
                                 rep, run_build=False, max_rounds=rounds)
    if not report.ok:
        for name in changed:
            paths[name].write_text(before[name], encoding="utf-8")
        rep.warn("validate", "AI versions did not type-check - kept the measured versions")
        return runtime_check_and_score(project, url, llm, rep, deadline=deadline)

    new_scores = runtime_check_and_score(project, url, llm, rep, deadline=deadline)
    new_sec = new_scores.get("sections") or {}
    kept, reverted = [], []
    for name in changed:
        old_s = (sec_scores.get(name) or {}).get("score")
        new_s = (new_sec.get(name) or {}).get("score")
        if new_s is not None and (old_s is None or new_s >= old_s - 0.5):
            kept.append(name)
        elif paths[name].read_text(encoding="utf-8") != before[name]:
            paths[name].write_text(before[name], encoding="utf-8")
            reverted.append(f"{name} ({new_s} < {old_s})")
    for m_ in manifest.get("sections", []):
        if m_["name"] in kept:
            m_["source"] = "llm"
    project.save_manifest(manifest)
    if kept:
        rep.success("validate", "Kept AI versions (same or better score): " + ", ".join(kept))
    if reverted:
        rep.warn("validate", "AI versions scored lower - kept the measured versions: " + ", ".join(reverted))
        new_scores = runtime_check_and_score(project, url, llm, rep, deadline=deadline)
    if kept:
        history.snapshot(project, "AI improvement", kind="refine")
    return new_scores
