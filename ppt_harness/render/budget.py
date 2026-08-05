"""Slot budgets — DESIGN §3.1, gate 1 of §5.1.

A budget is the most content a slot can hold at the theme's type size inside its region,
less the fidelity margin. It is a **function**, not a constant: the same slot gives each
item less room at five items than at three, and less again in a half-width region.

Everything here is in **canvas px**, the same unit as the theme's type scale and the same
unit the HTML preview lays out in. One coordinate system from budget to preview to frozen
geometry is what lets the browser's numbers be compared to ours at all.

Two numbers, two audiences. Capacity is **enforced** in advance width against real font
metrics, because that is what actually decides whether text fits. It is **communicated** as
a per-script character hint, because a model cannot reason in ems. The hint guides; the
measurement decides.

The point of this gate is that it fires before anything renders. A rejected write is the
cheapest possible failure, so its error has to carry enough to act on — the numbers, and
the ways out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..components import registry
from ..state import richtext, slots
from ..state.document import Mode, Shape, Slide, Theme, TypeSpec
from . import expand, measure
from .expand import LaidOutSlot

#: Fallback fidelity margin as a fraction of capacity, until `fidelity/margins.generated.json`
#: exists (v1). Derived from the §6.3 causes that a margin rather than an assertion covers:
#: shaping differences, line-break rules, and font substitution.
DEFAULT_MARGIN = 0.083

#: Per-script em-per-character, measured once from the theme's own faces. Only ever used to
#: turn a capacity into a human-readable hint — never to decide whether text fits.
HINT_SCRIPTS = ("latin", "cjk")


@dataclass(frozen=True)
class RoleCapacity:
    """How much of a slot's geometric capacity a type role is allowed to spend, and why.

    The `why` is not decoration. It is the sentence the refusal carries: a writer told
    "~51 chars" for a box that geometrically holds sixty will otherwise read the number as
    a measurement bug, and the next thing they do is stop trusting the gate.
    """

    fraction: float
    why: str


#: Role → the share of its box a slot may fill — DESIGN §2 (the type scale is roles) and
#: §3.1 (a budget is a function of where the text sits, not of the box alone).
#:
#: **This is a second discount, and it is not the fidelity margin.** `DEFAULT_MARGIN` prices
#: *measurement uncertainty*: the §6.3 causes — shaping, line-break rules, font substitution
#: — under which our ruler and PowerPoint's disagree about text neither of them thinks is
#: over. This table prices *composition*: text that both rulers agree fits, and that still
#: lands wrong. Different causes, so they compose (`geometric × (1 - margin) × fraction`)
#: rather than one subsuming the other — but a reader comparing capacities against the box
#: should expect both to be in play.
#:
#: The fractions grade with type size, because the fault they guard against does. A face set
#: large fills its box in few enough glyphs that the last one lands hard against the edge,
#: and at 52px that gap is the first thing on the slide the eye measures; at 20px body it is
#: a line ending, which is what line endings look like. `label` and `stat` break the grading
#: for a different reason, stated per entry: their capacity is nominally multi-line and their
#: content is not, so the fraction is what stops a budget from *offering* a second line to
#: content that is broken by taking it.
#:
#: A role absent here is undiscounted. A role added to a theme's scale tomorrow gets the
#: neutral value rather than an accidental tightening nobody chose.
#: Roles whose crowding is a *composition* fault, reported by `review` and never refused —
#: DESIGN §5 (three gates) against §5.5 (deck-level checks).
#:
#: These began in `ROLE_CAPACITY` below and were moved out deliberately. `budget_exceeded`
#: is the harness's one unarguable sentence: the text does not fit, measured, and here is by
#: how much. A title at 88% of its box *does* fit — it reads as cramped, which is a
#: judgement, and a judgement wearing the same error code as a measurement teaches a model
#: that the measurement is negotiable. So the fractions survive, the gate does not apply
#: them, and `review` says the same thing where being wrong is allowed.
#:
#: The two that stayed are not the same claim. A `stat` or a `label` that spends its whole
#: box takes a second line, and neither is legible as two — that is still a fit fact.
COMPOSITION_CAPACITY: dict[str, RoleCapacity] = {
    # The largest face in the deck, usually alone on a cover with the most room to give
    # back. Its crowding is also the most conspicuous in the deck — there is nothing else on
    # the slide to look at instead.
    "deck_title": RoleCapacity(0.80, "a cover title set edge to edge reads as cramped"),
    # Large, and read first on every slide that has one. Same fault as `deck_title`, one
    # step down the scale, so one step less discount.
    "slide_title": RoleCapacity(0.85, "a title set edge to edge reads as cramped"),
    # One step above `body`, and it names a block rather than the slide — the eye reads it
    # as part of the thing it labels, not as the composition. The lightest hand of the three
    # titles, because it is the one closest to being prose.
    "block_title": RoleCapacity(0.90, "a heading reads as part of its block, but not "
                                      "edge to edge"),
}


ROLE_CAPACITY: dict[str, RoleCapacity] = {
    # A figure is one line by construction and disastrous as two: "$4.2" over "M" is not a
    # number any more. Discounted with `label` rather than with the display faces it shares
    # a size with, because what is being bought is the second line, not the margin.
    #
    # This is also the only place in the catalog where a discount meets a decoration `pad`:
    # `stat_row/carded` spends 10.9% of its cell on the card before this fraction applies,
    # so a carded figure ends at 0.62 of the row's geometry — the tightest total anywhere
    # here. Not double-charging, because the two are not the same claim: the pad is *box*
    # (the words are drawn inside the card, and the measurer has to know that or it and the
    # renderer disagree), the fraction is *policy*. Worth knowing it is the value most
    # likely to be the one that is wrong; the catalog's own worst case still lands at 0.41.
    "stat": RoleCapacity(0.70, "a figure that wraps stops reading as a number"),
    # A label is a noun phrase in a cell; `max_lines` is 2 only so a long one degrades
    # rather than clips, and budgeting it at the full two lines is how the gate ends up
    # certifying the wrapped version as fitting. Read as a line count rather than as a
    # margin, 0.70 of two lines is "you may run onto a second line, but not fill it", which
    # is the difference between a deliberate two-line label and a sentence in a cell.
    #
    # The tightest fixture in the catalog after this change (`icon_row/icon_top` at 0.67 of
    # budget, from 0.47), so it is the first value to relax if the refusal rate moves.
    "label": RoleCapacity(0.70, "a label that wraps reads as broken"),
    # `body` and `caption` are deliberately absent — prose is *meant* to fill its measure,
    # and a line that reaches the end of it is what a measure is for. Discounting them would
    # refuse sentences that read correctly, and `body` is where the catalog's own worst-case
    # fixtures already run closest to the line (0.61 of capacity, against 0.29 for a title),
    # so it is also where a discount would do the most damage. `caption` is the same
    # argument at a smaller size: it is prose, it wraps, and wrapping is not a fault in it.
    # Keeping `body` at 1.0 additionally means this table cannot hide a global tightening —
    # every entry here is a statement about a role, not about the ruler.
}


def role_capacity(role: str) -> RoleCapacity | None:
    """The discount a role carries, or `None` where it carries none."""
    return ROLE_CAPACITY.get(role)


@dataclass(frozen=True)
class Crowding:
    """A composition role filling more of its box than it reads well at."""

    slide_id: str
    block_id: str
    slot: str
    role: str
    fill: float
    """Share of the *geometric* box the text spends — 1.0 is edge to edge."""
    allowed: float
    why: str


def crowded(theme: Theme, slide: Slide) -> list[Crowding]:
    """Composition roles on one slide that fit and still read as cramped.

    The same ruler as the gate, deliberately: this measures with `measure.measure` against
    the same shaped advance width, so `review` cannot disagree with `lint` about a number
    they both derived. What differs is only the verdict — over a `COMPOSITION_CAPACITY`
    fraction is a finding, and a finding never refused anything.

    Silent on anything the gate would already have stopped. A slot whose text does not fit
    is a `lint` problem with its own message, and saying "also, it looks cramped" about text
    that overflows is noise on top of a fact.
    """
    if slide.mode is not Mode.MANAGED:
        return []
    out: list[Crowding] = []
    for laid_out in expand.expand_slide(theme, slide):
        rule = COMPOSITION_CAPACITY.get(laid_out.role)
        if rule is None:
            continue
        text = _as_text(slide.block(laid_out.block_id).slots.get(laid_out.slot))
        if not text:
            continue
        budget = for_slot(theme, laid_out)
        used = measure.measure(text, budget.stack, budget.spec.size, budget.spec.track)
        geometric = budget.geometric_em
        if not geometric or used.width_em > geometric:
            continue
        fill = used.width_em / geometric
        if fill > rule.fraction:
            out.append(Crowding(slide_id=slide.id, block_id=laid_out.block_id,
                                slot=laid_out.slot, role=laid_out.role, fill=fill,
                                allowed=rule.fraction, why=rule.why))
    return out


@dataclass(frozen=True)
class Budget:
    """What a slot can hold, and how to say so."""

    target: str
    capacity_em: float
    max_lines: int
    width_px: float
    spec: TypeSpec
    stack: str
    margin: float = DEFAULT_MARGIN
    role: str = ""
    """The theme type-scale entry this slot is set in — carried, not re-derived, because the
    role is what selects the discount and the refusal has to be able to name it."""
    discount: float = 1.0
    """The `ROLE_CAPACITY` fraction already applied to `capacity_em`. 1.0 where the role
    carries none, so nothing downstream has to branch on whether a discount exists."""
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def capacity_px(self) -> float:
        return self.capacity_em * self.spec.size

    @property
    def geometric_em(self) -> float:
        """Capacity before the role discount — what the box holds, not what the role may use.

        Only ever reported. Nothing compares text against this number; if it did, the
        discount would be advice rather than a budget.
        """
        return self.capacity_em / self.discount if self.discount else self.capacity_em

    def hint(self, theme: Theme) -> dict[str, int]:
        """Approximate character counts per script. Guidance only."""
        out = {}
        for label, sample in (("latin", "the quick brown fox jumps over a lazy dog"),
                              ("cjk", "敏捷的棕色狐狸跳过懒狗继续向前奔跑")):
            per_char = measure.measure(sample, self.stack, self.spec.size).width_em / len(sample)
            out[label] = int(self.capacity_em / per_char) if per_char else 0
        return out


@dataclass(frozen=True)
class BudgetResult:
    ok: bool
    budget: Budget
    used_em: float
    lines: int
    script: str
    options: list[str] = field(default_factory=list)

    @property
    def over_em(self) -> float:
        return max(0.0, self.used_em - self.budget.capacity_em)

    def error(self, theme: Theme) -> str:
        """The rejection message from DESIGN §3.1 — numbers, then ways out."""
        hint = self.budget.hint(theme)
        used_chars = int(self.used_em / self.budget.capacity_em * hint[self._hint_key()]) \
            if self.budget.capacity_em else 0
        ctx = self.budget.context
        where = "/".join(str(ctx[k]) for k in ("component", "variant", "region") if k in ctx)
        head = f'budget_exceeded: slot "{ctx.get("slot", self.budget.target)}"'
        if where:
            head += f" in {where}"
        if ctx.get("n_items"):
            head += f" ({ctx['n_items']} items)"
        return (
            f"{head}\n"
            f"  capacity {self.budget.capacity_em:.1f}ew "
            f"(~{hint[self._hint_key()]} {self._hint_key()} chars) · "
            f"got {self.used_em:.1f}ew (~{used_chars})\n"
            f"{self._why_discounted()}"
            f"  options: " + " · ".join(self.options)
        )

    def _why_discounted(self) -> str:
        """The role discount, said out loud — or nothing where there is none.

        A capacity that is 70% of the box the writer can see is a number they cannot check,
        and an unexplained one reads as the measurer being wrong rather than as a rule they
        could have planned around. It sits between the numbers and the ways out because that
        is what it is: the last of the numbers, and the reason the first of them is smaller
        than the box.
        """
        rule = ROLE_CAPACITY.get(self.budget.role)
        if self.budget.discount >= 1.0 or rule is None:
            # Asked of the budget, not of the table: a freeform shape carries a role it was
            # imported with and no discount, and quoting a rule that was never applied to
            # its capacity would be the same lie in the other direction.
            return ""
        return (f"  a {self.budget.role} is budgeted at {self.budget.discount:.0%} of its "
                f"box ({self.budget.geometric_em:.1f}ew): {rule.why}\n")

    def _hint_key(self) -> str:
        return "cjk" if self.script in ("han", "kana", "hangul") else "latin"


# --------------------------------------------------------------------- deriving


def _stack_for(theme: Theme, spec: TypeSpec) -> str:
    return theme.type.families.get(spec.family, spec.family)


def for_slot(theme: Theme, laid_out: LaidOutSlot) -> Budget:
    """Budget for a managed slot, from its expanded geometry.

    Capacity is width times line count, less the margin and less the role's share — the
    total advance width the slot can absorb before it overflows its own box, and then the
    part of that a slot in this role is allowed to spend (`ROLE_CAPACITY`).
    """
    spec = laid_out.spec
    stack = _stack_for(theme, spec)
    # The cell an item actually gets, not the slot divided by a count. Those were the same
    # number only for a single-column list; for `stat_row` and `card_grid` the old form
    # charged each item the *whole* width divided by the item count while the renderer gave
    # it the whole width outright — three stats measured a third as wide as they drew, and
    # the write was refused for overflow that could not happen.
    cell = laid_out.cells()[0]
    # Height, not the declared max, decides how many lines actually fit.
    lines_by_height = max(1, int(cell.h / spec.line))
    if laid_out.component == "stat_row":
        # A stat's second line is its label, set at a fraction of the figure — so a cell
        # needs one full line plus a small one, not two full ones. Charging it two would
        # refuse a row of statistics that renders with room to spare.
        lines_by_height = 2 if cell.h >= spec.line * (1 + slots.STAT_LABEL_EM) else 1
    lines = min(laid_out.max_lines, lines_by_height)
    width_px = cell.w
    # The discount lands on the total advance width and *not* on `width_px`, which is what
    # keeps it a composition rule rather than a second geometry. Narrowing the wrap width
    # would make the measurer break lines where the renderer will not, and the two agreeing
    # about where the text breaks is the whole basis for comparing them at all. Charged this
    # way, a discounted slot may still use every line it has — it just may not end the last
    # of them against the edge.
    discount = ROLE_CAPACITY.get(laid_out.role)
    fraction = discount.fraction if discount else 1.0
    capacity_em = (width_px / spec.size) * lines * (1 - DEFAULT_MARGIN) * fraction
    return Budget(
        target=f"{laid_out.block_id}/{laid_out.slot}",
        capacity_em=capacity_em,
        max_lines=lines,
        width_px=width_px,
        spec=spec,
        stack=stack,
        role=laid_out.role,
        discount=fraction,
        context={"slot": laid_out.slot, "n_items": laid_out.items,
                 "columns": laid_out.columns},
    )


def for_shape(theme: Theme, shape: Shape, slide_cx: int, slide_cy: int) -> Budget:
    """Budget for a freeform shape, from its own box.

    There is no component to consult on an imported slide, so the shape's frame *is* the
    budget, and the shape's **own** type decides what fits in it. Budgeting imported text
    against the theme's scale would report overflow on every caption set smaller than the
    theme's body size — a slide the file does not contain. The theme role is only a fallback
    for shapes whose size nothing in the file states.

    No role discount here, for the same reason. `ROLE_CAPACITY` is a rule about text this
    harness is about to compose; an imported shape is text somebody already composed, in a
    box they chose, and the role on it is our guess at what it was for. Discounting it would
    report `budget_exceeded` on slides that are already in the file and render as their
    author left them — the harness inventing an overflow rather than measuring one.
    """
    spec = shape.type_spec or theme.type.scale[
        shape.role if shape.role in theme.type.scale else "body"
    ]
    stack = _stack_for(theme, spec)
    canvas_w, canvas_h = theme.grid.canvas
    width_px = shape.frame.cx / slide_cx * canvas_w if slide_cx else 0
    height_px = shape.frame.cy / slide_cy * canvas_h if slide_cy else 0
    lines = max(1, int(height_px / spec.line)) if spec.line else 1
    capacity_em = (width_px / spec.size) * lines * (1 - DEFAULT_MARGIN) if spec.size else 0
    return Budget(
        target=shape.id,
        capacity_em=capacity_em,
        max_lines=lines,
        width_px=width_px,
        spec=spec,
        stack=stack,
        role=shape.role or "",
        context={"slot": shape.role or "text", "region": "freeform"},
    )


# ---------------------------------------------------------------------- checking


def check(
    text: str, budget: Budget, theme: Theme, ways_out: list[str] | None = None
) -> BudgetResult:
    """Measure `text` against `budget`. This is the whole of gate 1."""
    m = measure.measure(text, budget.stack, budget.spec.size, budget.spec.track)
    lines = measure.wrap(text, budget.stack, budget.spec.size, budget.width_px,
                         budget.spec.track)
    fits = m.width_em <= budget.capacity_em and len(lines) <= budget.max_lines

    options = list(ways_out or [])
    if not fits:
        over_em = m.width_em - budget.capacity_em
        per_char = m.width_em / max(1, len(text))
        if over_em > 0 and per_char:
            options.insert(0, f"shorten ~{max(1, int(over_em / per_char))} chars")
        else:
            options.insert(0, f"reduce to {budget.max_lines} lines (now {len(lines)})")

    return BudgetResult(
        ok=fits,
        budget=budget,
        used_em=m.width_em,
        lines=len(lines),
        script=m.dominant_script,
        options=options,
    )


def _check_stat(item: Any, budget: Budget, theme: Theme,
                ways_out: list[str] | None = None) -> BudgetResult:
    """A figure and its label, each measured at the size it is actually set in.

    Measuring `"8.3%\\nEMEA mid-market churn"` as one blob at the stat size charges the label
    nearly three times its real advance width, which refuses labels that render inside their
    cell with room around them. The two lines are different type, so they are two
    measurements; the worse one decides, and it is reported against the budget the caller
    was given so the error still names a slot rather than half of one.
    """
    figure = check(_as_text(item.get("value")), budget, theme, ways_out)

    if budget.max_lines < 2:
        # The cell has room for one line, and a stat is two. Checking the figure and the
        # label as separate one-line measurements would pass a pair that cannot be drawn
        # together — the same disagreement between the measurer and the renderer that this
        # whole path exists to close.
        return BudgetResult(
            ok=False, budget=budget, used_em=figure.used_em, lines=2, script=figure.script,
            options=["a variant with fewer rows", "a taller region",
                     *(ways_out or [])],
        )

    small = budget.spec.model_copy(update={
        "size": budget.spec.size * slots.STAT_LABEL_EM,
        "line": budget.spec.line * slots.STAT_LABEL_EM,
    })
    label_budget = Budget(
        target=budget.target,
        # Rebuilt from the cell's width rather than divided out of the caller's capacity, so
        # the role's discount has to be re-applied by hand — it is charged against the box,
        # and this is a different type size in the same box. Inheriting the figure's
        # discount rather than looking up `label`'s is deliberate: the caption under a
        # figure is part of a `stat` cell, and a cell budgeted by two different rules is a
        # cell whose two halves disagree about how much of it they may have.
        capacity_em=(budget.width_px / small.size) * (1 - budget.margin) * budget.discount,
        max_lines=1,
        width_px=budget.width_px,
        spec=small,
        stack=budget.stack,
        margin=budget.margin,
        role=budget.role,
        discount=budget.discount,
        context={**budget.context, "part": "label"},
    )
    label = check(_as_text(item.get("label")), label_budget, theme, ways_out)
    if figure.ok and label.ok:
        return figure
    # Report the one that failed, but against the caller's budget: a `stat` slot is the
    # addressable thing, and an error quoting a capacity in label-ems would send a model
    # looking for a target that does not exist.
    failed = figure if not figure.ok else label
    return BudgetResult(ok=False, budget=budget, used_em=failed.used_em,
                        lines=figure.lines + label.lines, script=failed.script,
                        options=failed.options)


def check_value(value: Any, budget: Budget, theme: Theme,
                ways_out: list[str] | None = None) -> BudgetResult:
    """Measure a slot's *value* against its budget.

    A `list` slot is budgeted **per item** — DESIGN §3.1 budgets `items[].label`, not the
    concatenation — so each item is checked against the per-item capacity and the worst one
    decides. Measuring the joined text against a capacity that was already divided by the
    item count rejects lists roughly n times too strictly, which reads as "the component is
    too small" when nothing is wrong.
    """
    if not isinstance(value, list):
        return check(_as_text(value), budget, theme, ways_out)

    worst: BudgetResult | None = None
    for item in value:
        result = (_check_stat(item, budget, theme, ways_out) if slots.is_stat(item)
                  else check(_as_text(item), budget, theme, ways_out))
        if worst is None or result.used_em > worst.used_em:
            worst = result
    if worst is None:
        return check("", budget, theme, ways_out)
    return worst


def _as_text(value: Any) -> str:
    """What the slot will render as, then stripped of markup.

    The rendering is `state.slots`, shared with the preview and the exporter — a private
    copy here is how a twenty-four-row table came to be measured as the empty string: it
    knew about `{label, value}` items and nothing about `tabular`.

    Markup is stripped because `**Q3**` is two characters on the slide and six in the slot,
    and budgeting the asterisks rejects text that fits.
    """
    return richtext.plain(slots.slot_text(value))


def ways_out_for_block(component: str, variant: str) -> list[str]:
    """The repair ladder, phrased as offers — DESIGN §5.2.

    Order matters: keep the user's words if at all possible, and never offer a smaller font.
    """
    out: list[str] = []
    try:
        comp = registry.get(component)
    except KeyError:
        return out
    for name in comp.variants:
        if name != variant:
            out.append(f'variant "{name}"')
    if comp.degrades_to:
        out.append(f'component "{comp.degrades_to}"')
    out.append("split the slide")
    return out
