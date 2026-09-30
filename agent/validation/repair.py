"""Stage 4 - Validate & repair.

Loop: type-check -> group errors per file -> LLM repair of only the failing
files (with the exact compiler messages) -> re-check. A section that still
fails after the retry budget is swapped for its deterministic fallback, so the
build always ends green. Then a real `next build` catches what tsc cannot
(bundling, server/client component rules, CSS).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..config import settings
from ..events import Reporter
from ..llm import LLM, LLMError, LLMTooSlow, LLMUnavailable
from ..project import Project
from .runtime import BuildResult, CheckError, NodeRuntime


@dataclass
class ValidationReport:
    ok: bool = False
    typecheck_rounds: int = 0
    repaired: list = field(default_factory=list)      # files fixed by the LLM
    fell_back: list = field(default_factory=list)     # files replaced by deterministic code
    build_ok: Optional[bool] = None
    build_seconds: float = 0.0
    last_errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {**self.__dict__, "last_errors": [str(e) for e in self.last_errors][:20]}


def _group(errors: list[CheckError]) -> dict[str, list[CheckError]]:
    out: dict[str, list[CheckError]] = defaultdict(list)
    for e in errors:
        out[e.file.lstrip("./")].append(e)
    return out


def validate_and_repair(
    project: Project,
    llm: LLM,
    runtime: NodeRuntime,
    repair_fn: Callable[[str, str, str], str],
    fallback_fn: Callable[[str], Optional[str]],
    reporter: Optional[Reporter] = None,
    run_build: bool = True,
    stage: str = "validate",
    max_rounds: Optional[int] = None,
) -> ValidationReport:
    """repair_fn(rel_path, code, errors) -> new code; fallback_fn(rel_path) -> safe code or None."""
    rep = reporter or Reporter()
    report = ValidationReport()
    attempts: dict[str, int] = defaultdict(int)
    max_attempts = settings.max_repair_attempts if max_rounds is None else max_rounds
    site = project.site

    def fix_files(errors: list[CheckError], label: str) -> bool:
        """Repair or fall back every file with errors. Returns False if nothing could be done."""
        progressed = False
        for rel, errs in _group(errors).items():
            path = project.file_at(rel)
            if path is None or not path.exists():
                continue
            msg = "\n".join(str(e) for e in errs[:15])
            use_fallback = attempts[rel] >= max_attempts or llm.is_mock
            if not use_fallback:
                attempts[rel] += 1
                try:
                    code = repair_fn(rel, path.read_text(encoding="utf-8"), msg)
                    path.write_text(code, encoding="utf-8")
                    rep.step(stage, f"LLM repaired {rel} ({label}, attempt {attempts[rel]})")
                    if rel not in report.repaired:
                        report.repaired.append(rel)
                    progressed = True
                    continue
                except (LLMUnavailable, LLMTooSlow) as e:
                    if isinstance(e, LLMTooSlow):
                        rep.warn(stage, f"No time left to repair {rel} with the AI")
                    use_fallback = True
                except LLMError as e:
                    rep.warn(stage, f"Repair call failed for {rel}: {str(e)[:120]}")
                    use_fallback = attempts[rel] >= max_attempts
                    progressed = True
            if use_fallback:
                safe = fallback_fn(rel)
                if safe is not None and safe != path.read_text(encoding="utf-8"):
                    path.write_text(safe, encoding="utf-8")
                    rep.warn(stage, f"{rel}: using deterministic fallback after {attempts[rel]} failed repair(s)")
                    report.fell_back.append(rel)
                    attempts[rel] = 99
                    progressed = True
        return progressed

    # ---- 1) type-check loop -------------------------------------------------
    for round_ in range(max_attempts + 3):
        res: BuildResult = runtime.typecheck(site)
        report.typecheck_rounds = round_ + 1
        if res.ok:
            rep.success(stage, f"Type-check passed ({res.seconds}s)")
            break
        rep.step(stage, f"Type-check found {len(res.errors)} error(s) in "
                        f"{len(_group(res.errors))} file(s)")
        report.last_errors = res.errors
        if not fix_files(res.errors, "tsc"):
            rep.error(stage, "Type errors remain that could not be attributed to a section file")
            return report

    else:
        return report

    # ---- 2) production build ---------------------------------------------
    if run_build:
        for round_ in range(2):
            rep.step(stage, "Running next build")
            b = runtime.build(site)
            report.build_seconds = b.seconds
            report.build_ok = b.ok
            if b.ok:
                rep.success(stage, f"Production build passed ({b.seconds}s)")
                break
            report.last_errors = b.errors
            rep.warn(stage, f"Build failed: {str(b.errors[0])[:200] if b.errors else 'unknown error'}")
            if not fix_files(b.errors, "build"):
                break
            t = runtime.typecheck(site)
            if not t.ok:
                fix_files(t.errors, "tsc")
        report.ok = bool(report.build_ok)
    else:
        report.ok = True
    return report
