"""Stage 5 - Modify the generated site with natural-language instructions.

    instruction -> PLAN (cheap JSON call: which operations, which files)
                -> APPLY (token edits are deterministic; section edits/additions
                          are focused LLM calls on ONE file each)
                -> VALIDATE (type-check + LLM repair loop, runtime check)
                -> COMMIT a new version, or ROLL BACK automatically on failure.

Only the files an instruction touches are sent to the LLM, which keeps edits
fast and cheap even on large sites.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .. import prompts
from ..analysis.blueprint import kebab, pascal
from ..config import settings
from ..events import Reporter
from ..generation.codefix import apply_patch, extract_code, sanitize
from ..generation.scaffold import update_theme_vars, write_page
from ..llm import LLM, LLMError, LLMUnavailable
from ..project import Project
from ..validation.repair import validate_and_repair
from . import history

NAMED_COLORS = {
    "blue": "#2563eb", "red": "#dc2626", "green": "#16a34a", "purple": "#7c3aed", "violet": "#7c3aed",
    "orange": "#ea580c", "pink": "#db2777", "yellow": "#eab308", "teal": "#0d9488", "indigo": "#4f46e5",
    "black": "#0a0a0a", "white": "#ffffff", "gray": "#6b7280", "grey": "#6b7280", "cyan": "#0891b2",
    "brown": "#92400e", "gold": "#ca8a04", "navy": "#1e3a8a", "emerald": "#059669", "rose": "#e11d48",
}


@dataclass
class ModifyResult:
    ok: bool
    summary: str = ""
    operations: list = field(default_factory=list)
    changed_files: list = field(default_factory=list)
    diff: str = ""
    version: Optional[int] = None
    error: str = ""
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------
def _theme_vars(css: str) -> str:
    m = re.search(r"@theme\s*{([^}]*)}", css)
    return m.group(1).strip() if m else ""


def _contrast_fg(hex_: str) -> str:
    h = hex_.lstrip("#")
    if len(h) < 6:
        return "#ffffff"
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#0a0a0a" if lum > 0.6 else "#ffffff"


def heuristic_plan(instruction: str, sections: list[dict]) -> dict:
    """Rule-based planner for offline mode (no API key). Covers common edits only."""
    text = instruction.lower()
    names = {s["name"].lower(): s["name"] for s in sections}
    by_type = {s.get("type", ""): s["name"] for s in sections}

    def find_section(words: str) -> Optional[str]:
        for key, name in names.items():
            if key in words or kebab(name).replace("-", " ") in words:
                return name
        for typ, name in by_type.items():
            if typ and typ in words:
                return name
        if "nav" in words or "header" in words or "menu" in words:
            return by_type.get("navbar")
        return None

    m = re.search(r"(primary|accent|brand|background|text|main)?\s*colou?r\s*(?:to|=|into|as)\s*(#[0-9a-f]{3,6}|\w+)", text)
    if m:
        role = {"accent": "accent", "background": "background", "text": "foreground"}.get(m.group(1) or "", "primary")
        val = m.group(2)
        hex_ = val if val.startswith("#") else NAMED_COLORS.get(val)
        if hex_:
            changes = {f"--color-{role}": hex_}
            if role == "primary":
                changes["--color-primary-foreground"] = _contrast_fg(hex_)
            return {"summary": f"Set the {role} colour to {val}", "operations": [{"op": "update_tokens", "changes": changes}]}
    if "sticky" in text or "fixed" in text:
        nav = find_section(text) or by_type.get("navbar")
        if nav:
            return {"summary": f"Make {nav} sticky", "operations": [{"op": "edit_section", "section": nav,
                                                                     "instruction": "make it sticky", "rule": "sticky"}]}
    m = re.search(r"(remove|delete|hide|drop)\s+(?:the\s+)?(.+?)(?:\s+section)?$", text)
    if m:
        target = find_section(m.group(2))
        if target:
            return {"summary": f"Remove the {target} section", "operations": [{"op": "remove_section", "section": target}]}
    return {"summary": "This instruction needs the LLM (set LLM_API_KEY); offline mode supports colour changes, "
                       "sticky navbar and removing sections.", "operations": []}


def plan(llm: LLM, project: Project, instruction: str, max_seconds: Optional[float] = None) -> dict:
    sections = [{"name": s["name"], "type": s.get("type"), "description": s.get("description", "")[:160]}
                for s in project.manifest.get("sections", [])]
    css = (project.site / "app" / "globals.css").read_text(encoding="utf-8")
    messages = [{"role": "system", "content": prompts.PLAN_SYSTEM},
                {"role": "user", "content": prompts.plan_user(instruction, sections, _theme_vars(css))}]
    try:
        data = llm.chat_json(messages, stage="modify:plan", fast=True, max_tokens=8000, max_seconds=max_seconds)
        if not isinstance(data, dict):
            raise ValueError("plan is not an object")
        data.setdefault("operations", [])
        data["source"] = "llm"
        return data
    except LLMUnavailable:
        p = heuristic_plan(instruction, project.manifest.get("sections", []))
        p["source"] = "heuristic"
        return p
    except (LLMError, ValueError) as e:
        # The LLM is misconfigured or returned junk: simple edits still work with the rule-based planner.
        p = heuristic_plan(instruction, project.manifest.get("sections", []))
        p["source"] = "heuristic (LLM planning failed)"
        p["llm_error"] = str(e)[:300]
        if not p["operations"]:
            p["summary"] = f"LLM planning failed: {str(e)[:300]}"
        return p


# ---------------------------------------------------------------------------
# applying operations
# ---------------------------------------------------------------------------
def _rule_edit(code: str, rule: str) -> str:
    """Deterministic edits used in offline mode."""
    if rule == "sticky":
        m = re.search(r'<(header|nav|section|div)([^>]*?)className="([^"]*)"', code)
        if m:
            cls = re.sub(r"\b(relative|absolute|fixed|static|sticky)\b", "", m.group(3))
            cls = re.sub(r"\b(top|inset-x|inset-y|z)-\S+", "", cls)
            new_cls = " ".join(("sticky top-0 z-50 " + cls).split())
            code = code[: m.start(3)] + new_cls + code[m.end(3):]
    return code


def _style_example(project: Project, near: Optional[str]) -> str:
    secs = project.manifest.get("sections", [])
    order = [s["name"] for s in secs if s.get("kind") == "section"]
    pick = near if near in order else (order[1] if len(order) > 1 else (order[0] if order else None))
    if not pick:
        return ""
    p = project.section_file(pick)
    return p.read_text(encoding="utf-8")[:6000] if p.exists() else ""


def apply_operations(llm: LLM, project: Project, ops: list[dict], rep: Reporter,
                     deadline: Optional[float] = None) -> list[str]:
    from ..pipeline import site_system

    manifest = project.manifest
    secs: list[dict] = manifest.get("sections", [])
    names = {s["name"] for s in secs}
    nm = str(settings.workspace / "node_modules")
    notes = []
    for op in ops:
        kind = op.get("op")
        if kind == "update_tokens":
            css_path = project.site / "app" / "globals.css"
            css, applied = update_theme_vars(css_path.read_text(encoding="utf-8"), op.get("changes") or {})
            css_path.write_text(css, encoding="utf-8")
            notes.append(f"theme: {', '.join(applied) or 'no matching variables'}")
            rep.step("modify", f"Updated theme tokens: {', '.join(applied)}")
        elif kind == "edit_section":
            name = op.get("section")
            if name not in names:
                raise LLMError(f"Plan referenced unknown section '{name}'")
            path = project.section_file(name)
            code = path.read_text(encoding="utf-8")
            if op.get("rule"):
                new = _rule_edit(code, op["rule"])
            else:
                rep.step("modify", f"Editing {name}: {op.get('instruction', '')[:100]}")
                user = prompts.edit_user(project.rel(path), code, op.get("instruction", ""))
                # Small SEARCH/REPLACE patches: the model writes a few lines instead of the whole file,
                # which is what makes edits fast on slow endpoints.
                reply = llm.chat([{"role": "system", "content": site_system(project) + prompts.EDIT_PATCH_SUFFIX},
                                  {"role": "user", "content": user}], stage=f"modify:edit:{name}", max_tokens=4000,
                                 max_seconds=None if deadline is None else deadline - time.time())
                new = apply_patch(code, reply)
                if new is None and "export default" in reply:
                    new = extract_code(reply)  # the model sent the whole file anyway
                if new is None:
                    left = None if deadline is None else deadline - time.time()
                    if left is not None and left < 90:
                        raise LLMError(f"The AI's change to {name} could not be applied")
                    rep.step("modify", f"Patch did not apply - asking for the full file of {name}")
                    new = extract_code(llm.chat([{"role": "system", "content": site_system(project) + prompts.EDIT_SUFFIX},
                                                 {"role": "user", "content": user}], stage=f"modify:edit-full:{name}",
                                                max_tokens=16000, max_seconds=left))
                if not new:
                    raise LLMError(f"Edit of {name} returned no code")
                new, _ = sanitize(new, name, nm)
            path.write_text(new, encoding="utf-8")
            for s in secs:
                if s["name"] == name:
                    s["source"] = "edited"
            notes.append(f"edited {name}")
        elif kind == "add_section":
            name = pascal(op.get("name") or "NewSection")
            base, i = name, 2
            while name in names:
                name, i = f"{base}{i}", i + 1
            slug = kebab(name)
            after = op.get("after")
            rep.step("modify", f"Creating new section {name}")
            msgs = [{"role": "system", "content": site_system(project)},
                    {"role": "user", "content": prompts.add_section_user(name, slug, op.get("instruction", ""),
                                                                         _style_example(project, after))}]
            code = extract_code(llm.chat(msgs, stage=f"modify:add:{name}", max_tokens=16000,
                                         max_seconds=None if deadline is None else deadline - time.time()))
            if not code:
                raise LLMError(f"New section {name} returned no code")
            code, _ = sanitize(code, name, nm)
            project.section_file(name).write_text(code, encoding="utf-8")
            entry = {"name": name, "file": f"components/sections/{name}.tsx", "kind": "section",
                     "type": op.get("type", "content"), "description": op.get("instruction", "")[:200],
                     "slug": slug, "source": "added"}
            idx = len(secs) - (1 if secs and secs[-1].get("kind") == "footer" else 0)
            if after == "START":
                idx = next((i + 1 for i, s in enumerate(secs[:2]) if s.get("kind") == "header"), 0)
            elif after in names:
                idx = next(i for i, s in enumerate(secs) if s["name"] == after) + 1
            secs.insert(idx, entry)
            names.add(name)
            notes.append(f"added {name}")
        elif kind == "remove_section":
            name = op.get("section")
            if name not in names:
                raise LLMError(f"Plan referenced unknown section '{name}'")
            secs[:] = [s for s in secs if s["name"] != name]
            names.discard(name)
            p = project.section_file(name)
            if p.exists():
                p.unlink()
            notes.append(f"removed {name}")
            rep.step("modify", f"Removed section {name}")
        elif kind == "move_section":
            name, after = op.get("section"), op.get("after")
            if name in names:
                item = next(s for s in secs if s["name"] == name)
                secs.remove(item)
                if after == "START":
                    idx = next((i + 1 for i, s in enumerate(secs[:2]) if s.get("kind") == "header"), 0)
                else:
                    idx = next((i + 1 for i, s in enumerate(secs) if s["name"] == after), len(secs))
                secs.insert(idx, item)
                notes.append(f"moved {name}")
    manifest["sections"] = secs
    project.save_manifest(manifest)
    write_page(project.site, secs)
    return notes


# ---------------------------------------------------------------------------
def modify(project: Project, instruction: str, llm: Optional[LLM] = None, reporter: Optional[Reporter] = None,
           runtime=None, check_url: Optional[str] = None) -> ModifyResult:
    from ..pipeline import make_repair_fn, runtime as get_runtime, runtime_check_and_score

    rep = reporter or Reporter()
    llm = llm or LLM()
    rt = runtime or get_runtime()
    instruction = (instruction or "").strip()
    if not instruction:
        return ModifyResult(ok=False, error="Please type an instruction.")
    base_version = history.current_version(project) or history.snapshot(project, "Before edits", kind="generate")
    cost_before = llm.summary()["cost_usd"]

    llm.reset_pause()  # a slow clone earlier must not block this edit
    deadline = time.time() + settings.modify_time_budget
    rep.step("modify", f"Planning: {instruction}")
    p = plan(llm, project, instruction, max_seconds=settings.modify_time_budget)
    if p.get("llm_error"):
        rep.warn("modify", f"LLM planning failed ({p['llm_error'][:160]}) - using the rule-based planner")
    ops = p.get("operations") or []
    rep.step("modify", f"Plan ({p.get('source')}): {p.get('summary', '')} - {len(ops)} operation(s)")
    if not ops:
        project.append_llm_calls(llm.drain())  # keep failed calls visible in the cost/log tab
        return ModifyResult(ok=False, summary=p.get("summary", ""), error=p.get("summary") or "Nothing to change")

    def rollback(msg: str) -> ModifyResult:
        history.restore(project, base_version)
        rep.error("modify", f"{msg} - rolled back to v{base_version}")
        project.append_llm_calls(llm.drain())
        return ModifyResult(ok=False, summary=p.get("summary", ""), operations=ops, error=msg)

    try:
        apply_operations(llm, project, ops, rep, deadline)
    except (LLMError, LLMUnavailable, StopIteration) as e:
        return rollback(f"Could not apply the change: {e}")

    # validate only what we touched: type-check + targeted repair, no full production build (fast)
    report = validate_and_repair(project, llm, rt, make_repair_fn(llm, project, deadline),
                                 lambda rel: None, rep, run_build=False, stage="modify", max_rounds=2)
    if not report.ok:
        return rollback("The edited code did not type-check after repairs")

    changed = history.changed_files(project, base_version)
    diff = history.diff_versions(project, base_version)
    version = history.snapshot(project, instruction, kind="modify",
                               extra={"summary": p.get("summary", ""), "files": changed})
    if check_url:
        try:
            runtime_check_and_score(project, check_url, llm, rep, repair=False)
        except Exception as e:  # preview problems should not undo a valid edit
            rep.warn("modify", f"Runtime check skipped: {str(e)[:120]}")
    cost = round(llm.summary()["cost_usd"] - cost_before, 5)
    project.append_llm_calls(llm.drain())
    rep.success("modify", f"Applied as v{version}: {', '.join(changed) or 'no file changes'}")
    return ModifyResult(ok=True, summary=p.get("summary", ""), operations=ops, changed_files=changed,
                        diff=diff, version=version, cost_usd=cost)


def undo(project: Project, reporter: Optional[Reporter] = None) -> Optional[int]:
    """Restore the previous version (creates a new history entry so undo is itself undoable)."""
    rep = reporter or Reporter()
    hist = history.versions(project)
    if len(hist) < 2:
        return None
    target = hist[-2]["version"]
    history.restore(project, target)
    v = history.snapshot(project, f"Undo (restored v{target})", kind="undo")
    rep.success("modify", f"Restored v{target} (saved as v{v})")
    return v
