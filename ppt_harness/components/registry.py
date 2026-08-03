"""Component catalog — DESIGN §1.4, §1.5, §3.

Sixteen components. Each declares which regions it can occupy, how it divides one, and
what it **degrades to** when its content will not fit — the chain the repair ladder walks
before it considers touching anyone's words.

Two rules keep the catalog honest:

- **Degradation preserves content.** A chain may only run between components sharing a slot
  shape, so `stat_row → card_grid → bullets` moves the same `list` between three renderings
  and loses nothing. A chain that crossed shapes would silently drop a column.
- **Adding a variant beats adding a component.** A new component earns its place only when
  it needs both a new variant *and* a new slot shape.

The invariant that matters: **components own geometry**. A component declares which regions
it can occupy and how it divides one, and the expander turns that into boxes. No component
holds a coordinate, and no caller may pass one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SlotShape = Literal["title", "prose", "list", "media", "tabular", "chart"]


@dataclass(frozen=True)
class SlotSpec:
    """One slot a component consumes.

    `role` names the theme type-scale entry, which is what keeps a component from carrying
    a font size of its own.
    """

    shape: SlotShape
    role: str
    required: bool = True
    max_items: int | None = None
    max_lines: int = 3
    #: Fraction of the region's height this slot may occupy. Shares within a component sum
    #: to <= 1 after gaps.
    height_share: float = 1.0


@dataclass(frozen=True)
class Variant:
    name: str
    #: Column span within the region, out of the theme's column count. None = full region.
    columns: int | None = None
    #: For list components, how many items sit per row.
    per_row: int = 1
    align: Literal["left", "center"] = "left"
    overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Component:
    key: str
    slots: dict[str, SlotSpec]
    variants: dict[str, Variant]
    regions: tuple[str, ...]
    degrades_to: str | None = None
    purpose: str = ""

    @property
    def default_variant(self) -> str:
        return next(iter(self.variants))

    def slot_shapes(self) -> dict[str, SlotShape]:
        return {name: spec.shape for name, spec in self.slots.items()}


@dataclass(frozen=True)
class Region:
    """A band of the slide, expressed on the theme grid rather than in pixels."""

    name: str
    #: Vertical extent as a fraction of the content box (after margins).
    top: float
    height: float
    columns: tuple[int, int] = (0, 12)
    """(start, span) on the theme's column grid."""


@dataclass(frozen=True)
class Layout:
    key: str
    regions: dict[str, Region]
    purpose: str = ""


# --------------------------------------------------------------------- layouts

LAYOUTS: dict[str, Layout] = {
    "title": Layout(
        key="title",
        purpose="Deck opener or closer.",
        regions={"hero": Region("hero", top=0.28, height=0.44)},
    ),
    "stack": Layout(
        key="stack",
        purpose="Title over a single body block. The default.",
        regions={
            "header": Region("header", top=0.0, height=0.18),
            "body": Region("body", top=0.24, height=0.76),
        },
    ),
    "two_col": Layout(
        key="two_col",
        purpose="Title over two side-by-side blocks.",
        regions={
            "header": Region("header", top=0.0, height=0.18),
            "left": Region("left", top=0.24, height=0.76, columns=(0, 6)),
            "right": Region("right", top=0.24, height=0.76, columns=(6, 6)),
        },
    ),
    "hero_plus_row": Layout(
        key="hero_plus_row",
        purpose="Title, a dominant block, and a row of supporting figures beneath it.",
        regions={
            "header": Region("header", top=0.0, height=0.16),
            "hero": Region("hero", top=0.20, height=0.52),
            "footer_row": Region("footer_row", top=0.76, height=0.24),
        },
    ),
    "full_bleed": Layout(
        key="full_bleed",
        purpose="Section break or full-slide image.",
        regions={"canvas": Region("canvas", top=0.0, height=1.0)},
    ),
}


# ------------------------------------------------------------------ components

COMPONENTS: dict[str, Component] = {
    "title_slide": Component(
        key="title_slide",
        purpose="Deck title with optional subtitle.",
        regions=("hero", "canvas"),
        slots={
            "title": SlotSpec("title", role="deck_title", max_lines=3, height_share=0.62),
            "prose": SlotSpec("prose", role="body", required=False, max_lines=2,
                              height_share=0.38),
        },
        variants={
            "left": Variant("left", align="left"),
            "centered": Variant("centered", align="center"),
        },
    ),
    "slide_title": Component(
        key="slide_title",
        purpose="The title line of a content slide.",
        regions=("header",),
        slots={"title": SlotSpec("title", role="slide_title", max_lines=2)},
        variants={"plain": Variant("plain")},
    ),
    "bullets": Component(
        key="bullets",
        purpose="A list of points. The general-purpose body block.",
        regions=("body", "left", "right", "canvas"),
        slots={
            "items": SlotSpec("list", role="body", max_items=8, max_lines=3),
        },
        variants={
            "plain": Variant("plain"),
            "two_col": Variant("two_col", per_row=2),
        },
    ),
    "agenda": Component(
        key="agenda",
        purpose="What this talk will cover, in order.",
        regions=("body", "left", "right", "canvas"),
        degrades_to="bullets",
        slots={"items": SlotSpec("list", role="body", max_items=8, max_lines=2)},
        variants={
            "list": Variant("list"),
            "two_col": Variant("two_col", per_row=2),
        },
    ),
    "stat_row": Component(
        key="stat_row",
        purpose="Three or four headline numbers with labels.",
        # `body` first, because the first region is the one a caller gets by default and the
        # `two_row` variant needs the height: two rows of figure-plus-label do not fit the
        # footer band, which is one row tall by design. `footer_row` stays for `flat` and
        # `carded`, which is what that band is for.
        regions=("body", "footer_row", "canvas"),
        degrades_to="card_grid",
        slots={
            # Two lines, because a stat *is* two lines: the figure, and the thing it
            # measures written under it. Declared as one for as long as the renderer dropped
            # the figure and drew only the label — the catalog was describing the bug.
            "items": SlotSpec("list", role="stat", max_items=4, max_lines=2,
                              height_share=1.0),
        },
        variants={
            "flat": Variant("flat", per_row=4, align="center"),
            "carded": Variant("carded", per_row=4, align="center"),
            "two_row": Variant("two_row", per_row=2, align="center"),
        },
    ),
    "card_grid": Component(
        key="card_grid",
        purpose="A grid of short titled blocks.",
        regions=("body", "hero", "canvas"),
        degrades_to="bullets",
        slots={
            "title": SlotSpec("title", role="block_title", required=False, max_lines=1,
                              height_share=0.18),
            "items": SlotSpec("list", role="body", max_items=6, max_lines=3,
                              height_share=0.82),
        },
        variants={
            "1x3": Variant("1x3", per_row=3),
            "2x2": Variant("2x2", per_row=2),
            "2x3": Variant("2x3", per_row=3),
        },
    ),
    "icon_row": Component(
        key="icon_row",
        purpose="A row of labelled marks — the same content as a card grid, lighter.",
        regions=("footer_row", "body", "canvas"),
        degrades_to="card_grid",
        slots={
            "title": SlotSpec("title", role="block_title", required=False, max_lines=1,
                              height_share=0.2),
            "items": SlotSpec("list", role="label", max_items=5, max_lines=2,
                              height_share=0.8),
        },
        variants={
            "icon_top": Variant("icon_top", per_row=5, align="center"),
            "icon_left": Variant("icon_left", per_row=2),
        },
    ),
    "timeline": Component(
        key="timeline",
        purpose="Ordered stages, with the order carrying meaning.",
        regions=("body", "hero", "canvas"),
        degrades_to="bullets",
        slots={
            "title": SlotSpec("title", role="block_title", required=False, max_lines=1,
                              height_share=0.18),
            "items": SlotSpec("list", role="body", max_items=6, max_lines=2,
                              height_share=0.82),
        },
        variants={
            "horizontal": Variant("horizontal", per_row=5, align="center"),
            "vertical": Variant("vertical", per_row=1),
        },
    ),
    "comparison": Component(
        key="comparison",
        purpose="Two sides set against each other.",
        regions=("body", "canvas"),
        # Terminal: a table holds `tabular`, and two `list` slots do not become one without
        # a conversion that would decide for the author which column is which.
        degrades_to=None,
        slots={
            "title": SlotSpec("title", role="block_title", required=False, max_lines=1,
                              height_share=0.16),
            "left": SlotSpec("list", role="body", max_items=5, max_lines=2,
                             height_share=0.42),
            "right": SlotSpec("list", role="body", max_items=5, max_lines=2,
                              height_share=0.42),
        },
        variants={
            "split": Variant("split", per_row=2),
            "table": Variant("table", per_row=2),
        },
    ),
    "data_table": Component(
        key="data_table",
        purpose="Rows and columns, when the numbers must be read exactly.",
        regions=("body", "hero", "canvas"),
        degrades_to=None,
        slots={
            # `max_lines` is rows, not lines in a cell: the slot holds the grid. `max_items`
            # caps the rows a person can read from the back of a room.
            "tabular": SlotSpec("tabular", role="label", max_items=10, max_lines=12,
                                height_share=0.82),
            "prose": SlotSpec("prose", role="caption", required=False, max_lines=2,
                              height_share=0.18),
        },
        variants={"plain": Variant("plain"), "zebra": Variant("zebra")},
    ),
    "chart": Component(
        key="chart",
        purpose="A figure the recipient can still edit. Never an image.",
        regions=("hero", "body", "left", "right", "canvas"),
        degrades_to=None,
        slots={
            # No text budget: the plot fills the frame. `max_items` bounds the categories,
            # which is a legibility limit rather than a fitting one.
            "chart": SlotSpec("chart", role="label", max_items=12, max_lines=1,
                              height_share=0.84),
            "prose": SlotSpec("prose", role="caption", required=False, max_lines=2,
                              height_share=0.16),
        },
        variants={"wide": Variant("wide"), "half": Variant("half", columns=6)},
    ),
    "image_full": Component(
        key="image_full",
        purpose="One picture carrying the slide.",
        regions=("canvas", "hero", "body"),
        degrades_to="image_split",
        slots={
            "media": SlotSpec("media", role="caption", height_share=0.86),
            "prose": SlotSpec("prose", role="caption", required=False, max_lines=2,
                              height_share=0.14),
        },
        variants={"bleed": Variant("bleed"), "inset": Variant("inset")},
    ),
    "image_split": Component(
        key="image_split",
        purpose="A picture beside the point it is making.",
        regions=("body", "hero", "canvas"),
        # Terminal: `bullets` has nowhere to put the picture. When this will not fit, the
        # honest answer is to split the slide, which the repair ladder says out loud.
        degrades_to=None,
        slots={
            "title": SlotSpec("title", role="block_title", required=False, max_lines=2,
                              height_share=0.2),
            "media": SlotSpec("media", role="caption", height_share=0.52),
            "prose": SlotSpec("prose", role="body", max_lines=4, height_share=0.28),
        },
        variants={
            "image_left": Variant("image_left", per_row=2),
            "image_right": Variant("image_right", per_row=2),
        },
    ),
    "quote": Component(
        key="quote",
        purpose="Someone else's words, given room.",
        regions=("canvas", "hero", "body"),
        # Terminal: a quotation is prose, and `bullets` would turn one sentence into a
        # list of fragments — a change to what was said, not to how it is laid out.
        degrades_to=None,
        slots={
            "prose": SlotSpec("prose", role="block_title", max_lines=6,
                              height_share=0.72),
            "title": SlotSpec("title", role="label", required=False, max_lines=2,
                              height_share=0.28),
        },
        variants={
            "pull": Variant("pull"),
            "full_bleed": Variant("full_bleed", align="center"),
        },
    ),
    "takeaway": Component(
        key="takeaway",
        purpose="The one sentence the audience should leave with.",
        regions=("body", "footer_row", "canvas", "hero"),
        degrades_to=None,
        slots={
            "title": SlotSpec("title", role="block_title", max_lines=3,
                              height_share=0.66),
            "items": SlotSpec("list", role="label", required=False, max_items=3,
                              max_lines=1, height_share=0.34),
        },
        variants={"bar": Variant("bar"), "centered": Variant("centered", align="center")},
    ),
    "section_break": Component(
        key="section_break",
        purpose="A divider announcing the next section.",
        regions=("canvas", "hero"),
        slots={
            "title": SlotSpec("title", role="slide_title", height_share=0.6),
            "prose": SlotSpec("prose", role="body", required=False, max_lines=2,
                              height_share=0.4),
        },
        variants={"bar": Variant("bar"), "centered": Variant("centered", align="center")},
    ),
}


#: Which components may fill which region, resolved once so the tool layer can answer
#: "what can go here" without scanning.
def components_for(region: str) -> list[str]:
    return [key for key, comp in COMPONENTS.items() if region in comp.regions]


def degradation_chain(key: str) -> list[str]:
    """The components `key` falls back through, nearest first.

    Walked by the repair ladder before it considers editing anyone's words. Cycles are
    impossible by construction — each link points at a component with the same slot shape
    and less room needed — but the guard stays, because a catalog is edited by hand.
    """
    chain: list[str] = []
    seen = {key}
    current = COMPONENTS.get(key)
    while current is not None and current.degrades_to:
        nxt = current.degrades_to
        if nxt in seen:
            break
        chain.append(nxt)
        seen.add(nxt)
        current = COMPONENTS.get(nxt)
    return chain


def swappable(key: str) -> list[str]:
    """Components that consume the same slot shapes, so a swap cannot lose content.

    This is what `set_component` is allowed to do. Anything outside this list would drop a
    slot on the floor, which is a data loss dressed up as a layout change.
    """
    comp = get(key)
    shapes = set(comp.slot_shapes().values())
    return sorted(
        other.key for other in COMPONENTS.values()
        if other.key != key and set(other.slot_shapes().values()) & shapes
        and set(other.slot_shapes().values()) >= {
            spec.shape for spec in comp.slots.values() if spec.required
        }
    )


def get(key: str) -> Component:
    if key not in COMPONENTS:
        raise KeyError(f"unknown component {key!r}; have {sorted(COMPONENTS)}")
    return COMPONENTS[key]


def layout(key: str) -> Layout:
    if key not in LAYOUTS:
        raise KeyError(f"unknown layout {key!r}; have {sorted(LAYOUTS)}")
    return LAYOUTS[key]


def catalog() -> list[dict[str, Any]]:
    """The always-on context form — keys and one-line purposes only (DESIGN §8.1 level 2).

    Full slot schemas stay behind `list_components(key)`; inlining them costs a couple of
    thousand tokens on every turn for information the model needs on maybe one.
    """
    return [
        {
            "key": comp.key,
            "purpose": comp.purpose,
            "variants": list(comp.variants),
            "regions": list(comp.regions),
        }
        for comp in COMPONENTS.values()
    ]


def describe(key: str) -> dict[str, Any]:
    comp = get(key)
    return {
        "key": comp.key,
        "purpose": comp.purpose,
        "regions": list(comp.regions),
        "variants": {
            name: {"columns": v.columns, "per_row": v.per_row, "align": v.align}
            for name, v in comp.variants.items()
        },
        "slots": {
            name: {
                "shape": spec.shape,
                "role": spec.role,
                "required": spec.required,
                "max_items": spec.max_items,
                "max_lines": spec.max_lines,
            }
            for name, spec in comp.slots.items()
        },
        "degrades_to": comp.degrades_to,
    }
