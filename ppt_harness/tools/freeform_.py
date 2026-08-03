"""Freeform-slide tools — DESIGN §4, PLAN A2.

Shape-level editing on an imported slide. Until these existed, an imported deck accepted
exactly one verb — `set_text` — which is not enough to call it editable.

Two rules hold here as firmly as on managed slides:

- **No coordinates.** `add_textbox` names a region of the theme grid and the expander
  places it. Nothing here takes an x.
- **Deletion is reversible.** The harness can delete shapes it does not understand, so undo
  stores the whole model and its index rather than a summary — an opaque shape has to come
  back identical, in the same z-position.
"""

from __future__ import annotations

from typing import Any

from ..components import registry
from ..core.session import Session
from ..render import expand
from ..state import richtext
from ..state.document import Author, Mode, Shape, Slide
from .base import Diff, ToolError, obj, string, tool

#: Where in the stack a shape can be sent. Named rather than numeric, because "front" is
#: what a person means and an index is what they would have to work out.
Z_POSITIONS = ("front", "back", "forward", "backward")


def _require_freeform(session: Session, slide_id: str) -> Slide:
    slide = session.slide(slide_id)
    if slide.mode is not Mode.FREEFORM:
        raise ToolError(
            "wrong_mode",
            f"{slide_id} is managed — it is built from components, so its geometry is "
            "derived. Use set_slots or set_variant instead of editing shapes.",
            mode=slide.mode.value,
        )
    return slide


def _find(session: Session, shape_id: str) -> tuple[Slide, Shape]:
    for slide in session.deck.slides:
        shape = slide.shape(shape_id)
        if shape is not None:
            return slide, shape
    raise ToolError("no_shape", f"no shape {shape_id!r} in this deck")


@tool("delete_shape", "Remove a shape from an imported slide. Reversible.",
      obj({"shape_id": string("Shape id, as reported by get_slide")}, ["shape_id"]),
      gate="freeform", mutating=True)
def delete_shape(session: Session, shape_id: str,
                 author: Author = Author.MODEL) -> dict[str, Any]:
    slide, shape = _find(session, shape_id)
    _require_freeform(session, slide.id)

    with session.transaction(author) as turn:
        session.store.write(turn, "delete_shape", f"{slide.id}/{shape.id}",
                            {"slide_id": slide.id, "shape_id": shape.id}, author)
        # The exporter patches the original package, so the deletion has to be recorded as
        # an instruction rather than merely dropped from the model.
        if shape.ooxml_id and shape.ooxml_id not in slide.removed:
            slide.removed.append(shape.ooxml_id)

    return Diff(summary=f"deleted {shape.type} {shape_id}", target=slide.id,
                before={"id": shape.id, "type": shape.type, "text": shape.text},
                render=session.measure_slide(slide.id)).as_result()


@tool("add_textbox",
      "Add a text box to an imported slide, placed in a region of the theme grid.",
      obj({"slide_id": string("Slide id"),
           # Derived, not hand-listed: a region the registry does not know about is one
           # the expander cannot place, and the two must not drift apart.
           "region": string("Where to place it on the theme grid", sorted(
               {name for frame in registry.LAYOUTS.values() for name in frame.regions})),
           "text": string("Text; **bold**, *italic*, <u>underline</u> are understood"),
           "role": string("Theme type role", ["slide_title", "block_title", "body",
                                              "label", "caption"])},
          ["slide_id", "region", "text"]),
      gate="freeform", mutating=True)
def add_textbox(session: Session, slide_id: str, region: str, text: str,
                role: str = "body", author: Author = Author.MODEL) -> dict[str, Any]:
    slide = _require_freeform(session, slide_id)
    if role not in session.theme.type.scale:
        raise ToolError("unknown_role",
                        f"no type role {role!r}; the theme has "
                        f"{sorted(session.theme.type.scale)}")

    box = expand.region_by_name(session.theme, region)
    if box is None:
        known = sorted({n for f in registry.LAYOUTS.values() for n in f.regions})
        raise ToolError("unknown_region",
                        f"no region {region!r} on the theme grid; it has {known}")

    cx, cy = session.slide_size_emu()
    canvas_w, canvas_h = session.theme.grid.canvas
    x, y, w, h = box.emu(canvas_w, canvas_h, cx, cy)

    runs = richtext.parse(text)
    shape = Shape(
        id=session.new_id("sh"),
        ooxml_id=0,  # assigned by the exporter; this shape is not in the file yet
        type="text_box",
        frame={"x": x, "y": y, "cx": w, "cy": h},
        role=role,
        text=richtext.to_plain(runs),
        runs=[] if len(runs) == 1 and runs[0].plain else runs,
        type_spec=session.theme.type.scale[role],
    )

    with session.transaction(author) as turn:
        session.store.write(turn, "add_shape", f"{slide.id}/{shape.id}",
                            {"slide_id": slide.id, "index": len(slide.shapes),
                             "shape": shape.model_dump(mode="json")}, author)

    return Diff(summary=f"added a text box in {region}", target=f"{slide.id}/{shape.id}",
                after={"id": shape.id, "region": region, "role": role},
                render=session.measure_slide(slide.id)).as_result()


@tool("duplicate_shape", "Copy a shape, offset slightly so the copy is visible.",
      obj({"shape_id": string("Shape id")}, ["shape_id"]),
      gate="freeform", mutating=True)
def duplicate_shape(session: Session, shape_id: str,
                    author: Author = Author.MODEL) -> dict[str, Any]:
    slide, shape = _find(session, shape_id)
    _require_freeform(session, slide.id)
    if shape.opaque:
        raise ToolError(
            "shape_opaque",
            f"{shape_id} is a {shape.type} the harness does not model, so it cannot be "
            "copied faithfully. It is preserved as-is on export.",
        )

    step = int(session.theme.grid.baseline * 3 * (session.slide_size_emu()[0] /
                                                  session.theme.grid.canvas[0]))
    copy = shape.model_copy(deep=True)
    copy.id = session.new_id("sh")
    copy.ooxml_id = 0
    copy.origin_text = None  # a new shape is dirty by definition; it is not in the file
    copy.frame = copy.frame.model_copy(update={"x": shape.frame.x + step,
                                               "y": shape.frame.y + step})

    index = slide.shapes.index(shape) + 1
    with session.transaction(author) as turn:
        session.store.write(turn, "add_shape", f"{slide.id}/{copy.id}",
                            {"slide_id": slide.id, "index": index,
                             "shape": copy.model_dump(mode="json")}, author)

    return Diff(summary=f"duplicated {shape_id}", target=f"{slide.id}/{copy.id}",
                after={"id": copy.id}, render=session.measure_slide(slide.id)).as_result()


@tool("set_z_order", "Move a shape forward or backward in the stacking order.",
      obj({"shape_id": string("Shape id"),
           "position": string("Where to move it", list(Z_POSITIONS))},
          ["shape_id", "position"]),
      gate="freeform", mutating=True)
def set_z_order(session: Session, shape_id: str, position: str,
                author: Author = Author.MODEL) -> dict[str, Any]:
    slide, shape = _find(session, shape_id)
    _require_freeform(session, slide.id)
    if position not in Z_POSITIONS:
        raise ToolError("unknown_position",
                        f"{position!r} is not one of {list(Z_POSITIONS)}")

    current = slide.shapes.index(shape)
    last = len(slide.shapes) - 1
    # Document order *is* z-order in OOXML: later elements paint on top.
    index = {"front": last, "back": 0,
             "forward": min(last, current + 1),
             "backward": max(0, current - 1)}[position]
    if index == current:
        raise ToolError("already_there",
                        f"{shape_id} is already at the {position} of the stack")

    with session.transaction(author) as turn:
        session.store.write(turn, "set_z_order", f"{slide.id}/{shape.id}",
                            {"slide_id": slide.id, "shape_id": shape.id, "index": index},
                            author)

    return Diff(summary=f"moved {shape_id} {position}", target=f"{slide.id}/{shape.id}",
                before={"index": current}, after={"index": index},
                render=session.measure_slide(slide.id)).as_result()
