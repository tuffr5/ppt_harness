"""Variant decoration — DESIGN §3.

Some variants differ from their siblings in what they *draw* rather than in how they arrange
things, and the catalog had a row of pairs that differed only in a name: `stat_row` flat and
carded, `comparison` split and table, `data_table` plain and zebra, `image_full` bleed and
inset all expanded to the same boxes and exported the same shapes. The catalog was promising
a rendering nothing produced, which is worse than not offering it — a model that asks for
`carded` is told it got one.

Named here rather than described in the catalog, for the reason an override names a step
rather than a value: a fill written into `registry` would be the component catalog deciding
what the theme decides. A decoration names **roles**, and the palette answers; its padding
names a step on the theme's spacing scale. A deck stays internally consistent even where a
block is dressed.

Two rules keep this from leaking:

- **A decoration dresses a slot *shape*, not a slot.** `card` dresses `list`, `banded`
  dresses `tabular`, `inset` dresses `media`. A decorated variant therefore cannot put a
  panel behind its own title by accident, and no component has to name its slots twice.
- **Padding is geometry, so the expander applies it.** The budget then measures the box the
  text is actually written into. A decoration that inset the text at write time and not at
  measure time would make the measurer and the renderer disagree about the same slide, and
  the arbiter of that disagreement is a rejected write.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..state.document import Theme


@dataclass(frozen=True)
class Decoration:
    """What a variant draws, in roles and steps rather than colours and pixels."""

    shapes: tuple[str, ...]
    """Slot shapes this dresses. Anything else in the block is left undecorated."""
    fill: str = ""
    line: str = ""
    pad: int | None = None
    """Which step of `theme.spacing` sits between the decoration's edge and the text.

    `None` for a decoration that takes nothing out of the box — banding paints a row it does
    not touch, and a step index cannot say that, because step 0 is a real four px.
    """
    shape_key: str = ""
    """The `theme.shape` entry that may name a different fill role for this decoration.

    `"none"` there is a theme drawing the decoration as an outline, not a theme opting out
    of it: the variant still has to be distinguishable from its undecorated sibling, which
    is the whole reason it is in the catalog.
    """


DECORATIONS: dict[str, Decoration] = {
    "card": Decoration(shapes=("list",), fill="surface", line="rule", pad=2,
                       shape_key="card_fill"),
    "banded": Decoration(shapes=("tabular",), fill="surface"),
    # No paint at all: `inset` is a margin, and a picture that gained a border would be the
    # variant restyling the image rather than framing it.
    "inset": Decoration(shapes=("media",), pad=5),
}


#: Width of a decoration's outline, in canvas px — the unit the type scale and the preview
#: are already in. One place, because the preview is the file rendered and a second copy of
#: this number is the shape of the bug that makes them differ.
LINE_PX = 1.0


@dataclass(frozen=True)
class Paint:
    """A decoration resolved against one theme. Empty means nothing is drawn."""

    fill: str = ""
    line: str = ""
    radius: float = 0.0

    @property
    def visible(self) -> bool:
        return bool(self.fill or self.line)


def _for(name: str, shape: str) -> Decoration | None:
    found = DECORATIONS.get(name)
    return found if found is not None and shape in found.shapes else None


def _colour(theme: Theme, role: str) -> str:
    value = theme.palette.get(role) if role else None
    return value if isinstance(value, str) else ""


def pad_for(theme: Theme, name: str, shape: str) -> float:
    """The space a decoration leaves between its edge and the text, in canvas px."""
    found = _for(name, shape)
    if found is None or found.pad is None or not theme.spacing:
        return 0.0
    return float(theme.spacing[min(found.pad, len(theme.spacing) - 1)])


def paint_for(theme: Theme, name: str, shape: str) -> Paint:
    """The colours a decoration resolves to, through the theme."""
    found = _for(name, shape)
    if found is None:
        return Paint()
    named = str(theme.shape.get(found.shape_key) or "") if found.shape_key else ""
    role = "" if named == "none" else (named or found.fill)
    return Paint(fill=_colour(theme, role), line=_colour(theme, found.line),
                 radius=float(theme.shape.get("radius") or 0.0))
