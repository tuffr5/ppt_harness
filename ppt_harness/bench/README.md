# bench

Measuring this harness, on two axes that are not the same kind of thing.

```bash
uv run ppt-harness bench suites                       # what ships
uv run ppt-harness bench run --suite core             # the expensive half: needs a model
uv run ppt-harness bench run --suite core --limit 3 --template slate
uv run ppt-harness bench corpus ~/govdocs1/pptx       # the free half: needs nothing
uv run ppt-harness bench submit --to presentbench     # hand artifacts to a public benchmark
uv run ppt-harness bench slidesbench --repo <clone>   # score against SlidesBench's evaluator
```

Numbers produced so far, with every caveat that makes them readable: **[RESULTS.md](RESULTS.md)**.

## Two halves, and the difference matters

**Measured** — `bench run` and `bench corpus`. Fit rate, refused writes, rounds spent, export
violations, parts lost on a round trip. Deterministic: no judge, no key for the corpus half,
the same answer twice. This is the half that can sit in CI, and it is the half no public
presentation benchmark has.

**Judged** — `bench submit`. Whether a deck is any *good* is a question a model answers, and
the public benchmarks already do it well. Nothing here re-implements or vendors one: the
adapters write our decks into the layout each benchmark's own scripts expect and print the
command. Their judge stays theirs, which is the only way their number means anything.

## What is measured, and what it is not

| Metric | Says | Does not say |
|---|---|---|
| `fit_rate` | every slide's text fits its boxes, measured against real font metrics | the words are worth reading |
| `refusals` / `refusal_rate` | how often a write was stopped before it landed | whether the model was right to try |
| `rounds`, `seconds` | what the turn cost | anything about quality |
| `violations` | the exported file failed its own writer assertions | how it looks |
| `parts_lost` | a round trip dropped media, charts, notes or embeddings | that the slides survived *visually* |
| `review_findings` | the advisory pass had this much to say | that any of it is right |

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
