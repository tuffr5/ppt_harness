"""The repair ladder — DESIGN §5.2, §5.3, PLAN B2.

The ladder's value is entirely in its *order*. Reshaping a container is cheap and
reversible; editing what someone wrote is neither. So it climbs structure — variant, then
density, then the degradation chain — and stops before content.

And it stops **differently** depending on who wrote the words. That is the arbiter: the op
log already records an author, so provenance is a query rather than new instrumentation.
Text the model wrote, it may offer to cut. Text a person wrote, or text that came out of
the imported file, it may only ask about.
"""

from __future__ import annotations

import pytest

from ppt_harness.components import fixtures, registry
from ppt_harness.core import repair as ladder
from ppt_harness.core.session import Session
from ppt_harness.state.document import Author, Block, Mode, Slide
from ppt_harness.tools import router

LONG_LABEL = "Annual recurring revenue, net of churn and one-off migration credits"


def _place(session: Session, component: str, slots: dict, variant: str | None = None,
           author: Author = Author.MODEL) -> str:
    """Put a block on a slide *past* the gate, so the ladder has something to fix.

    The budget refuses this content at the door — which is the design — so a test of the
    repair path has to write it directly.
    """
    layout, region = fixtures.region_for(component)
    comp = registry.get(component)
    slide = Slide(id=session.new_id("sl"), index=len(session.deck.slides),
                  mode=Mode.MANAGED, layout=layout,
                  blocks=[Block(id=session.new_id("bk"), region=region,
                                component=component,
                                variant=variant or comp.default_variant, slots=slots)])
    with session.transaction(author) as turn:
        session.store.write(turn, "add_slide", slide.id,
                            {"index": slide.index,
                             "slide": slide.model_dump(mode="json")}, author)
    return slide.id


@pytest.fixture
def overflowing(blank: Session) -> str:
    slots = {"items": [{"value": "$4.2M", "label": LONG_LABEL} for _ in range(4)]}
    return _place(blank, "stat_row", slots, variant="flat")


# ----------------------------------------------------------------------- order


def test_the_ladder_fixes_an_overflow_without_touching_the_words(blank: Session,
                                                                 overflowing: str) -> None:
    slide = blank.deck.slide(overflowing)
    before = slide.blocks[0].slots["items"]
    assert blank.measure_slide(overflowing)["clean"] is False

    outcome = ladder.repair(blank, overflowing)
    assert outcome.fixed
    assert blank.measure_slide(overflowing)["clean"] is True
    assert slide.blocks[0].slots["items"] == before, "the content was edited"


def test_a_variant_is_tried_before_a_component_swap(blank: Session) -> None:
    """The cheapest rung first: same component, same words, different arrangement.

    The payload is deliberately over-long rather than merely long. It used to be six
    `LONG_LABEL`s, which overflowed only because the budget charged every item the slot's
    full width divided by the item count — a `2x3` grid measured six times narrower than it
    drew. With cells measured where they are actually placed that payload fits, and the test
    quietly skipped itself: a guard written to keep the suite honest, doing the opposite.
    """
    slots = {"items": [f"{LONG_LABEL}, {LONG_LABEL}" for _ in range(6)]}
    slide_id = _place(blank, "card_grid", slots, variant="2x3")
    assert not blank.measure_slide(slide_id)["clean"], \
        "the payload must overflow, or this test proves nothing about the ladder"

    outcome = ladder.repair(blank, slide_id)
    if outcome.fixed and outcome.steps:
        first = outcome.steps[0]
        assert first.rung in ("variant", "override"), \
            f"reached for {first.rung} before exhausting cheaper rungs"


def test_degradation_keeps_every_item(blank: Session, overflowing: str) -> None:
    """A chain runs within a slot shape, so it rearranges rather than drops."""
    slide = blank.deck.slide(overflowing)
    before = list(slide.blocks[0].slots["items"])
    ladder.repair(blank, overflowing)
    assert slide.blocks[0].slots["items"] == before


def test_the_ladder_never_changes_the_type_scale(blank: Session,
                                                 overflowing: str) -> None:
    """Shrinking a font is the silent degradation autofit was disabled to prevent."""
    scale = {k: v.model_dump() for k, v in blank.theme.type.scale.items()}
    floor = blank.theme.type.floor
    ladder.repair(blank, overflowing)
    assert {k: v.model_dump() for k, v in blank.theme.type.scale.items()} == scale
    assert blank.theme.type.floor == floor


def test_density_tightens_spacing_and_nothing_else(blank: Session) -> None:
    from ppt_harness.render import expand

    slots = {"title": "A heading", "items": [LONG_LABEL for _ in range(4)]}
    slide_id = _place(blank, "card_grid", slots, variant="2x2")
    slide = blank.deck.slide(slide_id)

    normal = {s.slot: s.spec.size for s in expand.expand_slide(blank.theme, slide)}
    slide.blocks[0].overrides = {"density": "compact"}
    compact = {s.slot: s.spec.size for s in expand.expand_slide(blank.theme, slide)}
    assert normal == compact, "density changed the type, not the spacing"


def test_the_ladder_gives_up_rather_than_looping(blank: Session) -> None:
    """Beyond a point the answer is not a smaller arrangement, it is a second slide."""
    slots = {"items": [LONG_LABEL * 6 for _ in range(8)]}
    slide_id = _place(blank, "bullets", slots)
    outcome = ladder.repair(blank, slide_id)
    assert outcome.fixed is False
    assert len(outcome.steps) <= ladder.MAX_RUNGS
    assert outcome.advice


# ------------------------------------------------------------------- provenance


def test_model_written_words_may_be_shortened(blank: Session) -> None:
    slots = {"items": [LONG_LABEL * 6 for _ in range(8)]}
    slide_id = _place(blank, "bullets", slots, author=Author.MODEL)
    block = blank.deck.slide(slide_id).blocks[0]

    with blank.transaction(Author.MODEL) as turn:
        blank.store.write(turn, "set_text", f"{slide_id}/{block.id}/items",
                          {"text": "rewritten"}, Author.MODEL)

    assert ladder.may_shorten(blank, f"{slide_id}/{block.id}/items") is True


def test_user_written_words_are_not_the_harness_to_cut(blank: Session) -> None:
    """The rule that makes the ladder safe to run automatically."""
    slots = {"items": [LONG_LABEL * 6 for _ in range(8)]}
    slide_id = _place(blank, "bullets", slots)
    block = blank.deck.slide(slide_id).blocks[0]

    with blank.transaction(Author.USER) as turn:
        blank.store.write(turn, "set_text", f"{slide_id}/{block.id}/items",
                          {"text": "the user's own words"}, Author.USER)

    assert ladder.may_shorten(blank, f"{slide_id}/{block.id}/items") is False


def test_text_from_the_file_belongs_to_its_author(imported: Session) -> None:
    """Nobody in this session wrote it, so it is the original author's."""
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    assert ladder.authored_by(imported, f"{slide.id}/{shape.id}") is None
    assert ladder.may_shorten(imported, f"{slide.id}/{shape.id}") is False


def test_the_advice_names_who_may_decide(blank: Session) -> None:
    slots = {"items": [LONG_LABEL * 6 for _ in range(8)]}
    slide_id = _place(blank, "bullets", slots)
    block = blank.deck.slide(slide_id).blocks[0]
    with blank.transaction(Author.USER) as turn:
        blank.store.write(turn, "set_text", f"{slide_id}/{block.id}/items",
                          {"text": LONG_LABEL * 6}, Author.USER)

    outcome = ladder.repair(blank, slide_id)
    assert outcome.fixed is False
    assert "not the harness" in outcome.advice or "ask" in outcome.advice


# ------------------------------------------------------------------ attribution


def test_repairs_are_attributed_to_lint(blank: Session, overflowing: str) -> None:
    """So a harness-chosen variant stays distinguishable in the log from one the user
    asked for — which is what the arbiter reads next time."""
    ladder.repair(blank, overflowing)
    authors = {op.author for op in blank.store.log.ops if op.op == "set_block_props"}
    assert authors == {Author.LINT}


def test_a_repair_is_undoable(blank: Session, overflowing: str) -> None:
    slide = blank.deck.slide(overflowing)
    before = (slide.blocks[0].component, slide.blocks[0].variant)
    ladder.repair(blank, overflowing)
    assert (slide.blocks[0].component, slide.blocks[0].variant) != before

    while router.dispatch(blank, "undo")["ok"]:
        pass
    assert (slide.blocks[0].component, slide.blocks[0].variant) == before


# ------------------------------------------------------------------- the tool


def test_the_tool_reports_the_rungs_it_tried(blank: Session, overflowing: str) -> None:
    result = router.dispatch(blank, "repair", {"slide_id": overflowing})
    assert result["ok"]
    assert result["after"]["fixed"] is True
    assert result["after"]["tried"], "a repair that explains nothing is not reviewable"
    assert result["render"]["clean"] is True


def test_repairing_an_imported_slide_points_at_the_right_tools(imported: Session) -> None:
    """A freeform slide's geometry is the original author's; the ladder has no components
    to reshape."""
    outcome = ladder.repair(imported, imported.deck.slides[0].id)
    assert outcome.fixed is False
    assert "fit_box_to_text" in outcome.advice


def test_repair_is_gated_to_managed_slides(imported: Session) -> None:
    result = router.dispatch(imported, "repair",
                             {"slide_id": imported.deck.slides[0].id})
    assert result["ok"] is False
    assert result["error"] == "wrong_mode"


def test_a_clean_slide_needs_no_repair(populated: Session) -> None:
    slide = next(s for s in populated.deck.slides if s.mode is Mode.MANAGED)
    if not populated.measure_slide(slide.id)["clean"]:
        pytest.skip("fixture slide already overflows")
    outcome = ladder.repair(populated, slide.id)
    assert outcome.fixed is True
    assert outcome.steps == []
