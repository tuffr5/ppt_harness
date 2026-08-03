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
from ..state.document import Shape, Theme, TypeSpec
from . import measure
from .expand import LaidOutSlot

#: Fallback fidelity margin as a fraction of capacity, until `fidelity/margins.generated.json`
#: exists (v1). Derived from the §6.3 causes that a margin rather than an assertion covers:
#: shaping differences, line-break rules, and font substitution.
DEFAULT_MARGIN = 0.083

#: Per-script em-per-character, measured once from the theme's own faces. Only ever used to
#: turn a capacity into a human-readable hint — never to decide whether text fits.
HINT_SCRIPTS = ("latin", "cjk")


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
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def capacity_px(self) -> float:
        return self.capacity_em * self.spec.size

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
            f"  options: " + " · ".join(self.options)
        )

    def _hint_key(self) -> str:
        return "cjk" if self.script in ("han", "kana", "hangul") else "latin"


# --------------------------------------------------------------------- deriving


def _stack_for(theme: Theme, spec: TypeSpec) -> str:
    return theme.type.families.get(spec.family, spec.family)


def for_slot(theme: Theme, laid_out: LaidOutSlot) -> Budget:
    """Budget for a managed slot, from its expanded geometry.

    Capacity is width times line count, less the margin — the total advance width the slot
    can absorb before it overflows its own box.
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
    capacity_em = (width_px / spec.size) * lines * (1 - DEFAULT_MARGIN)
    return Budget(
        target=f"{laid_out.block_id}/{laid_out.slot}",
        capacity_em=capacity_em,
        max_lines=lines,
        width_px=width_px,
        spec=spec,
        stack=stack,
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
        capacity_em=(budget.width_px / small.size) * (1 - budget.margin),
        max_lines=1,
        width_px=budget.width_px,
        spec=small,
        stack=budget.stack,
        margin=budget.margin,
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
