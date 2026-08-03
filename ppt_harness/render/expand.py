"""Expansion — blocks to absolute boxes. DESIGN §1.4.

This is the only module permitted to produce a coordinate. Components declare regions and
proportions; the theme declares margins, columns, and gutters; the expander turns the two
into boxes. Nothing upstream of here ever sees a number, which is what makes DESIGN's first
principle enforceable rather than aspirational.

Boxes are computed in **px on the theme canvas**, then converted to EMU at export. Working
in px keeps one coordinate system shared with the HTML preview, so preview and export can
be compared directly instead of through a conversion nobody trusts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..components import overrides, registry
from ..state.document import EMU_PER_INCH, Block, Slide, Theme, TypeSpec

#: Vertical gap between a component's own slots, in px at 720p. Scaled with the canvas.
SLOT_GAP = 12
#: Gap between blocks sharing a region.
BLOCK_GAP = 24
#: Gap between the cells of a multi-column list. Wider than SLOT_GAP because the eye needs
#: more separation across a row than down a column to read the cells as separate things.
CELL_GAP = 20


@dataclass(frozen=True)
class Box:
    """An absolute rectangle in canvas px."""

    x: float
    y: float
    w: float
    h: float

    def emu(
        self, canvas_w: int, canvas_h: int, slide_cx: int, slide_cy: int
    ) -> tuple[int, int, int, int]:
        """Convert to EMU against the real slide size.

        Width rounds **down** and height rounds **up**, always in favour of fitting — see
        The px-to-EMU rounding rule. A box one EMU too narrow cannot cause
        a wrap; one EMU too short can clip a descender.
        """
        sx, sy = slide_cx / canvas_w, slide_cy / canvas_h
        return (
            math.floor(self.x * sx),
            math.floor(self.y * sy),
            math.floor(self.w * sx),
            math.ceil(self.h * sy),
        )


@dataclass(frozen=True)
class LaidOutSlot:
    """One slot with its final geometry and the type spec it will be set in."""

    block_id: str
    slot: str
    box: Box
    spec: TypeSpec
    role: str
    align: str
    max_lines: int
    items: int = 1
    component: str = ""
    """Which component owns this slot. Carried so the writer can tell a `tabular` slot from
    a `title` one without re-deriving it from the block."""
    shape: str = "title"
    """The canonical slot shape, so a table is written as a table."""
    overrides: dict[str, Any] = field(default_factory=dict)
    """The block's clamped overrides, carried so the writer resolves emphasis through the
    theme rather than re-deriving it from the block."""
    columns: int = 1
    """How many items of a list slot sit side by side — the variant's `per_row`.

    This is the difference between `stat_row` and a list of sentences, and for a long time it
    was declared in the catalog and read by nothing: twelve variants across six components
    said `per_row` and every one of them rendered as a single column of text. Worse, the
    budget already *assumed* the row — it divided a slot's capacity by the item count, which
    is only correct if the items share the width — so measurement and rendering disagreed
    about the same slide, and the arbiter of that disagreement was a rejected write. A model
    asked for three stats would be told they did not fit, shorten them to `[X]%`, and land a
    slide that was a vertical list anyway.
    """
    cell_gap: float = 0.0
    """Gap between cells, in canvas px, already scaled to the canvas."""

    def cells(self, count: int | None = None) -> list[Box]:
        """The box each item occupies, row-major.

        One entry per item, always — a single-column slot returns one box per item stacked
        down the slot, which is the same geometry the renderer produced before `columns`
        existed. Callers therefore do not branch: a plain list is the degenerate grid, not a
        special case, and that is what keeps the budget's formula and the writer's loop
        single-pathed.
        """
        n = max(1, self.items if count is None else count)
        across = max(1, min(self.columns, n))
        down = math.ceil(n / across)
        gap = self.cell_gap
        w = (self.box.w - gap * (across - 1)) / across
        h = (self.box.h - gap * (down - 1)) / down
        out = []
        for index in range(n):
            row, column = divmod(index, across)
            out.append(Box(x=self.box.x + column * (w + gap),
                           y=self.box.y + row * (h + gap), w=w, h=h))
        return out


def content_box(theme: Theme) -> Box:
    w, h = theme.grid.canvas
    m = theme.grid.margin
    return Box(x=m, y=m, w=w - 2 * m, h=h - 2 * m)


def region_box(theme: Theme, region: registry.Region) -> Box:
    """A region's rectangle: vertical fraction of the content box, horizontal column span."""
    content = content_box(theme)
    cols, gutter = theme.grid.columns, theme.grid.gutter
    col_w = (content.w - gutter * (cols - 1)) / cols
    start, span = region.columns
    return Box(
        x=content.x + start * (col_w + gutter),
        y=content.y + region.top * content.h,
        w=span * col_w + (span - 1) * gutter,
        h=region.height * content.h,
    )


def region_by_name(theme: Theme, name: str) -> Box | None:
    """A region's rectangle, looked up by name across the layout frames.

    Freeform slides have no layout of their own, so a tool that places something "in the
    header" needs the grid's answer rather than a component's. Names are shared across
    frames — `header` means the same band wherever it appears — so the first match is the
    right one.
    """
    for frame in registry.LAYOUTS.values():
        region = frame.regions.get(name)
        if region is not None:
            return region_box(theme, region)
    return None


def expand_block(theme: Theme, block: Block, box: Box) -> list[LaidOutSlot]:
    """Divide one block's box among its slots by declared height share."""
    comp = registry.get(block.component)
    variant = comp.variants.get(block.variant) or comp.variants[comp.default_variant]
    scale = theme.grid.canvas[1] / 720
    gap = SLOT_GAP * scale

    present = [(name, spec) for name, spec in comp.slots.items()
               if name in block.slots and block.slots[name] not in (None, "", [])]
    if not present:
        return []

    # A bounded override, and bounded to *spacing*. Tightening the gaps buys room without
    # touching the type scale — shrinking a font is the silent degradation autofit was
    # disabled to prevent.
    gap *= overrides.gap_factor(block.overrides)

    total_share = sum(spec.height_share for _, spec in present) or 1.0
    free = box.h - gap * (len(present) - 1)

    out: list[LaidOutSlot] = []
    y = box.y
    for name, spec in present:
        h = free * (spec.height_share / total_share)
        value = block.slots[name]
        items = len(value) if isinstance(value, list) else 1
        # Only a list is arranged; a title with `per_row` on its variant is still one box.
        # Reading the variant's number for a `prose` slot would silently halve its width.
        across = variant.per_row if spec.shape == "list" else 1
        out.append(
            LaidOutSlot(
                block_id=block.id,
                slot=name,
                box=Box(x=box.x, y=y, w=box.w, h=h),
                spec=theme.type.scale[spec.role],
                role=spec.role,
                align=overrides.OVERRIDES["align"].clamp(
                    block.overrides.get("align", variant.align)),
                max_lines=spec.max_lines,
                items=items,
                component=block.component,
                shape=spec.shape,
                overrides=overrides.clamp_all(block.overrides),
                columns=max(1, across),
                cell_gap=CELL_GAP * scale,
            )
        )
        y += h + gap
    return out


def expand_slide(theme: Theme, slide: Slide) -> list[LaidOutSlot]:
    """Every slot on a managed slide, with final geometry.

    Blocks sharing a region stack vertically in declared order and split the region by
    equal share — v0 has no component that needs uneven splits, and inventing a weighting
    scheme before one does would be guessing.
    """
    if slide.layout is None:
        raise ValueError(f"slide {slide.id} is managed but has no layout")
    frame = registry.layout(slide.layout)

    by_region: dict[str, list[Block]] = {}
    for block in slide.blocks:
        if block.region not in frame.regions:
            raise ValueError(
                f"block {block.id} targets region {block.region!r}, "
                f"which layout {frame.key!r} does not have ({sorted(frame.regions)})"
            )
        by_region.setdefault(block.region, []).append(block)

    scale = theme.grid.canvas[1] / 720
    out: list[LaidOutSlot] = []
    for name, blocks in by_region.items():
        box = region_box(theme, frame.regions[name])
        gap = BLOCK_GAP * scale
        each_h = (box.h - gap * (len(blocks) - 1)) / len(blocks)
        y = box.y
        for block in blocks:
            out.extend(expand_block(theme, block, Box(box.x, y, box.w, each_h)))
            y += each_h + gap
    return out


def slide_size_emu(theme: Theme) -> tuple[int, int]:
    w, h = theme.grid.canvas
    return round(w / 96 * EMU_PER_INCH), round(h / 96 * EMU_PER_INCH)
