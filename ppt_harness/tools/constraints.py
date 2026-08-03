"""Freeform constraint tools — DESIGN §4, PLAN A3.

Semantic geometry. A model says *align these left*, not *put this at 838200 EMU*, and the
harness works out the number. That is the whole reason `set_frame` can stay an escape hatch
rather than the ordinary way to move something.

The maths lives in `freeform/constraints.py` as pure functions over rectangles, so it can be
checked against hand-computed cases. What is here is the part that needs a session: finding
the shapes, refusing the ones that cannot move, and recording one op for the whole
selection so undo puts them all back together.
"""

from __future__ import annotations

from typing import Any

from ..core.session import Session
from ..freeform import constraints as geo
from ..render import budget as budget_mod
from ..state.document import Author, Frame, Mode, Shape, Slide
from .base import Diff, ToolError, integer, obj, string, tool


def _gather(session: Session, shape_ids: list[str]) -> tuple[Slide, list[Shape]]:
    """Resolve a selection, and insist it lives on one slide.

    Aligning shapes across two slides is not a thing that has a meaning, and the error is
    clearer than whatever geometry would come out of pretending otherwise.
    """
    if not shape_ids:
        raise ToolError("no_shapes", "name at least one shape")

    found: list[tuple[Slide, Shape]] = []
    for shape_id in shape_ids:
        for slide in session.deck.slides:
            shape = slide.shape(shape_id)
            if shape is not None:
                found.append((slide, shape))
                break
        else:
            raise ToolError("no_shape", f"no shape {shape_id!r} in this deck")

    slides = {slide.id for slide, _ in found}
    if len(slides) > 1:
        raise ToolError("across_slides",
                        f"those shapes are on {sorted(slides)}; a constraint applies to one "
                        "slide at a time")

    slide = found[0][0]
    if slide.mode is not Mode.FREEFORM:
        raise ToolError("wrong_mode",
                        f"{slide.id} is managed — components own its geometry. Use "
                        "set_variant, or eject the slide to place shapes by hand.")
    return slide, [shape for _, shape in found]


def _write(session: Session, slide: Slide, shapes: list[Shape], frames: list[Frame],
           author: Author, summary: str, **after: Any) -> dict[str, Any]:
    moved = {
        shape.id: frame.model_dump(mode="json")
        for shape, frame in zip(shapes, frames, strict=True)
        if frame.model_dump() != shape.frame.model_dump()
    }
    if not moved:
        raise ToolError("already_there", f"{summary} would change nothing")

    with session.transaction(author) as turn:
        session.store.write(turn, "set_frames", slide.id,
                            {"slide_id": slide.id, "frames": moved}, author)

    return Diff(summary=summary, target=slide.id,
                after={"moved": len(moved), **after},
                render=session.measure_slide(slide.id)).as_result()


def _shape_ids(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


@tool("align", "Line shapes up on one edge of their own bounding box.",
      obj({"shape_ids": _shape_ids("Two or more shape ids"),
           "edge": string("Edge to align to", list(geo.EDGES))},
          ["shape_ids", "edge"]),
      gate="freeform", mutating=True)
def align(session: Session, shape_ids: list[str], edge: str,
          author: Author = Author.MODEL) -> dict[str, Any]:
    slide, shapes = _gather(session, shape_ids)
    if len(shapes) < 2:
        raise ToolError("too_few", "aligning needs at least two shapes")
    try:
        frames = geo.align([s.frame for s in shapes], edge)
    except ValueError as exc:
        raise ToolError("unknown_edge", str(exc)) from exc
    return _write(session, slide, shapes, frames, author,
                  f"aligned {len(shapes)} shapes {edge}", edge=edge)


@tool("distribute", "Space shapes evenly along an axis.",
      obj({"shape_ids": _shape_ids("Three or more shape ids, or two with a gap"),
           "axis": string("Axis to spread along", list(geo.AXES)),
           "gap": integer("Fixed gap in points; omit to spread within the current extent")},
          ["shape_ids", "axis"]),
      gate="freeform", mutating=True)
def distribute(session: Session, shape_ids: list[str], axis: str, gap: int | None = None,
               author: Author = Author.MODEL) -> dict[str, Any]:
    slide, shapes = _gather(session, shape_ids)
    if len(shapes) < 3 and gap is None:
        raise ToolError(
            "too_few",
            "spreading within the current extent needs three shapes — with two there is "
            "nothing between them to space. Give a gap instead.",
        )
    try:
        frames = geo.distribute([s.frame for s in shapes], axis,
                                None if gap is None else int(gap) * 12700)
    except ValueError as exc:
        raise ToolError("unknown_axis", str(exc)) from exc
    return _write(session, slide, shapes, frames, author,
                  f"distributed {len(shapes)} shapes {axis}ly", axis=axis)


@tool("match_size", "Resize shapes to match the first one named.",
      obj({"shape_ids": _shape_ids("The reference shape first, then the ones to resize"),
           "dimension": string("What to match", list(geo.DIMENSIONS))},
          ["shape_ids", "dimension"]),
      gate="freeform", mutating=True)
def match_size(session: Session, shape_ids: list[str], dimension: str,
               author: Author = Author.MODEL) -> dict[str, Any]:
    slide, shapes = _gather(session, shape_ids)
    if len(shapes) < 2:
        raise ToolError("too_few", "matching needs a reference and at least one other")
    try:
        frames = geo.match_size([s.frame for s in shapes], dimension)
    except ValueError as exc:
        raise ToolError("unknown_dimension", str(exc)) from exc
    return _write(session, slide, shapes, frames, author,
                  f"matched {dimension} to {shape_ids[0]}", dimension=dimension)


@tool("snap_to_grid", "Pull shapes onto the theme's columns and vertical rhythm.",
      obj({"shape_ids": _shape_ids("Shape ids")}, ["shape_ids"]),
      gate="freeform", mutating=True)
def snap_to_grid(session: Session, shape_ids: list[str],
                 author: Author = Author.MODEL) -> dict[str, Any]:
    slide, shapes = _gather(session, shape_ids)
    grid = session.theme.grid
    cx, _ = session.slide_size_emu()
    per_px = cx / grid.canvas[0]
    content = grid.canvas[0] - 2 * grid.margin
    column = (content - grid.gutter * (grid.columns - 1)) / grid.columns

    frames = geo.snap_to_grid([s.frame for s in shapes],
                              column=round(column * per_px),
                              gutter=round(grid.gutter * per_px),
                              margin=round(grid.margin * per_px),
                              baseline=round(grid.baseline * per_px))
    return _write(session, slide, shapes, frames, author,
                  f"snapped {len(shapes)} shapes to the grid")


@tool("nudge", "Move one shape a little, without letting it leave the slide.",
      obj({"shape_id": string("Shape id"),
           "direction": string("Which way", list(geo.DIRECTIONS)),
           "step": string("How far", list(geo.STEPS))},
          ["shape_id", "direction"]),
      gate="freeform", mutating=True)
def nudge(session: Session, shape_id: str, direction: str, step: str = "small",
          author: Author = Author.MODEL) -> dict[str, Any]:
    slide, shapes = _gather(session, [shape_id])
    shape = shapes[0]
    cx, cy = session.slide_size_emu()
    per_px = cx / session.theme.grid.canvas[0]
    try:
        distance = geo.step_size(session.theme.spacing, step, per_px)
        frame = geo.nudge(shape.frame, direction, distance, (cx, cy))
    except ValueError as exc:
        raise ToolError("bad_nudge", str(exc)) from exc
    return _write(session, slide, [shape], [frame], author,
                  f"nudged {shape_id} {direction}", direction=direction, step=step)


@tool("fit_box_to_text", "Resize a shape's height to the text it holds.",
      obj({"shape_id": string("Shape id")}, ["shape_id"]),
      gate="freeform", mutating=True)
def fit_box_to_text(session: Session, shape_id: str,
                    author: Author = Author.MODEL) -> dict[str, Any]:
    """The measurer run backwards.

    Everywhere else the harness asks "does this text fit this box"; here it asks "what box
    would this text fit". It is what makes an overflow fixable without touching the words —
    the first rung of the repair ladder that does not touch what the author wrote.
    """
    slide, shapes = _gather(session, [shape_id])
    shape = shapes[0]
    if shape.opaque or shape.text is None:
        raise ToolError("no_text", f"{shape_id} is a {shape.type} and holds no text")

    cx, cy = session.slide_size_emu()
    b = budget_mod.for_shape(session.theme, shape, cx, cy)
    result = budget_mod.check(shape.text, b, session.theme)

    per_px = cy / session.theme.grid.canvas[1]
    needed = max(1, result.lines) * b.spec.line * per_px
    frame = shape.frame.model_copy(update={"cy": max(1, round(needed))})
    return _write(session, slide, [shape], [frame], author,
                  f"fitted {shape_id} to {result.lines} line(s)", lines=result.lines)


@tool("restyle", "Set a shape's type to a role from the theme.",
      obj({"shape_id": string("Shape id"),
           "role": string("Theme type role")}, ["shape_id", "role"]),
      gate="freeform", mutating=True)
def restyle(session: Session, shape_id: str, role: str,
            author: Author = Author.MODEL) -> dict[str, Any]:
    """A role, never a size.

    This is the one way the harness changes how text looks, and it does it by naming a role
    the theme defines. A point size here would be the moment the palette and the type scale
    stopped meaning anything.
    """
    slide, shapes = _gather(session, [shape_id])
    shape = shapes[0]
    if role not in session.theme.type.scale:
        raise ToolError("unknown_role",
                        f"no type role {role!r}; the theme has "
                        f"{sorted(session.theme.type.scale)}")

    before = shape.role
    spec = session.theme.type.scale[role]
    with session.transaction(author) as turn:
        session.store.write(turn, "set_props", f"{slide.id}/{shape.id}",
                            {"role": role, "type_spec": spec.model_dump(mode="json")},
                            author)

    return Diff(summary=f"restyled {shape_id} as {role}", target=f"{slide.id}/{shape.id}",
                before={"role": before}, after={"role": role},
                render=session.measure_slide(slide.id)).as_result()


@tool("set_frame",
      "ESCAPE HATCH: place a shape at an absolute position. Prefer align, distribute, "
      "snap_to_grid or nudge — this is logged and flagged for review.",
      obj({"shape_id": string("Shape id"),
           "frame": {"type": "object",
                     "description": "x, y, cx, cy in EMU (914400 per inch)"}},
          ["shape_id", "frame"]),
      gate="freeform", mutating=True)
def set_frame(session: Session, shape_id: str, frame: dict[str, int],
              author: Author = Author.MODEL) -> dict[str, Any]:
    """The one tool that takes coordinates, and it says so in its own description.

    Kept because some placements have no semantic name, and a harness with no escape hatch
    is one people work around. Logged and flagged so that a deck full of these is visible
    as a design problem rather than invisible as ordinary use.
    """
    slide, shapes = _gather(session, [shape_id])
    shape = shapes[0]
    try:
        placed = Frame.model_validate({**shape.frame.model_dump(), **frame})
    except Exception as exc:
        raise ToolError("bad_frame", f"{frame!r} is not a frame: {exc}") from exc

    result = _write(session, slide, [shape], [placed], author,
                    f"set the frame of {shape_id} directly")
    result["escape_hatch"] = True
    result["note"] = ("placed by coordinate rather than by constraint; align, distribute, "
                      "snap_to_grid and nudge express intent that survives an edit")
    return result
