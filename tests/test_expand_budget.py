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
from ppt_harness.state.document import Block, Frame, Mode, Shape, Slide
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
    """Capacity is what fits *after* the measured divergence from PowerPoint is set aside.

    Asserted on a `body` slot, which carries no role discount, so this pins the margin on
    its own. The composed form — margin *and* discount, in that order — is pinned by
    `test_the_role_discount_composes_with_the_fidelity_margin`.
    """
    b = _budget(_slide("stack", [Block(id="b", region="body", component="bullets",
                                       variant="plain", slots={"items": ["a"]})]),
                "b", "items")
    assert b.discount == 1.0, "this slot was chosen for carrying no discount"
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
                              budget_mod.ways_out_for_block("icon_row", "icon_top"))
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


# ------------------------------------------------------------------ role discounts


def _same_box(role: str, lines: int = 2) -> budget_mod.Budget:
    """A budget for one fixed box, varying only the role the text is set in.

    Roles differ in size and leading, so two roles in the same *component* are never in the
    same box. Building the geometry by hand is what isolates the discount from everything
    else a budget is a function of — and `capacity_px` is the comparable number, because
    `capacity_em` is denominated in a size the role also chooses.
    """
    spec = THEME.type.scale[role]
    box = expand.Box(x=100, y=100, w=600, h=spec.line * lines + 1)
    return budget_mod.for_slot(THEME, expand.LaidOutSlot(
        block_id="b", slot="s", box=box, spec=spec, role=role, align="left",
        max_lines=lines))


def test_the_gate_does_not_discount_a_title() -> None:
    """A title that fits is not refused for looking cramped.

    `budget_exceeded` says one thing — the text does not fit, measured. A title at 88% of
    its box *does* fit, so the gate passes it and `review` raises `cramped_title` instead.
    The fractions still exist; they moved to `COMPOSITION_CAPACITY`, which the gate never
    reads.
    """
    body, title = _same_box("body"), _same_box("slide_title")
    assert body.width_px == title.width_px and body.max_lines == title.max_lines
    assert title.capacity_px == pytest.approx(body.capacity_px, rel=1e-6)
    assert title.discount == 1.0


def test_only_wrap_faults_are_discounted_in_the_gate() -> None:
    """What stayed is a fit claim, not a taste one.

    A `stat` or a `label` that spends its whole box takes a second line, and neither is
    legible as two — so refusing it is still "this does not fit". The three title roles are
    the ones whose fault was only ever composition.
    """
    body = _same_box("body")
    for role in ("stat", "label"):
        assert _same_box(role).capacity_px < body.capacity_px
    for role in ("deck_title", "slide_title", "block_title"):
        assert _same_box(role).capacity_px == pytest.approx(body.capacity_px, rel=1e-6)


def test_the_composition_fractions_grade_with_the_size_of_the_face() -> None:
    """Ordering, not pixels: the larger the face, the harder its crowding is to miss, so the
    less of the box it may spend before `review` says so. Asserted on the advisory table now
    that the gate no longer applies it."""
    table = budget_mod.COMPOSITION_CAPACITY
    assert (table["deck_title"].fraction < table["slide_title"].fraction
            < table["block_title"].fraction < 1.0)
    assert not set(table) & set(budget_mod.ROLE_CAPACITY), "a role cannot be in both"


def test_prose_roles_are_left_alone() -> None:
    """`body` and `caption` are prose, and prose is meant to fill its measure. Discounting
    them would refuse sentences that read correctly — and with `body` at 1.0 this table
    cannot smuggle in a global tightening under the name of a role."""
    for role in ("body", "caption"):
        b = _same_box(role)
        assert b.discount == 1.0, role
        assert b.capacity_em == pytest.approx(b.geometric_em, rel=1e-9)


def test_the_role_discount_composes_with_the_fidelity_margin() -> None:
    """Two discounts, two causes, both applied. The margin prices the divergence between our
    ruler and PowerPoint's (§6.3); the role prices text that both rulers agree fits and that
    still lands wrong. One replacing the other would quietly drop a guarantee."""
    b = _same_box("label")
    raw = (b.width_px / b.spec.size) * b.max_lines
    assert b.capacity_em == pytest.approx(
        raw * (1 - budget_mod.DEFAULT_MARGIN) * b.discount, rel=1e-6)
    assert b.capacity_em < raw * (1 - budget_mod.DEFAULT_MARGIN)


def test_a_refusal_says_the_capacity_was_discounted_and_why() -> None:
    """A writer told "~115 chars" for a box that visibly holds more will read the number as
    the measurer being wrong. The refusal has to name the role, the fraction, and the reason
    — otherwise the gate is teaching distrust of itself."""
    b = _same_box("label")
    assert b.discount < 1.0, "this test needs a role the gate still discounts"

    # Grown a word at a time so the text lands just past the discounted capacity — the
    # window this test is about is the one between the discount and the box.
    words = ["the", "quick", "brown", "fox", "jumps", "over", "a", "lazy", "dog"]
    grown: list[str] = []
    while budget_mod.check(" ".join(grown), b, THEME).ok and len(grown) < 400:
        grown.append(words[len(grown) % len(words)])
    result = budget_mod.check(" ".join(grown), b, THEME,
                              budget_mod.ways_out_for_block("icon_row", "icon_top"))

    assert not result.ok
    assert result.used_em < b.geometric_em, \
        "this text fits the box; it is the role discount that refuses it"
    message = result.error(THEME)
    assert b.role in message
    assert f"{b.discount:.0%}" in message
    assert budget_mod.ROLE_CAPACITY[b.role].why in message
    assert "options:" in message, "the reason never replaces the ways out"


def test_an_undiscounted_refusal_claims_no_discount() -> None:
    """The line appears because a discount was applied, not because the role exists. A
    `body` refusal that explained a discount would be an invented cause."""
    slide = _slide("stack", [Block(id="b", region="body", component="bullets",
                                   variant="plain", slots={"items": ["a"]})])
    b = _budget(slide, "b", "items")
    message = budget_mod.check("word " * 300, b, THEME).error(THEME)
    assert "% of its box" not in message


def test_every_discounted_role_is_one_the_catalog_sets_text_in() -> None:
    """A typo here is silent: the role stops matching, the discount stops applying, and
    nothing fails. Pinned against the catalog rather than a list, so a role that leaves the
    scale takes its entry with it."""
    in_use = {spec.role for comp in registry.COMPONENTS.values()
              for spec in comp.slots.values()}
    for role, rule in budget_mod.ROLE_CAPACITY.items():
        assert role in in_use, f"{role} is discounted but no component uses it"
        assert role in THEME.type.scale, f"{role} is not a step of the type scale"
        assert 0 < rule.fraction <= 1
        assert rule.why.strip(), f"{role} discounts without saying why"


def test_a_role_the_table_does_not_name_is_left_undiscounted() -> None:
    """The neutral default. A role added to a theme tomorrow gets the ruler it had, not an
    accidental tightening nobody chose."""
    assert budget_mod.role_capacity("footnote") is None
    spec = THEME.type.scale["body"]
    b = budget_mod.for_slot(THEME, expand.LaidOutSlot(
        block_id="b", slot="s", box=expand.Box(x=0, y=0, w=600, h=spec.line * 2 + 1),
        spec=spec, role="footnote", align="left", max_lines=2))
    assert b.discount == 1.0


def test_an_imported_shape_is_never_role_discounted() -> None:
    """A freeform shape is text somebody already composed, in a box they chose. Holding it
    to 85% would report `budget_exceeded` on a slide that is already in the file and renders
    as its author left it — an overflow the harness invented rather than measured."""
    shape = Shape(id="sp1", ooxml_id=2, type="textbox", role="slide_title",
                  frame=Frame(x=0, y=0, cx=6096000, cy=800000), text="A heading")
    b = budget_mod.for_shape(THEME, shape, 12192000, 6858000)
    assert b.discount == 1.0
    raw = (b.width_px / b.spec.size) * b.max_lines
    assert b.capacity_em == pytest.approx(raw * (1 - budget_mod.DEFAULT_MARGIN), rel=1e-6)
    over = budget_mod.check("word " * 300, b, THEME)
    assert "% of its box" not in over.error(THEME)


# --------------------------------------------------------------------- decoration


def _slot_of(slide: Slide, slot: str) -> expand.LaidOutSlot:
    return next(s for s in expand.expand_slide(THEME, slide) if s.slot == slot)


def _comparison(variant: str) -> Slide:
    return _slide("stack", [Block(id="c", region="body", component="comparison",
                                  variant=variant, slots={"left": ["one"],
                                                          "right": ["two"]})])


def test_a_decorated_side_is_written_inside_the_panel_drawn_behind_it() -> None:
    """`split` and `table` held the same slots at the same size in the same boxes, so no
    content a caller could pass would tell them apart. The difference has to be drawn.

    The panel is the box the undecorated variant writes into, and the text moves in by the
    pad. Asserted as containment rather than as pixels, so the theme's spacing scale stays
    free to change.
    """
    plain = _slot_of(_comparison("split"), "left")
    carded = _slot_of(_comparison("table"), "left")
    panel = carded.panels()[0]

    assert carded.pad > 0, "a card with no padding is text with a box drawn on it"
    assert (panel.x, panel.y, panel.w, panel.h) == pytest.approx(
        (plain.box.x, plain.box.y, plain.box.w, plain.box.h)), \
        "the card does not occupy the room the plain variant gives the words"
    assert carded.box.x > panel.x and carded.box.y > panel.y
    assert carded.box.x + carded.box.w < panel.x + panel.w


def test_a_decorations_padding_is_charged_to_the_budget() -> None:
    """The inset has to be visible to the measurer, or the two disagree about one slide.

    Padding applied at write time only would leave the budget promising room the writer then
    spends on the card, and the arbiter of that disagreement is a rejected write — against a
    slot whose content fits.
    """
    split, table = _comparison("split"), _comparison("table")
    assert _budget(table, "c", "left").capacity_em < _budget(split, "c", "left").capacity_em
    assert _budget(table, "c", "left").width_px == pytest.approx(
        _slot_of(table, "left").cells()[0].w)


def _stat_row(variant: str) -> Slide:
    return _slide("stack", [Block(id="s", region="body", component="stat_row",
                                  variant=variant, slots={"items": [
                                      {"value": "8.3%", "label": "Churn"},
                                      {"value": "+41%", "label": "Expansion"}]})])


def test_a_carded_row_pads_each_cell_and_not_the_row() -> None:
    """A row of cards is one card per figure. Padding the slot would inset the row once and
    leave the cells still touching each other, which is a margin, not a card."""
    flat = _slot_of(_stat_row("flat"), "items")
    carded = _slot_of(_stat_row("carded"), "items")
    assert carded.box == flat.box, "the row itself moved"

    panels = carded.panels(2)
    assert len(panels) == 2, "a card behind each figure, or none at all"
    for panel, plain, cell in zip(panels, flat.cells(2), carded.cells(2), strict=True):
        assert (panel.x, panel.w) == pytest.approx((plain.x, plain.w))
        assert cell.x > panel.x and cell.w < panel.w


def test_the_picture_swaps_sides_without_leaving_its_row() -> None:
    """`image_left` and `image_right` are a slot *order*, and order is what `_bands` reads.

    Both variants said `per_row=2`, which arranges the items inside a list slot — this
    component has none — so the pair rendered identically twice over.
    """
    def sides(variant: str) -> dict[str, expand.Box]:
        slide = _slide("stack", [Block(id="i", region="body", component="image_split",
                                       variant=variant,
                                       slots={"media": {"asset_id": "a"},
                                              "prose": "What the picture shows"})])
        return {s.slot: s.box for s in expand.expand_slide(THEME, slide)}

    left, right = sides("image_left"), sides("image_right")
    assert left["media"].x < left["prose"].x, "the picture is not on the left"
    assert right["media"].x > right["prose"].x, "the picture is not on the right"
    assert abs(left["media"].y - left["prose"].y) < 0.5, "the two are not on one row"


def test_an_inset_picture_is_held_off_the_edges_a_bleed_reaches() -> None:
    """A margin, which is what the two words mean — and a margin is geometry, so it is the
    expander's and it is visible before anything is written.

    This pins the box the writer is handed; that the *file* then shows two different pictures
    is `test_bleed_and_inset_reach_the_file_as_different_pictures` in `test_export.py`. Both
    halves are needed: the expander drew this distinction correctly for as long as `inset`
    has existed, and the writer placed no picture at all, so the pair was identical to the
    recipient while the geometry test passed.
    """
    def media(variant: str) -> expand.Box:
        slide = _slide("stack", [Block(id="m", region="body", component="image_full",
                                       variant=variant, slots={"media": {"asset_id": "a"}})])
        return _slot_of(slide, "media").box

    bleed, inset = media("bleed"), media("inset")
    assert inset.x > bleed.x and inset.y > bleed.y
    assert inset.w < bleed.w and inset.h < bleed.h


@pytest.mark.parametrize("aspect", [0.25, 1.0, 1.6, 4.0])
def test_a_fitted_picture_keeps_its_shape_and_stays_inside_its_box(aspect: float) -> None:
    """Letterboxing, stated as the two properties that make it safe.

    The harness picks the box but has never seen the image, so the picture keeps its own
    proportions and the box keeps its edges — see `io/media.py` for why cropping to fill
    would be the writer deciding which part of a photograph is the point. Inside on every
    edge is the load-bearing half: a picture that overflowed would cross the theme's margins
    and there is no budget to catch it, because a picture has no text to measure.
    """
    box = expand.Box(x=100, y=50, w=400, h=300)
    fitted = box.fit(aspect)

    assert fitted.w / fitted.h == pytest.approx(aspect)
    assert fitted.x >= box.x and fitted.y >= box.y
    assert fitted.x + fitted.w <= box.x + box.w + 1e-9
    assert fitted.y + fitted.h <= box.y + box.h + 1e-9
    # Centred, so the leftover shows as equal bands rather than as a picture pushed aside.
    assert fitted.x + fitted.w / 2 == pytest.approx(box.x + box.w / 2)
    assert fitted.y + fitted.h / 2 == pytest.approx(box.y + box.h / 2)


def test_media_scale_shrinks_a_box_about_its_own_centre() -> None:
    """`media_scale` was a clamped override with no reader anywhere in the harness.

    Concentric, because "fills 60% of its box" describes a smaller picture in the same place
    rather than one anchored into a corner — and clamped above at 1.0, since a factor over
    one is a picture leaving the box the layout gave it.
    """
    box = expand.Box(x=100, y=50, w=400, h=300)
    small = box.scaled(0.6)

    assert (small.w, small.h) == pytest.approx((240.0, 180.0))
    assert small.x + small.w / 2 == pytest.approx(box.x + box.w / 2)
    assert small.y + small.h / 2 == pytest.approx(box.y + box.h / 2)
    assert box.scaled(1.0) == box and box.scaled(4.0) == box


def test_both_writers_are_handed_the_same_cells() -> None:
    """`export_mutate` writes a managed slide and `eject_slide` freezes one.

    Two writers, one arrangement, or the door out of managed mode is a reflow — and eject is
    one-way, so there would be nothing left to compare the result against. The function is
    shared for that reason; this is the claim it is shared *for*.
    """
    row = _slot_of(_stat_row("flat"), "items")
    value = [{"value": "8.3%", "label": "Churn"}, {"value": "+41%", "label": "Expansion"}]

    cells = expand.written_cells(row, value)
    assert len(cells) == 2, "a row of two figures is two boxes"
    assert [box for _, box in cells] == row.cells(2)
    assert all("8.3%" in text or "+41%" in text for text, _ in cells)

    # A single-column slot is the degenerate grid: one box, holding the whole slot.
    stacked = _slot_of(_slide("stack", [Block(id="b", region="body", component="bullets",
                                              variant="plain",
                                              slots={"items": ["one", "two"]})]), "items")
    assert expand.written_cells(stacked, ["one", "two"]) == [("one\ntwo", stacked.box)]


def test_a_grid_variant_names_its_columns_and_lets_the_items_name_the_rows() -> None:
    """`1x3` and `2x3` are the same geometry on purpose, and this pins that decision.

    A grid variant owns its column count; the rows follow from how many items it is given.
    Capping `1x3` at three would refuse content over a difference no reader can see, so the
    two names describe one rule at the two item counts they are named for.
    """
    def items(variant: str, count: int) -> expand.LaidOutSlot:
        slide = _slide("stack", [Block(id="g", region="body", component="card_grid",
                                       variant=variant,
                                       slots={"items": [str(i) for i in range(count)]})])
        return _slot_of(slide, "items")

    assert items("1x3", 3).columns == items("2x3", 3).columns
    assert len({round(c.y) for c in items("1x3", 3).cells()}) == 1
    assert len({round(c.y) for c in items("2x3", 6).cells()}) == 2, "six items are two rows"
