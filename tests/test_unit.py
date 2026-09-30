"""Fast unit tests for the deterministic parts of the agent (no browser, no Node)."""
from agent.analysis.stylemap import TokenMap, space, safe_arb, to_hex, _grid_place
from agent.generation.codefix import extract_code, sanitize, fix_lucide
from agent.generation.scaffold import update_theme_vars, wrapper_class
from agent.llm import parse_json
from agent.modification.modify import heuristic_plan
from agent.analysis.blueprint import pascal, kebab


def test_spacing_scale_and_arbitrary():
    assert space("p", 16) == "p-4"
    assert space("p", 6) == "p-1.5"
    assert space("p", 13) == "p-[13px]"
    assert space("m", -8) == "-m-2"


def test_colors():
    assert to_hex("rgb(255, 0, 0)") == "#ff0000"
    assert to_hex("rgba(0, 0, 0, 0)") is None
    assert to_hex("rgba(0, 0, 0, 0.5)") == "#00000080"
    tm = TokenMap(colors={"primary": "#533afd", "background": "#ffffff", "foreground": "#000000"})
    assert tm.color("bg", "#533afd") == "bg-primary"
    assert tm.color("text", "#123456") == "text-[#123456]"


def test_unsafe_arbitrary_values_are_dropped():
    assert safe_arb("url('data:image/svg+xml;utf8,<svg></svg>')") is None
    assert safe_arb('url("/assets/a.png")') == "url(/assets/a.png)"


def test_grid_placement():
    assert _grid_place("auto", "span 3", "col") == "col-span-3"
    assert _grid_place("1", "-1", "col") == "col-span-full"
    assert _grid_place("2", "5", "col") == "col-[2/5]"
    assert _grid_place("auto", "auto", "col") == ""


def test_extract_code_prefers_largest_block():
    text = "intro\n```tsx\nexport default function A() { return null }\n```\nand ```ts\nx\n```"
    assert extract_code(text).startswith("export default function A")
    assert extract_code("no code here") is None


def test_sanitize_fixes_common_mistakes():
    code = ('import { useState } from "react";\nexport default function Nav() {\n'
            '  const [o, s] = useState(false);\n  return <div class="x" onClick={() => s(!o)} />;\n}\n')
    fixed, notes = sanitize(code, "Nav")
    assert fixed.startswith('"use client";')
    assert 'className="x"' in fixed
    assert any("use client" in n for n in notes)


def test_sanitize_adds_default_export():
    fixed, _ = sanitize("function Hero() { return <section /> }\n", "Hero")
    assert "export default Hero;" in fixed


def test_fix_lucide_replaces_unknown_icons():
    code = 'import { Menu, NotARealIcon } from "lucide-react";\n'
    fixed, bad = fix_lucide(code, frozenset({"Menu", "Circle"}))
    assert bad == ["NotARealIcon"]
    assert "Circle as NotARealIcon" in fixed and "Menu" in fixed


def test_parse_json_tolerates_fences_and_trailing_commas():
    assert parse_json('```json\n{"a": [1, 2,],}\n```') == {"a": [1, 2]}
    assert parse_json('Sure! {"ok": true} hope that helps') == {"ok": True}


def test_update_theme_vars():
    css = "@theme {\n  --color-primary: #533afd;\n}\n"
    out, applied = update_theme_vars(css, {"--color-primary": "#2563eb", "color-accent": "#ff0000"})
    assert "--color-primary: #2563eb;" in out and "--color-accent: #ff0000;" in out
    assert applied == ["--color-primary", "--color-accent"]
    _, applied = update_theme_vars(css, {"--color-primary": "red; } body { x: y"})
    assert applied == []  # injection attempt rejected


def test_wrapper_class():
    assert wrapper_class({}) == "contents"
    assert wrapper_class({"gap_d": 40, "gap_m": 20}) == "pt-[20px] lg:pt-[40px]"


def test_heuristic_plan():
    secs = [{"name": "Navbar", "type": "navbar"}, {"name": "Pricing", "type": "pricing"}]
    p = heuristic_plan("Change the primary color to blue", secs)
    assert p["operations"][0]["changes"]["--color-primary"] == "#2563eb"
    assert heuristic_plan("Make the navbar sticky", secs)["operations"][0]["section"] == "Navbar"
    assert heuristic_plan("Remove the pricing section", secs)["operations"][0] == {"op": "remove_section", "section": "Pricing"}
    assert heuristic_plan("Add a testimonials section", secs)["operations"] == []


def test_names():
    assert pascal("feature grid!") == "FeatureGrid"
    assert pascal("3 columns") == "Section3Columns"
    assert kebab("FeatureGrid") == "feature-grid"


def test_modify_plan_falls_back_to_rules_when_llm_fails(tmp_path):
    from agent.llm import LLM, LLMError
    from agent.modification.modify import plan
    from agent.project import Project

    class BrokenLLM(LLM):
        @property
        def is_mock(self):
            return False

        def chat(self, *a, **kw):
            raise LLMError("Model 'x' is not available")

    site = tmp_path / "p" / "site"
    (site / "app").mkdir(parents=True)
    (site / "app" / "globals.css").write_text("@theme {\n  --color-primary: #ff0000;\n}\n", encoding="utf-8")
    proj = Project(slug="p", root=tmp_path / "p")
    proj.save_manifest({"sections": [{"name": "Navbar", "type": "navbar"}]})
    p = plan(BrokenLLM(), proj, "Change the primary color to blue")
    assert p["operations"][0]["op"] == "update_tokens"
    assert "not available" in p["llm_error"]


# ---- CSS -> Tailwind translation of captured layouts ------------------------------------
def _style(**kw) -> dict:
    base = {"display": "block", "position": "static", "box-sizing": "border-box", "color": "rgb(0, 0, 0)",
            "font-size": "16px", "font-family": "Inter", "font-weight": "400", "line-height": "24px",
            "text-align": "start", "max-width": "none", "min-height": "0px"}
    base.update({k.replace("_", "-"): v for k, v in kw.items()})
    return base


def _tree(parent_style: dict, parent_rect: list, child_style: dict, child_rect: list, child_tag="div",
          kids=("x",), siblings=0):
    from agent.capture.dom import Node

    parent = Node(id="1", tag="div", d={"rect": parent_rect, "s": parent_style, "a": {}}, m=None)
    child = Node(id="2", tag=child_tag, d={"rect": child_rect, "s": child_style, "a": {}}, m=None,
                 parent=parent, kids=list(kids))
    parent.kids = [child] + [Node(id=str(3 + i), tag="div", d={"rect": [0, 0, 10, 10], "s": _style(), "a": {}},
                                  m=None, parent=parent) for i in range(siblings)]
    return parent, child


def test_content_box_sizes_include_padding():
    from agent.analysis.stylemap import style_classes

    tm = TokenMap()
    parent, hero = _tree(_style(), [0, 0, 1440, 980],
                         _style(box_sizing="content-box", min_height="900px", padding_bottom="80px"),
                         [0, 0, 1440, 980])
    assert style_classes(hero, "d", tm, parent.d["s"])["min-h"] == "min-h-[calc(100vh_+_80px)]"
    parent, wrap = _tree(_style(), [0, 0, 1440, 400],
                         _style(box_sizing="content-box", max_width="1200px", padding_left="24px",
                                padding_right="24px", margin_left="96px", margin_right="96px"),
                         [96, 0, 1248, 400])
    assert style_classes(wrap, "d", tm, parent.d["s"])["max-w"] == "max-w-[1248px]"


def test_explicit_block_widths_are_kept():
    from agent.analysis.stylemap import style_classes

    tm = TokenMap()
    parent, icon = _tree(_style(padding_left="28px", padding_right="28px"), [0, 0, 300, 200],
                         _style(display="flex"), [28, 28, 44, 44], kids=[])
    assert style_classes(icon, "d", tm, parent.d["s"])["w"] == "w-[44px] max-w-full"
    parent, table = _tree(_style(), [0, 0, 800, 300], _style(display="table"), [0, 0, 800, 300], child_tag="table")
    assert style_classes(table, "d", tm, parent.d["s"])["w"] == "w-full"
    # a block that simply fills its parent gets no width class
    parent, full = _tree(_style(), [0, 0, 800, 300], _style(), [0, 0, 800, 300])
    assert "w" not in style_classes(full, "d", tm, parent.d["s"])


def test_centering_margins():
    from agent.analysis.stylemap import style_classes

    tm = TokenMap()
    # fixed-width centred container (width: 960px; margin: 0 auto)
    parent, box = _tree(_style(), [0, 0, 1440, 300], _style(margin_left="240px", margin_right="240px"),
                        [240, 0, 960, 300])
    cls = style_classes(box, "d", tm, parent.d["s"])
    assert "mx-auto" in cls["margin"] and cls["max-w"] == "max-w-[960px]" and cls["w"] == "w-full"
    # equal margins between flex-row siblings are spacing, not centring
    parent, item = _tree(_style(display="flex", flex_direction="row"), [0, 0, 600, 40],
                         _style(margin_left="12px", margin_right="12px"), [12, 0, 80, 40], siblings=2)
    assert style_classes(item, "d", tm, parent.d["s"])["margin"] == "mx-3"


def test_percent_radius_th_alignment_and_justify_self():
    from agent.analysis.stylemap import radius_px, style_classes

    assert radius_px("50%", 44, 44) == 22 and radius_px("8px 4px", 10, 10) == 8
    tm = TokenMap()
    parent, img = _tree(_style(), [0, 0, 200, 44], _style(border_top_left_radius="50%",
                        border_top_right_radius="50%", border_bottom_right_radius="50%",
                        border_bottom_left_radius="50%"), [0, 0, 44, 44], child_tag="img", kids=[])
    assert style_classes(img, "d", tm, parent.d["s"])["rounded"] == "rounded-full"
    parent, th = _tree(_style(text_align="left"), [0, 0, 400, 40], _style(text_align="left"), [0, 0, 200, 40],
                       child_tag="th")
    assert style_classes(th, "d", tm, parent.d["s"])["text-align"] == "text-left"
    parent, btn = _tree(_style(display="grid"), [0, 0, 400, 40], _style(justify_self="start"), [0, 0, 120, 40],
                        child_tag="button")
    assert style_classes(btn, "d", tm, parent.d["s"])["justify-self"] == "justify-self-start"


def test_responsive_merge_resets_sides_only_mobile_set():
    from agent.analysis.stylemap import _side_resets

    assert _side_resets("margin", "mx-2", "ml-6.5") == ["mr-0"]
    assert _side_resets("padding", "py-5 px-4", "px-8") == ["pt-0", "pb-0"]
    assert _side_resets("inset", "inset-x-0 top-0", "left-4 top-2") == ["right-auto"]
    assert _side_resets("margin", "m-4", "m-8") == []


def test_merge_kids_orders_pseudo_elements():
    from agent.capture.dom import _merge_kids

    desktop = ["5::before", "6", "9", "5::after"]
    mobile = ["5::before", "6", "7", "9", "5::after"]
    assert _merge_kids(desktop, mobile, set(desktop)) == ["5::before", "6", "7", "9", "5::after"]


def test_top_strip_does_not_become_the_header():
    from agent.capture.dom import Node, assign_names, find_sections

    def block(id_, tag, y, h, text="", cls=""):
        n = Node(id=id_, tag=tag, d={"rect": [0, y, 1440, h], "s": _style(), "a": {"cls": cls}},
                 m={"rect": [0, y, 390, h], "s": _style(), "a": {"cls": cls}})
        n.kids = [text] if text else []
        return n

    body = Node(id="0", tag="body", d={"rect": [0, 0, 1440, 2000], "s": _style(), "a": {}},
                m={"rect": [0, 0, 390, 2000], "s": _style(), "a": {}})
    kids = [block("1", "div", 0, 30, "Open daily 12-23", "topbar"), block("2", "header", 30, 80, "Trattoria"),
            block("3", "section", 110, 900, "Welcome"), block("4", "footer", 1010, 990, "© 2026")]
    for k in kids:
        k.parent = body
    body.kids = kids
    secs = find_sections(body, 1440)
    assert [s.kind for s in secs] == ["section", "header", "section", "footer"]
    assign_names(secs)
    assert [s.name for s in secs] == ["TopBar", "Navbar", "Hero", "Footer"]


def test_free_port_skips_ports_that_answer():
    import socket

    from agent.validation.runtime import free_port

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        assert free_port(port) != port
        assert free_port(port, taken={port + 1}) not in (port, port + 1)


def test_apply_patch_exact_and_indentation_tolerant():
    from agent.generation.codefix import apply_patch

    code = 'export default function A() {\n  return (\n    <a className="bg-primary px-4">Go</a>\n  );\n}\n'
    reply = '<<<<<<< SEARCH\n    <a className="bg-primary px-4">Go</a>\n=======\n    <a className="bg-[#16a34a] px-4">Go</a>\n>>>>>>> REPLACE'
    assert 'bg-[#16a34a]' in apply_patch(code, reply)
    loose = '<<<<<<< SEARCH\n<a className="bg-primary px-4">Go</a>\n=======\n<a className="bg-green-600 px-4">Go</a>\n>>>>>>> REPLACE'
    out = apply_patch(code, loose)
    assert '    <a className="bg-green-600 px-4">Go</a>' in out  # original indentation kept
    assert apply_patch(code, "no blocks here") is None
    missing = '<<<<<<< SEARCH\nnot in file\n=======\nx\n>>>>>>> REPLACE'
    assert apply_patch(code, missing) is None
