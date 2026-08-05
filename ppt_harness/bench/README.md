# bench

Measuring this harness, on two axes that are not the same kind of thing.

```bash
uv run ppt-harness bench suites                       # what ships
uv run ppt-harness bench run --suite core             # the expensive half: needs a model
uv run ppt-harness bench run --suite core --limit 3 --template slate
uv run ppt-harness bench corpus ~/govdocs1/pptx       # the free half: needs nothing
uv run ppt-harness bench score deck.pptx              # PPTEval-style: needs a vision model
uv run ppt-harness bench score deck.pptx --verify-only  # the model-free checks alone
uv run ppt-harness bench submit --to presentbench     # hand artifacts to a public benchmark
uv run ppt-harness bench slidesbench --repo <clone>   # score against SlidesBench's evaluator
```

Numbers produced so far, with every caveat that makes them readable: **[RESULTS.md](RESULTS.md)**.

## Two halves, and the difference matters

**Measured** — `bench run` and `bench corpus`. Fit rate, refused writes, rounds spent, export
violations, parts lost on a round trip. Deterministic: no judge, no key for the corpus half,
the same answer twice. This is the half that can sit in CI, and it is the half no public
presentation benchmark has.

**Judged** — `bench score` here, `bench submit` elsewhere. Whether a deck is any *good* is a
question a model answers. `bench submit` hands our decks to the public benchmarks in the
layout their own scripts expect and prints the command — nothing here re-implements or vendors
one, because their judge staying theirs is the only way their number means anything.
`bench score` is the one exception and it is a method, not a benchmark: PPTEval's
describe-then-score procedure, run against our own decks so a visual change can be measured
before and after instead of argued about.

## `bench score` — PPTEval, run locally

Two model calls per slide per axis, and the split is the method:

1. a **vision** model writes a neutral description of the rendered slide along fixed axes;
2. a **separate text-only** call scores that description 1-5 against a rubric, and never sees
   the image.

That decoupling is what the PPTAgent paper (EMNLP 2025) validated against human raters —
**design r=0.90**, content r=0.70 — and one call asked to look and judge does not reproduce
it. The slide scored is the real file: it is rendered through `render/preview`, which exports
through the ordinary exporter and rasterises what a real renderer made of it.

Descriptions are cached on disk under `.harness/ppteval`, keyed on the image bytes, the prompt
and the model — the three things a description is an answer to. Re-scoring an unchanged deck
costs text tokens only; an edited slide invalidates itself, because different pixels are a
different question.

```bash
export PPT_HARNESS_VISION_MODEL=claude-opus-4-8   # or gpt-4o, or a local VLM via
                                                  # PPT_HARNESS_VISION_BASE_URL
export PPT_HARNESS_SCORE_MODEL=deepseek-v4-flash  # optional: the scorer reads prose, so a
                                                  # cheap text model can do half the job
uv run ppt-harness bench score deck.pptx --expect-slides 5-12 --expect-script latin
```

**Without a vision model nothing is scored.** The command prints the deterministic checks,
names the variable that is missing, and exits non-zero. It never substitutes a default, a zero
or a midpoint — an unmeasured deck and an ugly deck must not read alike, and a metric that
cannot tell them apart is worse than no metric.

| Axis | 1-5 means | Human r |
|---|---|---|
| `content` | 3 = clear and complete but no visual aids · 5 = images and text complement each other | 0.70 |
| `design` | 2 = monochrome · 3 = basic colour but no icons, backgrounds, images or shapes · 5 = harmonious and engaging | 0.90 |
| `coherence` | **not implemented** — see `rubrics.WHY_NO_COHERENCE` | 0.55 |

Coherence is left out deliberately. It is the paper's weakest axis against human raters, and
its top two levels require a speaker, a date, an institution and acknowledgements — furniture
that would make a board update *worse*. Rewriting the levels would ship an unvalidated rubric
wearing a validated one's reputation. What is reported instead is the deck-level half of
`core/review.py` — `weak_close`, `duplicate_title`, drift — which decides narrative mechanics
by counting and cannot be wrong about itself.

### The free half of `bench score`

`--verify-only` needs no model, no key and no renderer. It reads the exported `.pptx` and
checks page count against a range, aspect ratio within 0.1, and the dominant writing system.
Twenty lines, deterministic, and it catches the embarrassments an LLM judge scores 4/5 without
noticing: a deck that came out 4:3, or one slide long, or in the wrong script. Reported
separately from the judged axes, and never averaged into them.

Every check is tri-state. `--expect-slides` omitted means the page count was *not asserted*,
which is printed as such rather than as a pass.

## What is measured, and what it is not

| Metric | Says | Does not say |
|---|---|---|
| `fit_rate` | every slide's text fits its boxes, measured against real font metrics | the words are worth reading |
| `refusals` / `refusal_rate` | how often a write was stopped before it landed | whether the model was right to try |
| `rounds`, `seconds` | what the turn cost | anything about quality |
| `violations` | the exported file failed its own writer assertions | how it looks |
| `parts_lost` | a round trip dropped media, charts, notes or embeddings | that the slides survived *visually* |
| `review_findings` | the advisory pass had this much to say | that any of it is right |
| `design` / `content` | what a model says about a described slide, on the paper's rubric | that a human would agree — r=0.90 and 0.70 are correlations, not equalities |
| `verification` | the file has the page count, shape and script it was meant to | anything about what is on the slides |

A high refusal rate is the interesting one. A refusal is the design working — but if the
model *cannot predict* what fits, the fix is a component schema, not a better prompt, and
this is the number that would say so.

## The corpus half

`bench corpus` opens every `.pptx` under a directory, exports it straight back out, and diffs
the package. Nothing was asked for, so any difference is damage. `--edit` sets one title
first — the smallest real edit — which exercises the writer without changing what the rest of
the file should contain.

This is DESIGN §12's "fidelity margin corpus", which was never built for want of a corpus.
[GOVDOCS1](https://digitalcorpora.org/corpora/file-corpora/files/) is ~1M documents crawled
from `.gov`, several thousand of them `.pptx`, freely redistributable. Point `--corpus` at a
directory of them and the result is a rate with a denominator — a sentence this repository
currently cannot say about itself.

## Public benchmarks

| Adapter | Benchmark | Wants | Judge |
|---|---|---|---|
| `presentbench` | [PresentBench](https://github.com/PresentBench/PresentBench) | `slides.pdf` per case in a fixed tree | per-case checklists, Gemini or OpenAI |

`presentbench` is the only adapter that needs a renderer, and it takes whichever one
`PPT_HARNESS_RENDERER` names. Pin it: a judged score carries the engine's line breaking with
it, so a run against LibreOffice and a run against PowerPoint are not the same experiment.
| `ppteval` | [PPTAgent](https://github.com/icip-cas/PPTAgent) | a `.pptx` | a language model **and** a vision model |

`submit --to ppteval` and `bench score` are not the same thing and should not be quoted as
though they were. The adapter hands decks to *their* evaluator, which is the number worth
publishing. `bench score` re-implements their *method* — the describe-then-score split and the
content and design rubrics — on our own rendering path, so a visual change can be measured
between two runs without a public benchmark in the loop. Their prompts, their model choices
and their coherence axis are not reproduced.

| `slidesbench` | [AutoPresent](https://github.com/para-lost/AutoPresent) | a `.pptx`, or a `.jpg` | reference-free judged; reference-based computed |

`slidesbench` is **wired end to end** — `bench slidesbench` generates a slide per instruction,
runs their `page_eval.py` unmodified in its own virtualenv, and reports every dimension beside
the score their reference gets against *itself*. Set it up with:

```bash
git clone --depth 1 https://github.com/para-lost/AutoPresent .harness/bench/external/AutoPresent
uv venv .harness/bench/external/sb-venv
VIRTUAL_ENV=.harness/bench/external/sb-venv uv pip install \
    python-pptx numpy colormath scikit-learn sentence-transformers pillow
```

Their evaluator has three bugs we route around without touching a metric — the two entry
points disagree about page numbering *and* about whether a block's colour is an RGB tuple or
a `FillFormat`, and the CLI cannot serialise its own scores. All three are documented in
`slidesbench.py` where the workarounds live.

**SlidesBench's reference-based half is a poor fit and the adapter says so in the folder it
writes.** It scores element positions and colours against a target slide; this harness derives
geometry from components and exposes no tool that accepts a coordinate. A low score there
restates that design decision rather than measuring the output. Its reference-free half has
no such problem.

Expect to score mid-pack on design-judged benchmarks by construction: the model cannot choose
a colour, a position or a font, and it has sixteen components where competitors emit arbitrary
code. That is the trade the design made. Publish those numbers *and* the two axes they do not
have, rather than picking whichever flatters.

## Tasks

`suites/core.json` — twelve briefs, hand-written and readable, across the shapes decks take:
board updates, a postmortem, a strategy argument, a table, a single number, sections and
dividers, prose that must be cut down. Several carry `follow_ups`, which run as further turns
against the same session — that is how a task exercises *editing what it just built*, the
thing every public benchmark skips because none of them keeps a session.

No task names a colour, a font, a position or a size. A benchmark that asks for what the API
forbids measures the ban, not the harness — and there is a test that fails if one ever does.
