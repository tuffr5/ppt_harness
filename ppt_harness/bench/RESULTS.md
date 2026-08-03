# Results

Every number here was produced by a command in this repository against a public benchmark's
own evaluator, and every caveat that makes a number readable is recorded beside it. Nothing
is quoted without its denominator, its baseline, and the machine it ran on.

**n is 5.** These are first runs on one domain, not a claim about the harness in general.

---

## SlidesBench (AutoPresent) · `food` · 5 examples

```bash
uv run ppt-harness bench slidesbench --repo <clone> --domain food --limit 5
uv run ppt-harness bench slidesbench --repo <clone> --domain food --limit 5 \
  --variant instruction.txt
```

Their `page_eval.py`, unmodified, in its own virtualenv. We supply the deck; they score it.

| dimension | high-level | detailed | **their reference vs itself** |
|---|---|---|---|
| `match` | 79.2 | 78.7 | 100.0 |
| `text` | 52.4 | **69.4** | 100.0 |
| `position` | 65.3 | 68.4 | 98.8 |
| `color` | 1.2 | 0.0 | **37.5** |

4 of 5 scored, both runs. Model `deepseek-v4-flash`, macOS 26.5.2 arm64, Python 3.11.15.
No renderer is involved in these figures — SlidesBench's reference-based metrics read the
`.pptx` directly, which is why they were runnable while this machine's PowerPoint was
refusing everything.

Per example:

| example | high-level | detailed |
|---|---|---|
| slide_1 | match 82 · text 95 · pos 70 | match 66 · text **100** · pos 78 |
| slide_2 | *evaluator crashed* | *evaluator crashed* |
| slide_3 | match 56 · text 41 · pos 70 | match 55 · text 54 · pos 64 |
| slide_4 | match 79 · text 36 · pos 66 | match 97 · text 66 · pos 66 |
| slide_5 | match 100 · text 38 · pos 56 | match 97 · text 57 · pos 65 |

### Read `color` against 37.5, not 100

Their reference deck scored **against itself** gives colour 0–37.5 depending on the slide.
Their block parser stores `shape.fill` — a `FillFormat` object — as a block's colour, and
`main()` also averages in a slide-background comparison, so the dimension does not measure
what its name suggests and its identity ceiling is not 100. `bench slidesbench` computes that
baseline per example and prints it beside every figure. Ours at 1.2 against a 37.5 ceiling is
not the same statement as 1.2 out of 100 — and neither figure is worth much.

### The instruction variant decides what is being measured

`instruction_high_level.txt` summarises the reference slide; `instruction.txt` contains its
**exact prose**. Swapping them moves `text` by 17 points and leaves `match` and `position`
flat — because under the high-level variant `text` scores whether the model guessed wording
nobody could guess, while `match` and `position` are unaffected either way. Any comparison
against published numbers has to name the variant, and the detailed one is the fair test of
layout.

### `position` measures a design decision, not quality

65–68 against a 98.8 identity. This harness derives geometry from components and exposes no
tool that accepts a coordinate, so it will not reproduce a reference slide's element
positions. That is the trade the design made deliberately. `match` and `text` are the fair
dimensions here; `position` and `color` are diagnostics.

### Coverage limits

- **One domain.** Only `examples/food/food.pptx` ships. The other nine domains carry
  instructions and no reference deck — their paper distributes slides as URLs with an opt-out
  — so reference-based scoring is limited to `food` until someone re-downloads the rest.
- **slide_2 cannot be scored by their code.** Its reference page parses to zero blocks and
  `block_match_score` divides by zero. Reported as an evaluator failure rather than as a zero,
  because scoring it zero would attribute their crash to our output.

### Bugs found on both sides

In theirs, worked around without touching a metric: `eval_page()` and `main()` disagree —
the function passes a `FillFormat` into a CIEDE2000 routine that subscripts it as RGB and
raises whenever a text block matches an image block, while `main()` routes the same
comparison through `get_shape_fill_similarity` and works, so we drive `main()`; the CLI
crashes serialising its own float32 scores unless `--output_path` is passed; the two entry
points also disagree about whether page numbers are 0- or 1-based.

In ours, found by running this and fixed: **`eject_slide` made a generated deck permanently
unexportable** — a shipped tool leaving the deck unrecoverable through the only door out of
the harness — and the benchmark's own integrity check conflated "a file appeared" with "the
export report was clean".

---

## Round trip (no model, no judge)

```bash
uv run ppt-harness bench corpus <dir-of-pptx> --edit
```

| corpus | decks | opened | preserved | slides | opaque shapes |
|---|---|---|---|---|---|
| repo fixtures | 3 | 3 (100%) | **3/3 (100%)** | 15 | 4 |

No renderer here either: a round trip is import → export → diff the package.

Three decks is not a corpus. This is the mechanism working, not a result:
[GOVDOCS1](https://digitalcorpora.org/corpora/file-corpora/files/) has several thousand real
`.pptx` and is what this should be run against before the preservation claim is quoted.

## Task suite (measured, ours)

```bash
uv run ppt-harness bench run --suite core
```

| tasks | met brief | fit rate | refused writes |
|---|---|---|---|
| 3 run so far | 3/3 | **1.000** (8/8 slides) | 1 of 22 calls |

Also not a full run — the suite has twelve tasks and each costs a model turn.
