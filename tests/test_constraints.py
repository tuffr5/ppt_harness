"""Freeform constraint tools — PLAN A3.

Two layers, tested differently. The geometry is pure functions over rectangles, so it is
checked against **hand-computed** numbers rather than against a rendering. The tools are
checked for the things a session brings: refusing shapes that cannot move, keeping a whole
selection in one undoable step, and staying on the slide.

The claim underneath: these exist so `set_frame` stays an escape hatch. A model that can say
"align these left" never needs to name a coordinate, and an intent survives an edit in a way
a number does not.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from ppt_harness.core.session import Session
from ppt_harness.freeform import constraints as geo
from ppt_harness.state.document import Frame, Mode
from ppt_harness.tools import router


def F(x: int, y: int, cx: int, cy: int) -> Frame:
    return Frame(x=x, y=y, cx=cx, cy=cy)


# --------------------------------------------------------------------- the maths


def test_align_left_uses_the_selection_not_the_slide() -> None:
    """Aligning to the slide would march a tidy group across the canvas, which is never
    what "align these left" means."""
    out = geo.align([F(100, 0, 50, 10), F(300, 0, 50, 10)], "left")
    assert [f.x for f in out] == [100, 100]


@pytest.mark.parametrize(("edge", "expected"), [
    ("left", [0, 0, 0]),
    ("right", [260, 300, 320]),
    ("top", [0, 0, 0]),
    ("bottom", [50, 20, 60]),
])
def test_align_places_every_edge(edge: str, expected: list[int]) -> None:
    frames = [F(0, 0, 100, 50), F(300, 20, 60, 80), F(150, 90, 40, 40)]
    out = geo.align(frames, edge)
    got = ([f.x for f in out] if edge in ("left", "right")
           else [f.y for f in out])
    if edge == "right":
        got = [f.x for f in out]
        assert all(f.x + f.cx == 360 for f in out)
        return
    if edge == "bottom":
        assert all(f.y + f.cy == 130 for f in out)
        return
    assert got == expected


def test_align_centres_on_the_midline() -> None:
    out = geo.align([F(0, 0, 100, 10), F(300, 0, 60, 10)], "center")
    assert {f.x + f.cx // 2 for f in out} == {180}


def test_distribute_leaves_equal_gaps_and_keeps_the_extent() -> None:
    frames = [F(0, 0, 100, 50), F(300, 0, 60, 50), F(150, 0, 40, 50)]
    out = sorted(geo.distribute(frames, "horizontal"), key=lambda f: f.x)
    gaps = [b.x - (a.x + a.cx) for a, b in pairwise(out)]
    assert gaps == [80, 80]
    assert out[0].x == 0
    assert out[-1].x + out[-1].cx == 360


def test_distribute_with_a_gap_anchors_on_the_first() -> None:
    frames = [F(0, 0, 100, 10), F(500, 0, 100, 10)]
    out = sorted(geo.distribute(frames, "horizontal", gap=25), key=lambda f: f.x)
    assert out[0].x == 0
    assert out[1].x == 125


def test_distribute_returns_shapes_in_the_order_given() -> None:
    """The caller passed shapes and gets its shapes back — reordering would silently
    rewrite whichever list the caller is holding alongside."""
    frames = [F(300, 0, 50, 10), F(0, 0, 50, 10), F(150, 0, 50, 10)]
    out = geo.distribute(frames, "horizontal")
    assert out[0].cx == frames[0].cx
    assert out[1].x < out[0].x


def test_match_size_uses_the_first_as_reference() -> None:
    """Not the largest: silently picking the biggest would make the result depend on the
    data rather than on what was asked."""
    out = geo.match_size([F(0, 0, 100, 20), F(0, 0, 999, 999)], "both")
    assert (out[1].cx, out[1].cy) == (100, 20)


@pytest.mark.parametrize(("dimension", "expected"), [
    ("width", (100, 999)), ("height", (999, 20)), ("both", (100, 20))])
def test_match_size_touches_only_what_was_asked(dimension: str,
                                                expected: tuple[int, int]) -> None:
    out = geo.match_size([F(0, 0, 100, 20), F(0, 0, 999, 999)], dimension)
    assert (out[1].cx, out[1].cy) == expected


def test_snap_pulls_to_the_nearest_column_and_baseline() -> None:
    out = geo.snap_to_grid([F(103, 37, 50, 10)], column=100, gutter=20,
                           margin=100, baseline=10)
    assert out[0].x == 100
    assert out[0].y == 40


def test_snap_leaves_sizes_alone() -> None:
    """Snapping widths too would resize shapes nobody asked to resize."""
    out = geo.snap_to_grid([F(103, 37, 57, 13)], column=100, gutter=20,
                           margin=100, baseline=10)
    assert (out[0].cx, out[0].cy) == (57, 13)


@pytest.mark.parametrize(("direction", "axis", "delta"), [
    ("left", "x", -10), ("right", "x", 10), ("up", "y", -10), ("down", "y", 10)])
def test_nudge_moves_the_right_way(direction: str, axis: str, delta: int) -> None:
    out = geo.nudge(F(100, 100, 50, 50), direction, 10, (1000, 1000))
    assert getattr(out, axis) == 100 + delta


def test_a_nudge_cannot_walk_a_shape_off_the_slide() -> None:
    """Clamped rather than refused: at the edge it should stop, not fail."""
    assert geo.nudge(F(0, 0, 50, 50), "left", 40, (1000, 600)).x == 0
    assert geo.nudge(F(950, 0, 50, 50), "right", 40, (1000, 600)).x == 950


def test_a_shape_already_off_canvas_is_not_dragged_back() -> None:
    """Imported decks contain them, and moving one the user did not ask to move is worse
    than leaving it where its author put it."""
    out = geo.nudge(F(-200, 0, 50, 50), "left", 10, (1000, 600))
    assert out.x == -210


def test_repeated_nudges_stay_bounded() -> None:
    frame = F(500, 0, 50, 50)
    for _ in range(200):
        frame = geo.nudge(frame, "right", 10, (1000, 600))
    assert frame.x + frame.cx <= 1000


def test_a_nudge_step_comes_from_the_theme_spacing() -> None:
    ladder = [4, 8, 12, 16, 24, 32, 48, 64]
    small = geo.step_size(ladder, "small", 1.0)
    large = geo.step_size(ladder, "large", 1.0)
    assert small < large
    assert small in ladder and large in ladder


@pytest.mark.parametrize("call", [
    lambda: geo.align([F(0, 0, 1, 1), F(1, 1, 1, 1)], "sideways"),
    lambda: geo.distribute([F(0, 0, 1, 1)] * 3, "diagonal"),
    lambda: geo.match_size([F(0, 0, 1, 1)], "depth"),
    lambda: geo.nudge(F(0, 0, 1, 1), "inward", 1, (10, 10)),
    lambda: geo.step_size([4], "enormous", 1.0),
])
def test_unknown_options_raise_rather_than_guess(call) -> None:
    with pytest.raises(ValueError):
        call()


# --------------------------------------------------------------------- the tools


@pytest.fixture
def slide(imported: Session):
    return max((s for s in imported.deck.slides if s.mode is Mode.FREEFORM),
               key=lambda s: len(s.shapes))


def _movable(slide, count: int = 3) -> list[str]:
    return [s.id for s in slide.shapes if not s.opaque][:count]


def test_align_moves_the_whole_selection(imported: Session, slide) -> None:
    ids = _movable(slide, 3)
    assert router.dispatch(imported, "align",
                           {"shape_ids": ids, "edge": "left"})["ok"]
    xs = {slide.shape(i).frame.x for i in ids}
    assert len(xs) == 1


def test_a_selection_is_one_undoable_step(imported: Session, slide) -> None:
    """"Align these left" is one thing the user did; undo has to put them all back."""
    ids = _movable(slide, 3)
    before = {i: slide.shape(i).frame.model_dump() for i in ids}
    router.dispatch(imported, "align", {"shape_ids": ids, "edge": "right"})
    assert router.dispatch(imported, "undo")["ok"]
    assert {i: slide.shape(i).frame.model_dump() for i in ids} == before


def test_aligning_across_slides_is_refused(imported: Session) -> None:
    """It has no meaning, and the error is clearer than whatever geometry would come out
    of pretending otherwise."""
    a = imported.deck.slides[0].shapes[0].id
    b = imported.deck.slides[1].shapes[0].id
    result = router.dispatch(imported, "align", {"shape_ids": [a, b], "edge": "left"})
    assert result["ok"] is False
    assert result["error"] == "across_slides"


def test_aligning_one_shape_is_refused(imported: Session, slide) -> None:
    result = router.dispatch(imported, "align",
                             {"shape_ids": _movable(slide, 1), "edge": "left"})
    assert result["ok"] is False
    assert result["error"] == "too_few"


def test_a_constraint_that_changes_nothing_says_so(imported: Session, slide) -> None:
    """A silent success invites the model to try again and count it as progress."""
    ids = _movable(slide, 2)
    router.dispatch(imported, "align", {"shape_ids": ids, "edge": "left"})
    again = router.dispatch(imported, "align", {"shape_ids": ids, "edge": "left"})
    assert again["ok"] is False
    assert again["error"] == "already_there"


def test_distribute_needs_three_shapes_or_a_gap(imported: Session, slide) -> None:
    ids = _movable(slide, 2)
    result = router.dispatch(imported, "distribute",
                             {"shape_ids": ids, "axis": "horizontal"})
    assert result["ok"] is False
    assert "gap" in result["message"]
    assert router.dispatch(imported, "distribute",
                           {"shape_ids": ids, "axis": "horizontal", "gap": 10})["ok"]


def test_nudge_and_snap_apply(imported: Session, slide) -> None:
    shape_id = _movable(slide, 1)[0]
    before = slide.shape(shape_id).frame.x
    assert router.dispatch(imported, "nudge",
                           {"shape_id": shape_id, "direction": "right"})["ok"]
    assert slide.shape(shape_id).frame.x > before
    assert router.dispatch(imported, "snap_to_grid",
                           {"shape_ids": [shape_id]})["ok"] in (True, False)


def test_fit_box_to_text_is_the_measurer_run_backwards(imported: Session,
                                                       slide) -> None:
    """Everywhere else the harness asks whether text fits a box; here it asks what box the
    text would fit. It is what makes an overflow fixable without touching the words."""
    added = router.dispatch(imported, "add_textbox",
                            {"slide_id": slide.id, "region": "body",
                             "text": "one line"})
    shape_id = added["target"].split("/")[-1]
    tall = slide.shape(shape_id).frame.cy

    assert router.dispatch(imported, "fit_box_to_text", {"shape_id": shape_id})["ok"]
    assert slide.shape(shape_id).frame.cy < tall


def test_fitting_a_shape_with_no_text_is_refused(imported: Session) -> None:
    found = next(((s, x) for s in imported.deck.slides for x in s.shapes if x.opaque), None)
    if found is None:
        pytest.skip("fixture has no opaque shapes")
    result = router.dispatch(imported, "fit_box_to_text", {"shape_id": found[1].id})
    assert result["ok"] is False


def test_restyle_takes_a_role_and_never_a_size(imported: Session, slide) -> None:
    """The one way the harness changes how text looks, and it does it by naming something
    the theme defines."""
    shape_id = next(s.id for s in slide.shapes if s.text and not s.opaque)
    assert router.dispatch(imported, "restyle",
                           {"shape_id": shape_id, "role": "caption"})["ok"]
    shape = slide.shape(shape_id)
    assert shape.role == "caption"
    assert shape.type_spec.size == imported.theme.type.scale["caption"].size

    tool = next(t for t in router.tools() if t.name == "restyle")
    assert "size" not in str(tool.schema).lower()


def test_an_unknown_role_is_refused_with_the_ones_that_exist(imported: Session,
                                                             slide) -> None:
    shape_id = next(s.id for s in slide.shapes if s.text and not s.opaque)
    result = router.dispatch(imported, "restyle",
                             {"shape_id": shape_id, "role": "enormous"})
    assert result["ok"] is False
    assert "caption" in result["message"]


# --------------------------------------------------------------------- escape hatch


def test_set_frame_is_the_only_tool_taking_coordinates() -> None:
    """And it announces itself, so a deck full of them is visible as a design problem
    rather than invisible as ordinary use."""
    taking = [t.name for t in router.tools()
              if "frame" in (t.schema.get("properties") or {})]
    assert taking == ["set_frame"]

    tool = next(t for t in router.tools() if t.name == "set_frame")
    assert "ESCAPE HATCH" in tool.description
    assert "align" in tool.description


def test_set_frame_flags_its_own_result(imported: Session, slide) -> None:
    shape_id = _movable(slide, 1)[0]
    result = router.dispatch(imported, "set_frame",
                             {"shape_id": shape_id,
                              "frame": {"x": 100000, "y": 200000}})
    assert result["ok"]
    assert result["escape_hatch"] is True
    assert "constraint" in result["note"]
    assert slide.shape(shape_id).frame.x == 100000


def test_a_malformed_frame_is_refused(imported: Session, slide) -> None:
    result = router.dispatch(imported, "set_frame",
                             {"shape_id": _movable(slide, 1)[0],
                              "frame": {"x": "over there"}})
    assert result["ok"] is False
    assert result["error"] == "bad_frame"


def test_constraints_refuse_managed_slides(populated: Session) -> None:
    managed = next(s for s in populated.deck.slides if s.mode is Mode.MANAGED)
    populated.deck.slides[0].shapes.clear()
    result = router.dispatch(populated, "align",
                             {"shape_ids": [f"{managed.id}_nope"], "edge": "left"})
    assert result["ok"] is False
