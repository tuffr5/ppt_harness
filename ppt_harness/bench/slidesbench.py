"""SlidesBench — the first public benchmark wired up end to end.

Their task: one natural-language instruction, one slide. Their scoring: parse both decks into
blocks, match them, and score `match`, `text`, `color` and `position` against a reference
slide. All computed — no judge model, which is why this is the one to run first.

**Their code does the scoring.** This module locates the examples, drives our harness, and
calls their `eval_page` in their own environment. Reimplementing a metric to score yourself
against is not a benchmark, it is a mirror.

Three things running it actually taught us, none of them in the paper:

1. **The colour metric scores 25 on identical input.** Comparing the reference deck's page to
   *itself* gives `match 100 · text 100 · position 100 · color 25`. Their block parser stores
   `shape.fill` — a `FillFormat` object — as the "colour" of a text block, so the comparison
   is not measuring what its name says. Any colour figure is therefore read against a 25
   baseline, and `baseline()` below computes it per example rather than assuming it.
2. **Their CLI crashes writing its own results** (`float32 is not JSON serializable`), so we
   call the function rather than the script.
3. **Only one reference deck ships** — `examples/food/food.pptx`. The paper says slides are
   distributed as URLs with an opt-out, so the other nine domains have instructions and no
   ground truth. Reference-based scoring is therefore possible on `food` alone until someone
   re-downloads the rest.

And the caveat that matters most, restated where it will be read: `position` and `color` score
*similarity to a reference slide's layout*. This harness derives geometry from components and
exposes no tool that accepts a coordinate, so those two dimensions largely measure the design
decision. `match` and `text` are the fair ones.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Their instruction variants, cheapest first. `high_level` is the one closest to how a person
#: asks for a slide, which is what this harness is built around; `instruction.txt` spells out
#: every element and reads like a layout spec.
VARIANTS = ("instruction_high_level.txt", "instruction_no_image.txt", "instruction.txt")

_SLIDE_DIR = re.compile(r"slide_(\d+)$")


@dataclass
class Example:
    id: str
    domain: str
    page: int
    instruction: str
    reference: Path

    @property
    def key(self) -> str:
        return f"{self.domain}/{self.id}"


@dataclass
class Score:
    example: str
    ok: bool = False
    error: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    baseline: dict[str, float] = field(default_factory=dict)
    blocks_generated: int = 0
    blocks_reference: int = 0
    generated: str = ""


def examples(repo: Path, domain: str = "food",
             variant: str = VARIANTS[0]) -> list[Example]:
    """Every example in a domain that has both an instruction and a reference page."""
    root = Path(repo) / "slidesbench" / "examples" / domain
    reference = root / f"{domain}.pptx"
    if not reference.is_file():
        return []

    found: list[Example] = []
    for folder in sorted(root.iterdir()):
        match = _SLIDE_DIR.match(folder.name) if folder.is_dir() else None
        text = folder / variant if match else None
        if match and text and text.is_file():
            found.append(Example(
                id=folder.name, domain=domain, page=int(match.group(1)),
                instruction=text.read_text(encoding="utf-8").strip(),
                reference=reference,
            ))
    return sorted(found, key=lambda e: e.page)


#: Their own CLI, run unmodified. Two of their entry points disagree and only this one
#: works: `eval_page()` passes a block's `FillFormat` straight into a CIEDE2000 function that
#: subscripts it as RGB, so it raises `'FillFormat' object is not subscriptable` the moment a
#: text block matches an image block. `main()` routes the same comparison through
#: `get_shape_fill_similarity`, which handles the type. We drive `main()`.
#:
#: `--output_path` is passed for a second reason: without it they build a default path and
#: `json.dump` their own float32 scores, which raises. With it, that branch is skipped.
_SCORE_LINE = re.compile(r"^(\w+)\s*:\s*([\d.]+)$")


def score_page(evaluate_dir: Path, python: Path, generated: Path, generated_page: int,
               reference: Path, reference_page: int) -> dict[str, float]:
    """Call their evaluator. Page numbers are **1-based**, as their CLI takes them.

    Their `eval_page` function is 0-based and their CLI is 1-based, which is a trap worth
    naming: the two entry points into the same evaluation disagree about the convention.
    """
    with tempfile.TemporaryDirectory() as scratch:
        done = subprocess.run(
            # `absolute`, never `resolve`, for the interpreter: a virtualenv's `bin/python`
            # is a symlink to the base interpreter, and resolving it hands back that base —
            # same binary, none of the venv's packages. Their evaluator then fails with
            # `No module named 'pptx'` while the venv it should have run in had it all along.
            [str(Path(python).absolute()), "page_eval.py",
             "--generated_pptx", str(Path(generated).resolve()),
             "--generated_page", str(generated_page),
             "--reference_pptx", str(Path(reference).resolve()),
             "--reference_page", str(reference_page),
             "--output_path", str(Path(scratch) / "scores.json")],
            # Their imports are relative to this directory (`from metrics import *`).
            cwd=str(evaluate_dir), capture_output=True, text=True, timeout=900)

    scores: dict[str, float] = {}
    for line in done.stdout.splitlines():
        found = _SCORE_LINE.match(line.strip())
        if found:
            scores[found.group(1)] = float(found.group(2)) / 100.0
    if not scores:
        raise RuntimeError(
            (done.stderr or done.stdout or "the evaluator printed nothing").strip()[-400:])
    return scores


def baseline(evaluate_dir: Path, python: Path, example: Example) -> dict[str, float]:
    """The reference page scored against itself.

    Not a formality. `color` comes back 25 here, so a generated deck scoring 25 has matched
    the ceiling rather than failed. A benchmark number without its identity baseline is not
    interpretable, and theirs is not 100.
    """
    return score_page(evaluate_dir, python, example.reference, example.page,
                      example.reference, example.page)


BRIEF = (
    "{instruction}\n\n"
    "Build exactly one slide for this. Use the components available; do not add a cover or "
    "any slide that was not asked for."
)


def run(repo: Path, out_dir: Path, *, domain: str = "food", limit: int | None = None,
        variant: str = VARIANTS[0], model: str | None = None, base_url: str | None = None,
        python: Path | None = None, client: Any = None) -> dict[str, Any]:
    """Generate a slide per example, score it with their code, and report both.

    Every failure is recorded rather than raised: an example our harness refuses, an
    evaluator that will not run, a missing reference page. A benchmark run that stops at the
    first problem never produces a number at all.
    """
    from .runner import run_task
    from .tasks import Task

    repo = Path(repo)
    evaluate_dir = repo / "evaluate"
    interpreter = Path(python) if python else Path(repo).parent / "sb-venv" / "bin" / "python"
    found = examples(repo, domain, variant)
    if limit:
        found = found[:limit]

    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[Score] = []
    for index, example in enumerate(found, 1):
        score = Score(example=example.key)
        task = Task(id=f"{domain}-{example.id}", kind="generate",
                    brief=BRIEF.format(instruction=example.instruction),
                    expects={"min_slides": 1, "clean": True})
        outcome = run_task(task, out_dir / example.id, model=model, base_url=base_url,
                           client=client)
        pptx = outcome.artifacts.get("pptx")
        score.generated = pptx or ""
        if not pptx:
            score.error = outcome.error or "the harness produced no deck"
            results.append(score)
            continue
        try:
            score.scores = score_page(evaluate_dir, interpreter, Path(pptx), 1,
                                      example.reference, example.page)
            score.baseline = baseline(evaluate_dir, interpreter, example)
            score.ok = True
        except Exception as exc:
            score.error = f"{type(exc).__name__}: {exc}"
        results.append(score)
        print(f"  [{index}/{len(found)}] {example.key}: "
              + (", ".join(f"{k} {v * 100:.0f}" for k, v in score.scores.items())
                 if score.ok else score.error[:70]))

    payload = _summarise(results, domain, variant, repo)
    (out_dir / "slidesbench.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return payload


def _summarise(results: list[Score], domain: str, variant: str, repo: Path) -> dict[str, Any]:
    scored = [r for r in results if r.ok]
    keys = sorted({k for r in scored for k in r.scores})

    def mean(rows: list[dict[str, float]], key: str) -> float | None:
        values = [row[key] for row in rows if key in row]
        return round(sum(values) / len(values) * 100, 1) if values else None

    return {
        "benchmark": "SlidesBench (AutoPresent)",
        "repo": str(repo),
        "domain": domain,
        "variant": variant,
        "examples": len(results),
        "scored": len(scored),
        "ours": {key: mean([r.scores for r in scored], key) for key in keys},
        # Their reference scored against itself. `color` is not 100, so ours is not a
        # failure at 25 — it is the ceiling. Reported beside every figure, always.
        "identity_baseline": {key: mean([r.baseline for r in scored], key) for key in keys},
        "note": (
            "match and text are the fair dimensions. position and color score similarity to "
            "a reference slide's layout, which a component-based harness does not reproduce "
            "by design — no tool it exposes accepts a coordinate. Read every figure against "
            "identity_baseline: their own reference scores 25 on color against itself."
        ),
        "results": [vars(r) for r in results],
    }
