"""What the model is sent, as opposed to what the tool returns.

A tool result has two audiences with different needs. The **UI** wants everything — box
coordinates to draw the overlay, per-shape geometry for the inspector. The **model** wants
what it can act on, and no tool it can call accepts a coordinate, so every number describing
where something sits is dead weight in its context.

That weight is paid repeatedly. Results stay in the conversation and are resent on every
subsequent round, so a verbose `get_slide` early in a turn is re-processed by the model
three or four times before the turn ends.

The rule here: keep what changes a decision, drop what cannot. A slot that fits needs no
description beyond the fact that it fits; a slot that overflows needs its target, its
overage, and the ways out.
"""

from __future__ import annotations

from typing import Any

#: Per-shape keys the model can act on. `frame` is deliberately absent — geometry is the
#: expander's job on managed slides and the constraint tools' on freeform ones.
SHAPE_KEYS = ("id", "role", "type", "text", "opaque")

#: Measurement keys worth sending. `box` is excluded for the same reason.
MEASURE_KEYS = ("target", "lines", "max_lines", "fits", "overflow_px", "note",
                "source_autofit", "capacity_em", "used_em")

#: Beyond this many shapes, a slide listing is summarised rather than enumerated. A model
#: choosing what to edit does not need all fifty; it needs the ones with text.
MAX_SHAPES = 24


def for_model(result: dict[str, Any]) -> dict[str, Any]:
    """Trim a tool result to what the model can use."""
    if not isinstance(result, dict):
        return result

    out = {k: v for k, v in result.items() if k not in ("render", "shapes", "slots", "boxes")}

    for key in ("render",):
        if isinstance(result.get(key), dict):
            out[key] = _measurement(result[key])

    for key in ("shapes", "boxes", "slots"):
        if key in result:
            out[key] = _entries(key, result[key])

    return out


def _measurement(render: dict[str, Any]) -> dict[str, Any]:
    """The measurement a write returns: keep the verdict, drop the geometry."""
    kept = {k: v for k, v in render.items()
            if k not in ("shapes", "slots", "boxes", "canvas")}
    for key in ("shapes", "slots", "boxes"):
        if key in render:
            kept[key] = _entries(key, render[key])
    return kept


def _entries(key: str, value: Any) -> Any:
    if not isinstance(value, list):
        return value

    if key in ("shapes", "boxes", "slots") and value and isinstance(value[0], dict):
        if "target" in value[0] or "fits" in value[0]:
            return _problems(value)
        return _shapes(value)
    return value


def _problems(entries: list[dict[str, Any]]) -> Any:
    """Only what does not fit, plus a count of what does.

    "38 slots fit" is as useful to a decision as thirty-eight identical records saying so,
    and costs two orders of magnitude less.
    """
    bad = [{k: e[k] for k in MEASURE_KEYS if k in e} for e in entries
           if not e.get("fits", True)]
    fine = len(entries) - len(bad)
    if not bad:
        return f"{fine} measured, all fit"
    return {"overflowing": bad, "others_fit": fine}


def _shapes(entries: list[dict[str, Any]]) -> Any:
    """Editable shapes, without their geometry."""
    trimmed = []
    for entry in entries:
        kept = {k: entry[k] for k in SHAPE_KEYS if k in entry}
        if isinstance(kept.get("text"), str) and len(kept["text"]) > 240:
            kept["text"] = kept["text"][:240] + "…"
        trimmed.append(kept)

    editable = [e for e in trimmed if e.get("text")]
    if len(trimmed) <= MAX_SHAPES:
        return trimmed
    # Past the cap, the shapes worth naming are the ones carrying words.
    return {
        "editable": editable[:MAX_SHAPES],
        "omitted": len(trimmed) - len(editable[:MAX_SHAPES]),
        "note": "shapes without text are omitted; ask for a specific id if needed",
    }
