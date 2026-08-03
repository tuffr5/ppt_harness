"""Deck-level operations — PLAN A5.

Changes to the deck rather than to anything on a slide: duplicating a slide, switching a
layout, resizing the canvas, hiding a slide from a slideshow, and correcting a theme role
the importer guessed.

The one with teeth is `set_slide_size`. Every frame in an imported deck is absolute EMU,
measured against the old canvas — resizing invalidates all of it. Rescaling silently would
produce a deck that looks right in the preview and wrong to anyone who opens it, so it
reports what it invalidated instead.
"""

from __future__ import annotations

from typing import Any

from ..components import registry
from ..core.session import Session
from ..state.document import Author, Mode
from ..state.theme_default import contrast, validate_theme
from .base import Diff, ToolError, integer, obj, string, tool

#: Canvas presets, in inches. Named rather than numeric for the usual reason: "16:9" is
#: what a person means, and 12192000 EMU is what they would have to look up.
SLIDE_SIZES = {
    "16:9": (13.333, 7.5),
    "16:10": (12.0, 7.5),
    "4:3": (10.0, 7.5),
    "a4": (11.69, 8.27),
}

EMU_PER_INCH = 914400


@tool("duplicate_slide", "Copy a slide, placing the copy after the original.",
      obj({"slide_id": string("Slide id"),
           "index": integer("Where to put the copy; omit to place it directly after")},
          ["slide_id"]),
      mutating=True)
def duplicate_slide(session: Session, slide_id: str, index: int | None = None,
                    author: Author = Author.MODEL) -> dict[str, Any]:
    """Every id is reissued.

    A copy sharing shape ids with its original would make every later edit ambiguous — the
    harness would find two shapes for one name and pick whichever came first.
    """
    slide = session.slide(slide_id)
    copy = slide.model_copy(deep=True)
    copy.id = session.new_id("sl")
    copy.duplicate_of = slide.id
    copy.removed = []
    # Shape ids are reissued, but `ooxml_id` is kept: the exporter clones the source part,
    # so the copy's shapes are the same shapes, in a new slide.
    for shape in copy.shapes:
        shape.id = session.new_id("sh")
    for block in copy.blocks:
        block.id = session.new_id("bk")

    at = slide.index + 1 if index is None else max(0, min(index, len(session.deck.slides)))
    copy.index = at

    with session.transaction(author) as turn:
        session.store.write(turn, "add_slide", copy.id,
                            {"index": at, "slide": copy.model_dump(mode="json")}, author)

    return Diff(summary=f"duplicated {slide_id} as {copy.id}", target=copy.id,
                after={"id": copy.id, "index": at},
                render=session.measure_slide(copy.id)).as_result()


@tool("hide_slide", "Skip a slide in the slideshow without deleting it.",
      obj({"slide_id": string("Slide id"),
           "hidden": {"type": "boolean", "description": "Hide it, or show it again"}},
          ["slide_id"]),
      mutating=True)
def hide_slide(session: Session, slide_id: str, hidden: bool = True,
               author: Author = Author.MODEL) -> dict[str, Any]:
    """Hidden, not removed. The slide stays in the deck and in the file; only presentation
    skips it, which is what someone means by "drop this for now"."""
    slide = session.slide(slide_id)
    if slide.hidden == hidden:
        raise ToolError("already_there",
                        f"{slide_id} is already {'hidden' if hidden else 'visible'}")

    with session.transaction(author) as turn:
        session.store.write(turn, "set_slide_props", slide_id,
                            {"slide_id": slide_id, "hidden": hidden}, author)

    return Diff(summary=f"{'hid' if hidden else 'unhid'} {slide_id}", target=slide_id,
                after={"hidden": hidden}).as_result()


@tool("set_layout", "Switch a managed slide to a different layout frame.",
      obj({"slide_id": string("Slide id"),
           "layout": string("Layout frame", list(registry.LAYOUTS))},
          ["slide_id", "layout"]),
      gate="managed", mutating=True)
def set_layout(session: Session, slide_id: str, layout: str,
               author: Author = Author.MODEL) -> dict[str, Any]:
    slide = session.slide(slide_id)
    if slide.mode is not Mode.MANAGED:
        raise ToolError("wrong_mode",
                        f"{slide_id} is freeform — it has no layout frame. Its shapes are "
                        "placed where the original author put them.")
    try:
        frame = registry.layout(layout)
    except KeyError as exc:
        raise ToolError("unknown_layout", str(exc)) from exc

    orphaned = [b.id for b in slide.blocks if b.region not in frame.regions]
    if orphaned:
        raise ToolError(
            "region_missing",
            f"{layout} has no region for block(s) {orphaned}; it has "
            f"{sorted(frame.regions)}. Move or remove them first — dropping a block "
            "silently would lose content.",
        )

    before = slide.layout
    with session.transaction(author) as turn:
        session.store.write(turn, "set_slide_props", slide_id,
                            {"slide_id": slide_id, "layout": layout}, author)

    return Diff(summary=f"set {slide_id} to the {layout} layout", target=slide_id,
                before={"layout": before}, after={"layout": layout},
                render=session.measure_slide(slide_id)).as_result()


@tool("set_slide_size", "Resize the canvas. Reports what this invalidates.",
      obj({"preset": string("Canvas size", list(SLIDE_SIZES))}, ["preset"]),
      mutating=True)
def set_slide_size(session: Session, preset: str,
                   author: Author = Author.MODEL) -> dict[str, Any]:
    """Resizing invalidates every absolute frame in the deck.

    Imported shapes are placed in EMU measured against the old canvas. Rescaling them
    silently would produce a deck that looks right here and wrong to whoever opens it, so
    this reports the count and leaves the judgement to a person.
    """
    if preset not in SLIDE_SIZES:
        raise ToolError("unknown_size", f"{preset!r} is not one of {list(SLIDE_SIZES)}")

    width_in, height_in = SLIDE_SIZES[preset]
    canvas = (round(width_in * 96), round(height_in * 96))
    if tuple(session.theme.grid.canvas) == canvas:
        raise ToolError("already_there", f"the deck is already {preset}")

    theme = session.theme.model_copy(deep=True)
    theme.grid = theme.grid.model_copy(update={"canvas": canvas})

    affected = sum(1 for s in session.deck.slides if s.mode is Mode.FREEFORM
                   for x in s.shapes if not x.opaque)

    before = tuple(session.theme.grid.canvas)
    with session.transaction(author) as turn:
        session.store.write(turn, "set_theme", session.deck.id,
                            {"theme": theme.model_dump(mode="json")}, author)

    result = Diff(summary=f"set the canvas to {preset} ({canvas[0]}x{canvas[1]})",
                  target=session.deck.id,
                  before={"canvas": list(before)},
                  after={"canvas": list(canvas)}).as_result()
    if affected:
        result["invalidated"] = affected
        result["note"] = (
            f"{affected} imported shapes are positioned against the previous canvas and "
            "were not rescaled. Run lint to see what no longer fits."
        )
    return result


@tool("set_theme_role", "Correct a theme colour role — often one the importer guessed.",
      obj({"role": string("Palette role, e.g. brand or ink_muted"),
           "value": string("Colour as #RRGGBB")},
          ["role", "value"]),
      mutating=True)
def set_theme_role(session: Session, role: str, value: str,
                   author: Author = Author.MODEL) -> dict[str, Any]:
    """A role, and the theme is re-validated as a whole.

    The theme is checked once at load so managed slides *cannot* fail contrast. A role
    changed without rechecking would quietly retire that guarantee, so a change that breaks
    a contrast pair is refused with the ratio it would have produced.
    """
    palette = session.theme.palette
    if role not in palette:
        raise ToolError("unknown_role",
                        f"no palette role {role!r}; the theme has "
                        f"{sorted(k for k, v in palette.items() if isinstance(v, str))}")
    if isinstance(palette[role], list):
        raise ToolError("not_a_single_colour",
                        f"{role!r} is an ordered list; set_theme_role changes one colour")

    colour = value.strip().upper()
    if not (colour.startswith("#") and len(colour) == 7):
        raise ToolError("bad_colour", f"{value!r} is not a #RRGGBB colour")
    try:
        int(colour[1:], 16)
    except ValueError as exc:
        raise ToolError("bad_colour", f"{value!r} is not a #RRGGBB colour") from exc

    theme = session.theme.model_copy(deep=True)
    updated = {**palette, role: colour}

    # Some roles exist only in relation to another. Changing `brand` and leaving `brand_ink`
    # behind fails contrast against a colour nobody chose — so the dependent role is
    # re-derived unless it is the one being set.
    for background, foreground in (("brand", "brand_ink"), ("bg", "ink_muted")):
        if role != background or not isinstance(updated.get(foreground), str):
            continue
        dark, light = str(updated.get("ink", "#000000")), str(updated.get("bg", "#FFFFFF"))
        picked = dark if contrast(dark, colour) >= contrast(light, colour) else light
        if contrast(str(updated[foreground]), colour) < 4.5:
            updated[foreground] = picked

    theme.palette = updated
    theme.inferred = [f for f in theme.inferred if f != f"palette.{role}"]

    problems = validate_theme(theme)
    if problems:
        raise ToolError("contrast_failed",
                        f"{colour} on {role} breaks the theme: " + "; ".join(problems),
                        problems=problems)

    before = palette[role]
    with session.transaction(author) as turn:
        session.store.write(turn, "set_theme", session.deck.id,
                            {"theme": theme.model_dump(mode="json")}, author)

    return Diff(summary=f"set {role} to {colour}", target=session.deck.id,
                before={role: before}, after={role: colour}).as_result()
