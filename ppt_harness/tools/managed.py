"""Managed-slide tools — DESIGN §4.

Note what is absent: `set_position`, `set_font`, `add_textbox`. A model working on a managed
slide names a component, a variant, and content; the expander owns every coordinate. That
is DESIGN's first principle, expressed as the shape of an API rather than as a rule someone
has to remember.
"""

from __future__ import annotations

from typing import Any

from ..components import overrides as overrides_mod
from ..components import registry
from ..core.session import Session
from ..render import budget as budget_mod
from ..state.document import Author, Block, Mode, Slide
from .base import Diff, ToolError, integer, obj, string, tool


def _require_managed(session: Session, slide_id: str) -> Slide:
    slide = session.slide(slide_id)
    if slide.mode is not Mode.MANAGED:
        raise ToolError(
            "wrong_mode",
            f"{slide_id} is freeform — it came from an imported file, so the harness holds "
            "its original shapes rather than components. Use set_text to edit its text.",
            mode=slide.mode.value,
        )
    return slide


#: What a structured slot has to look like before the writer will get anything out of it,
#: and the shape to quote back when it does not — DESIGN §1.5 (canonical slot shapes).
#:
#: Only the shapes the writer builds an *object* from are here. `title`, `prose` and `list`
#: all reach the file through the text path, which flattens whatever it is given, so a
#: mistyped one lands as text rather than as nothing; these three land as nothing at all.
#: Each entry is the keys the writer needs, as groups of alternatives — a group is satisfied
#: by any one of its names. `chart` reads `categories` or `labels` and this has to accept
#: both, or the gate would refuse payloads the writer builds happily. `kind` is absent
#: because the writer defaults it; a gate stricter than the writer is its own bug.
_STRUCTURED: dict[str, tuple[tuple[tuple[str, ...], ...], str]] = {
    "tabular": ((("headers",), ("rows",)),
                '{"headers": [...], "rows": [[...], ...]}'),
    "chart": ((("categories", "labels"), ("series",)),
              '{"kind": "bar", "categories": [...], '
              '"series": [{"name": ..., "values": [...]}]}'),
}


def _check_structured(component: str, slots: dict[str, Any]) -> None:
    """A `tabular` or `chart` slot carries the object the writer builds from, or it is refused.

    Found by generating a deck: a model handed `data_table.tabular` a list of pre-joined
    strings, `add_slide` said `ok`, budget-checked it, measured it, and returned a render —
    and the deck then failed to export with `slot_not_written`, having been certified twice
    on the way. The slot's *shape* was the one thing nothing checked.

    That ordering is the fault. "A write is checked before it lands" is the harness's claim,
    and a write that lands and cannot be written is a worse failure than a refusal, because
    the model has already been told it succeeded and moved on. Same argument as `_check_media`
    below: refuse at the gate, name the shape, and let the cheapest failure be the one that
    never rendered.

    Keys only, never their contents. Whether `rows` is rectangular or a series has as many
    values as there are labels is the writer's business, and a gate that tried to decide it
    here would be a second implementation of the writer, free to disagree with it.
    """
    comp = registry.get(component)
    for name, value in slots.items():
        spec = comp.slots.get(name)
        if spec is None or not value:
            continue
        expected = _STRUCTURED.get(spec.shape)
        if expected is None:
            continue
        groups, example = expected
        missing = [" or ".join(g) for g in groups
                   if not isinstance(value, dict) or not any(value.get(k) for k in g)]
        if missing:
            got = type(value).__name__
            raise ToolError(
                "bad_slot_shape",
                f"{component}.{name} is a {spec.shape} slot and takes {example}; "
                f"got {got} missing {missing}. The writer builds a real "
                f"{'table' if spec.shape == 'tabular' else 'chart'} from those keys, so "
                "text arranged to look like one cannot be written at all.",
            )


def _check_media(component: str, slots: dict[str, Any]) -> None:
    """A `media` slot names a picture and describes it. Both, or the write is refused.

    Refused here rather than reported at export for the reason `add_slide` budgets before it
    writes: a rejected write is the cheapest possible failure, and the alternative is a model
    told `ok` and a slide whose picture turns up missing in the exported file.

    Alt text is required for the same reason `add_image` has required it since it shipped —
    a deck that cannot be read aloud is a deck part of the audience cannot use — and the
    managed path is not a loophole in that. Whether the key is behind anything *yet* is not
    checked: nothing here can see the deck's assets, and authoring the slides before the
    pictures is an ordinary way to work. A key behind nothing at export time is reported as
    `slot_not_written`, which is where the writer already says what it could not build.

    A key that is plainly a *path*, though, is refused on sight. It is never a valid key —
    keys cannot contain a separator — and it is the one mistake the removal of the
    `asset_id`-as-path fallback (see `io/media.py`) has made silent: it used to work on the
    machine that wrote it and nowhere else, so a model that learned the habit deserves to be
    told rather than left to read a violation at export.
    """
    comp = registry.get(component)
    for name, value in slots.items():
        spec = comp.slots.get(name)
        if spec is None or spec.shape != "media" or not value:
            continue
        payload = value if isinstance(value, dict) else {}
        asset_id = str(payload.get("asset_id") or "").strip()
        if asset_id and ("/" in asset_id or "\\" in asset_id or asset_id.startswith("~")):
            raise ToolError(
                "asset_is_a_path",
                f"{component}.{name} names {asset_id!r}, which is a path, not an asset key. "
                "Call add_asset with that path and use the key it returns — a deck has to "
                "carry its own pictures, or the picture is gone the moment the file is.",
            )
        if not asset_id:
            raise ToolError(
                "media_needs_asset",
                f"{component}.{name} is a picture slot; it takes "
                '{"asset_id": ..., "alt": ...}, where asset_id is the key add_asset '
                "returned. A path is not one: the deck carries its own pictures, so that a "
                "file mailed to somebody else still has them.",
            )
        if not str(payload.get("alt") or "").strip():
            raise ToolError(
                "alt_required",
                f"{component}.{name} needs alt text: a picture with none is invisible to "
                "anyone using a screen reader",
            )


def _check_block(session: Session, slide: Slide, block: Block) -> list[str]:
    """Budget every filled slot of a block. Returns human-readable failures."""
    from ..render import expand

    problems = []
    for laid_out in expand.expand_slide(session.theme, slide):
        if laid_out.block_id != block.id:
            continue
        value = block.slots.get(laid_out.slot)
        if not value:
            continue
        b = budget_mod.for_slot(session.theme, laid_out)
        ways = budget_mod.ways_out_for_block(block.component, block.variant)
        result = budget_mod.check_value(value, b, session.theme, ways)
        if not result.ok:
            problems.append(result.error(session.theme))
    return problems


@tool("add_slide", "Insert a managed slide built from components and the deck's theme.",
      obj({
          "index": integer("Where to insert; omit to append"),
          "layout": string("Layout frame", list(registry.LAYOUTS)),
          "blocks": {
              "type": "array",
              "description": "Ordered blocks. Each names a region, component, variant, slots.",
              "items": obj({
                  "region": string("Region of the layout"),
                  "component": string("Component key", list(registry.COMPONENTS)),
                  "variant": string("Variant name; omit for the default"),
                  "slots": {"type": "object", "description": "Slot contents"},
              }, ["region", "component", "slots"]),
          },
          "notes": string("Speaker notes"),
      }, ["layout", "blocks"]),
      gate="managed", mutating=True)
def add_slide(
    session: Session,
    layout: str,
    blocks: list[dict[str, Any]],
    index: int | None = None,
    notes: str = "",
    author: Author = Author.MODEL,
) -> dict[str, Any]:
    try:
        frame = registry.layout(layout)
    except KeyError as exc:
        raise ToolError("unknown_layout", str(exc)) from exc

    built: list[Block] = []
    for spec in blocks:
        key = spec["component"]
        try:
            comp = registry.get(key)
        except KeyError as exc:
            raise ToolError("unknown_component", str(exc)) from exc
        region = spec["region"]
        if region not in frame.regions:
            raise ToolError(
                "bad_region",
                f"layout {layout!r} has no region {region!r}; it has "
                f"{sorted(frame.regions)}",
            )
        if region not in comp.regions:
            raise ToolError(
                "component_not_allowed_here",
                f"{key} cannot fill {region!r}; it fits {sorted(comp.regions)}. "
                f"Components for {region!r}: {registry.components_for(region)}",
            )
        variant = spec.get("variant") or comp.default_variant
        if variant not in comp.variants:
            raise ToolError("unknown_variant",
                            f"{key} has no variant {variant!r}; it has {sorted(comp.variants)}")
        unknown = set(spec["slots"]) - set(comp.slots)
        if unknown:
            raise ToolError("unknown_slot",
                            f"{key} has no slot(s) {sorted(unknown)}; it has {sorted(comp.slots)}")
        missing = [n for n, s in comp.slots.items() if s.required and not spec["slots"].get(n)]
        if missing:
            raise ToolError("missing_slot", f"{key} requires {missing}")
        _check_media(key, spec["slots"])
        _check_structured(key, spec["slots"])
        built.append(Block(id=session.new_id("bk"), region=region, component=key,
                           variant=variant, slots=spec["slots"]))

    at = len(session.deck.slides) if index is None else max(0, min(index, len(session.deck.slides)))
    slide = Slide(id=session.new_id("sl"), index=at, mode=Mode.MANAGED, layout=layout,
                  blocks=built, notes=notes)

    # Budget before writing: a rejected write is the cheapest possible failure.
    problems = [p for block in built for p in _check_block(session, slide, block)]
    if problems:
        raise ToolError("budget_exceeded", "\n\n".join(problems))

    with session.transaction(author) as turn:
        session.store.write(turn, "add_slide", slide.id,
                            {"index": at, "slide": slide.model_dump(mode="json")}, author)

    return Diff(summary=f"added {layout} slide at {at}", target=slide.id,
                after={"id": slide.id, "blocks": [b.component for b in built]},
                render=session.measure_slide(slide.id)).as_result()


@tool("set_override",
      "Nudge one block — density, emphasis, alignment, accent or media scale. Clamped.",
      obj({"slide_id": string("Slide id"), "block_id": string("Block id"),
           "key": string("Which knob", list(overrides_mod.OVERRIDES)),
           "value": {"description": "A step on that knob's scale; clamped if out of range"}},
          ["slide_id", "block_id", "key", "value"]),
      gate="managed", mutating=True)
def set_override(session: Session, slide_id: str, block_id: str, key: str, value: Any,
                 author: Author = Author.MODEL) -> dict[str, Any]:
    """The pressure valve — DESIGN §3.

    Without one, "make it a bit tighter" becomes a reason to eject the slide, and an ejected
    slide is one the harness can no longer reason about. Every value here resolves through
    the theme, so a nudged block still belongs to the same deck.
    """
    slide = _require_managed(session, slide_id)
    block = slide.block(block_id)
    if block is None:
        raise ToolError("no_block", f"slide {slide_id} has no block {block_id!r}")
    if key not in overrides_mod.OVERRIDES:
        raise ToolError("unknown_override",
                        f"no override {key!r}; the knobs are "
                        f"{list(overrides_mod.OVERRIDES)}")

    knob = overrides_mod.OVERRIDES[key]
    clamped = knob.clamp(value)
    if block.overrides.get(key, knob.default) == clamped:
        # Say *why* it changes nothing. Asking for 9.0 and being told "already 1.0" is
        # only confusing until you know 9.0 was clamped to 1.0 on the way in.
        because = (f" ({value!r} clamps to {clamped!r}; the range is "
                   f"{min(knob.values)}..{max(knob.values)})") if clamped != value else ""
        raise ToolError("already_there",
                        f"{key} is already {clamped!r} on {block_id}{because}")

    before = dict(block.overrides)
    merged = overrides_mod.clamp_all({**block.overrides, key: clamped})

    probe = slide.model_copy(deep=True)
    probe.blocks = [b.model_copy(update={"overrides": merged}) if b.id == block_id else b
                    for b in probe.blocks]
    trial = probe.block(block_id)
    problems = _check_block(session, probe, trial) if trial else []
    if problems:
        raise ToolError("budget_exceeded", "\n\n".join(problems))

    with session.transaction(author) as turn:
        session.store.write(turn, "set_block_props", block_id,
                            {"slide_id": slide_id, "block_id": block_id,
                             "overrides": merged}, author)

    result = Diff(summary=f"set {key}={clamped!r} on {block_id}", target=block_id,
                  before={"overrides": before}, after={"overrides": merged},
                  render=session.measure_slide(slide_id)).as_result()
    if clamped != value:
        result["clamped_from"] = value
        result["note"] = f"{value!r} is outside {key}'s range; used {clamped!r}"
    return result


@tool("repair", "Fix an overflowing managed slide by reshaping it, never by rewriting it.",
      obj({"slide_id": string("Slide id")}, ["slide_id"]),
      gate="managed", mutating=True)
def repair(session: Session, slide_id: str,
           author: Author = Author.LINT) -> dict[str, Any]:
    """Walk the ladder: variant, then density, then the degradation chain.

    Content is the rung it will not climb. If structure cannot absorb the overflow, this
    reports how much has to go and who may decide — words the model wrote are fair game,
    words a person wrote are not.
    """
    from ..core import repair as ladder

    session.slide(slide_id)  # refuses an unknown id with the usual message
    outcome = ladder.repair(session, slide_id, author)

    return Diff(summary=("repaired " if outcome.fixed else "could not fully repair ")
                + slide_id,
                target=slide_id,
                after=outcome.as_dict(),
                render=session.measure_slide(slide_id)).as_result()


@tool("set_slots", "Patch some slots of a block. Budget-checked.",
      obj({"slide_id": string("Slide id"), "block_id": string("Block id"),
           "patch": {"type": "object", "description": "Slot names to new values"}},
          ["slide_id", "block_id", "patch"]),
      gate="managed", mutating=True)
def set_slots(session: Session, slide_id: str, block_id: str, patch: dict[str, Any],
              author: Author = Author.MODEL) -> dict[str, Any]:
    slide = _require_managed(session, slide_id)
    block = slide.block(block_id)
    if block is None:
        raise ToolError("no_block", f"slide {slide_id} has no block {block_id!r}")
    comp = registry.get(block.component)
    unknown = set(patch) - set(comp.slots)
    if unknown:
        raise ToolError("unknown_slot",
                        f"{block.component} has no slot(s) {sorted(unknown)}")
    _check_media(block.component, patch)
    _check_structured(block.component, patch)

    before = dict(block.slots)
    trial = Block(id=block.id, region=block.region, component=block.component,
                  variant=block.variant, slots={**block.slots, **patch},
                  overrides=block.overrides)
    probe = slide.model_copy(deep=True)
    probe.blocks = [trial if b.id == block.id else b for b in probe.blocks]
    problems = _check_block(session, probe, trial)
    if problems:
        raise ToolError("budget_exceeded", "\n\n".join(problems))

    with session.transaction(author) as turn:
        for name, value in patch.items():
            if isinstance(value, str):
                session.store.write(turn, "set_text", f"{slide_id}/{block_id}/{name}",
                                    {"text": value}, author)
            else:
                block.slots[name] = value  # non-text slots carry no invertible op in v0

    return Diff(summary=f"patched {sorted(patch)} on {block_id}", target=block_id,
                before=before, after=dict(block.slots),
                render=session.measure_slide(slide_id)).as_result()


@tool("set_variant", "Switch a block to a different variant of the same component.",
      obj({"slide_id": string("Slide id"), "block_id": string("Block id"),
           "variant": string("Variant name")}, ["slide_id", "block_id", "variant"]),
      gate="managed", mutating=True)
def set_variant(session: Session, slide_id: str, block_id: str, variant: str,
                author: Author = Author.MODEL) -> dict[str, Any]:
    slide = _require_managed(session, slide_id)
    block = slide.block(block_id)
    if block is None:
        raise ToolError("no_block", f"slide {slide_id} has no block {block_id!r}")
    comp = registry.get(block.component)
    if variant not in comp.variants:
        raise ToolError("unknown_variant",
                        f"{block.component} has no variant {variant!r}; "
                        f"it has {sorted(comp.variants)}")
    before = block.variant
    with session.transaction(author) as turn:
        session.store.write(turn, "set_block_props", block_id,
                            {"slide_id": slide_id, "block_id": block_id,
                             "variant": variant}, author)
    return Diff(summary=f"{block_id}: {before} -> {variant}", target=block_id,
                before=before, after=variant,
                render=session.measure_slide(slide_id)).as_result()


@tool("set_component", "Swap a block's component. Legal only within the same slot shapes.",
      obj({"slide_id": string("Slide id"), "block_id": string("Block id"),
           "component": string("Component key")}, ["slide_id", "block_id", "component"]),
      gate="managed", mutating=True)
def set_component(session: Session, slide_id: str, block_id: str, component: str,
                  author: Author = Author.MODEL) -> dict[str, Any]:
    slide = _require_managed(session, slide_id)
    block = slide.block(block_id)
    if block is None:
        raise ToolError("no_block", f"slide {slide_id} has no block {block_id!r}")
    try:
        target = registry.get(component)
    except KeyError as exc:
        raise ToolError("unknown_component", str(exc)) from exc

    current = registry.get(block.component)
    # Swaps are legal only *within* a slot shape — that is what makes them content-preserving
    # by construction rather than by a migration nobody wrote (DESIGN §1.5).
    if current.slot_shapes() != target.slot_shapes():
        raise ToolError(
            "slot_shape_mismatch",
            f"{block.component} consumes {current.slot_shapes()} but {component} consumes "
            f"{target.slot_shapes()}. A swap across slot shapes would silently drop or "
            "invent content; rebuild the block instead.",
        )
    before = block.component
    # Component and variant in one op: the swap resets the variant, and an undo that put
    # back the component but not the variant would leave a block its component disowns.
    with session.transaction(author) as turn:
        session.store.write(turn, "set_block_props", block_id,
                            {"slide_id": slide_id, "block_id": block_id,
                             "component": component,
                             "variant": target.default_variant}, author)
    return Diff(summary=f"{block_id}: {before} -> {component}", target=block_id,
                before=before, after=component,
                render=session.measure_slide(slide_id)).as_result()


@tool("remove_block", "Delete a block from a managed slide.",
      obj({"slide_id": string("Slide id"), "block_id": string("Block id")},
          ["slide_id", "block_id"]), gate="managed", mutating=True)
def remove_block(session: Session, slide_id: str, block_id: str,
                 author: Author = Author.MODEL) -> dict[str, Any]:
    slide = _require_managed(session, slide_id)
    block = slide.block(block_id)
    if block is None:
        raise ToolError("no_block", f"slide {slide_id} has no block {block_id!r}")
    with session.transaction(author) as turn:
        session.store.write(turn, "delete_block", block_id,
                            {"slide_id": slide_id, "block_id": block_id}, author)
    return Diff(summary=f"removed {block_id}", target=block_id,
                before=block.component,
                render=session.measure_slide(slide_id)).as_result()
