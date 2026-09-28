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
