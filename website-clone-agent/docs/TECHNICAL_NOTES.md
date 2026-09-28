# Technical discussion notes

Short answers to the discussion topics listed in the assignment, with pointers into the code.

## Why this architecture?

It is a pipeline of specialised stages with an LLM only where judgement is needed. Measurement (capture, tokens, CSS→Tailwind) is deterministic because the browser already knows the truth. The LLM does what code can't: name sections, turn raw markup into clean components, infer interactivity, and apply open-ended edits. Every LLM stage has a deterministic fallback, so the agent degrades instead of failing. See `agent/pipeline.py`.

## How does the agent analyze a website?

1. `capture/browser.py` loads the page in Chromium at 1440 px and scrolls it to trigger lazy content. It finishes animations (`document.getAnimations()`), disables motion, dismisses consent banners and detects bot walls.
2. `capture/extract.js` tags every element with a stable id and records, for visible elements only: about 80 computed style properties, geometry, attributes and ordered children (text nodes included). It does this at 1440 px and again at 390 px on the same DOM.
3. `capture/dom.py` merges both into one tree, where each node has a desktop and a mobile record, and segments sections. It descends through wrapper divs, expands `<main>`-like wrappers, ignores decorative rails, groups small lead-ins with the next block, splits very tall blocks, wraps table rows in valid tables, and drops overlays.
4. `analysis/tokens.py` derives palette roles (background, foreground, primary from saturated CTA backgrounds, surface, muted, border), font stacks, radii, container width and type scale.
5. `analysis/stylemap.py` converts styles to Tailwind. Inherited properties are emitted only when they change. It restores `mx-auto`/`ml-auto`, maps grid tracks and placement, and emits `min-h` when a box is taller than its content. Base classes come from the mobile capture and `lg:` overrides from the desktop capture.
6. One JSON LLM call (`analysis/blueprint.py`) labels each section with a type, a component name, its layout intent, responsive notes and interactivity.

## How do you generate reliable frontend code?

- There is a fixed, pre-validated scaffold. The LLM never writes configs, the layout or `page.tsx`.
- The LLM gets exact reference markup and a small set of hard rules: allowed imports, `"use client"` only when needed, typed arrays + `.map`, semantic root with an id, no `next/image`.
- One file per section, generated in parallel. This keeps outputs short and failures isolated.
- The sanitizer (`generation/codefix.py`) fixes fence extraction, `class`→`className`, the `"use client"` directive, a missing default export, the React namespace import, and lucide icon names checked against the installed package's exports.

## How do you handle generated-code errors?

`validation/repair.py` runs a loop: `tsc` errors grouped by file → repair prompt containing only that file and its compiler messages → up to 3 attempts → deterministic fallback (`generation/fallback.py`, same classes, always valid TSX) → placeholder.

After that comes `next build`, whose errors are mapped to files the same way. The browser runtime check (`validation/checks.py`) catches page errors, console and hydration errors, the Next error overlay, and mobile overflow. These are attributed to a section through React component stacks or the dev-server log. Modifications validate the same way and roll back automatically.

## How do you improve visual accuracy?

- Measured values instead of guesses; downloaded images, SVGs and fonts; the original font stacks with their generic fallbacks.
- Spacing that the page applied *between* sections is recreated in the page wrapper (vertical gaps, symmetric insets).
- `validation/visual.py` scores SSIM, colour histogram and height ratio per section. The ≤3 worst LLM sections are re-prompted with the measured differences, and the result is kept only if it still type-checks.
- Next steps: a vision model for the refine loop, element-level box matching (original vs generated) instead of section-level, and hover/animation capture.

## How would you reduce AI/API costs?

These are already in place: deterministic stages, compressed outlines, a prompt-prefix cache warm-up, a local response cache, targeted repairs, edits and refines, non-thinking mode, model routing, and per-call metering.

Further options:

- Reuse components across sections with the same signature (for example card grids) and generate once.
- Diff-based edits (search/replace blocks) instead of full-file rewrites for large files.
- Batch the off-peak DeepSeek discount for bulk runs.
- A small fine-tuned model for the markup→component step.

## How would you scale the system?

- Split the pipeline into workers on a job queue (capture, generate, validate). Capture and validation use pooled headless browsers; generation is an LLM fan-out with rate-limit-aware concurrency.
- Put the shared Node runtime in a container image; builds run in sandboxed containers; previews are deployed to ephemeral URLs instead of local ports.
- Store projects in object storage and keep version history in git.
- Add observability: the per-call metering already exists, and would be complemented by per-site success metrics and an eval set of sites run in CI to catch regressions.

## What would you improve with more time?

- Multi-page crawling and routing (internal links → Next.js routes).
- Real interactive widgets (tabs, accordions, carousels) detected from the DOM and ARIA roles.
- Hover and focus states and animations captured via CSS rules.
- Vision-model verification.
- Streaming progress with a background job in the UI.
- A component library shared across sections (detect repeated cards/buttons once).
- Accessibility checks with axe.
- Code-level edits with AST-aware patches.
