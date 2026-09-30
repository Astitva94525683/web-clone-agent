"""A scripted stand-in for the real LLM so the *LLM code path* (prompting,
parsing, sanitising, repair loop, fallbacks, modification ops) can be tested
offline and deterministically.
"""
from __future__ import annotations

import json
import re

from agent.llm import LLM, LLMCall


def simple_component(name: str, slug: str, heading: str = "", extra: str = "") -> str:
    heading = heading or name
    return f'''```tsx
import {{ Container }} from "@/components/ui";

const items: {{ title: string; text: string }}[] = [
  {{ title: "One", text: "First item" }},
  {{ title: "Two", text: "Second item" }},
];

export default function {name}() {{
  return (
    <section id="{slug}" className="bg-background py-20">
      <Container>
        <h2 className="font-heading text-[36px] text-foreground">{heading}</h2>{extra}
        <div className="mt-8 grid grid-cols-1 gap-6 lg:grid-cols-2">
          {{items.map((it) => (
            <div key={{it.title}} className="rounded-card border border-border p-6">
              <h3 className="text-[20px] font-semibold">{{it.title}}</h3>
              <p className="text-muted-foreground">{{it.text}}</p>
            </div>
          ))}}
        </div>
      </Container>
    </section>
  );
}}
```'''


NAVBAR_WITH_MISTAKES = '''Here is the component:
```tsx
import { useState } from "react";
import { Menu, HamburgerIconThatDoesNotExist } from "lucide-react";
import { Button } from "@/components/ui";

const links = [{ label: "Features", href: "#features" }, { label: "Pricing", href: "#pricing" }];

export default function Navbar() {
  const [open, setOpen] = useState(false);
  return (
    <header id="navbar" className="bg-white border-b border-border">
      <div class="mx-auto flex h-[72px] max-w-site items-center justify-between px-6">
        <a href="#" className="text-[22px] font-bold text-primary">Brewly</a>
        <nav className="hidden gap-7 lg:flex">
          {links.map((l) => <a key={l.href} href={l.href} className="text-[15px] text-muted-foreground">{l.label}</a>)}
        </nav>
        <Button href="#" className="hidden lg:inline-flex">Start brewing</Button>
        <button type="button" aria-label="Open menu" className="lg:hidden" onClick={() => setOpen(!open)}>
          {open ? <HamburgerIconThatDoesNotExist /> : <Menu />}
        </button>
      </div>
    </header>
  );
}
```'''


class ScriptedLLM(LLM):
    """Behaves like a real model, including typical mistakes."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.prompts: list[tuple[str, list]] = []
        self.saw_image = False

    @property
    def is_mock(self) -> bool:  # pretend a real API is configured
        return False

    @property
    def label(self) -> str:
        return "scripted test LLM"

    def chat(self, messages, *, stage, fast=False, json_mode=False, max_tokens=8000, temperature=0.2,
             max_seconds=None):
        self.prompts.append((stage, messages))
        self._record(LLMCall(stage=stage, model="scripted", prompt_tokens=1000, completion_tokens=500,
                             cost_usd=0.001))
        content = messages[-1]["content"]
        if isinstance(content, list):  # vision message: [{"type": "text"}, {"type": "image_url"}]
            self.saw_image = any(part.get("type") == "image_url" for part in content)
            content = " ".join(part.get("text", "") for part in content if part.get("type") == "text")
        user = content
        if stage == "analyze":
            idx = [int(i) for i in re.findall(r'"index": (\d+)', user)]
            names = ["Navbar", "Hero", "FeatureGrid", "Pricing", "Testimonials", "Footer"]
            return json.dumps({
                "site_name": "Brewly", "summary": "Coffee subscription landing page.",
                "style_notes": "Warm light theme, serif type.", "tokens": {"primary": "#b45309"},
                "sections": [{"index": i, "name": names[i] if i < len(names) else f"Extra{i}",
                              "type": "content", "description": "d", "responsive": "stacks",
                              "interactive": ""} for i in idx],
            })
        if stage.startswith("generate:"):
            name = stage.split(":", 1)[1]
            slug = re.search(r"Root id: (\S+)", user).group(1)
            if name == "Navbar":
                return NAVBAR_WITH_MISTAKES
            if name == "FeatureGrid":  # type error -> must be repaired
                return simple_component(name, slug, extra='\n        <p>{(1 as number).toFixed("x")}</p>')
            if name == "Pricing":  # no code at all -> deterministic fallback
                return "Sorry, I cannot help with that."
            return simple_component(name, slug)
        if stage.startswith("repair:"):
            fname = stage.split(":", 1)[1].removesuffix(".tsx")
            slug = re.sub(r"(?<!^)(?=[A-Z])", "-", fname).lower()
            return simple_component(fname, slug, heading="Repaired")
        if stage == "modify:plan":
            return json.dumps({"summary": "blue, sticky nav, testimonials", "operations": [
                {"op": "update_tokens", "changes": {"--color-primary": "#2563eb"}},
                {"op": "edit_section", "section": "Navbar", "instruction": "make sticky"},
                {"op": "add_section", "name": "CustomerStories", "after": "FeatureGrid",
                 "instruction": "three testimonials"},
                {"op": "remove_section", "section": "Pricing"},
            ]})
        if stage.startswith("modify:edit:"):
            code = re.search(r"```tsx\n(.*?)```", user, re.S).group(1)
            return "```tsx\n" + code.replace('className="bg-white', 'className="sticky top-0 z-50 bg-white', 1) + "```"
        if stage.startswith("modify:add:"):
            name = stage.rsplit(":", 1)[1]
            slug = re.search(r'root id="([^"]+)"', user).group(1)
            return simple_component(name, slug, heading="What customers say")
        if stage.startswith("refine:"):
            return "no changes"
        raise AssertionError(f"unexpected stage {stage}")
