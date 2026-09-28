# Demo video script (5-10 min)

Covers the six items the assignment asks for. Before recording:

- Put your DeepSeek key in `.env`.
- Run one throwaway clone so the shared Next.js runtime is already installed.
- Start `streamlit run app.py`.
- Have the evaluation URLs ready.

| # | Time | Show | Say (short) |
|---|---|---|---|
| 0 | 0:00-0:40 | README architecture diagram | "Six stages. The browser measures, the LLM structures, code validates. Green boxes are free, purple ones are LLM calls." |
| 1 | 0:40-1:10 | Paste URL #1 into the control panel → **Clone website** | "Any public URL. Nothing is hard-coded per site." |
| 2 | 1:10-2:30 | Live log: *Analyze website* / *Understand UI / layout* lines, then the **Analysis** tab: colour swatches, fonts, section table, reference markup | "Captured at 1440 and 390 px. Tokens and Tailwind classes come from computed styles. One cheap LLM call names the sections and describes their layout and responsiveness." |
| 3 | 2:30-3:30 | Log lines per section (*generated*), repairs if any; **Code** tab: `page.tsx`, one section, `globals.css` | "One component per section, generated in parallel, shared prompt prefix cached. The sanitizer and type-checker catch errors; failing files are repaired alone." |
| 4 | 3:30-4:30 | **Preview** tab, desktop frame; *Original vs generated* strip and similarity scores | "Local `next dev`. Runtime check in headless Chrome, visual score per section, and the worst sections get one refine pass." |
| 5 | 4:30-5:15 | Switch to **Mobile · 390** (open the hamburger menu) and **Tablet** | "Responsive classes come from the real mobile capture, not guesses." |
| 6 | 5:15-7:30 | **Modify with AI**: click *Change the primary color to blue*, then *Make the navbar sticky*, then type *Add a testimonials section* / *Replace the hero section with a bakery hero* / *Remove the pricing section*. Open a **Diff**; press **Undo** once | "Plan → only the affected files → validate → new version, or automatic rollback. The colour change is one CSS variable and costs no code tokens." |
| 7 | 7:30-8:30 | Terminal: `python cli.py eval <url2> <url3>` (or clone them in the UI) | "Same agent on different sites. The table shows build status, LLM vs fallback sections, scores, time and cost." |
| 8 | 8:30-9:15 | **Validation & cost** tab | "Every call is metered: tokens, provider-cache hits, USD. Re-runs hit the local cache and are free." |

Tips:

- Record at 1440 px or wider.
- If a site takes long, cut the waiting in editing but keep the log visible.
- If one site shows fallback sections, mention it: the site still builds.
