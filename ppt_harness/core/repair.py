"""The repair ladder — DESIGN §5.2, §5.3, PLAN B2.

When a slide overflows, the harness has a choice about *what to change*, and the order of
that choice is the whole design. Reshaping the container is cheap and reversible. Editing
what someone wrote is neither.

So the ladder climbs structure first and stops at content:

1. **A different variant** of the same component — same words, different arrangement.
2. **A bounded override**, `density: compact` — same words, tighter.
3. **The degradation chain**, `stat_row → card_grid → bullets` — same words, different
   component. Legal only within a slot shape, so nothing is dropped.
4. **Report.** If structure cannot absorb it, the ladder says so and names the overage.

Rung 4 is deliberately not "shorten it". The harness does not write the deck. What it can
do is say how much has to go and who is allowed to decide — which is the arbiter's job.

**Provenance decides who may edit.** An op authored by the *user* means their words are not
the harness's to trim: reshape the container or ask. An op authored by the *model* means
the words are fair game, because the model wrote them and can write them again. The op log
already records `author`, so this is a query rather than new instrumentation.

Two rules the ladder may never break:

- **Never reduce a font size.** The type scale belongs to the theme; shrinking it is exactly
  the silent degradation autofit was disabled to prevent.
- **Never cross `theme.type.floor`.** Nothing below it is legible from the back of a room.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..components import registry
from ..render import budget as budget_mod
from ..render import expand
from ..state.document import Author, Block, Mode, Slide

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

#: How far the ladder will climb before giving up. Beyond this the answer is not a smaller
#: arrangement, it is a second slide.
MAX_RUNGS = 6


@dataclass
class Step:
    """One rung the ladder tried."""

    rung: str
    block_id: str
    detail: str
    worked: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"rung": self.rung, "block": self.block_id, "change": self.detail,
                "worked": self.worked}


@dataclass
class Outcome:
    fixed: bool
    steps: list[Step] = field(default_factory=list)
    remaining: list[dict[str, Any]] = field(default_factory=list)
    advice: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "fixed": self.fixed,
            "tried": [s.as_dict() for s in self.steps],
        }
        if self.remaining:
            out["still_overflowing"] = self.remaining
        if self.advice:
            out["advice"] = self.advice
        return out


# ------------------------------------------------------------------- provenance


def authored_by(session: Session, target: str) -> Author | None:
    """Who last wrote this text, according to the op log.

    `None` means nobody in this session did — it came from the imported file, which makes
    it the original author's and therefore not the harness's to edit.
    """
    for op in reversed(session.store.log.ops):
        if op.target == target and op.op in ("set_text", "set_slots", "set_props"):
            return op.author
    return None


def may_shorten(session: Session, target: str) -> bool:
    """Whether the harness may propose cutting these words.

    Only text the *model* wrote. Anything a person typed, and anything that came out of the
    file, is theirs — the container moves instead.
    """
    return authored_by(session, target) is Author.MODEL


# ------------------------------------------------------------------- the rungs


def _overflowing(session: Session, slide: Slide) -> list[dict[str, Any]]:
    measured = session.measure_slide(slide.id)
    entries = measured.get("slots") or measured.get("shapes") or []
    return [e for e in entries if isinstance(e, dict) and not e.get("fits", True)]


def _block_of(slide: Slide, target: str) -> Block | None:
    parts = target.split("/")
    return slide.block(parts[1]) if len(parts) == 3 else None


def _fits_with(session: Session, slide: Slide, block: Block, **changes: Any) -> bool:
    """Would this block fit if it were changed like that? Measured, never guessed."""
    trial = block.model_copy(deep=True, update=changes)
    probe = slide.model_copy(deep=True)
    probe.blocks = [trial if b.id == block.id else b for b in probe.blocks]

    for laid_out in expand.expand_slide(session.theme, probe):
        if laid_out.block_id != block.id:
            continue
        value = trial.slots.get(laid_out.slot)
        if not value:
            continue
        b = budget_mod.for_slot(session.theme, laid_out)
        if not budget_mod.check_value(value, b, session.theme).ok:
            return False
    return True


def _variants(session: Session, slide: Slide, block: Block) -> tuple[str, str] | None:
    """A different arrangement of the same component. The cheapest rung: same words."""
    comp = registry.COMPONENTS.get(block.component)
    if comp is None:
        return None
    for name in comp.variants:
        if name != block.variant and _fits_with(session, slide, block, variant=name):
            return "variant", name
    return None


def _density(session: Session, slide: Slide, block: Block) -> tuple[str, str] | None:
    """A bounded override. Tighter, but still the theme's spacing — never its type."""
    if block.overrides.get("density") == "compact":
        return None
    tightened = {**block.overrides, "density": "compact"}
    if _fits_with(session, slide, block, overrides=tightened):
        return "override", "density=compact"
    return None


def _degrade(session: Session, slide: Slide, block: Block) -> tuple[str, str] | None:
    """The degradation chain. A different component holding the same content.

    Only within a slot shape, which the registry guarantees — so this rearranges, it never
    drops a column.
    """
    for nxt in registry.degradation_chain(block.component):
        comp = registry.COMPONENTS.get(nxt)
        if comp is None:
            continue
        if not set(block.slots) <= set(comp.slots):
            continue
        variant = comp.default_variant
        if _fits_with(session, slide, block, component=nxt, variant=variant):
            return "degrade", f"{block.component} -> {nxt}"
    return None


RUNGS = (_variants, _density, _degrade)


# -------------------------------------------------------------------- the ladder


def repair(session: Session, slide_id: str, author: Author = Author.LINT) -> Outcome:
    """Walk the ladder until the slide fits, or until only content is left to change.

    Every change is measured before it is written — the ladder never applies a rung and
    hopes. Changes are attributed to `lint`, which is what keeps a harness-chosen variant
    distinguishable in the op log from one the user asked for.
    """
    slide = session.slide(slide_id)
    if slide.mode is not Mode.MANAGED:
        return Outcome(
            fixed=False,
            advice=("this slide came from an imported file, so its geometry is the original "
                    "author's. Use fit_box_to_text, or reshape the box with the constraint "
                    "tools."),
            remaining=_overflowing(session, slide),
        )

    steps: list[Step] = []
    for _ in range(MAX_RUNGS):
        problems = _overflowing(session, slide)
        if not problems:
            return Outcome(fixed=True, steps=steps)

        progressed = False
        for problem in problems:
            block = _block_of(slide, problem.get("target", ""))
            if block is None:
                continue
            for rung in RUNGS:
                found = rung(session, slide, block)
                if found is None:
                    continue
                kind, detail = found
                _apply(session, slide, block, kind, detail, author)
                steps.append(Step(kind, block.id, detail, worked=True))
                progressed = True
                break
            if progressed:
                break

        if not progressed:
            break

    remaining = _overflowing(session, slide)
    if not remaining:
        return Outcome(fixed=True, steps=steps)
    return Outcome(fixed=False, steps=steps, remaining=remaining,
                   advice=_advice(session, slide, remaining))


def _apply(session: Session, slide: Slide, block: Block, kind: str, detail: str,
           author: Author) -> None:
    if kind == "variant":
        props: dict[str, Any] = {"variant": detail}
    elif kind == "override":
        key, _, value = detail.partition("=")
        props = {"overrides": {**block.overrides, key: value}}
    else:
        target = detail.split(" -> ")[-1]
        # A swap carries a variant reset with it: the new component has never heard of the
        # old variant, and half a swap is not a state the deck should be able to reach.
        props = {"component": target, "variant": registry.get(target).default_variant}

    with session.transaction(author) as turn:
        session.store.write(turn, "set_block_props", block.id,
                            {"slide_id": slide.id, "block_id": block.id, **props}, author)


def _advice(session: Session, slide: Slide, remaining: list[dict[str, Any]]) -> str:
    """What is left to do, and who is allowed to do it.

    The harness does not write the deck, so the last rung is a sentence rather than an edit.
    Which sentence depends on provenance: words the model wrote it may offer to cut, words
    a person wrote it may only ask about.
    """
    editable, protected = [], []
    for problem in remaining:
        target = problem.get("target", "")
        (editable if may_shorten(session, target) else protected).append(problem)

    parts = []
    if editable:
        shortest = min(editable, key=lambda p: p.get("overflow_px", 0))
        over = shortest.get("overflow_px", 0)
        parts.append(
            f"structure cannot absorb this; about {over:.0f}px of model-written text has to "
            "go. Shorten it and try again."
        )
    if protected:
        parts.append(
            "the remaining text was written by the user or came from the file, so it is not "
            "the harness's to cut. Split the slide, or ask before shortening."
        )
    if not parts:
        parts.append("split the slide.")
    return " ".join(parts)
