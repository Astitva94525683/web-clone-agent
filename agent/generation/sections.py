"""Stage 3 - Generate: one LLM call per section, run in parallel.

Why per-section instead of one giant prompt?
  * smaller outputs -> fewer truncations and syntax errors,
  * a failure is isolated to one file and repaired/fallen-back alone,
  * the shared prompt prefix is cached by DeepSeek -> cheaper,
  * parallel calls -> faster.
"""
from __future__ import annotations

import concurrent.futures as cf
from dataclasses import dataclass
from typing import Callable, Optional

from .. import prompts
from ..analysis.outline import VNode
from ..config import settings
from ..llm import LLM, LLMError, LLMUnavailable
from .codefix import disallowed_imports, extract_code, sanitize
from .fallback import placeholder_component, render_component


@dataclass
class SectionJob:
    name: str
    slug: str
    kind: str
    spec: dict
    outline: str
    vnode: Optional[VNode]


@dataclass
class SectionResult:
    name: str
    code: str
    source: str  # llm | fallback | placeholder
    notes: list
    error: str = ""


def fallback_code(job: SectionJob) -> tuple[str, str]:
    if job.vnode is not None:
        return render_component(job.name, job.slug, job.vnode, job.kind, job.spec.get("type", "content")), "fallback"
    return placeholder_component(job.name, job.slug, job.spec.get("description", "")[:60]), "placeholder"


def llm_section(llm: LLM, system: str, job: SectionJob, node_modules: str,
                max_seconds: Optional[float] = None) -> SectionResult:
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": prompts.generate_user(job.name, job.slug, job.spec, job.outline)}]
    text = llm.chat(messages, stage=f"generate:{job.name}", max_tokens=16000, temperature=0.2,
                    max_seconds=max_seconds)
    code = extract_code(text)
    if not code:
        raise LLMError("model reply contained no TSX code block")
    code, notes = sanitize(code, job.name, node_modules)
    bad = disallowed_imports(code)
    if bad:
        notes.append("disallowed imports: " + ", ".join(bad))
    return SectionResult(job.name, code, "llm", notes)


def generate_sections(llm: LLM, system: str, jobs: list[SectionJob], node_modules: str,
                      on_done: Optional[Callable[[SectionResult], None]] = None) -> list[SectionResult]:
    """Generate all sections. The first call runs alone to warm the provider's prompt cache."""

    def run(job: SectionJob) -> SectionResult:
        try:
            return llm_section(llm, system, job, node_modules)
        except LLMUnavailable:
            code, src = fallback_code(job)
            return SectionResult(job.name, code, src, [])
        except Exception as e:  # LLM/network/parse errors never abort the whole site
            code, src = fallback_code(job)
            return SectionResult(job.name, code, src, [], error=str(e)[:300])

    if not jobs:
        return []
    results: dict[str, SectionResult] = {}
    first = run(jobs[0])  # runs alone so the provider caches the shared prompt prefix
    results[first.name] = first
    if on_done:
        on_done(first)
    workers = 1 if llm.is_mock else max(1, settings.max_concurrency)
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, j) for j in jobs[1:]]
        for fut in cf.as_completed(futures):
            res = fut.result()
            results[res.name] = res
            if on_done:
                on_done(res)  # called from the caller's thread (safe for UI sinks)
    return [results[j.name] for j in jobs]


def repair_section(llm: LLM, path: str, code: str, errors: str, node_modules: str, system: str = "",
                   max_seconds: Optional[float] = None) -> str:
    messages = [{"role": "system", "content": (system or prompts.GENERATE_RULES) + prompts.REPAIR_SUFFIX},
                {"role": "user", "content": prompts.repair_user(path, code, errors)}]
    text = llm.chat(messages, stage=f"repair:{path.rsplit('/', 1)[-1]}", fast=True, max_tokens=16000,
                    temperature=0.1, max_seconds=max_seconds)
    fixed = extract_code(text)
    if not fixed:
        raise LLMError("repair reply contained no code")
    name = path.rsplit("/", 1)[-1].removesuffix(".tsx")
    fixed, _ = sanitize(fixed, name, node_modules)
    return fixed
