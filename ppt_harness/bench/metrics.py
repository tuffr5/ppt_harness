"""What we can score without asking a model.

Every public presentation benchmark scores the same thing: how a rendered slide *looks* to a
judge model. That is worth measuring and `bench/adapters` feeds those benchmarks — but it
leaves the two claims this harness actually makes untested, because nothing out there checks
whether text fits or whether an edited file survived being edited.

So this half is deterministic. No key, no network, no judge, same answer twice. It is also
the half that can run in CI, which is the difference between a benchmark and a press release.

Three families:

- **Fit** — does the text fit the boxes, measured the way the harness measures during a turn.
  A deck that overflows is broken whatever a judge thinks of it.
- **Friction** — how hard the model had to fight the API to get there: refused writes, repair
  ladders walked, rounds spent, tools called. A design that makes bad slides
  unrepresentable should show up as a *low* number here, and if it does not, the schemas are
  costing more than they prevent.
- **Integrity** — does the exported file assert clean, and on an imported deck, did the parts
  we never touched survive.

None of these say the deck is any good. They say it is sound, which is the necessary half
nobody else measures.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core.session import Session
from ..state.document import Mode
from ..tools import router


@dataclass
class Fit:
    slides: int = 0
    clean_slides: int = 0
    overflow_px: float = 0.0
    worst_slide: str | None = None

    @property
    def fit_rate(self) -> float:
        """The headline. 1.0 means every slide in the deck fits its boxes."""
        return self.clean_slides / self.slides if self.slides else 1.0


@dataclass
class Friction:
    """What the run cost in arguments with the API.

    `refusals` is the interesting one. A refusal is the design working — a write that would
    have overflowed, stopped before it landed — but a *high* refusal rate means the model
    cannot predict what fits, and the fix is a component schema, not a better prompt.
    """

    rounds: int = 0
    tool_calls: int = 0
    refusals: int = 0
    refusal_codes: dict[str, int] = field(default_factory=dict)
    repairs: int = 0
    seconds: float = 0.0

    @property
    def refusal_rate(self) -> float:
        return self.refusals / self.tool_calls if self.tool_calls else 0.0


@dataclass
class Integrity:
    exported: bool = False
    violations: list[str] = field(default_factory=list)
    parts_before: int = 0
    parts_after: int = 0
    parts_lost: list[str] = field(default_factory=list)
    inherited_problems: int = 0

    @property
    def sound(self) -> bool:
        return self.exported and not self.violations and not self.parts_lost


@dataclass
class Shape:
    """What the harness actually built, so a score can be read against the deck it describes."""

    components: dict[str, int] = field(default_factory=dict)
    variants: dict[str, int] = field(default_factory=dict)
    layouts: dict[str, int] = field(default_factory=dict)
    words: int = 0
    notes_slides: int = 0
    review_findings: int = 0
    review_rules: dict[str, int] = field(default_factory=dict)


def measure_fit(session: Session) -> Fit:
    """The same measurement a turn does, over the whole deck."""
    out = Fit(slides=len(session.deck.slides))
    worst = 0.0
    for slide in session.deck.slides:
        measurement = session.measure_slide(slide.id)
        over = float(measurement.get("overflow_px") or 0.0)
        out.overflow_px += over
        if measurement.get("clean"):
            out.clean_slides += 1
        if over > worst:
            worst, out.worst_slide = over, slide.id
    out.overflow_px = round(out.overflow_px, 1)
    return out


def measure_shape(session: Session) -> Shape:
    """What is on the slides, and what the editorial pass makes of it."""
    from ..state import slots as slot_text

    out = Shape()
    for slide in session.deck.slides:
        if slide.layout:
            out.layouts[slide.layout] = out.layouts.get(slide.layout, 0) + 1
        if (slide.notes or "").strip():
            out.notes_slides += 1
        if slide.mode is Mode.MANAGED:
            for block in slide.blocks:
                out.components[block.component] = out.components.get(block.component, 0) + 1
                key = f"{block.component}:{block.variant}"
                out.variants[key] = out.variants.get(key, 0) + 1
                for value in block.slots.values():
                    out.words += len(slot_text.slot_text(value).split())
        else:
            for shape in slide.shapes:
                if shape.text:
                    out.words += len(shape.text.split())

    # The advisory pass, counted rather than read. A rising findings-per-slide across runs is
    # the deck getting worse in a way `lint` cannot see.
    review = router.dispatch(session, "review_deck", {"include_raised": True})
    findings = review.get("findings", []) if review.get("ok", True) else []
    out.review_findings = len(findings)
    for finding in findings:
        rule = finding.get("rule", "?")
        out.review_rules[rule] = out.review_rules.get(rule, 0) + 1
    return out


def measure_friction(events: Iterable[Any], seconds: float) -> Friction:
    """Read the turn's own event stream. Nothing extra is instrumented for this."""
    out = Friction(seconds=round(seconds, 1))
    for event in events:
        kind = getattr(event, "kind", None)
        if kind == "round":
            out.rounds = max(out.rounds, getattr(event, "round", 0))
        elif kind == "tool_call":
            out.tool_calls += 1
            if getattr(event, "tool", "") == "repair":
                out.repairs += 1
        elif kind == "tool_result":
            result = getattr(event, "result", {}) or {}
            if not result.get("ok", True):
                out.refusals += 1
                code = str(result.get("error", "unknown"))
                out.refusal_codes[code] = out.refusal_codes.get(code, 0) + 1
    return out


def measure_integrity(session: Session, out_path: Path,
                      original: Path | None = None) -> Integrity:
    """Export it, then ask the package itself whether anything was lost.

    `original` turns this into the round-trip claim: parts present before an edit that are
    absent after it. Baselining against the source is what keeps a deck's *inherited* debris
    from being reported as damage this harness did.
    """
    import zipfile

    from ..io import package

    out = Integrity()
    report = router.dispatch(session, "export", {"path": str(out_path)})
    # Did a file appear, not "was the report clean". They are different questions and
    # conflating them cost a benchmark run: a deck whose only fault was an unfillable media
    # slot reported `exported: false`, so the runner discarded a perfectly scoreable file.
    # `sound` is where cleanliness belongs.
    out.exported = out_path.exists()
    out.violations = list(report.get("violations") or [])
    if not out.exported:
        return out

    with zipfile.ZipFile(out_path) as after:
        names_after = set(after.namelist())
    out.parts_after = len(names_after)

    if original is not None and Path(original).exists():
        with zipfile.ZipFile(original) as before:
            names_before = set(before.namelist())
        out.parts_before = len(names_before)
        # Media, embeddings and the parts nothing here models. Slide XML is expected to
        # differ; a *missing* picture or worksheet is the failure this claim is about.
        keep = ("ppt/media/", "ppt/embeddings/", "ppt/notesSlides/", "ppt/charts/",
                "customXml/", "docProps/")
        out.parts_lost = sorted(
            name for name in names_before - names_after if name.startswith(keep))

    audit = package.audit(out_path, original=original)
    out.violations += [f"{f.kind}: {f.part}" for f in audit.findings]
    out.inherited_problems = len(audit.inherited)
    return out


def as_dict(*parts: Any) -> dict[str, Any]:
    """Flatten measurement dataclasses into one JSON-able record, derived fields included."""
    out: dict[str, Any] = {}
    for part in parts:
        out[type(part).__name__.lower()] = asdict(part)
    fit = next((p for p in parts if isinstance(p, Fit)), None)
    friction = next((p for p in parts if isinstance(p, Friction)), None)
    integrity = next((p for p in parts if isinstance(p, Integrity)), None)
    if fit is not None:
        out["fit"]["fit_rate"] = round(fit.fit_rate, 4)
    if friction is not None:
        out["friction"]["refusal_rate"] = round(friction.refusal_rate, 4)
    if integrity is not None:
        out["integrity"]["sound"] = integrity.sound
    return out
