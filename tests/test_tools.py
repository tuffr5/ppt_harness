"""Tool surface and routing — DESIGN §4.

The behaviours worth defending are the ones that make the loop short:

- **No tool takes a coordinate.** Principle 1 is only real if the API cannot express a
  violation of it.
- **Mode gating explains rather than coerces.** A refusal a model can act on is worth more
  than one it can only retry.
- **Verification travels with the write.** Under MCP you cannot force a host to call
  `render`, so a write that returned bare "ok" would let a host report success on a slide
  that overflows.
"""

from __future__ import annotations

import json

import pytest

from ppt_harness.core.session import Session
from ppt_harness.state.document import Mode
from ppt_harness.tools import router
from ppt_harness.tools.base import REGISTRY

COORDINATE_WORDS = {"x", "y", "left", "top", "width", "height", "cx", "cy",
                    "position", "font_size", "size_pt", "color", "colour"}


# ---------------------------------------------------------------- shape of the API


def test_no_tool_accepts_a_coordinate() -> None:
    """Principle 1, enforced by the schema rather than by discipline.

    A *name* is not enough to judge by: `set_z_order` takes a `position`, and it is
    `front | back | forward | backward` — a place in the stacking order, which is exactly
    the kind of thing the model is supposed to say instead of a number. What is banned is a
    suspicious name carrying a **numeric** type.
    """
    for tool in REGISTRY.values():
        for name, spec in (tool.schema.get("properties") or {}).items():
            if name not in COORDINATE_WORDS:
                continue
            assert spec.get("enum") or spec.get("type") not in ("number", "integer"), (
                f"{tool.name}.{name} is a numeric {name}"
            )


def test_no_tool_lets_the_model_set_a_font() -> None:
    """The type scale belongs to the theme; a per-call override is how decks drift."""
    for tool in REGISTRY.values():
        assert "font" not in json.dumps(tool.schema).lower(), tool.name


def test_every_tool_has_a_schema_and_a_description() -> None:
    for tool in REGISTRY.values():
        assert tool.description.strip()
        assert tool.schema["type"] == "object"
        assert tool.mcp()["inputSchema"] is tool.schema


def test_managed_tools_are_hidden_on_a_freeform_deck() -> None:
    """Filtering at the transport boundary beats rejecting at dispatch: a model never shown
    `set_variant` cannot waste a turn calling it."""
    freeform = {t.name for t in router.tools(Mode.FREEFORM)}
    managed = {t.name for t in router.tools(Mode.MANAGED)}
    assert "add_slide" in managed and "add_slide" not in freeform
    assert "set_text" in freeform and "set_text" in managed


# ------------------------------------------------------------------------ reads


def test_the_outline_stays_small(imported: Session) -> None:
    """Level 1 of the context pyramid is always on, so its size is a running cost."""
    payload = json.dumps(router.dispatch(imported, "get_outline"))
    assert len(payload) < 4000, f"outline is {len(payload)} chars"


def test_get_slide_carries_its_own_measurement(imported: Session) -> None:
    result = router.dispatch(imported, "get_slide",
                             {"slide_id": imported.deck.slides[0].id})
    assert "render" in result
    assert "overflow_px" in result["render"]


def test_the_catalog_is_one_line_per_component(imported: Session) -> None:
    """Full slot schemas stay behind `list_components(key)`; inlining them costs thousands
    of tokens every turn for something needed on maybe one."""
    brief = router.dispatch(imported, "list_components")
    from ppt_harness.components import registry

    # Per component, not a fixed total: the catalog grows, and a total would either fail on
    # the next component or be raised until it stopped meaning anything. What must stay
    # true is that a component costs a line, so level 2 scales with the catalog rather than
    # with its slot schemas.
    per_component = len(json.dumps(brief)) / len(registry.COMPONENTS)
    assert per_component < 220, f"{per_component:.0f} chars per component"
    assert '"slots"' not in json.dumps(brief), "full schemas belong behind list_components(key)"
    full = router.dispatch(imported, "list_components", {"key": "bullets"})
    assert "slots" in full and "max_items" in json.dumps(full)


def test_unknown_tools_are_refused_with_the_list(imported: Session) -> None:
    result = router.dispatch(imported, "make_it_nice")
    assert result["ok"] is False
    assert result["error"] == "unknown_tool"
    assert "get_outline" in result["message"]


def test_bad_arguments_do_not_raise(imported: Session) -> None:
    result = router.dispatch(imported, "get_slide", {"nope": 1})
    assert result["ok"] is False
    assert result["error"] == "bad_arguments"


# ------------------------------------------------------------------- mode gating


def test_a_managed_tool_on_a_freeform_slide_explains_itself(imported: Session) -> None:
    result = router.dispatch(imported, "set_variant", {
        "slide_id": imported.deck.slides[0].id, "block_id": "x", "variant": "y"})
    assert result["ok"] is False
    assert result["error"] == "wrong_mode"
    assert "set_text" in result["message"], "a refusal should name the way forward"


def test_opaque_shapes_are_refused_with_a_reason(imported: Session) -> None:
    opaque = next(((slide, s) for slide in imported.deck.slides for s in slide.shapes
                   if s.opaque), None)
    if opaque is None:
        pytest.skip("fixture has no opaque shapes")
    slide, shape = opaque
    result = router.dispatch(imported, "set_text",
                             {"target": f"{slide.id}/{shape.id}", "text": "no"})
    assert result["error"] == "shape_opaque"
    assert "preserved" in result["message"]


# ------------------------------------------------------------------------ writes


def test_set_text_returns_the_resulting_measurement(imported: Session) -> None:
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    result = router.dispatch(imported, "set_text",
                             {"target": f"{slide.id}/{shape.id}", "text": "Short"})
    assert result["ok"]
    assert result["render"]["slide"] == slide.id
    assert "overflow_px" in result["render"]


def test_set_text_refuses_over_budget_with_numbers_and_ways_out(imported: Session) -> None:
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    result = router.dispatch(imported, "set_text",
                             {"target": f"{slide.id}/{shape.id}", "text": "word " * 500})
    assert result["error"] == "budget_exceeded"
    assert result["capacity_em"] > 0
    assert result["used_em"] > result["capacity_em"]
    assert "options:" in result["message"]
    assert "hint_chars" in result


def test_a_refused_write_leaves_the_deck_alone(imported: Session) -> None:
    """The cheapest possible failure is one that never rendered and never landed."""
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    before = shape.text
    router.dispatch(imported, "set_text",
                    {"target": f"{slide.id}/{shape.id}", "text": "word " * 500})
    assert shape.text == before
    assert shape.dirty is False
    assert len(imported.store.log) == 0


def test_add_slide_rejects_a_region_the_layout_lacks(blank: Session) -> None:
    result = router.dispatch(blank, "add_slide", {
        "layout": "stack",
        "blocks": [{"region": "hero", "component": "title_slide",
                    "slots": {"title": "x"}}]})
    assert result["error"] == "bad_region"
    assert "header" in result["message"]


def test_add_slide_rejects_a_component_that_does_not_fit_the_region(blank: Session) -> None:
    result = router.dispatch(blank, "add_slide", {
        "layout": "stack",
        "blocks": [{"region": "header", "component": "bullets",
                    "slots": {"items": ["a"]}}]})
    assert result["error"] == "component_not_allowed_here"
    assert "slide_title" in result["message"], "should name what does fit"


def test_add_slide_rejects_unknown_slots(blank: Session) -> None:
    result = router.dispatch(blank, "add_slide", {
        "layout": "stack",
        "blocks": [{"region": "header", "component": "slide_title",
                    "slots": {"title": "ok", "subtitle": "not a slot"}}]})
    assert result["error"] == "unknown_slot"


def test_add_slide_budget_checks_before_it_lands(blank: Session) -> None:
    result = router.dispatch(blank, "add_slide", {
        "layout": "stack",
        "blocks": [{"region": "header", "component": "slide_title",
                    "slots": {"title": "word " * 400}}]})
    assert result["error"] == "budget_exceeded"
    assert blank.deck.slides == []


def test_add_slide_lands_and_measures(blank: Session) -> None:
    result = router.dispatch(blank, "add_slide", {
        "layout": "stack",
        "blocks": [
            {"region": "header", "component": "slide_title", "slots": {"title": "Title"}},
            {"region": "body", "component": "bullets", "slots": {"items": ["a", "b"]}},
        ]})
    assert result["ok"], result
    assert len(blank.deck.slides) == 1
    assert result["render"]["clean"] is True


def test_component_swaps_are_refused_across_slot_shapes(blank: Session) -> None:
    """Swaps are content-preserving *by construction* only within a slot shape."""
    router.dispatch(blank, "add_slide", {
        "layout": "stack",
        "blocks": [{"region": "body", "component": "bullets", "slots": {"items": ["a"]}}]})
    slide = blank.deck.slides[0]
    result = router.dispatch(blank, "set_component", {
        "slide_id": slide.id, "block_id": slide.blocks[0].id, "component": "section_break"})
    assert result["error"] == "slot_shape_mismatch"
    assert "silently drop" in result["message"]


def test_imported_slides_cannot_be_deleted_in_v0(imported: Session) -> None:
    result = router.dispatch(imported, "delete_slide",
                             {"slide_id": imported.deck.slides[0].id})
    assert result["error"] == "freeform_slide_not_removable"


# ------------------------------------------------------------------ verification


def test_lint_reports_theme_problems_and_overflow(imported: Session) -> None:
    report = router.dispatch(imported, "lint")
    assert report["checked"] == len(imported.deck.slides)
    assert "theme_problems" in report


def test_undo_reverses_a_tool_call(imported: Session) -> None:
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    before = shape.text
    router.dispatch(imported, "set_text",
                    {"target": f"{slide.id}/{shape.id}", "text": "Changed"})
    assert shape.text == "Changed"
    assert router.dispatch(imported, "undo")["ok"]
    assert shape.text == before


def test_every_layout_changing_write_returns_its_own_measurement(blank: Session) -> None:
    """Verification is part of the write's response, not a follow-up call the model might
    skip — which matters most under MCP, where you cannot force a host to call `render`.

    Exercised by calling the tools, not by reading their source: what matters is what a
    host receives, and a handler could satisfy any source-level check while returning
    nothing useful.
    """
    added = router.dispatch(blank, "add_slide", {
        "layout": "stack",
        "blocks": [
            {"region": "header", "component": "slide_title", "slots": {"title": "Title"}},
            {"region": "body", "component": "bullets", "slots": {"items": ["a", "b"]}},
        ]})
    assert added["ok"], added
    slide = blank.deck.slides[0]
    block = slide.blocks[1]

    calls = [
        ("add_slide", added),
        ("set_text", router.dispatch(blank, "set_text", {
            "target": f"{slide.id}/{slide.blocks[0].id}/title", "text": "Retitled"})),
        ("set_slots", router.dispatch(blank, "set_slots", {
            "slide_id": slide.id, "block_id": block.id,
            "patch": {"items": ["one", "two"]}})),
        ("set_variant", router.dispatch(blank, "set_variant", {
            "slide_id": slide.id, "block_id": block.id, "variant": "two_col"})),
        ("remove_block", router.dispatch(blank, "remove_block", {
            "slide_id": slide.id, "block_id": block.id})),
    ]
    for name, result in calls:
        assert result.get("ok"), f"{name}: {result}"
        assert "overflow_px" in result.get("render", {}), f"{name} returned no measurement"


#: Reads that no naming convention identifies. Kept as a named set rather than inlined
#: twice, so the rule below reads as a rule.
#:
#: `review_deck` belongs here despite marking findings raised: `mutating` means "takes the
#: writer lock and may need approval", and what it writes is conversation bookkeeping that
#: dies with the session. `remember_preference` is marked mutating precisely because its
#: write does *not* die with the session.
READS = ("render", "lint", "export", "query_data", "review_deck")


def test_the_mutating_flag_matches_the_tool_set() -> None:
    """A read marked mutating would take the writer lock; a write left unmarked would skip
    the approval gate a host may rely on."""
    writes = {t.name for t in REGISTRY.values() if t.mutating}
    reads = {t.name for t in REGISTRY.values() if not t.mutating}

    # Stated as a rule rather than a list, so a new tool is classified by what it does
    # rather than by whether someone remembered to update a set.
    assert not any(n.startswith(("get_", "list_")) or n in READS
                   for n in writes), f"a read is marked mutating: {sorted(writes)}"
    assert all(n.startswith(("get_", "list_")) or n in READS
               for n in reads), f"a write is not marked mutating: {sorted(reads)}"
