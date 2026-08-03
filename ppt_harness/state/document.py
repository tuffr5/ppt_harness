"""Deck model — ARCHITECTURE.md §2.

The authored unit on a managed slide is a component with filled slots; geometry is
derived, never authored. Freeform slides carry absolute shapes over the original OOXML.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .richtext import Run, to_markup

EMU_PER_INCH = 914400
EMU_PER_POINT = 12700


class Mode(StrEnum):
    MANAGED = "managed"
    FREEFORM = "freeform"


class Author(StrEnum):
    USER = "user"
    MODEL = "model"
    LINT = "lint"


# --------------------------------------------------------------------------- theme


class TypeSpec(BaseModel):
    family: str
    size: float
    weight: int = 400
    line: float
    track: float = 0.0
    italic: bool = False


class Typography(BaseModel):
    families: dict[str, str]
    scale: dict[str, TypeSpec]
    floor: float = 16.0


class Grid(BaseModel):
    canvas: tuple[int, int] = (1280, 720)
    margin: int = 64
    columns: int = 12
    gutter: int = 16
    baseline: int = 4


class Theme(BaseModel):
    """Roles, never loose colors. Validated once at load — see §2.6."""

    id: str
    source: Literal["extracted", "authored"] = "authored"
    palette: dict[str, str | list[str]]
    type: Typography
    grid: Grid = Field(default_factory=Grid)
    spacing: list[int] = [4, 8, 12, 16, 24, 32, 48, 64]
    shape: dict[str, Any] = Field(default_factory=dict)
    layouts: list[str] = []
    inferred: list[str] = []
    """Fields guessed during extraction rather than read — surfaced for correction."""

    def accent(self, index: int) -> str:
        accents = self.palette.get("accents") or ["#000000"]
        assert isinstance(accents, list)
        return accents[index % len(accents)]


# ------------------------------------------------------------------- managed slides


class Block(BaseModel):
    id: str
    region: str
    component: str
    variant: str
    slots: dict[str, Any]
    overrides: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ freeform slides


class Frame(BaseModel):
    """Absolute geometry in EMU, as stored in OOXML."""

    x: int
    y: int
    cx: int
    cy: int


class Geometry(BaseModel):
    """How a shape is drawn, as the file states it.

    Captured so the preview can show arrows, callouts and rules rather than a slide of
    floating words. Colours are already resolved through the theme's scheme and its
    luminance transforms — a raw `schemeClr` slot means nothing downstream.
    """

    preset: str = "rect"
    fill: str | None = None
    fill_alpha: float = 1.0
    line: str | None = None
    line_alpha: float = 1.0
    line_width_pt: float = 0.75
    flip_h: bool = False
    flip_v: bool = False
    rotation: float = 0.0

    @property
    def visible(self) -> bool:
        return bool(self.fill or self.line)


class ChartSpec(BaseModel):
    """Enough of a native chart to draw it. The authoritative data stays in the package."""

    kind: str = "bar"
    categories: list[str] = Field(default_factory=list)
    series: list[dict[str, Any]] = Field(default_factory=list)


class TableSpec(BaseModel):
    """A table's contents. Geometry is the frame's; column widths are derived.

    Modelled as a grid of strings rather than of runs: a cell that needs emphasis is rare
    enough that the simpler shape is worth more than the generality.
    """

    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)

    @property
    def columns(self) -> int:
        return max([len(self.headers), *(len(r) for r in self.rows)])

    def cell(self, row: int, col: int) -> str:
        grid = [self.headers, *self.rows] if self.headers else self.rows
        if not 0 <= row < len(grid):
            raise IndexError(f"row {row} is outside 0..{len(grid) - 1}")
        line = grid[row]
        if not 0 <= col < self.columns:
            raise IndexError(f"column {col} is outside 0..{self.columns - 1}")
        return line[col] if col < len(line) else ""


class Shape(BaseModel):
    id: str
    ooxml_id: int
    type: str
    frame: Frame
    role: str | None = None
    text: str | None = None
    asset: str | None = None
    opaque: bool = False
    """True for shapes the harness does not model (SmartArt, groups) — preserved as-is."""
    runs: list[Run] = Field(default_factory=list)
    """Emphasis, as OOXML stores it: a paragraph is a list of runs, each with its own
    properties. Empty means the text is unemphasised and one run is enough.

    `text` stays the plain projection, because budgets, search and diffs all want the
    words without the markup.
    """
    origin_text: str | None = None
    """Text as the original file has it. `dirty` is derived from this rather than set by
    whoever wrote last, so an edit that is undone stops being dirty."""
    origin_markup: str | None = None
    """Emphasis as the original file has it, in markup form; `None` when the file had none.

    Kept beside `origin_text` because a shape can differ from the file in its emphasis while
    reading identically as plain words — bolding one word of a sentence changes nothing that
    `origin_text` can see.
    """
    autofit_scale: float | None = None
    """`normAutofit fontScale` the original file carries, as a fraction.

    Its presence is an admission by the source deck that the text does not fit at its
    declared size — PowerPoint shrank it silently. The harness reports the overflow instead
    of inheriting the shrink, so this is kept purely to explain *why* a slide that "looks
    fine in PowerPoint" measures as overflowing here.
    """
    bullet: str = "none"
    """`none`, `bullet` or `number` — OOXML's `buNone` / `buChar` / `buAutoNum`."""
    indent: int = 0
    """List nesting level, 0-based, as `<a:pPr lvl>`."""
    align: str = "left"
    """Paragraph alignment as the file states it, resolved through the placeholder chain."""
    anchor: str = "top"
    """Vertical anchoring from `bodyPr`. PowerPoint bottom-anchors most title placeholders,
    and ignoring it puts every title in the wrong place on the slide."""
    color: str | None = None
    """Explicit text colour, or `None` to take the theme's ink."""
    geometry: Geometry | None = None
    chart: ChartSpec | None = None
    table: TableSpec | None = None
    alt: str = ""
    """Alternative text. Required when the harness adds a picture: a deck that cannot be
    read aloud is a deck some of the audience cannot use."""
    source: str | None = None
    """Where an added asset came from, so a replacement can be traced."""
    type_spec: TypeSpec | None = None
    """Type as the *file* sets it, in canvas px.

    An imported slide is being previewed, not restyled, so its own sizes win over the
    theme's type scale. Substituting a theme role would make both the preview and the
    budget describe a slide that does not exist — a 12pt caption budgeted at 21px overflows
    on paper and nowhere else.
    """

    @property
    def markup(self) -> str:
        """The text with its emphasis, as `set_text` would have been asked for it."""
        return to_markup(self.runs) if self.runs else (self.text or "")

    @property
    def dirty(self) -> bool:
        """Does this shape differ from the imported file?

        Export patches dirty shapes and only dirty shapes. Deriving it from a comparison
        rather than latching a flag matters because the writer *hardens* every shape it
        patches — zeroing insets, disabling autofit — and doing that to a shape the user
        ended up not changing would restyle the original author's slide for nothing.

        Emphasis counts as a difference. Comparing the words alone let a bolded word render
        bold in the preview and export unbolded: the shape had changed in the only way that
        mattered and still measured clean.
        """
        if self.text != self.origin_text:
            return True
        return self.markup != (self.origin_markup or self.origin_text or "")


# ------------------------------------------------------------------------- slides


class Slide(BaseModel):
    id: str
    index: int
    mode: Mode
    notes: str = ""

    # managed
    layout: str | None = None
    blocks: list[Block] = Field(default_factory=list)

    # freeform
    origin: dict[str, Any] | None = None
    shapes: list[Shape] = Field(default_factory=list)
    duplicate_of: str | None = None
    """The slide this was copied from, when it was.

    Export clones that slide's OOXML part rather than rebuilding the copy from the model —
    the harness does not understand every shape it can duplicate, and a copy that quietly
    dropped the SmartArt would not be a copy.
    """
    hidden: bool = False
    """Skipped in a slideshow. `<p:sld show="0">` — the slide stays in the deck and in the
    file; only presentation skips it."""
    removed: list[int] = Field(default_factory=list)
    """`ooxml_id`s deleted from an imported slide.

    Recorded rather than acted on immediately: export patches the original package, so a
    deletion is an instruction to the exporter, not something the in-memory model can carry
    on its own.
    """
    inherited: list[Shape] = Field(default_factory=list)
    """Shapes from the slide's layout and master — logos, footer bars, decorative rules.

    Rendered beneath the slide's own shapes and never editable or exported: they belong to
    the layout, not to this slide. Omitting them makes every preview look bare in a way the
    real deck is not.
    """

    def block(self, block_id: str) -> Block | None:
        return next((b for b in self.blocks if b.id == block_id), None)

    def shape(self, shape_id: str) -> Shape | None:
        return next((s for s in self.shapes if s.id == shape_id), None)


class Deck(BaseModel):
    id: str
    title: str = ""
    theme: Theme
    slides: list[Slide] = Field(default_factory=list)
    source_path: str | None = None
    """Original .pptx for imported decks — exports mutate this package, never rebuild it."""
    theme_from: str | None = None
    """The deck whose theme this one borrowed, for a deck that started from a template.

    Distinct from `source_path`, and the distinction is the whole point: a template lends its
    palette, faces and grid and **no slides**, so there is no package to patch and the
    exporter builds this deck from nothing. Kept because the person needs to be told which
    file the look came from — a theme that silently matches something is indistinguishable
    from one that was invented.
    """

    def slide(self, slide_id: str) -> Slide | None:
        return next((s for s in self.slides if s.id == slide_id), None)
