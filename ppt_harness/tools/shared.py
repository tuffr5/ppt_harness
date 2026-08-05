"""Tools available in both modes — DESIGN §4.

Reads, `set_text`, slide-level structure, export, and history. `set_text` is the one that
matters in v0: it is budget-checked in *both* modes, which is what makes "fix this wording
on slide 4" safe on a deck the harness did not create.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..components import registry
from ..core.session import Session
from ..io import export_mutate
from ..render import budget as budget_mod
from ..state import richtext
from ..state.document import Author, Mode
from .base import Diff, ToolError, integer, obj, string, tool

# ------------------------------------------------------------------------- reads


@tool("get_outline", "Every slide's index, mode, and gist. Cheap; call it first.",
      obj({}))
def get_outline(session: Session) -> dict[str, Any]:
    return session.outline()


@tool("get_slide", "Full contents of one slide: blocks and slots, or shapes.",
      obj({"slide_id": string("Slide id from get_outline")}, ["slide_id"]))
def get_slide(session: Session, slide_id: str) -> dict[str, Any]:
    slide = session.slide(slide_id)
    body: dict[str, Any] = {"id": slide.id, "index": slide.index, "mode": slide.mode.value,
                            "notes": slide.notes}
    if slide.mode is Mode.MANAGED:
        body["layout"] = slide.layout
        body["blocks"] = [
            {"id": b.id, "region": b.region, "component": b.component,
             "variant": b.variant, "slots": b.slots, "overrides": b.overrides}
            for b in slide.blocks
        ]
    else:
        body["shapes"] = [
            {"id": s.id, "type": s.type, "role": s.role, "text": s.text,
             "opaque": s.opaque, "editable": not s.opaque and s.text is not None}
            for s in slide.shapes
        ]
    body["render"] = session.measure_slide(slide_id)
    return body


@tool("get_theme", "Theme roles, type scale, and grid. `inferred` lists guessed fields.",
      obj({}))
def get_theme(session: Session) -> dict[str, Any]:
    theme = session.theme
    return {
        **theme.model_dump(mode="json"),
        "problems": session.theme_problems,
    }


@tool("list_components",
      "Component catalog. Without a key, one line each; with a key, the full slot schema.",
      obj({"key": string("Component key, e.g. 'bullets'")}))
def list_components(session: Session, key: str | None = None) -> dict[str, Any]:
    if key:
        return registry.describe(key)
    return {"components": registry.catalog(),
            "layouts": {k: {"purpose": v.purpose, "regions": sorted(v.regions)}
                        for k, v in registry.LAYOUTS.items()}}


@tool("get_budget", "How much text a target can hold, in characters and ems.",
      obj({"target": string("slide/shape or slide/block/slot")}, ["target"]))
def get_budget(session: Session, target: str) -> dict[str, Any]:
    b = session.budget_for(target)
    return {"target": target, "capacity_em": round(b.capacity_em, 1),
            "max_lines": b.max_lines, "hint_chars": b.hint(session.theme),
            "type_role": b.spec.family, "size_px": b.spec.size}


# ------------------------------------------------------------------------ writes


@tool("set_text", "Replace the text of a shape or slot. Budget-checked before it lands.",
      obj({"target": string("slide/shape or slide/block/slot"),
           "text": string("Replacement text")}, ["target", "text"]),
      mutating=True)
def set_text(
    session: Session, target: str, text: str, author: Author = Author.MODEL
) -> dict[str, Any]:
    slide, holder, slot = session.store.resolve_text_target(target)

    if slot is None and holder.opaque:
        raise ToolError(
            "shape_opaque",
            f"{target} is a {holder.type} the harness does not model (SmartArt, a group, "
            "or an embedded object). It is preserved exactly on export, but its text "
            "cannot be edited here.",
        )

    ways_out: list[str] = []
    if slot is not None:
        ways_out = budget_mod.ways_out_for_block(holder.component, holder.variant)
    else:
        # No component to degrade, so the ways out reshape the container instead. `check`
        # prepends the specific "shorten ~N chars", so a generic "shorten" here would only
        # repeat it.
        ways_out = ["widen the shape", "split across two shapes"]

    # Measure what will render, not what was typed. `<u>under</u>` is five characters on
    # the slide and thirteen in the argument, and budgeting the markup rejects text that
    # fits comfortably.
    rendered = richtext.to_plain(richtext.parse(text))

    b = session.budget_for(target)
    result = budget_mod.check(rendered, b, session.theme, ways_out)
    if not result.ok:
        raise ToolError("budget_exceeded", result.error(session.theme),
                        capacity_em=round(b.capacity_em, 1),
                        used_em=round(result.used_em, 1),
                        hint_chars=b.hint(session.theme))

    before = holder.text if slot is None else holder.slots.get(slot)
    with session.transaction(author) as turn:
        session.store.write(turn, "set_text", target, {"text": text}, author)

    return Diff(
        summary=f"set text on {target}",
        target=target,
        before=before,
        after=rendered,
        render=session.measure_slide(slide.id),
    ).as_result()


@tool("reorder", "Move a slide to a new index.",
      obj({"slide_id": string("Slide id"), "index": integer("New 0-based index")},
          ["slide_id", "index"]), mutating=True)
def reorder(
    session: Session, slide_id: str, index: int, author: Author = Author.MODEL
) -> dict[str, Any]:
    slide = session.slide(slide_id)
    before = slide.index
    if not 0 <= index < len(session.deck.slides):
        raise ToolError("out_of_range",
                        f"index {index} is outside 0..{len(session.deck.slides) - 1}")
    with session.transaction(author) as turn:
        session.store.write(turn, "reorder", slide_id,
                            {"slide_id": slide_id, "index": index}, author)
    return Diff(summary=f"moved {slide_id} from {before} to {index}", target=slide_id,
                before=before, after=index).as_result()


@tool("delete_slide", "Remove a slide.",
      obj({"slide_id": string("Slide id")}, ["slide_id"]), mutating=True)
def delete_slide(session: Session, slide_id: str, author: Author = Author.MODEL) -> dict[str, Any]:
    slide = session.slide(slide_id)
    if slide.mode is Mode.FREEFORM and session.deck.source_path:
        raise ToolError(
            "freeform_slide_not_removable",
            f"{slide_id} came from {Path(session.deck.source_path).name}. v0 patches the "
            "original package rather than rebuilding it, so imported slides cannot be "
            "deleted without rewriting the file wholesale.",
        )
    with session.transaction(author) as turn:
        session.store.write(turn, "delete_slide", slide_id, {"slide_id": slide_id}, author)
    return Diff(summary=f"deleted {slide_id}", target=slide_id).as_result()


# ------------------------------------------------------------------ verification


@tool("render", "Measure a slide: boxes, line counts, and overflow.",
      obj({"slide_id": string("Slide id")}, ["slide_id"]))
def render(session: Session, slide_id: str) -> dict[str, Any]:
    return session.measure_slide(slide_id)


@tool("lint", "Check every slide, or one, for overflow and theme problems.",
      obj({"slide_id": string("Slide id; omit for the whole deck")}))
def lint(session: Session, slide_id: str | None = None) -> dict[str, Any]:
    targets = [slide_id] if slide_id else [s.id for s in session.deck.slides]
    findings = []
    for sid in targets:
        measurement = session.measure_slide(sid)
        if not measurement["clean"]:
            findings.append(measurement)
    return {"checked": len(targets), "problems": findings,
            "theme_problems": session.theme_problems,
            "clean": not findings and not session.theme_problems}


@tool("review_deck",
      "Editorial review: titles that file rather than state, crowded lists, prose that "
      "belongs in the notes, and style that drifts between slides. Advisory — nothing here "
      "blocks a write, and lint is the measured one.",
      obj({"slide_id": string("Slide id; omit for the whole deck"),
           "include_raised": {"type": "boolean",
                              "description": "Repeat findings already returned this "
                                             "session"}}))
def review_deck(session: Session, slide_id: str | None = None,
                include_raised: bool = False) -> dict[str, Any]:
    """`lint`'s sibling on the axis nothing measures.

    Returning a finding counts as raising it, and it is not offered again unless asked for.
    That is a real trade — a model can be handed a finding and never mention it, and the
    user never hears it — but the alternative failure is worse and far more likely: a
    channel that repeats itself every turn is one people learn to skip, and then the good
    findings go unread along with the rest. `suppressed` keeps the loss visible, and
    `include_raised` gets everything back.
    """
    from ..core import review as editorial

    findings = editorial.review(session.deck)
    if slide_id:
        session.slide(slide_id)  # raises if it does not exist, before reporting nothing
        findings = [f for f in findings if f.slide_id == slide_id]

    fresh = [f for f in findings if include_raised or f.key not in session.raised]
    session.raised.update(f.key for f in fresh)

    return {
        "checked": len(session.deck.slides) if not slide_id else 1,
        "findings": [f.as_dict() for f in fresh],
        "suppressed": len(findings) - len(fresh),
        "clean": not findings,
        "advisory": "Judgement, not measurement. Say what is worth saying and let them "
                    "decide; none of this refuses a write.",
    }


@tool("export", "Write the deck to a .pptx. Fidelity is asserted before it returns.",
      obj({"path": string("Destination path"),
           "mode": string("Export mode", ["editable", "pixel_locked"])}, ["path"]))
def export(session: Session, path: str, mode: str = "editable") -> dict[str, Any]:
    report = export_mutate.export(session.deck, path, mode=mode, strict=False,  # type: ignore[arg-type]
                                  assets=session.assets)
    return {
        "ok": report.ok,
        "path": str(report.path),
        "summary": report.summary(),
        "slides": report.slides_written,
        "patched": report.shapes_patched,
        "added": report.shapes_added,
        "violations": [str(v) for v in report.violations],
    }


# ---------------------------------------------------------------------- history


@tool("eject_slide",
      "Turn a managed slide into freeform shapes. One-way: the components are gone after.",
      obj({"slide_id": string("Slide id")}, ["slide_id"]),
      gate="managed", mutating=True)
def eject_slide(session: Session, slide_id: str,
                author: Author = Author.MODEL) -> dict[str, Any]:
    """Lossless, and one-way.

    The expander already decided every box; this writes those numbers down. What is lost is
    the ability to re-derive them — which is why the door only opens one way, and why the
    catalog exists so this stays rare.
    """
    from ..io import adopt as adoption

    slide = session.slide(slide_id)
    if slide.mode is not Mode.MANAGED:
        raise ToolError("wrong_mode", f"{slide_id} is already freeform")

    cx, cy = session.slide_size_emu()
    shapes = adoption.eject(slide, session.theme, cx, cy, session.assets)
    if not shapes:
        raise ToolError("nothing_to_eject", f"{slide_id} has no filled slots")

    with session.transaction(author) as turn:
        session.store.write(turn, "set_slide_props", slide_id,
                            {"slide_id": slide_id, "mode": Mode.FREEFORM,
                             "blocks": [], "layout": None,
                             "shapes": [s.model_dump(mode="json") for s in shapes]},
                            author)

    return Diff(summary=f"ejected {slide_id} to {len(shapes)} freeform shapes",
                target=slide_id, after={"mode": "freeform", "shapes": len(shapes)},
                render=session.measure_slide(slide_id)).as_result()


@tool("adopt_slide",
      "Propose turning an imported slide into components. Reflows it, so it asks first.",
      obj({"slide_id": string("Slide id"),
           "confirm": {"type": "boolean",
                       "description": "Omit to see the proposal; true to apply it"}},
          ["slide_id"]),
      gate="freeform", mutating=True)
def adopt_slide(session: Session, slide_id: str, confirm: bool = False,
                author: Author = Author.MODEL) -> dict[str, Any]:
    """A proposal, never an inference — DESIGN §7.

    The classifier reads arrangement, not meaning: it recognises four evenly-spaced boxes
    with big numbers, it does not know what they mean. Acting on that reflows the slide, so
    without `confirm` this returns the before/after and changes nothing.
    """
    from ..io import adopt as adoption

    slide = session.slide(slide_id)
    if slide.mode is not Mode.FREEFORM:
        raise ToolError("wrong_mode", f"{slide_id} is already managed")

    plan = adoption.proposal(slide, session.theme)
    if plan is None:
        raise ToolError(
            "not_recognised",
            f"the arrangement of {slide_id} does not clearly match a component. Leaving it "
            "freeform is the right outcome — its shapes still edit normally.",
        )
    if not confirm:
        return {"ok": True, "summary": f"proposal for {slide_id}", "proposal": plan,
                "applied": False,
                "next": "call again with confirm=true to apply it"}

    blocks = adoption.adopted_blocks(slide, session.theme, session.new_id)
    if blocks is None:  # pragma: no cover - proposal already gated on the same check
        raise ToolError("not_recognised", "the classifier is no longer confident")

    with session.transaction(author) as turn:
        session.store.write(turn, "set_slide_props", slide_id,
                            {"slide_id": slide_id, "mode": Mode.MANAGED,
                             "layout": "stack", "shapes": [],
                             "blocks": [b.model_dump(mode="json") for b in blocks]},
                            author)

    return Diff(summary=f"adopted {slide_id} as {plan['component']}", target=slide_id,
                before={"mode": "freeform"},
                after={"mode": "managed", "component": plan["component"]},
                render=session.measure_slide(slide_id)).as_result()


@tool("undo", "Undo the last turn. A turn is one request, not one tool call.", obj({}),
      mutating=True)
def undo(session: Session) -> dict[str, Any]:
    if not session.store.undo():
        # "Nothing to undo" is a fact, not a fault. Saying so stops a model retrying, which
        # a bare `ok: false` invites.
        raise ToolError("nothing_to_undo",
                        "there is no committed turn to undo; the deck is as it was opened")
    return {"ok": True, "summary": "undid the last turn", "outline": session.outline()}


@tool("redo", "Redo the last undone turn.", obj({}), mutating=True)
def redo(session: Session) -> dict[str, Any]:
    if not session.store.redo():
        raise ToolError("nothing_to_redo",
                        "there is no undone turn to redo; undo something first")
    return {"ok": True, "summary": "redid the last undone turn",
            "outline": session.outline()}
