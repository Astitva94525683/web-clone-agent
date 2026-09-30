"""Streamlit control panel for the AI Website Clone Agent.

Run:  streamlit run app.py
"""
from __future__ import annotations


import streamlit as st
import streamlit.components.v1 as components

from agent.config import reload_settings, settings
from agent.events import Event, Reporter
from agent.llm import LLM
from agent.modification import history
from agent.modification.modify import modify, undo
from agent.pipeline import clone, previews, runtime_check_and_score
from agent.project import Project

st.set_page_config(page_title="AI Website Clone Agent", page_icon="🧬", layout="wide")

STAGE_ORDER = ["capture", "analyze", "generate", "validate", "preview"]
STAGE_LABEL = {"capture": "Analyze website", "analyze": "Understand UI / layout", "generate": "Generate Next.js",
               "validate": "Run & validate", "preview": "Local preview", "modify": "Modify"}
EFFORTS = {  # label -> (generation mode, AI fixes for weak sections)
    "Balanced (recommended)": ("fast", True),
    "Fastest - no AI rewrites": ("fast", False),
    "Full - AI rewrites every part (slow)": ("full", True),
}
EXAMPLES = ["Change the primary color to blue", "Add a testimonials section",
            "Replace the hero section with a bakery hero", "Make the navbar sticky", "Remove the pricing section"]

st.markdown("""
<style>
  .block-container {padding-top: 1.6rem;}
  .swatch {display:inline-flex; align-items:center; gap:8px; margin:0 14px 8px 0; font-size:13px;}
  .swatch span.c {width:26px; height:26px; border-radius:6px; border:1px solid rgba(128,128,128,.35); display:inline-block;}
  .pill {display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; margin-right:4px;
         background:rgba(128,128,128,.15);}
  .pill.llm {background:rgba(34,197,94,.18);} .pill.fallback {background:rgba(234,179,8,.22);}
  .pill.edited, .pill.added {background:rgba(59,130,246,.2);}
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_llm() -> LLM:
    return LLM()


llm = get_llm()
ss = st.session_state
ss.setdefault("project", None)
ss.setdefault("chat", {})


def load_project(slug: str | None) -> Project | None:
    if not slug:
        return None
    try:
        return Project.load(slug)
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("🧬 Clone Agent")
    if llm.is_mock:
        st.warning("**Offline mode** - no `LLM_API_KEY` set. Sections are rendered by the deterministic "
                   "fallback and only simple edits work. Add a DeepSeek key to `.env` for the full agent.")
    else:
        st.success(f"LLM: `{settings.model}`\n\n{settings.base_url}")
    s = llm.lifetime
    st.caption(f"Session LLM usage: {s['calls']} calls · {s['tokens']:,} tokens · ${s['cost_usd']:.4f}")
    st.divider()
    projects = Project.list_all()
    slugs = [p.slug for p in projects]
    if slugs:
        idx = slugs.index(ss.project) if ss.project in slugs else 0
        chosen = st.selectbox("Projects", slugs, index=idx,
                              format_func=lambda sl: f"{sl}")
        if chosen != ss.project:
            ss.project = chosen
    else:
        st.caption("No projects yet.")
    proj = load_project(ss.project)
    if proj:
        srv = previews().get(proj.slug)
        c1, c2 = st.columns(2)
        if srv:
            c1.markdown(f"🟢 [Preview]({srv.url})")
            if c2.button("Stop", width="stretch"):
                previews().stop(proj.slug)
                st.rerun()
        else:
            if c1.button("▶ Start preview", width="stretch"):
                with st.spinner("Starting next dev..."):
                    try:
                        previews().start(proj.slug, proj.site, proj.data)
                    except Exception as e:
                        st.error(str(e)[:400])
                st.rerun()
        st.caption(f"Code: `{proj.site}`")
    st.divider()
    if st.button("↻ Reload .env settings", width="stretch",
                 help="Re-read LLM_API_KEY / LLM_MODEL etc. without restarting Streamlit"):
        reload_settings()
        get_llm.clear()
        st.rerun()

# ---------------------------------------------------------------------------
# Header + URL input
# ---------------------------------------------------------------------------
st.title("AI Website Clone Agent")
st.caption("URL → Analyze → Understand layout → Generate Next.js + TypeScript + Tailwind → Validate → Preview → Modify with prompts")

with st.form("clone"):
    c1, c2 = st.columns([5, 1])
    url = c1.text_input("Website URL", placeholder="https://example.com", label_visibility="collapsed")
    submitted = c2.form_submit_button("Clone website", type="primary", width="stretch")
    effort = st.radio(
        "AI effort", list(EFFORTS), index=0, horizontal=True,
        help="Balanced: parts are built from the measured layout in about a minute, then the AI fixes only the "
             "weakest ones (time limit: AI_TIME_BUDGET, 5 min by default). Fastest: no AI rewrites at all. "
             "Full: the AI rewrites every part (much slower on free endpoints). An AI version is only kept "
             "if it looks at least as close to the original.")

if submitted and url.strip():
    progress = st.progress(0.0, text="Starting...")
    status = st.status(f"Cloning {url}", expanded=True)
    icons = {"info": "▫️", "success": "✅", "warning": "⚠️", "error": "❌"}

    def sink(ev: Event) -> None:
        if ev.stage in STAGE_ORDER:
            i = STAGE_ORDER.index(ev.stage)
            progress.progress(min(1.0, (i + (1 if ev.level == "success" else 0.4)) / len(STAGE_ORDER)),
                              text=STAGE_LABEL.get(ev.stage, ev.stage))
        status.write(f"{icons.get(ev.level, '▫️')} **{STAGE_LABEL.get(ev.stage, ev.stage)}** - {ev.message}")

    mode, refine = EFFORTS[effort]
    result = clone(url.strip(), Reporter(sink), llm=llm, mode=mode, refine=refine)
    ss.project = result.project.slug
    ss.last_run = {"ok": result.ok, "error": result.error, "url": url.strip()}
    st.rerun()  # redraw everything (sidebar, preview) for the new project

if ss.get("last_run"):
    lr = ss.last_run
    if lr["ok"]:
        st.success(f"✅ Clone of {lr['url']} is ready - preview, analysis and the full agent log are below.")
    else:
        st.error(f"❌ Clone of {lr['url']} failed: {lr['error'][:300]}")
    ss.last_run = None

if not proj:
    st.info("Enter a public website URL above to start. Generated projects appear in the sidebar.")
    st.stop()

manifest = proj.manifest
scores = proj.read_json("scores.json", {}) or {}
st.subheader(manifest.get("site_name") or manifest.get("title") or proj.slug)
sections_meta = manifest.get("sections", [])
ai_sections = [s_ for s_ in sections_meta if s_.get("source") in ("llm", "edited", "added")]
runs = proj.read_json("runs.json", []) or []
calls = proj.read_json("llm_calls.json", []) or []
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Sections", len(sections_meta))
m2.metric("Desktop similarity", f"{scores.get('desktop', {}).get('score', '-')}")
m3.metric("Mobile similarity", f"{scores.get('mobile', {}).get('score', '-')}")
m4.metric("AI-written sections", f"{len(ai_sections)} / {len(sections_meta)}",
          help="Sections whose final code was written by the AI. The rest are built directly from the "
               "measured layout - the AI only replaces a section when its version looks at least as close.")
m5.metric("Version", f"v{history.current_version(proj)}")
st.caption(f"LLM usage for this project: {sum(1 for c in calls if not c.get('cache_hit'))} calls · "
           f"${sum(c.get('cost_usd', 0) for c in calls):.4f}")
failed_calls = [c for c in calls if not c.get("ok", True)]
if sections_meta and not ai_sections and failed_calls and len(failed_calls) == len(calls):
    st.warning("**The LLM was not used for this clone** - every section came from the deterministic fallback, "
               "so the result is only a rough copy.  \n"
               f"Last LLM error: `{failed_calls[-1].get('error', '')[:300]}`  \n"
               "Fix `LLM_MODEL` / `LLM_API_KEY` in `.env` (run `python cli.py models deepseek` to see valid "
               "model ids), press **Reload .env settings** in the sidebar, then clone again.")

tab_prev, tab_mod, tab_an, tab_code, tab_val = st.tabs(
    ["🖥️ Preview", "✏️ Modify with AI", "🔍 Analysis", "📄 Code", "✅ Validation & cost"])


# ---------------------------------------------------------------------------
def preview_frame(url: str, width: int, height: int = 820) -> None:
    """Render the site at a true device width, scaled down to fit the panel."""
    html = f"""
    <div id="wrap" style="width:100%;height:{height}px;overflow:hidden;display:flex;justify-content:center;
         background:repeating-conic-gradient(#8881 0% 25%, transparent 0% 50%) 50% / 20px 20px;border-radius:10px">
      <iframe id="f" src="{url}" style="width:{width}px;min-width:{width}px;flex-shrink:0;height:{height}px;border:0;background:white;
              transform-origin:top center;box-shadow:0 4px 24px rgba(0,0,0,.18)"></iframe>
    </div>
    <script>
      const wrap = document.getElementById('wrap'), f = document.getElementById('f');
      function fit() {{ const s = Math.min(1, wrap.clientWidth / {width});
        f.style.transform = 'scale(' + s + ')'; f.style.height = ({height} / s) + 'px'; }}
      fit(); window.addEventListener('resize', fit);
    </script>"""
    components.html(html, height=height + 10)


with tab_prev:
    srv = previews().get(proj.slug)
    if not srv:
        st.info("The preview server is not running.")
        if st.button("▶ Start preview", key="start_prev"):
            with st.spinner("Starting next dev..."):
                try:
                    previews().start(proj.slug, proj.site, proj.data)
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not start the preview: {str(e)[:600]}")
    else:
        c1, c2 = st.columns([3, 1])
        device = c1.radio("Device", ["Desktop · 1440", "Tablet · 768", "Mobile · 390"], horizontal=True,
                          label_visibility="collapsed")
        c2.markdown(f"[Open in new tab ↗]({srv.url})")
        width = int(device.split("·")[1])
        preview_frame(srv.url, width, 820 if width > 500 else 780)
    st.markdown("#### Original vs generated")
    vp = st.radio("Screenshot", ["desktop", "mobile"], horizontal=True, key="cmpvp")
    cmp = proj.validation_dir / f"compare-{vp}.png"
    if cmp.exists():
        st.caption("Left: original · Middle: generated · Right: pixel difference")
        with st.container(height=640):
            st.image(str(cmp), width="stretch")
    else:
        st.caption("Run a preview check to produce screenshots.")
    if srv and st.button("Re-run runtime check + visual score"):
        with st.spinner("Checking in headless Chromium..."):
            runtime_check_and_score(proj, srv.url, llm, Reporter(), repair=False)
        st.rerun()

# ---------------------------------------------------------------------------
with tab_mod:
    st.caption("Describe a change in plain English. The agent plans the smallest edit, changes only the affected "
               "files, type-checks them (auto-repairing errors) and rolls back if anything breaks.")
    chat = ss.chat.setdefault(proj.slug, [])
    cols = st.columns(len(EXAMPLES))
    clicked = None
    for c, ex in zip(cols, EXAMPLES):
        if c.button(ex, width="stretch"):
            clicked = ex
    for msg in chat:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("diff"):
                with st.expander("Diff"):
                    st.code(msg["diff"][:20000], language="diff")
    instruction = st.chat_input("e.g. Make the navbar sticky") or clicked
    if instruction:
        chat.append({"role": "user", "content": instruction})
        with st.chat_message("user"):
            st.markdown(instruction)
        with st.chat_message("assistant"):
            box = st.status("Working...", expanded=True)
            rep = Reporter(lambda ev: box.write(f"{ev.message}"))
            srv = previews().get(proj.slug)
            res = modify(proj, instruction, llm, rep, check_url=srv.url if srv else None)
            if res.ok:
                box.update(label=f"✅ v{res.version}: {res.summary}", state="complete", expanded=False)
                text = (f"**{res.summary}**  \nChanged: `{'`, `'.join(res.changed_files)}` · "
                        f"cost ${res.cost_usd:.4f} · saved as v{res.version}")
            else:
                box.update(label="Not applied", state="error", expanded=False)
                text = f"⚠️ {res.error}"
            st.markdown(text)
            chat.append({"role": "assistant", "content": text, "diff": res.diff})
        st.rerun()

    st.divider()
    hist = history.versions(proj)
    c1, c2 = st.columns([3, 1])
    c1.markdown("**Version history**")
    if c2.button("↶ Undo last change", disabled=len(hist) < 2, width="stretch"):
        undo(proj)
        st.rerun()
    for h in reversed(hist[-12:]):
        st.caption(f"v{h['version']} · {h['kind']} · {h['message']}")

# ---------------------------------------------------------------------------
with tab_an:
    tokens = proj.read_json("tokens.json", {}) or {}
    bp = proj.read_json("blueprint.json", {}) or {}
    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown("**Summary**")
        st.write(bp.get("summary") or "-")
        if bp.get("style_notes"):
            st.caption(bp["style_notes"])
        st.markdown("**Design tokens** (measured from computed styles)")
        sw = "".join(f'<div class="swatch"><span class="c" style="background:{v}"></span><div><b>{k}</b><br>'
                     f'<code>{v}</code></div></div>' for k, v in (tokens.get("colors") or {}).items())
        st.markdown(sw, unsafe_allow_html=True)
        f = tokens.get("fonts", {})
        r = tokens.get("radius", {})
        st.markdown(f"Fonts: heading **{f.get('heading')}** · body **{f.get('body')}**"
                    + (f" · mono **{f.get('mono')}**" if f.get("mono") else "")
                    + f"  \nRadius: buttons {r.get('button')}px · cards {r.get('card')}px · container "
                      f"{tokens.get('container') or '-'}px  \nType scale: "
                    + ", ".join(f"{k} {v}px" for k, v in (tokens.get("typeScale") or {}).items() if v))
    with c2:
        dpng = proj.capture_dir / "desktop.png"
        if dpng.exists():
            with st.container(height=420):
                st.image(str(dpng), caption="Original (desktop capture)", width="stretch")
    st.markdown("**Sections** (layout understood by the agent)")
    rows = []
    for s_ in manifest.get("sections", []):
        box = s_.get("orig_box_d") or [0, 0, 0, 0]
        rows.append({"component": s_["name"], "type": s_.get("type"), "source": s_.get("source"),
                     "y": box[1], "height": box[3], "description": s_.get("description", "")})
    st.dataframe(rows, width="stretch", hide_index=True)
    names = [s_["name"] for s_ in manifest.get("sections", [])]
    if names:
        pick = st.selectbox("Reference markup extracted for section", names)
        o = proj.data / "outlines" / f"{pick}.html"
        if o.exists():
            st.code(o.read_text(encoding="utf-8")[:12000], language="html")
    with st.expander("Blueprint JSON"):
        st.json(bp)

# ---------------------------------------------------------------------------
with tab_code:
    files = sorted([p for p in proj.site.rglob("*") if p.is_file() and p.suffix in (".tsx", ".ts", ".css", ".json", ".mjs")
                    and "node_modules" not in p.parts and ".next" not in p.parts])
    rels = [proj.rel(p) for p in files]
    default = rels.index("app/page.tsx") if "app/page.tsx" in rels else 0
    pick = st.selectbox("File", rels, index=default)
    if pick:
        p = proj.site / pick
        lang = {"tsx": "tsx", "ts": "typescript", "css": "css", "json": "json", "mjs": "javascript"}.get(p.suffix[1:], "text")
        st.code(p.read_text(encoding="utf-8"), language=lang, line_numbers=True)

# ---------------------------------------------------------------------------
with tab_val:
    val = proj.read_json("validation.json", {}) or {}
    rt = proj.read_json("runtime.json", {}) or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Type-check rounds", val.get("typecheck_rounds", "-"))
    c2.metric("LLM repairs", len(val.get("repaired", [])))
    c3.metric("Sections without LLM", sum(1 for s_ in sections_meta if s_.get("source") in ("fallback", "placeholder")),
              help="Rendered by the deterministic fallback (LLM unavailable, or its code failed every repair)")
    c4.metric("Production build", {True: "passed", False: "failed", None: "-"}[val.get("build_ok")])
    if val.get("repaired"):
        st.caption("Repaired by the LLM: " + ", ".join(val["repaired"]))
    if rt:
        st.markdown("**Runtime check**: " + ("✅ no page errors" if rt.get("ok") else "❌ page errors"))
        for e in (rt.get("page_errors") or []) + (rt.get("console_errors") or []):
            st.code(e[:800])
        for w in rt.get("warnings") or []:
            st.warning(w)
    sec_scores = scores.get("sections") or {}
    if sec_scores:
        st.markdown("**Per-section visual similarity** (desktop)")
        st.dataframe([{"section": k, **v} for k, v in sec_scores.items()], width="stretch", hide_index=True)
    if runs:
        last = runs[-1]
        st.markdown("**Stage timings (s)**: " + " · ".join(f"{k} {v}" for k, v in last.get("timings", {}).items()))
    if calls:
        st.markdown("**LLM calls**")
        total_in = sum(c.get("prompt_tokens", 0) for c in calls if not c.get("cache_hit"))
        cached = sum(c.get("cached_tokens", 0) for c in calls)
        st.caption(f"{len(calls)} calls · {total_in:,} input tokens ({cached:,} served from provider cache) · "
                   f"{sum(c.get('completion_tokens', 0) for c in calls if not c.get('cache_hit')):,} output tokens · "
                   f"{sum(1 for c in calls if c.get('cache_hit'))} local cache hits · "
                   f"${sum(c.get('cost_usd', 0) for c in calls):.4f}")
        st.dataframe([{k: c.get(k) for k in ("stage", "model", "prompt_tokens", "cached_tokens", "completion_tokens",
                                             "cost_usd", "latency_s", "cache_hit", "ok", "error")} for c in calls],
                     width="stretch", hide_index=True)
    elif llm.is_mock:
        st.caption("No LLM calls - offline mode.")
    if runs and runs[-1].get("events"):
        with st.expander("Agent log (last run)"):
            for ev in runs[-1]["events"]:
                st.text(f"[{ev['stage']}] {ev['message']}")
