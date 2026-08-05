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
from dataclasses import dataclass, field, replace
from typing import Any

from ..components import decoration, icons, overrides, registry
from ..state import slots as slot_render
from ..state.document import EMU_PER_INCH, Block, Slide, Theme, TypeSpec

#: Vertical gap between a component's own slots, in px at 720p. Scaled with the canvas.
SLOT_GAP = 12
#: Gap between blocks sharing a region.
BLOCK_GAP = 24
#: Gap between the cells of a multi-column list. Wider than SLOT_GAP because the eye needs
#: more separation across a row than down a column to read the cells as separate things.
CELL_GAP = 20

# ---------------------------------------------------------------- where an icon goes
#
# `icon_top` and `icon_left` name a position, and a position is a rectangle, so it is decided
# here — DESIGN §1.4 again. The numbers are fractions of the *cell*, never of the slide, so a
# mark in a footer band and one in a hero region are the same mark at two sizes rather than
# the same number of pixels in two very different boxes.
#
# Both shares are capped against the other axis, and that cap is the one doing the work: five
# cells across a body region are much wider than they are tall, and a mark sized off the
# height alone would be a postage stamp in the middle of a wide cell — while two cells across
# are much taller than the label needs, and a mark sized off *that* height would be a picture
# with a caption. Taking the smaller of the two keeps a square that reads as an icon at every
# cell aspect the catalog can produce.

#: Space between the mark and the word, in px at 720p. Smaller than CELL_GAP: this separates
#: two parts of one thing, where CELL_GAP separates one thing from the next.
ICON_GAP = 8

#: The mark's size against the label's own line height, which is what actually decides
#: whether the two read as one thing. `icon_top` stands above the word and can carry three
#: lines' worth; `icon_left` sits beside it and at much more than a line and a half stops
#: being a mark on a label and becomes a picture with a caption.
#:
#: Tied to the type rather than to the cell because the cell is the wrong ruler: `icon_row`
#: in a `body` region gets a cell five times taller than its label needs, and a mark at a
#: fraction of *that* is an enormous glyph over a caption. The cell fractions below remain as
#: the ceiling for the opposite case — a band so short that three lines' worth would not fit.
ICON_TOP_LINES = 3.0
ICON_LEFT_LINES = 1.75

#: `icon_top` — the mark stands above the label and is the dominant element of the cell.
ICON_TOP_HEIGHT = 0.46
ICON_TOP_WIDTH = 0.60

#: `icon_left` — the mark sits beside the label and must leave the words most of the cell, or
#: the variant is a picture list rather than a labelled row.
ICON_LEFT_HEIGHT = 0.62
ICON_LEFT_WIDTH = 0.24

#: Below this a mark is a smudge rather than a symbol, and the cell is better off with the
#: word alone — a two-px-tall icon costs the recipient a shape and tells them nothing. In px
#: at 720p, scaled with the canvas like everything else here.
ICON_MIN = 12

#: One px of rounding slack, held back from the mark and handed to the words.
#:
#: Not superstition. `budget.for_slot` counts *whole* lines off the text box — `int(h / line)`
#: — so a box that is exactly two lines tall is one float away from being budgeted as one, and
#: the icon's height is arrived at by subtraction. A px is invisible on the mark and is the
#: difference between a label that fits and a refusal nobody can act on.
ICON_SLACK = 1.0

# ------------------------------------------------------- where a decoration's layers go
#
# A decoration names a `place` and this module answers with a rectangle, for the same reason
# a component names a region and not a box: DESIGN §1.4 gives exactly one module permission
# to produce a coordinate, and a shadow offset is a coordinate. The numbers are proportions
# of the panel they hang off, so a card in a footer band and a hero panel get shade at the
# same *scale* rather than the same number of pixels.

#: The contact ellipse, as fractions of the panel it sits beneath. Narrower than the panel
#: because the light wraps its corners, and shallow because a shadow on the floor is seen
#: almost edge-on.
#:
#: `SINK` is exactly half, and that is the whole trick: it puts the ellipse's centre — the
#: darkest point of the ramp — precisely on the panel's bottom edge, so the upper half is
#: hidden behind the panel and what is left is dark where the object meets the ground and
#: gone a little way out from it. Sunk further, the shadow separates from the panel and
#: reads as a grey blob lying near it.
CONTACT_WIDTH = 0.88
CONTACT_HEIGHT = 0.14
CONTACT_SINK = 0.50

#: The ground plane: wider than the object standing on it and far shallower than the object
#: is tall, which is what makes a horizontal surface read as receding rather than as a second
#: panel. Sunk less than half, so most of it is out in front of the object where the eye can
#: see there is a floor.
GROUND_WIDTH = 1.15
GROUND_HEIGHT = 0.55
GROUND_SINK = 0.30

#: The slide wash, as fractions of the canvas: centred slightly below the middle so the light
#: pools under the content rather than haloing it, and *larger than the slide on every side*.
#: That is not a rounding allowance. A radial gradient reaches zero at its own rim, so an
#: ellipse whose rim lands inside the canvas draws a visible arc with clean white outside it —
#: which is an object, and this is meant to be a change in the air. Only the inner part of the
#: ramp is ever on screen.
WASH_CENTRE = (0.5, 0.55)
WASH_RADIUS = (0.95, 0.85)


@dataclass(frozen=True)
class Box:
    """An absolute rectangle in canvas px."""

    x: float
    y: float
    w: float
    h: float

    def inset(self, by: float) -> Box:
        """The same rectangle, `by` px in from every edge. Negative grows it back out.

        Clamped at one px rather than allowed to invert: a pad wider than the box it dresses
        is a theme's spacing scale meeting a very small cell, and a negative width would
        travel all the way to an EMU the file cannot hold.
        """
        if not by:
            return self
        return Box(x=self.x + by, y=self.y + by,
                   w=max(1.0, self.w - 2 * by), h=max(1.0, self.h - 2 * by))

    def scaled(self, by: float) -> Box:
        """The same rectangle at `by` of its size, about its own centre.

        Concentric rather than anchored at a corner, because the only caller is the
        `media_scale` override and "fills 60% of its box" describes a smaller picture in the
        same place, not one pushed into the top-left. Clamped to (0, 1] for the same reason
        the override is: a factor above one is a picture leaving the box it was measured in.
        """
        by = max(0.01, min(1.0, by))
        if by >= 1.0:
            return self
        w, h = self.w * by, self.h * by
        return Box(x=self.x + (self.w - w) / 2, y=self.y + (self.h - h) / 2, w=w, h=h)

    def fit(self, aspect: float) -> Box:
        """The largest box of width/height `aspect` that fits inside this one, centred.

        Letterboxing, and it is a decision rather than an obvious default — see
        `io/media.py` for why the harness holds a picture's proportions instead of cropping
        or stretching it. Expressed here because it is a coordinate, and DESIGN §1.4 says
        this module is the only one that may produce one: the writer and `eject_slide` both
        need the same rectangle, and two copies of this arithmetic is the shape of the bug
        that makes an ejected slide stop matching the managed one it came from.
        """
        if aspect <= 0 or self.w <= 0 or self.h <= 0:
            return self
        w = min(self.w, self.h * aspect)
        h = w / aspect
        return Box(x=self.x + (self.w - w) / 2, y=self.y + (self.h - h) / 2, w=w, h=h)

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
    decoration: str = ""
    """What the variant draws behind this slot, resolved through the theme by the writer and
    the preview. A name, so neither of them has to be told a colour."""
    pad: float = 0.0
    """Space the decoration takes out of the box, in canvas px.

    Applied once per *written* box, which is why a single-column slot arrives here already
    deflated while a row of cells is deflated cell by cell in `cells`. Charging it per item
    on a slot that renders as one frame would have the budget refuse content that fits, and
    the whole point of applying padding in the expander is that the measurer and the writer
    read the same number.
    """
    icon_place: str = ""
    """Where each item's mark sits within its cell — `top`, `left`, or empty for none.

    The variant's word, carried rather than looked up again, for the same reason `decoration`
    is: the writer and the preview must be answering one question, and a second lookup is a
    second chance to answer it differently.
    """
    icon_side: float = 0.0
    """The side of the square a mark occupies, in canvas px. Square by construction — every
    icon in the set is drawn in a square view box, and a non-square frame would stretch the
    artwork rather than place it."""
    icon_gap: float = 0.0
    """Space between the mark and the word it labels, in canvas px, already scaled."""
    icon_offset: float = 0.0
    """How far down the cell the mark begins, in canvas px.

    Always vertical, for both placements, but it buys two different things. Under `top` it
    centres the whole mark-and-label group in a cell that is usually far taller than the pair
    needs. Under `left` it centres the mark against the lines the label may occupy, rather
    than against the cell — which is where a mark sitting visibly below its own word came
    from.
    """

    def _carve(self, cell: Box) -> Box:
        """What is left of a cell once its mark has taken its side.

        This is the single most important line of the feature: the budget measures
        `cells()[0]`, so taking the icon's rectangle out *here* is what makes the gate charge
        a labelled mark for the room it actually leaves the words. Reserving the space at
        write time instead would have the measurer certify two lines into a box that holds
        one — the same disagreement between measurement and rendering that the decoration pad
        exists here to avoid.
        """
        if not self.icon_place or self.icon_side <= 0:
            return cell
        take = self.icon_side + self.icon_gap
        if self.icon_place == "top":
            take += self.icon_offset
            return Box(x=cell.x, y=cell.y + take, w=cell.w, h=max(1.0, cell.h - take))
        return Box(x=cell.x + take, y=cell.y, w=max(1.0, cell.w - take), h=cell.h)

    def _grid(self, count: int | None = None) -> list[Box]:
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

    def cells(self, count: int | None = None) -> list[Box]:
        """The box each item's text occupies, row-major.

        One entry per item, always — a single-column slot returns one box per item stacked
        down the slot, which is the same geometry the renderer produced before `columns`
        existed. Callers therefore do not branch: a plain list is the degenerate grid, not a
        special case, and that is what keeps the budget's formula and the writer's loop
        single-pathed.
        """
        pad = self.pad if self.columns > 1 else 0.0
        return [self._carve(box.inset(pad)) for box in self._grid(count)]

    def panels(self, count: int | None = None) -> list[Box]:
        """The rectangle a decoration paints behind each written box.

        One per cell across a row, and exactly one for a slot the writer emits as a single
        frame — the count has to match the boxes, or a card ends up behind nothing. The
        single case grows back out by the pad the expander already took off, so the text sits
        inside its panel by the same margin the budget was charged.
        """
        if self.columns > 1:
            return self._grid(count)
        return [self.box.inset(-self.pad)]

    def icons(self, count: int | None = None) -> list[Box]:
        """The square each item's mark is drawn in, row-major and aligned with `cells`.

        Empty for a slot that carries none, so callers iterate a list rather than branch on
        whether icons exist — the same shape as `panels`, and for the same reason: a writer
        with a special case for "no decoration" grew one for "no icon" the moment there were
        two of them.

        Positioned against the *cell*, not the text box: `cells` has already had this square
        subtracted, so deriving the mark's place from the text would put it back inside the
        words it was moved out of.
        """
        if not self.icon_place or self.icon_side <= 0:
            return []
        pad = self.pad if self.columns > 1 else 0.0
        side = self.icon_side
        out = []
        for box in self._grid(count):
            cell = box.inset(pad)
            y = cell.y + self.icon_offset
            if self.icon_place == "top":
                # Centred across the cell, because `icon_top` centres its label too and a
                # mark hanging left over centred words reads as a mistake rather than a
                # choice.
                out.append(Box(x=cell.x + (cell.w - side) / 2, y=y, w=side, h=side))
            else:
                out.append(Box(x=cell.x, y=y, w=side, h=side))
        return out


def written_cells(laid_out: LaidOutSlot, value: Any) -> list[tuple[str, Box]]:
    """The text boxes one slot becomes, each with the text that goes in it.

    Usually one, and one per item across a row. Single-column slots return the whole slot as
    one frame, which is what the renderer produced before `columns` existed, so a plain list
    is the degenerate grid rather than a branch anyone has to remember.

    It lives beside the geometry, and not inside either writer, because there are two of
    them: `export_mutate` writes a managed slide and `io/adopt.eject` freezes one. A slide
    that ejected to a different arrangement from the one it exported would make the door out
    of managed mode a reflow — and eject is one-way, so there would be nothing to compare it
    against afterwards.
    """
    if laid_out.columns <= 1 or not isinstance(value, list):
        return [(slot_render.slot_text(value), laid_out.box)]
    parts = [slot_render.slot_text(item) for item in value]
    return list(zip(parts, laid_out.cells(len(parts)), strict=False))


def written_icons(laid_out: LaidOutSlot, value: Any) -> list[tuple[str, Box]]:
    """The mark each item names, with the square it is drawn in. Empty where there are none.

    Beside `written_cells` and matching it item for item, because the two are one answer:
    the cell the words go in and the square the mark goes in are cut from the same rectangle,
    and a caller that derived one of them itself would be the second implementation this file
    exists to prevent. Items without an icon simply contribute nothing, so a list that has
    been half filled draws the marks it has rather than nothing at all.
    """
    if not laid_out.icon_place or not isinstance(value, list):
        return []
    boxes = laid_out.icons(len(value))
    return [(str(item["icon"]), box)
            for item, box in zip(value, boxes, strict=False)
            if slot_render.is_icon(item)]


def icon_stroke_px(box: Box) -> float:
    """How thick an icon's line is at the size it is being drawn, in canvas px.

    The set is drawn at two units on a twenty-four unit box, so the weight is a *proportion*
    of the mark and this converts it once. A width fixed in px would be a hairline on a hero
    icon and a blot on a footer one; a width the writer computed for itself would be a second
    copy of this ratio, free to disagree with the preview's.
    """
    return max(0.5, box.w * icons.stroke_units() / icons.view_box())


def _icon_metrics(laid_out: LaidOutSlot, place: str, gap: float,
                  scale: float) -> tuple[float, float]:
    """`(side, offset)` for one slot's marks: how big, and how far down the cell they start.

    Sized against the label's line height first and against the cell only as a ceiling — see
    the constants above for why the type is the better ruler. `icon_top` carries one further
    bound the other does not: the mark and the label share the cell's *height*, so it may take
    no more than leaves the label the lines it would have had without it. Anything else is
    the mark quietly retiring capacity that the budget — which reads the carved box — then
    refuses content against. A footer band five cells across gave the words a box shorter
    than one line of their own type, and every label anyone could write was rejected for
    overflowing a box the icon had taken.

    The offset is what keeps the pair looking composed rather than jammed against the top of
    a cell that is much taller than either of them. Text frames are top-anchored throughout
    the writer, so an unshifted `icon_top` group sat in the top third of a body-region cell
    with a third of the slide empty beneath it; the group is centred against the room the
    label could use instead, which puts the whitespace where a designer would.
    """
    cell = laid_out._grid()[0].inset(laid_out.pad if laid_out.columns > 1 else 0.0)
    line = laid_out.spec.line
    # The height the label may actually claim: its declared line count, or fewer where the
    # cell cannot hold that many. This is the same number `budget.for_slot` derives from the
    # carved box, which is why the two never disagree about how much room the words have.
    keep = min(laid_out.max_lines, max(1, int(cell.h / line))) * line if line else 0.0
    if place == "top":
        side = min(line * ICON_TOP_LINES, cell.h * ICON_TOP_HEIGHT, cell.w * ICON_TOP_WIDTH,
                   cell.h - gap - keep - ICON_SLACK)
        offset = max(0.0, (cell.h - (side + gap + keep)) / 2)
    else:
        side = min(line * ICON_LEFT_LINES, cell.h * ICON_LEFT_HEIGHT,
                   cell.w * ICON_LEFT_WIDTH)
        # Centred on the band the *words* occupy, not on the cell: a mark centred in a cell
        # five times its label's height sits well below the line it belongs to.
        offset = max(0.0, (min(cell.h, keep) - side) / 2)
    if side < ICON_MIN * scale:
        return 0.0, 0.0
    return side, offset


def _beneath(panel: Box, width: float, height: float, sink: float) -> Box:
    """A shallow ellipse's box, centred under `panel` and sunk past its foot.

    `sink` is a fraction of the ellipse's own height, so the shape sits *at* the panel's
    bottom edge at every panel size instead of drifting out from under tall ones.
    """
    w, h = panel.w * width, max(4.0, panel.h * height)
    return Box(x=panel.x + (panel.w - w) / 2,
               y=panel.y + panel.h - h * (1.0 - sink), w=w, h=h)


def wash_box(theme: Theme) -> Box:
    """The slide-wide radial's rectangle, in canvas px.

    Deliberately larger than the canvas and partly off it: the ellipse's own outline must
    never be visible, or the wash stops being a wash and becomes a very large pale shape.
    Negative coordinates are legal in OOXML and in the preview, which clips them.
    """
    w, h = theme.grid.canvas
    cx, cy = WASH_CENTRE
    rx, ry = WASH_RADIUS
    return Box(x=(cx - rx) * w, y=(cy - ry) * h, w=2 * rx * w, h=2 * ry * h)


def decoration_box(theme: Theme, panel: Box, place: str) -> Box:
    """The rectangle one decoration layer occupies.

    `place` is the decoration's word — `panel`, `contact`, `ground`, `slide` — and this is
    where it becomes a number. Both writers and the preview call this and nothing else, so a
    shadow cannot be in one place in the file and another in the preview of it.
    """
    if place == "slide":
        return wash_box(theme)
    if place == "contact":
        return _beneath(panel, CONTACT_WIDTH, CONTACT_HEIGHT, CONTACT_SINK)
    if place == "ground":
        return _beneath(panel, GROUND_WIDTH, GROUND_HEIGHT, GROUND_SINK)
    return panel


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


def _bands(present: list[tuple[str, registry.SlotSpec]],
           ) -> list[list[tuple[str, registry.SlotSpec]]]:
    """Group consecutive slots into horizontal bands by declared width share.

    A slot joins the band being built while there is room for its share; otherwise it opens
    the next one. Declaration order therefore decides adjacency, which keeps the catalog
    readable: `left` then `right` is what a reader already expects to mean side by side.

    Every share defaulting to 1.0 makes each slot its own band, so a component that never
    mentions width is laid out exactly as it was before bands existed.
    """
    bands: list[list[tuple[str, registry.SlotSpec]]] = []
    current: list[tuple[str, registry.SlotSpec]] = []
    used = 0.0
    for name, spec in present:
        share = spec.width_share if 0 < spec.width_share <= 1 else 1.0
        # The epsilon is for shares that are meant to fill a band but cannot say so exactly
        # in binary — three thirds being the case that matters.
        if current and used + share > 1.0 + 1e-9:
            bands.append(current)
            current, used = [], 0.0
        current.append((name, spec))
        used += share
    if current:
        bands.append(current)
    return bands


def _ordered(present: list[tuple[str, registry.SlotSpec]], order: tuple[str, ...],
             ) -> list[tuple[str, registry.SlotSpec]]:
    """The variant's slot order, where it states one.

    Sorted stably against the declared order so a variant only has to name the slots it
    moves. This is the whole of `image_right`: the two slots share a band, `_bands` builds
    bands in declaration order, and which of them is drawn first is therefore the only thing
    that decides which side the picture is on.
    """
    if not order:
        return present
    rank = {name: index for index, name in enumerate(order)}
    return sorted(present, key=lambda item: rank.get(item[0], len(rank)))


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

    bands = _bands(_ordered(present, variant.slot_order))
    # A band is as tall as its tallest member, so two slots sharing a row cost the height of
    # one. Summing over bands rather than over slots is what reclaims the space the stacked
    # layout would have spent putting them under each other.
    band_heights = [max(spec.height_share for _, spec in band) for band in bands]
    total_share = sum(band_heights) or 1.0
    free = box.h - gap * (len(bands) - 1)

    out: list[LaidOutSlot] = []
    y = box.y
    for band, share in zip(bands, band_heights, strict=True):
        h = free * (share / total_share)
        # Shares are normalised across the band, so two halves fill the width even after the
        # gap is taken out of it, and a band that declares less than a full width still fills
        # its row rather than leaving a ragged edge.
        widths = [spec.width_share if 0 < spec.width_share <= 1 else 1.0 for _, spec in band]
        inner_gap = CELL_GAP * scale if len(band) > 1 else 0.0
        usable = box.w - inner_gap * (len(band) - 1)
        x = box.x
        for (name, spec), width_share in zip(band, widths, strict=True):
            w = usable * (width_share / (sum(widths) or 1.0))
            value = block.slots[name]
            items = len(value) if isinstance(value, list) else 1
            # Only a list is arranged; a title with `per_row` on its variant is still one box.
            # Reading the variant's number for a `prose` slot would silently halve its width.
            across = variant.per_row if spec.shape == "list" else 1
            pad = decoration.pad_for(theme, variant.decoration, spec.shape)
            # A slot the writer emits as one frame is deflated here, so the box every
            # downstream reader sees — the budget, the preview, the frozen geometry — is the
            # box the text lands in. A row of cells keeps its outer box and is deflated cell
            # by cell instead, because there the decoration is drawn once per cell.
            slot_box = Box(x=x, y=y, w=w, h=h)
            laid_out = (
                LaidOutSlot(
                    block_id=block.id,
                    slot=name,
                    box=slot_box if across > 1 else slot_box.inset(pad),
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
                    decoration=variant.decoration,
                    pad=pad,
                )
            )
            # The mark is sized from the cell, so the slot has to exist before it can be
            # measured — hence a second pass rather than an argument. Only a slot that
            # *declares* icons and a variant that *places* them get one, and only when the
            # items are laid out as a grid: `written_cells` writes a single-column list as
            # one text frame, and one frame cannot carry five marks at five positions.
            if spec.icons and variant.icon and across > 1:
                gap_px = ICON_GAP * scale
                side, offset = _icon_metrics(laid_out, variant.icon, gap_px, scale)
                laid_out = replace(laid_out, icon_place=variant.icon, icon_gap=gap_px,
                                   icon_side=side, icon_offset=offset)
            out.append(laid_out)
            x += w + inner_gap
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
