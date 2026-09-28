"""Command-line interface for the AI Website Clone Agent.

Examples:
  python cli.py clone https://example.com
  python cli.py modify example-com "Change the primary color to blue"
  python cli.py eval https://site-a.com https://site-b.com https://site-c.com
  python cli.py preview example-com
  python cli.py list
  python cli.py models deepseek      # which model ids your API key can use
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from agent.events import Reporter, console_sink
from agent.llm import LLM
from agent.project import Project


def cmd_clone(args) -> int:
    from agent.pipeline import clone, previews

    llm = LLM()
    res = clone(args.url, Reporter(console_sink), llm=llm, preview=not args.no_preview, refine=not args.no_refine)
    print()
    print(f"Project:  {res.project.slug}  ({res.project.site})")
    if res.preview_url:
        print(f"Preview:  {res.preview_url}")
    if res.error:
        print(f"Error:    {res.error}")
    if res.preview_url and not args.exit:
        print("\nPreview server running - press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    previews().stop_all()
    return 0 if res.ok else 1


def cmd_modify(args) -> int:
    from agent.modification.modify import modify

    project = Project.load(args.project)
    res = modify(project, args.instruction, LLM(), Reporter(console_sink))
    print()
    if res.ok:
        print(f"v{res.version}: {res.summary}")
        print("Changed: " + ", ".join(res.changed_files))
        if args.diff:
            print(res.diff)
    else:
        print(f"Not applied: {res.error}")
    return 0 if res.ok else 1


def cmd_undo(args) -> int:
    from agent.modification.modify import undo

    v = undo(Project.load(args.project), Reporter(console_sink))
    print("Nothing to undo" if v is None else f"Restored (now v{v})")
    return 0


def cmd_preview(args) -> int:
    from agent.pipeline import previews

    project = Project.load(args.project)
    srv = previews().start(project.slug, project.site, project.data)
    print(f"Preview: {srv.url}  (Ctrl+C to stop)")
    try:
        while srv.alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    previews().stop_all()
    return 0


def cmd_list(args) -> int:
    for p in Project.list_all():
        m = p.manifest
        scores = p.read_json("scores.json", {}) or {}
        d = scores.get("desktop", {}).get("score", "-")
        print(f"{p.slug:40} {len(m.get('sections', [])):>2} sections  desktop score {d}  {m.get('url', '')}")
    return 0


def cmd_eval(args) -> int:
    """Clone several sites in a row and print a comparison table (generalisation test)."""
    from agent.pipeline import clone, previews

    rows = []
    for url in args.urls:
        llm = LLM()
        t0 = time.time()
        res = clone(url, Reporter(console_sink), llm=llm, preview=True, refine=not args.no_refine)
        runs = res.project.read_json("runs.json", []) or [{}]
        m = res.project.manifest
        srcs = [s.get("source") for s in m.get("sections", [])]
        rows.append({
            "url": url, "ok": res.ok, "sections": len(srcs), "llm_sections": srcs.count("llm"),
            "fallback_sections": srcs.count("fallback") + srcs.count("placeholder"),
            "desktop": res.scores.get("desktop", {}).get("score"), "mobile": res.scores.get("mobile", {}).get("score"),
            "seconds": round(time.time() - t0), "cost_usd": runs[-1].get("llm", {}).get("cost_usd"),
            "project": res.project.slug, "error": res.error[:80],
        })
        previews().stop(res.project.slug)
    print("\n" + json.dumps(rows, indent=2))
    print(f"\n{'site':34} {'ok':>3} {'secs':>4} {'llm/fb':>7} {'desk':>5} {'mob':>5} {'time':>5} {'cost$':>8}")
    for r in rows:
        print(f"{r['url'][:34]:34} {'✓' if r['ok'] else '✗':>3} {r['sections']:>4} "
              f"{r['llm_sections']:>3}/{r['fallback_sections']:<3} {r['desktop'] or '-':>5} {r['mobile'] or '-':>5} "
              f"{r['seconds']:>5} {r['cost_usd'] or 0:>8.4f}")
    return 0 if all(r["ok"] for r in rows) else 1


def cmd_models(args) -> int:
    llm = LLM()
    if llm.is_mock:
        print("No LLM_API_KEY set in .env")
        return 1
    for m in llm.list_models(args.filter):
        print(m)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="AI agent that recreates a website's frontend as a Next.js app")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("clone", help="clone a website URL")
    c.add_argument("url")
    c.add_argument("--no-preview", action="store_true", help="skip dev server + runtime/visual checks")
    c.add_argument("--no-refine", action="store_true", help="skip the visual refinement pass")
    c.add_argument("--exit", action="store_true", help="exit after cloning instead of keeping the preview open")
    c.set_defaults(fn=cmd_clone)
    m = sub.add_parser("modify", help="modify a generated site with a natural-language instruction")
    m.add_argument("project")
    m.add_argument("instruction")
    m.add_argument("--diff", action="store_true")
    m.set_defaults(fn=cmd_modify)
    u = sub.add_parser("undo", help="restore the previous version of a project")
    u.add_argument("project")
    u.set_defaults(fn=cmd_undo)
    p = sub.add_parser("preview", help="start the local preview of a project")
    p.add_argument("project")
    p.set_defaults(fn=cmd_preview)
    ls = sub.add_parser("list", help="list generated projects")
    ls.set_defaults(fn=cmd_list)
    e = sub.add_parser("eval", help="clone several URLs and compare results")
    e.add_argument("urls", nargs="+")
    e.add_argument("--no-refine", action="store_true")
    e.set_defaults(fn=cmd_eval)
    md = sub.add_parser("models", help="list model ids available at LLM_BASE_URL")
    md.add_argument("filter", nargs="?", default="deepseek")
    md.set_defaults(fn=cmd_models)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
