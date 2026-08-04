"""Expansion and budgets — DESIGN §1.4, §3.1, §5.1.

Two invariants:

- Geometry is *derived*. Nothing outside the expander produces a coordinate, and everything
  it produces stays inside the theme's margins.
- A budget is a **function**, not a constant. The same slot must offer less room when the
  region is narrower or the item count higher; a budget that ignores either would pass
  content that then overflows.
"""

from __future__ import annotations

import pytest

from ppt_harness.components import registry
from ppt_harness.core.session import Session
from ppt_harness.render import budget as budget_mod
from ppt_harness.render import expand
from ppt_harness.state.document import Block, Mode, Slide
from ppt_harness.state.theme_default import default_theme

THEME = default_theme()


def _slide(layout: str, blocks: list[Block]) -> Slide:
    return Slide(id="s", index=0, mode=Mode.MANAGED, layout=layout, blocks=blocks)


# ----------------------------------------------------------------------- geometry


def test_every_box_stays_inside_the_margins(managed_slide: Slide) -> None:
    content = expand.content_box(THEME)
    for slot in expand.expand_slide(THEME, managed_slide):
        assert slot.box.x >= content.x - 0.5
        assert slot.box.y >= content.y - 0.5
        assert slot.box.x + slot.box.w <= content.x + content.w + 0.5
        assert slot.box.y + slot.box.h <= content.y + content.h + 0.5


def test_two_col_regions_do_not_overlap() -> None:
    slide = _slide("two_col", [
        Block(id="l", region="left", component="bullets", variant="plain",
              slots={"items": ["a"]}),
        Block(id="r", region="right", component="bullets", variant="plain",
              slots={"items": ["b"]}),
    ])
    left, right = expand.expand_slide(THEME, slide)
    assert left.box.x + left.box.w <= right.box.x + 0.5


def test_a_half_width_region_is_narrower_than_a_full_one() -> None:
    full = expand.region_box(THEME, registry.layout("stack").regions["body"])
    half = expand.region_box(THEME, registry.layout("two_col").regions["left"])
    assert half.w < full.w * 0.55


def test_blocks_sharing_a_region_stack_without_overlapping() -> None:
    slide = _slide("stack", [
        Block(id="a", region="body", component="bullets", variant="plain",
              slots={"items": ["one"]}),
        Block(id="b", region="body", component="bullets", variant="plain",
              slots={"items": ["two"]}),
    ])
    first, second = expand.expand_slide(THEME, slide)
    assert first.box.y + first.box.h <= second.box.y + 0.5


def test_a_block_in_a_region_the_layout_lacks_is_refused() -> None:
    slide = _slide("stack", [Block(id="x", region="hero", component="bullets",
                                   variant="plain", slots={"items": ["a"]})])
    with pytest.raises(ValueError, match="does not have"):
        expand.expand_slide(THEME, slide)


def test_a_comparisons_two_sides_sit_beside_each_other() -> None:
    """The bug this component is named for: stacked sides are not a comparison.

    `left` and `right` sharing a band is the whole content of the claim — same top, no
    horizontal overlap, and neither side wider than half. Asserted as an ordering rather
    than as pixels so the theme's margins stay free to change.
    """
    slide = _slide("stack", [
        Block(id="c", region="body", component="comparison", variant="split",
              slots={"left": ["a", "b"], "right": ["c", "d"]}),
    ])
    left, right = expand.expand_slide(THEME, slide)
    assert (left.slot, right.slot) == ("left", "right")
    assert left.box.x + left.box.w <= right.box.x + 0.5, "sides overlap"
    assert abs(left.box.y - right.box.y) < 0.5, "sides do not share a top"
    assert abs(left.box.h - right.box.h) < 0.5, "sides are not the same height"
    body = expand.region_box(THEME, registry.layout("stack").regions["body"])
    assert left.box.w < body.w * 0.55, "a side is not half the block"


def test_a_side_of_a_comparison_stacks_its_own_items() -> None:
    """`per_row` arranges items within a slot, never the slots themselves.

    With the sides side by side, a side that still flowed its items across two columns would
    put half of one argument beside half of the other.
    """
    slide = _slide("stack", [
        Block(id="c", region="body", component="comparison", variant="split",
              slots={"left": ["a", "b"], "right": ["c", "d"]}),
    ])
    for side in expand.expand_slide(THEME, slide):
        assert side.columns == 1
        first, second = side.cells()
        assert abs(first.x - second.x) < 0.5, "items are not in one column"


def test_a_slot_that_declares_no_width_still_owns_its_row() -> None:
    """Bands are backward compatible: default shares reproduce the stacked layout."""
    slide = _slide("stack", [
        Block(id="b", region="body", component="card_grid", variant="1x3",
              slots={"title": "T", "items": ["a", "b", "c"]}),
    ])
    title, items = expand.expand_slide(THEME, slide)
    assert title.box.y + title.box.h <= items.box.y + 0.5
    assert abs(title.box.w - items.box.w) < 0.5


def test_empty_slots_take_no_space(managed_slide: Slide) -> None:
    managed_slide.blocks[0].slots["title"] = ""
    slots = expand.expand_slide(THEME, managed_slide)
    assert all(s.slot != "title" for s in slots)


def test_emu_conversion_rounds_in_favour_of_fitting() -> None:
    """Width down, height up — the px-to-EMU rounding rule."""
    box = expand.Box(x=10.9, y=10.9, w=100.9, h=100.9)
    _, _, w, h = box.emu(1280, 720, 12192000, 6858000)
    scale = 12192000 / 1280
    assert w <= 100.9 * scale
    assert h >= 100.9 * (6858000 / 720)


# ------------------------------------------------------------------------ budgets


def _budget(slide: Slide, block_id: str, slot: str) -> budget_mod.Budget:
    for laid_out in expand.expand_slide(THEME, slide):
        if laid_out.block_id == block_id and laid_out.slot == slot:
            return budget_mod.for_slot(THEME, laid_out)
    raise AssertionError("slot not laid out")


def test_more_items_means_less_room_each() -> None:
    """The core claim: a budget is a function of item count, not a constant."""
    three = _slide("stack", [Block(id="b", region="body", component="bullets",
                                   variant="plain", slots={"items": ["a", "b", "c"]})])
    six = _slide("stack", [Block(id="b", region="body", component="bullets",
                                 variant="plain",
                                 slots={"items": list("abcdef")})])
    assert _budget(six, "b", "items").capacity_em < _budget(three, "b", "items").capacity_em


def test_a_narrower_region_means_less_room() -> None:
    wide = _slide("stack", [Block(id="b", region="body", component="bullets",
                                  variant="plain", slots={"items": ["a"]})])
    narrow = _slide("two_col", [Block(id="b", region="left", component="bullets",
                                      variant="plain", slots={"items": ["a"]})])
    assert _budget(narrow, "b", "items").capacity_em < _budget(wide, "b", "items").capacity_em


def test_budget_geometry_is_in_canvas_px() -> None:
    """One unit from budget to preview to frozen geometry. The type scale is in canvas px,
    so a budget in points would silently measure every box a quarter too narrow."""
    slide = _slide("stack", [Block(id="b", region="body", component="bullets",
                                   variant="plain", slots={"items": ["a"]})])
    laid_out = next(s for s in expand.expand_slide(THEME, slide) if s.block_id == "b")
    assert _budget(slide, "b", "items").width_px == pytest.approx(laid_out.box.w)


def test_capacity_excludes_the_fidelity_margin() -> None:
    """Capacity is what fits *after* the measured divergence from PowerPoint is set aside."""
    b = _budget(_slide("stack", [Block(id="b", region="body", component="bullets",
                                       variant="plain", slots={"items": ["a"]})]),
                "b", "items")
    raw = (b.width_px / b.spec.size) * b.max_lines
    assert b.capacity_em == pytest.approx(raw * (1 - budget_mod.DEFAULT_MARGIN), rel=1e-6)


def test_hints_are_reported_per_script(managed_slide: Slide) -> None:
    b = _budget(managed_slide, "bk_title", "title")
    hint = b.hint(THEME)
    assert hint["latin"] > hint["cjk"], "the same capacity holds fewer CJK characters"


def test_short_text_fits_and_long_text_does_not(managed_slide: Slide) -> None:
    b = _budget(managed_slide, "bk_title", "title")
    assert budget_mod.check("A short title", b, THEME).ok
    assert not budget_mod.check("word " * 300, b, THEME).ok


def test_a_rejection_carries_numbers_and_ways_out(managed_slide: Slide) -> None:
    b = _budget(managed_slide, "bk_title", "title")
    result = budget_mod.check("word " * 300, b, THEME,
                              budget_mod.ways_out_for_block("slide_title", "plain"))
    message = result.error(THEME)
    assert "budget_exceeded" in message
    assert "capacity" in message and "ew" in message
    assert "options:" in message
    assert "shorten" in message


def test_ways_out_never_offer_a_smaller_font() -> None:
    """The type scale is part of the theme; shrinking it is the silent degradation autofit
    was disabled to prevent."""
    for key in registry.COMPONENTS:
        offers = " ".join(budget_mod.ways_out_for_block(key, "plain")).lower()
        assert "font" not in offers
        assert "smaller" not in offers
        assert "shrink" not in offers


def test_freeform_budget_comes_from_the_shapes_own_box(imported: Session) -> None:
    shape = next(s for slide in imported.deck.slides for s in slide.shapes
                 if s.text and not s.opaque and s.frame.cx > 0)
    cx, cy = imported.slide_size_emu()
    b = budget_mod.for_shape(imported.theme, shape, cx, cy)
    assert b.capacity_em > 0
    assert b.max_lines >= 1


def test_a_list_slot_is_budgeted_per_item_not_per_concatenation() -> None:
    """DESIGN §3.1 budgets `items[].label`. Measuring the joined list against a capacity
    already divided by the item count rejects lists roughly n times too strictly."""
    slide = _slide("stack", [Block(id="b", region="body", component="bullets",
                                   variant="plain",
                                   slots={"items": ["A short bullet"] * 4})])
    b = _budget(slide, "b", "items")
    assert budget_mod.check_value(["A short bullet"] * 4, b, THEME).ok
    joined = "\n".join(["A short bullet"] * 4)
    assert not budget_mod.check(joined, b, THEME).ok, \
        "this is the wrong comparison, and it must stay visibly wrong"


def test_the_worst_item_decides_a_list_budget() -> None:
    slide = _slide("stack", [Block(id="b", region="body", component="bullets",
                                   variant="plain", slots={"items": ["ok", "ok"]})])
    b = _budget(slide, "b", "items")
    assert budget_mod.check_value(["fine", "word " * 200], b, THEME).ok is False


def test_dict_items_are_measured_as_they_render() -> None:
    slide = _slide("stack", [Block(id="b", region="body", component="bullets",
                                   variant="plain", slots={"items": ["x"]})])
    b = _budget(slide, "b", "items")
    result = budget_mod.check_value([{"label": "ARR", "desc": "annual recurring"}], b, THEME)
    assert result.used_em > 0
