"""Benchmark tasks — what we ask the harness to build, and how it is judged.

A task is a brief plus the *kind* of deck it wants. Nothing here scores writing quality:
that judgement belongs to a model, and the tasks are deliberately separable from it so the
deterministic half of the suite runs with no key, no network and no judge.

The set is small and hand-written rather than scraped. A benchmark you cannot read is one you
cannot debug, and twenty briefs covering the shapes real decks take is worth more here than a
thousand sampled from the internet — the public benchmarks already do the second thing, and
`bench/adapters` exists to feed them.

Held deliberately: **no task names a colour, a font, a position or a slide count that the
component catalog cannot express.** A benchmark that asks for what the API forbids measures
the ban, not the harness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Kind = Literal["generate", "edit", "review"]


@dataclass(frozen=True)
class Task:
    """One thing to ask for, and what a good answer would contain.

    `expects` is *not* a rubric — it is a list of plain signals a deterministic check can
    look for, like "there is a chart" or "there are at least four slides". Anything needing
    taste is left to the judged adapters.
    """

    id: str
    kind: Kind
    brief: str
    expects: dict[str, Any] = field(default_factory=dict)
    follow_ups: tuple[str, ...] = ()
    """Extra turns, run in order. This is how a task exercises *editing* its own output —
    the thing every public benchmark skips, because none of them keep a session."""
    source: str | None = None
    """A `.pptx` to open first, for `edit` tasks. Relative to the suite file."""
    notes: str = ""

    @property
    def turns(self) -> tuple[str, ...]:
        return (self.brief, *self.follow_ups)


def load(path: Path) -> list[Task]:
    """Every task in one suite file."""
    blob = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Task(**entry) for entry in blob["tasks"]]


def suites_root() -> Path:
    return Path(__file__).resolve().parent / "suites"


def suites() -> dict[str, Path]:
    return {p.stem: p for p in sorted(suites_root().glob("*.json"))}


def load_suite(name: str) -> list[Task]:
    found = suites()
    if name not in found:
        raise KeyError(f"no suite {name!r}; have {sorted(found)}")
    return load(found[name])
