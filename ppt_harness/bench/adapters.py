"""Submitting our artifacts to somebody else's benchmark.

Nothing here re-implements a public benchmark or vendors its code. Each function takes decks
this suite already produced and writes them into the exact layout that benchmark's own
scripts expect, then prints the command to run. Their judge stays theirs — which is the only
way a number from it means anything.

The three that fit, and what each wants:

- **PresentBench** wants `slides.pdf` per case under a fixed directory tree, and judges it
  against a per-case checklist with Gemini or OpenAI.
- **PPTEval** (PPTAgent) wants a `.pptx`, or a `.pptx` plus a folder of slide images, and
  needs a language model *and* a vision model.
- **SlidesBench** (AutoPresent) wants a `.pptx` plus a page index, or a `.jpg`. Its
  reference-based half is computed; its reference-free half is judged.

**One of these is a poor fit and it is worth saying so in the code that feeds it.**
SlidesBench's reference-based metric scores element positions and colours against a target
slide. This harness derives geometry from components on purpose and will not reproduce
another deck's coordinates, so a low score there measures the design decision rather than the
output. Feed it as a diagnostic; do not quote it as a headline. The reference-free half has
no such problem.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .runner import Result, render_pdf


@dataclass
class Submission:
    benchmark: str
    root: Path
    cases: int
    missing: list[str]
    command: str

    def as_dict(self) -> dict[str, object]:
        return {"benchmark": self.benchmark, "root": str(self.root), "cases": self.cases,
                "missing": self.missing, "command": self.command}


def _decks(results: list[Result]) -> list[tuple[Result, Path]]:
    out = []
    for result in results:
        pptx = result.artifacts.get("pptx")
        if pptx and Path(pptx).exists():
            out.append((result, Path(pptx)))
    return out


def presentbench(results: list[Result], root: Path, *, agent_name: str = "ppt-harness",
                 domain: str = "harness") -> Submission:
    """The tree `judge_all.py` reads: `<root>/<domain>/<case>/generation_task/results/`.

    PDF rather than pptx, so this is the one adapter that needs a real renderer — the same
    one the preview uses. On a machine without Office or LibreOffice it reports the cases it
    could not convert instead of writing an empty tree that scores zero for the wrong reason.
    """
    missing: list[str] = []
    cases = 0
    for result, pptx in _decks(results):
        case = root / domain / result.task / "generation_task" / "results"
        case.mkdir(parents=True, exist_ok=True)
        if render_pdf(pptx, case / "slides.pdf") is None:
            missing.append(result.task)
            # Leave nothing behind. A case directory with no `slides.pdf` is one their judge
            # would score as an empty submission, turning "we could not render it" into "the
            # harness produced nothing" — a zero for the wrong reason.
            shutil.rmtree(case.parent.parent, ignore_errors=True)
            continue
        cases += 1
    return Submission(
        benchmark="PresentBench", root=root, cases=cases, missing=missing,
        command=f"python judge_all.py --agent_name {agent_name}   "
                f"# with RESULT_ROOT={root}",
    )


def ppteval(results: list[Result], root: Path) -> Submission:
    """A flat folder of `.pptx`, which `eval_ppt` / `eval_parsed_ppts` take directly.

    PPTEval renders its own images, so nothing is converted here — and it needs a vision
    model as well as a language model, which is worth knowing before budgeting a run.
    """
    root.mkdir(parents=True, exist_ok=True)
    cases = 0
    for result, pptx in _decks(results):
        shutil.copy2(pptx, root / f"{result.task}.pptx")
        cases += 1
    (root / "manifest.json").write_text(
        json.dumps({"decks": sorted(p.name for p in root.glob("*.pptx"))}, indent=1),
        encoding="utf-8")
    return Submission(
        benchmark="PPTEval", root=root, cases=cases, missing=[],
        command="python -c \"from pptagent.ppteval.ppteval import eval_parsed_ppts; "
                f"eval_parsed_ppts(sorted(__import__('glob').glob('{root}/*.pptx')), [])\"",
    )


def slidesbench(results: list[Result], root: Path) -> Submission:
    """`.pptx` per task, plus a note about which half of their eval to trust.

    Written next to the decks rather than only in this docstring, because the person who runs
    `page_eval.py` in six months will be reading the folder, not this file.
    """
    root.mkdir(parents=True, exist_ok=True)
    cases = 0
    for result, pptx in _decks(results):
        shutil.copy2(pptx, root / f"{result.task}.pptx")
        cases += 1
    (root / "READ-THIS-FIRST.md").write_text(
        "# SlidesBench submission\n\n"
        "`reference_free_eval.py` is a fair read of these decks: it judges design quality "
        "on its own terms.\n\n"
        "`page_eval.py` is **not**. It scores element positions and colours against a "
        "reference slide, and this harness derives geometry from components rather than "
        "reproducing another deck's coordinates — no tool it exposes even accepts one. A low "
        "score there is a restatement of that design decision, not a measurement of the "
        "output. Use it as a diagnostic; do not quote it.\n",
        encoding="utf-8")
    return Submission(
        benchmark="SlidesBench", root=root, cases=cases, missing=[],
        command=f"python reference_free_eval.py --image_path <render of {root}/<task>.pptx>",
    )


ADAPTERS = {"presentbench": presentbench, "ppteval": ppteval, "slidesbench": slidesbench}
