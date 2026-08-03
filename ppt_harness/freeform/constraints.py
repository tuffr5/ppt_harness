"""Geometry for freeform slides — DESIGN §4, PLAN A3.

The arithmetic behind `align`, `distribute`, `match_size`, `snap_to_grid` and `nudge`. Pure
functions over rectangles: no session, no tools, no OOXML, so the maths can be checked
against hand-computed cases rather than inferred from a rendered slide.

These tools exist so that **`set_frame` stays an escape hatch**. A model that can say "align
these left" never needs to say "put this at 838200 EMU", and an intent is reviewable in a way
that a coordinate is not.

Everything is in EMU, the unit the file uses, because these operate on imported shapes whose
geometry is already absolute.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..state.document import Frame

EDGES = ("left", "right", "top", "bottom", "center", "middle")
AXES = ("horizontal", "vertical")
DIMENSIONS = ("width", "height", "both")
DIRECTIONS = ("left", "right", "up", "down")
STEPS = ("small", "medium", "large")


def _span(frames: Iterable[Frame]) -> tuple[int, int, int, int]:
    """The bounding box of a selection."""
    frames = list(frames)
    return (min(f.x for f in frames), min(f.y for f in frames),
            max(f.x + f.cx for f in frames), max(f.y + f.cy for f in frames))


def align(frames: list[Frame], edge: str) -> list[Frame]:
    """Line shapes up on one edge of their own bounding box.

    The bounding box, not the slide: aligning to the slide would move a tidy group across
    the canvas, which is never what "align these left" means.
    """
    if edge not in EDGES:
        raise ValueError(f"{edge!r} is not one of {list(EDGES)}")
    if len(frames) < 2:
        return [f.model_copy() for f in frames]

    left, top, right, bottom = _span(frames)
    out = []
    for f in frames:
        moved = f.model_copy()
        if edge == "left":
            moved.x = left
        elif edge == "right":
            moved.x = right - f.cx
        elif edge == "center":
            moved.x = round(left + (right - left - f.cx) / 2)
        elif edge == "top":
            moved.y = top
        elif edge == "bottom":
            moved.y = bottom - f.cy
        else:  # middle
            moved.y = round(top + (bottom - top - f.cy) / 2)
        out.append(moved)
    return out


def distribute(frames: list[Frame], axis: str, gap: int | None = None) -> list[Frame]:
    """Space shapes evenly along an axis.

    With no `gap`, the outermost two stay put and the rest are spread between them — the
    selection keeps its extent, which is what makes the result look deliberate. With a
    `gap`, the first stays put and the others follow at that spacing.
    """
    if axis not in AXES:
        raise ValueError(f"{axis!r} is not one of {list(AXES)}")
    if len(frames) < 3 and gap is None:
        return [f.model_copy() for f in frames]

    horizontal = axis == "horizontal"
    ordered = sorted(frames, key=lambda f: f.x if horizontal else f.y)
    sizes = [(f.cx if horizontal else f.cy) for f in ordered]

    if gap is None:
        left, top, right, bottom = _span(ordered)
        extent = (right - left) if horizontal else (bottom - top)
        spacing = (extent - sum(sizes)) / (len(ordered) - 1)
        start = left if horizontal else top
    else:
        spacing = float(gap)
        start = ordered[0].x if horizontal else ordered[0].y

    placed = {}
    cursor = float(start)
    for frame, size in zip(ordered, sizes, strict=True):
        moved = frame.model_copy()
        if horizontal:
            moved.x = round(cursor)
        else:
            moved.y = round(cursor)
        placed[id(frame)] = moved
        cursor += size + spacing

    # Returned in the caller's order, not sorted order: the caller passed shapes, and gets
    # its shapes back in the same positions.
    return [placed[id(f)] for f in frames]


def match_size(frames: list[Frame], dimension: str) -> list[Frame]:
    """Resize every shape to match the first.

    The first, not the largest: "make these the same size as that one" is the request, and
    the reference is whichever the caller named first. Silently picking the largest would
    make the result depend on data rather than on intent.
    """
    if dimension not in DIMENSIONS:
        raise ValueError(f"{dimension!r} is not one of {list(DIMENSIONS)}")
    if not frames:
        return []

    reference = frames[0]
    out = [reference.model_copy()]
    for f in frames[1:]:
        moved = f.model_copy()
        if dimension in ("width", "both"):
            moved.cx = reference.cx
        if dimension in ("height", "both"):
            moved.cy = reference.cy
        out.append(moved)
    return out


def snap_to_grid(frames: list[Frame], column: int, gutter: int, margin: int,
                 baseline: int) -> list[Frame]:
    """Pull shapes onto the theme's column grid and vertical rhythm.

    Snapping the *left edge* to a column and the top to the baseline is enough to make a
    slide look composed; snapping widths too would resize shapes the user never asked to
    resize.
    """
    step = column + gutter
    out = []
    for f in frames:
        moved = f.model_copy()
        if step > 0:
            index = round((f.x - margin) / step)
            moved.x = max(0, margin + index * step)
        if baseline > 0:
            moved.y = max(0, round(f.y / baseline) * baseline)
        out.append(moved)
    return out


def nudge(frame: Frame, direction: str, step: int, bounds: tuple[int, int]) -> Frame:
    """Move a shape a little, without letting it leave the slide.

    Clamped rather than refused: "nudge it right" at the edge should stop at the edge, not
    fail. A shape already off-canvas — imported decks contain them — is not dragged back,
    only prevented from going further.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"{direction!r} is not one of {list(DIRECTIONS)}")

    width, height = bounds
    moved = frame.model_copy()
    if direction == "left":
        moved.x = frame.x - step
    elif direction == "right":
        moved.x = frame.x + step
    elif direction == "up":
        moved.y = frame.y - step
    else:
        moved.y = frame.y + step

    if moved.x < 0 <= frame.x:
        moved.x = 0
    if moved.y < 0 <= frame.y:
        moved.y = 0
    if moved.x + frame.cx > width >= frame.x + frame.cx:
        moved.x = width - frame.cx
    if moved.y + frame.cy > height >= frame.y + frame.cy:
        moved.y = height - frame.cy
    return moved


def step_size(spacing: list[int], size: str, scale: float) -> int:
    """A nudge distance drawn from the theme's spacing scale, in EMU.

    From the theme rather than a constant, so a nudge is a step of the same rhythm
    everything else is placed on.
    """
    if size not in STEPS:
        raise ValueError(f"{size!r} is not one of {list(STEPS)}")
    ladder = sorted(spacing) or [4, 8, 16]
    pick = {"small": 0, "medium": len(ladder) // 3, "large": len(ladder) // 2}[size]
    return max(1, round(ladder[min(pick, len(ladder) - 1)] * scale))
