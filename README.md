# ppt-harness

An interface for creating, editing, and inspecting presentations by talking to an LLM —
covering both decks it generates and decks you import.

Design: **[ARCHITECTURE.md](ARCHITECTURE.md)** for the shape, **[DESIGN.md](DESIGN.md)** for
the schemas and signatures.

---

## What it does

Opens a real `.pptx`, extracts its theme, measures every text box against real font metrics,
lets a model edit and extend it, and writes the file back by **patching the original
package** rather than rebuilding it — so SmartArt, media, animations, comments, and
sensitivity labels survive an edit that never touched them.

```bash
uv sync --all-extras                 # locked in uv.lock
uv run playwright install chromium   # optional; only the test-time oracle needs it

uv run ppt-harness new     deck.pptx  # an empty themed deck; --from borrows a theme
uv run ppt-harness serve   deck.pptx  # chat + live preview at localhost:8000
uv run ppt-harness generate "a deck about X" out.pptx   # the one command that needs a model
uv run ppt-harness outline deck.pptx  # slides, modes, gists
uv run ppt-harness theme   deck.pptx  # palette and type, and what had to be guessed
uv run ppt-harness lint    deck.pptx  # overflow, measured not eyeballed
uv run ppt-harness review  deck.pptx  # editorial findings — judged, not measured
uv run ppt-harness compare deck.pptx  # preview vs PowerPoint, side by side
uv run ppt-harness freeze  deck.pptx s1   # lay out in a real browser, report geometry
uv run ppt-harness fits    deck.pptx s1/s1_sh2 "Some replacement text"
uv run ppt-harness templates          # themes that ship with the harness
uv run ppt-harness bench run          # score the harness on a task suite
uv run ppt-harness bench corpus DIR   # round-trip real decks, no model needed
uv run ppt-harness tools              # every tool, with its mode gate
uv run ppt-harness call    --args '{...}' set_text
uv run ppt-harness export  deck.pptx out.pptx
```

### Starting from nothing

Every other command above takes an existing deck, and `new` only creates an empty one. Two
commands write slides, and both need a model — `generate` for a finished file and `serve`
for a conversation; the rest of the CLI is deliberately offline and deterministic. So
`serve` with no argument is the way in:

```bash
uv run ppt-harness serve                      # blank deck, chat, Export when you like
uv run ppt-harness serve --from company-template.pptx --title "Q3 board review"
uv run ppt-harness serve --template slate     # a theme that ships with the harness
uv run ppt-harness new brand-new.pptx --from company-template.pptx
```

On `serve` with no deck the browser offers the four built-in themes as a picker, each card a
real slide rendered on that theme rather than a swatch — the preview is the export, so the
honest thumbnail costs nothing but the render.

`generate` is the same turn loop without the browser, for a script or a benchmark that wants
a finished file rather than a conversation. It runs the same mode gate, budget and repair
ladder the chat client does, then lints and reviews before writing:

```bash
uv run ppt-harness generate "Q3 board review: growth, churn, and the hiring plan" \
    out.pptx --template slate --slides 8
```

`--from` reads the other deck's palette, faces and canvas size without copying a single
slide — the way to start something that matches a template you already have. On `serve` it
is the usual way in, because nobody starts from a blank canvas when a company file exists:
the deck opens empty and already on-brand, and the slides get written in the chat. Nothing
is imported, so every slide is `managed` and the whole component catalog is available.

`new --from` is the same borrow without a model — an empty themed file you can open later.

### When there is nothing to borrow

```bash
uv run ppt-harness templates          # slate, editorial, graphite, signal
uv run ppt-harness templates slate    # its palette and type, in full
uv run ppt-harness serve --template editorial --title "Where growth came from"
```

Four themes ship in [ppt_harness/templates/](ppt_harness/templates/), and they are **themes,
not decks**. An authored theme is ~1.5 kB of readable JSON that states every value; the same
look shipped as a `.pptx` is ~27 kB of binary from which extraction reads the palette and the
faces and has to **infer** nine other fields — the type scale, the spacing ramp, the grid
columns, half the semantic palette. Templates here come back with `inferred` empty, so
`get_theme` has nothing to ask you to correct.

`--from` remains the primary path: a real organisation has a real file, and matching it beats
any palette shipped here. These are for the deck that would otherwise be built on nothing.
Each is contrast-validated in the suite, so a template that shipped could not fail the
guarantee that managed slides never fail contrast.

A template carries no slides. A starter outline is a playbook's job — see `narrative-arc`.

Working files — preview renders, exports, saved sessions — go in `.harness/` beside the
project, never the system temp directory: on macOS that lives under `/private`, and a
sandboxed PowerPoint raises a permission dialog for folders the user never chose.
`PPT_HARNESS_CACHE` overrides the location.

The preview and the fidelity oracle need a real renderer — PowerPoint where installed,
LibreOffice otherwise, and LibreOffice anyway when PowerPoint is installed but will not
convert. `PPT_HARNESS_RENDERER=libreoffice` pins one. Pin it whenever a number will be
quoted: the two engines break lines and substitute fonts differently, so figures are only
comparable within an engine, and `to_pdf` reports the one it actually used.

As an MCP server, so any MCP host becomes a client:

```bash
ppt-harness-mcp deck.pptx
```

The web client needs a model. Claude and DeepSeek are reached by name alone; anything else
that speaks the OpenAI chat API is reached by pointing at it:

```bash
export ANTHROPIC_API_KEY=... && export PPT_HARNESS_MODEL=claude-opus-4-8
export DEEPSEEK_API_KEY=...  && export PPT_HARNESS_MODEL=deepseek-v4-flash

export OPENAI_API_KEY=...            # or point at anything that speaks the same API
export PPT_HARNESS_MODEL=gpt-4o
export PPT_HARNESS_BASE_URL=http://localhost:11434/v1   # e.g. Ollama, vLLM, LM Studio
ppt-harness serve deck.pptx
```

Copy `.env.example` to `.env` if you would rather not put a key in your shell profile. Real
environment variables always win.

Or drive it directly:

```python
from ppt_harness import Session, dispatch

session = Session.open("deck.pptx")
dispatch(session, "set_text", {"target": "s1/s1_sh2", "text": "FY26 outlook"})
dispatch(session, "add_slide", {
    "layout": "stack",
    "blocks": [
        {"region": "header", "component": "slide_title", "slots": {"title": "Findings"}},
        {"region": "body", "component": "bullets", "slots": {"items": ["One", "Two"]}},
    ],
})
dispatch(session, "export", {"path": "out.pptx"})
```

## Seven things worth knowing

**Slides carry a mode.** Imported slides are `freeform` — the harness holds the original
author's shapes and edits their text. Generated slides are `managed` — built from
components, with geometry derived. The tool set is gated on the mode, so a model is never
shown an operation that cannot apply. `adopt_slide` promotes an imported slide into
components when its structure is recognisable; `eject_slide` goes back.

**No tool takes a coordinate.** Not `x`, not a font size, not a colour. Components own
geometry and the theme owns type. This is enforced by the schemas, and there is a test that
fails if a coordinate ever appears in one. Freeform shapes are moved by *relationship* —
align, distribute, snap to grid — never by a number the model invented.

**A write is checked before it lands, and returns its own measurement.** Text is measured in
advance width against the font that will actually render it — HarfBuzz-shaped, resolved per
script, because CJK runs about 2.2x the width of Latin per character and counting characters
would misprice it. A rejection carries the capacity, the overage, and the ways out. When a
managed slide overflows anyway, `repair` walks a ladder — variant, then density, then
component degradation — and refuses to climb the last rung: it will reshape the slide, never
rewrite the user's words.

**Measurement never touches the renderer.** It is analytic, ~1ms, and works where no Office
is installed — the model's loop cannot depend on a GUI application. Only a person looking at
a picture pays the ~1s.

**There is a second axis, and it is advisory.** `lint` answers *does this slide fit*, which
is measured and never wrong. `review_deck` answers *does this deck land* — a title that
files rather than states, two claims joined by "and", a list past seven items, prose long
enough to be read rather than glanced at, a deck ending on "Questions?", plus the places
style drifts between slides: title case, bullet full stops, repeated titles. It **cannot
refuse a write**. A budget refusal is a fact; a finding is an opinion, and an opinion that
can block a write is a style guide holding a gun. The rules are tuned for precision over
recall and stay silent where the text cannot decide, because an advisory channel dies of
false positives. The interesting half — whether the argument holds — is not countable and
belongs to a model reading the deck with `house-style` or `narrative-arc` in front of it.

**The preview is the export, rendered.** Deck state goes out through the real exporter, a
real renderer turns it into a PDF, and pages are rasterised on demand — so the preview
cannot drift from the file. SmartArt, gradients and video posters come for free because
nothing reimplements them. Fonts are embedded on export where the licence permits, and the
faces that may not travel are **reported** rather than dropped quietly.

Two numbers, and they only mean anything with the renderer named. Measured against
**Microsoft PowerPoint** on macOS: an HTML reimplementation of *imported* slides reached
0.041 mean difference and still had no end of tail, which is why the preview goes through
the real exporter instead; *generated* slides, where the harness controls the CSS, measure
0.007. `ppt-harness compare` reproduces this on your own deck.

The reference renderer is PowerPoint where installed and **LibreOffice otherwise**
([fidelity/reference.py](ppt_harness/fidelity/reference.py)). The two do not agree — they
substitute fonts and break lines differently — so a figure produced against one is not
comparable to a figure produced against the other. Any number quoted from a comparison
should say which drew it.

**Work survives the process.** Every committed turn is journalled to `.harness/workspace/`
— a whole-deck snapshot for state, an op log for history — so a crash resumes with the deck
intact and undo still reaching back past the restart. Recovery walks a ladder (snapshot,
previous snapshot, source file replayed through the journal) and says which rung it landed
on, because a degraded resume is something to know before you export. `serve --fresh`
discards it.

## Tools

58, gated by mode. `ppt-harness tools` prints the current set.

| | |
|---|---|
| Read | `get_outline` `get_slide` `get_theme` `get_budget` `list_components` `render` `lint` `review_deck` `get_preferences` |
| Text | `set_text` `set_emphasis` `set_list` `set_align` `set_link` `set_notes` |
| Managed | `add_slide` `set_slots` `set_variant` `set_component` `set_override` `remove_block` `repair` |
| Freeform | `add_textbox` `add_image` `replace_image` `delete_shape` `duplicate_shape` `set_z_order` |
| Geometry | `align` `distribute` `nudge` `snap_to_grid` `match_size` `set_frame` `fit_box_to_text` |
| Objects | `add_table` `add_chart` `set_cell` `set_chart_data` |
| Data | `load_data` `list_datasets` `query_data` |
| Assets | `add_asset` `list_assets` |
| Deck | `duplicate_slide` `delete_slide` `hide_slide` `reorder` `set_layout` `set_slide_size` `adopt_slide` `eject_slide` `restyle` `set_theme_role` |
| History | `undo` `redo` `export` `remember_preference` |

16 components across 5 layout frames (`stack`, `two_col`, `title`, `hero_plus_row`,
`full_bleed`). The always-on context carries only component keys and one-line purposes; full
slot schemas sit behind `list_components(key)`.

## Preferences

The harness learns how you like decks made, from two channels. **Stated** rules — "never
pie charts" — are recorded by `remember_preference` and trusted at once. **Observed** ones
come free from the op log, which already records the author of every edit: a `user` op
landing on a target a `model` op just touched is a correction, so noticing one is a query
rather than new instrumentation.

Confidence is `n/(n+2)` and travels with the value. One correction is a hint the model may
weigh; eight is a rule. Nothing crosses from noticed to rule without you confirming it —
`get_preferences` surfaces what is worth asking about, and the profile is a readable JSON
file you can edit or delete. No fine-tuning, no weights.

## Layout

```
ppt_harness/
  state/       document, ops, store, richtext, slots, workspace, themes
  io/          import, theme extraction, mutating export, adoption, font embedding
  render/      fonts, measurement, expansion, budgets, html, browser, svg, preview
  components/  the catalog, layout frames, overrides
  freeform/    geometry constraints
  tools/       registry, router, and the tool modules
  core/        session, agent loop, providers, repair ladder, preferences, review
  adapters/    MCP server, CLI, web
  fidelity/    reference renderer, preview-vs-truth comparison
  templates/   authored themes to start a deck from
  bench/       task suites, metrics, corpus round-trip, benchmark adapters
skills/        playbooks the model can follow by name
scripts/       demo deck generator, brand template, demo recorder
tests/         31 files, 1354 tests
```

Subpackages have no `__init__.py` — they are namespace packages.

## Benchmarks

```bash
uv run ppt-harness bench run --suite core        # 12 briefs through the real agent loop
uv run ppt-harness bench corpus ~/govdocs1/pptx  # round-trip real decks — no model at all
uv run ppt-harness bench score deck.pptx         # PPTEval's design rubric, describe then score
uv run ppt-harness bench submit --to ppteval     # hand the same decks to a public benchmark
```

Two axes, and they are different kinds of claim. **Measured**: fit rate, refused writes,
export violations, parts lost on a round trip — deterministic, no judge, and the corpus half
needs no key or network. **Judged**: whether the deck is any good, which the public
benchmarks already do well, so `bench submit` writes our decks into the layout
[PresentBench](https://github.com/PresentBench/PresentBench),
[PPTEval](https://github.com/icip-cas/PPTAgent) and
[AutoPresent](https://github.com/para-lost/AutoPresent) expect and prints their command.
Nothing here re-implements or vendors a public benchmark.

The measured half exists because none of those benchmarks checks whether text fits or whether
an edited file survived being edited — the two things this harness is actually for. Details,
including which public metric is a poor fit for a component-based system and why, in
[ppt_harness/bench/README.md](ppt_harness/bench/README.md).

`bench score` runs PPTEval's rubric locally: a vision model describes a rendered slide, a
separate text-only call scores that description, and the scorer never sees the image — the
split is what earns the paper's human correlation. It needs a model that can actually read
an image (`PPT_HARNESS_VISION_MODEL`), and **nothing is substituted when there is not one**:
the deterministic checks still print, the judged axes are named as unmeasured, and the
command exits non-zero. An unmeasured deck and a badly-designed one must not read alike.

Read the number as a *delta* between two runs of the same deck, never as a grade. The rubric
turns 3 into 4 on the presence of icons, backgrounds and shapes, which is a fair question to
ask of a generator and an unfair one to ask of a harness whose whole design is that the model
cannot choose a colour or a position.

## The demo

```bash
uv run python scripts/record_demo.py                     # ~6 min to run, ~3 min of video
uv run python scripts/record_demo.py --acts open,build   # a shorter cut
uv run python scripts/record_demo.py --template company.pptx --title "FY26 kickoff"
```

Drives the real UI in Chromium and writes a `.webm`, in five acts: a new deck on a
template's theme, one turn that writes it from a brief, a customisation turn, an editorial
review the model acts on, then measurement and export.

It starts from `tests/fixtures/brand-template.pptx`, built by `scripts/make_template.py` —
a deck whose only content is a theme. The two other decks here carry the stock Office
palette, and a recording claiming a template's colours came across while showing Calibri on
white proves nothing a viewer can see. `--template` points it at your own.

**Nothing is simulated.** The chat turns go to whichever model `.env` selects, the tool
cards are the ones that model produced, and the preview is the exported file rendered.

One thing *is* edited, and it is the pacing. Five real turns take about six minutes, most of
it a spinner, so the recorder timestamps every stretch it spends blocked on the model and
afterwards compresses **only** those to ~6s each. Nothing is cut, re-ordered or re-shot —
the tool cards, the round counter and the elapsed clock all stay on screen in order — and
the caption during those stretches says it is sped up, because a viewer who discovers the
pacing was doctored will assume the tool calls were too. Both files are kept: the `.webm` is
the untouched real-time take, the `.mp4` is the cut. `--no-tighten` skips it entirely.

## Tests

```bash
uv run pytest                                          # tests/fixtures/demo.pptx
PPT_HARNESS_FIXTURE=path/to/deck.pptx uv run pytest    # or your own deck
uv run python scripts/make_demo_deck.py                # rebuild the fixture
```

1291 pass, 63 skip. The skips are tests needing something the machine may not have — a
browser, a real Office renderer, a fixture with a part the demo deck lacks.

The committed fixture is 7 slides carrying a native chart with its embedded worksheet, a
table, a group, a picture, filled autoshapes, a connector, master art, and a `normAutofit`
confession — the parts most likely to break an exporter.

Some tests drive **real PowerPoint** through AppleScript to produce reference renders. Those
fail loudly on a machine where PowerPoint is absent, unlicensed, or sandboxed; that is a
local environment problem, not a code one.

### Tested on

Every number quoted in this README was produced on one machine. Nothing here has been run on
Windows or Linux, and the reference-render path is macOS-specific — it drives PowerPoint
through AppleScript.

| | |
|---|---|
| OS | macOS 26.5.2 (Darwin 25.5.0), Apple Silicon (arm64) |
| Python | 3.11.15, via `uv` 0.11.19 |
| Reference renderer | **LibreOffice 26.2.5.2** — the numbers below predate it and were drawn against Microsoft PowerPoint 16.111.2 |
| Headless browser | Playwright 1.62.0 + Chromium (test-time oracle only) |
| Model driving the agent | DeepSeek `deepseek-v4-flash` |
| Key libraries | python-pptx 1.0.2 · lxml 6.1.1 · fonttools 4.63.0 · uharfbuzz 0.56.0 · pydantic 2.13.4 |

Three things this environment taught us that are worth repeating:

- **PowerPoint fails under concurrency.** Two processes driving it at once return
  `error -9074`, which surfaces as a wall of failing preview tests that pass individually.
  Do not run the suite while a demo or a second session is rendering.
- **`error -9074` also means "PowerPoint is not in a state to answer".** A long-lived
  instance — hours of AppleScript conversions, or one holding a modal dialog it does not
  expose as a window — keeps answering `is running` while refusing every `open`, for every
  file, from every folder. It reports zero windows and refuses to quit with `-128`, and no
  restart clears it; it needs a person to dismiss whatever is on screen. Selection used to
  test whether PowerPoint was *installed*, never whether it worked, so such a machine failed
  every render with a working LibreOffice sitting beside it. `to_pdf` now falls back, and
  `PPT_HARNESS_RENDERER` pins an engine.
- **LibreOffice is the supported alternative and is now exercised by a run**, not by code
  review: the full suite passes against it in 43s, against 150s for PowerPoint. It
  substitutes fonts and breaks lines differently, so **every fidelity figure above was drawn
  against PowerPoint and is not comparable to one measured here.** `to_pdf` returns the
  engine it actually used so a report can say which.

## Not built yet

The **fidelity margin corpus** — budgets fall back to a fixed margin until
`fidelity/margins.generated.json` is generated from a real deck corpus. **Memory** for what
was said across sessions, as distinct from the preference profile for what was done. See
[DESIGN §12](DESIGN.md).

The round-trip guarantee is **proven for native charts and OLE embeddings** (the fixture
carries both) and **unproven for SmartArt, transitions, and animation timing** — the fixture
has none of those. The existing tests cover them unchanged if you supply a deck that does,
which is why a small fixture carrying an exotic part is worth more here than a large one.
