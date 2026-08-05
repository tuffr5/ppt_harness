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

import colorsys
from dataclasses import dataclass, replace

from ..state.document import Theme
from ..state.theme_default import contrast

#: Where a slide's light comes from, in OOXML degrees — the direction the gradient *runs*,
#: clockwise from due east, so 45° is a light source at the top left.
#:
#: One number, and it is deliberately a constant rather than a per-decoration field: a slide
#: whose card is lit from the left and whose staged panel is lit from the right does not read
#: as two lit objects, it reads as two flat gradients. A single light direction is most of
#: what makes a group of shapes look like one scene.
LIGHT_ANGLE = 45.0

#: The contrast a decorated background still has to give the theme's ink. The theme validates
#: its own pairs at load (DESIGN §2.6) so managed slides *cannot* fail contrast; a decoration
#: that painted underneath text would retire that guarantee, so every layer is composited
#: against the background and checked against the same threshold before it is drawn.
MIN_CONTRAST = 4.5

#: How far a layer is walked back when it fails that check — halving both the lightness
#: shift and the opacity converges on the theme's own validated role colour, which is the
#: only value known to be safe. Six halvings is well past the point where anything is visible.
_RETRIES = 6

#: Below this a wash is not a wash, it is a rounding error. A layer capped this far is
#: dropped instead, because a shape nobody can see still costs the recipient a shape.
_MIN_ALPHA = 0.02


@dataclass(frozen=True)
class Stop:
    """One stop of a gradient, as a *transform* of a role rather than as a colour.

    `lift` moves the role's lightness — positive towards white, negative towards black —
    holding hue and saturation. That is what keeps a dimensional gradient on one hue, which
    is the whole technique: a ramp between two different colours reads as two colours, and a
    ramp on one hue reads as a lit surface.
    """

    at: float
    """Where along the ramp, 0..1."""
    lift: float = 0.0
    alpha: float = 1.0


@dataclass(frozen=True)
class Layer:
    """One shape a decoration draws, back to front.

    A decoration used to be a single panel, so it could be described by a fill and a line.
    Depth cannot: a raised card is a lit panel *and* the shadow it casts, and a staged one is
    also the ground it stands on. Each layer names where it goes and what it is made of; the
    expander turns `place` into a rectangle and the theme turns the roles into colour.
    """

    place: str
    """Which rectangle this occupies: `panel`, `contact`, `ground`, or `slide`.

    A name, resolved by `render/expand.decoration_box`, because DESIGN §1.4 says that module
    is the only one allowed to produce a coordinate — and a shadow offset written here would
    be exactly that.
    """
    preset: str = "roundRect"
    """OOXML preset geometry. Only shapes PowerPoint draws natively: no blur, no soft edge,
    no mask. An effect that has to be baked to a bitmap is not one this layer may ask for."""
    fill: str = ""
    """The palette role the layer's colour derives from."""
    kind: str = ""
    """`linear`, `radial`, or empty for a flat fill."""
    stops: tuple[Stop, ...] = ()
    line: str = ""


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
    layers: tuple[Layer, ...] = ()
    """The shapes this draws, back to front.

    Empty for the flat decorations, which are one panel and are described by `fill` and
    `line` alone; `layers_for` synthesises that panel so both kinds arrive at the writer as
    the same list and neither the exporter nor the preview has a branch for depth.
    """


# --------------------------------------------------------------------- the layers
#
# Four techniques, all of them plain geometry and gradient paint, because those are the two
# things OOXML draws natively. Nothing here needs a blur, a soft edge, an inner shadow or a
# mask: those either rasterise on export or are dropped by whichever renderer opens the file
# next, and a decoration the recipient cannot see is worse than one nobody asked for.


#: A panel lit from one side. Three stops on one hue, varying only in lightness — light,
#: dark, light — because a two-stop ramp reads as flat however strong its contrast is. The
#: middle stop is the whole effect: it is the terminator, and the third stop is the bounce
#: light off whatever the panel is standing on. Take the middle one out and the card goes
#: back to looking like a rectangle somebody filled in.
DIMENSIONAL = (Stop(0.0, lift=0.035), Stop(0.55, lift=-0.030), Stop(1.0, lift=0.018))

#: The dark an object puts on the surface directly beneath it. A radial ramp to nothing on an
#: ellipse — *not* a blurred rectangle, which is the usual way to draw this and which does not
#: survive export. Three stops rather than two so the falloff is soft; two gives a visible
#: edge where the shadow ends, which reads as a grey ellipse rather than as shade.
CONTACT = (Stop(0.0, alpha=0.30), Stop(0.55, alpha=0.13), Stop(1.0, alpha=0.0))

#: The surface the object is standing on. Wider and shallower than the contact shadow and
#: much fainter, fading to the background colour, so the hero sits in a place instead of
#: floating in the middle of nothing.
GROUND = (Stop(0.0, alpha=0.14), Stop(0.6, alpha=0.06), Stop(1.0, alpha=0.0))

#: The cheapest move from "blank white" to "designed": one very faint radial in the theme's
#: own brand colour, behind everything. One shape for a whole slide.
WASH = (Stop(0.0, alpha=0.13), Stop(0.55, alpha=0.07), Stop(1.0, alpha=0.0))


DECORATIONS: dict[str, Decoration] = {
    "card": Decoration(shapes=("list",), fill="surface", line="rule", pad=2,
                       shape_key="card_fill"),
    "banded": Decoration(shapes=("tabular",), fill="surface"),
    # No paint at all: `inset` is a margin, and a picture that gained a border would be the
    # variant restyling the image rather than framing it.
    "inset": Decoration(shapes=("media",), pad=5),
    # `card` with the two things that make a card look like an object: a gradient that gives
    # it a lit face, and the shade it drops on what is behind it. No outline — a stroke round
    # a lit panel flattens it again by drawing the same edge the light was describing.
    "raised": Decoration(
        shapes=("list",), pad=2, shape_key="card_fill",
        layers=(
            Layer(place="contact", preset="ellipse", fill="ink", kind="radial",
                  stops=CONTACT),
            Layer(place="panel", preset="roundRect", fill="surface", kind="linear",
                  stops=DIMENSIONAL),
        ),
    ),
    # The same panel, staged: an object floating in empty canvas looks pasted on, so it is
    # given a ground plane to stand on and a contact shadow where it meets it. Two shapes
    # more than `raised`, and they are the two that turn a floating rectangle into a scene.
    "staged": Decoration(
        shapes=("title",), pad=4, shape_key="card_fill",
        layers=(
            Layer(place="ground", preset="ellipse", fill="ink", kind="radial", stops=GROUND),
            Layer(place="contact", preset="ellipse", fill="ink", kind="radial",
                  stops=CONTACT),
            Layer(place="panel", preset="roundRect", fill="surface", kind="linear",
                  stops=DIMENSIONAL),
        ),
    ),
    # One shape for the whole slide, drawn before everything else. It dresses the `title`
    # shape because every component that offers it has one, but its layer is slide-scoped —
    # see `slide_layers`, which is what stops a two-slot component washing its slide twice.
    "washed": Decoration(
        shapes=("title",),
        layers=(Layer(place="slide", preset="ellipse", fill="brand", kind="radial",
                      stops=WASH),),
    ),
}


#: Width of a decoration's outline, in canvas px — the unit the type scale and the preview
#: are already in. One place, because the preview is the file rendered and a second copy of
#: this number is the shape of the bug that makes them differ.
LINE_PX = 1.0


@dataclass(frozen=True)
class Stopped:
    """One resolved gradient stop: a literal colour, at a position, at an opacity."""

    at: float
    colour: str
    alpha: float = 1.0


@dataclass(frozen=True)
class Paint:
    """A decoration resolved against one theme. Empty means nothing is drawn."""

    fill: str = ""
    line: str = ""
    radius: float = 0.0
    stops: tuple[Stopped, ...] = ()
    """A gradient's resolved ramp, or empty for a flat fill. Never fewer than three when it
    is set: two stops is the ramp that reads as a flat fill, which is what this is for."""
    kind: str = ""
    """`linear`, `radial`, or empty."""
    angle: float = LIGHT_ANGLE
    preset: str = "roundRect"
    place: str = "panel"
    """Which rectangle to draw it in — the expander's word, carried so the writer and the
    preview ask the same question and cannot answer it differently."""

    @property
    def visible(self) -> bool:
        return bool(self.fill or self.line or self.stops)


def _for(name: str, shape: str) -> Decoration | None:
    found = DECORATIONS.get(name)
    return found if found is not None and shape in found.shapes else None


def _colour(theme: Theme, role: str) -> str:
    value = theme.palette.get(role) if role else None
    return value if isinstance(value, str) else ""


# ------------------------------------------------------------------ colour maths


def _rgb(colour: str) -> tuple[float, float, float]:
    value = colour.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in rgb)


def lift(colour: str, amount: float) -> str:
    """The same hue at a different lightness.

    Lightness only, in HLS. A "darker blue" mixed towards black in RGB desaturates as it
    goes, so a three-stop ramp built that way is not one hue any more — it is a hue and a
    grey, which is the thing that makes a gradient look cheap.
    """
    if not colour or not amount:
        return colour
    hue, lum, sat = colorsys.rgb_to_hls(*_rgb(colour))
    return _hex(colorsys.hls_to_rgb(hue, max(0.0, min(1.0, lum + amount)), sat))


def over(colour: str, alpha: float, base: str) -> str:
    """`colour` at `alpha` composited on `base` — what the eye actually receives.

    Contrast is a property of the light that reaches the reader, so a translucent layer has
    to be flattened before it can be measured. Doing it here rather than trusting an opacity
    to be "obviously small enough" is the difference between a validated theme and a
    validated theme with a wash on top of it.
    """
    if alpha >= 1.0:
        return colour
    front, back = _rgb(colour), _rgb(base)
    return _hex(tuple(f * alpha + b * (1 - alpha)
                      for f, b in zip(front, back, strict=True)))  # type: ignore[arg-type]


def _legible(theme: Theme, colour: str, alpha: float) -> bool:
    """Would the theme's ink still read over this layer?"""
    ink = _colour(theme, "ink") or "#000000"
    bg = _colour(theme, "bg") or "#FFFFFF"
    return contrast(ink, over(colour, alpha, bg)) >= MIN_CONTRAST


def _capped(theme: Theme, base: str, stops: tuple[Stop, ...]) -> tuple[Stopped, ...]:
    """`stops` resolved against `base`, walked back until every one of them is legible.

    The theme validates `ink` against `bg` and `surface` at load, which is what lets DESIGN
    say a managed slide cannot fail contrast. A decoration paints *underneath text*, so it
    is the one thing that could take that away. Rather than trust the numbers above to be
    small — they are, on the four shipped themes — the whole ramp is halved towards the
    role's own validated colour until the worst stop passes, and dropped entirely if that
    never happens. A slide loses its gradient before it loses its legibility.
    """
    for _ in range(_RETRIES):
        resolved = tuple(Stopped(at=s.at, colour=lift(base, s.lift), alpha=s.alpha)
                         for s in stops)
        if all(_legible(theme, s.colour, s.alpha) for s in resolved):
            return resolved
        # An opaque stop keeps its opacity and gives up its lightness shift: halving the
        # alpha of a card would make the card translucent, which is a different decoration.
        # A translucent one gives up both, so a wash fades out rather than turning grey.
        stops = tuple(replace(s, lift=s.lift / 2,
                              alpha=s.alpha if s.alpha >= 1.0 else s.alpha / 2)
                      for s in stops)
        if max((s.alpha for s in stops), default=0.0) < _MIN_ALPHA:
            break
    return ()


def pad_for(theme: Theme, name: str, shape: str) -> float:
    """The space a decoration leaves between its edge and the text, in canvas px."""
    found = _for(name, shape)
    if found is None or found.pad is None or not theme.spacing:
        return 0.0
    return float(theme.spacing[min(found.pad, len(theme.spacing) - 1)])


def _role(theme: Theme, found: Decoration, declared: str) -> str:
    """The palette role a decoration's fill actually resolves through.

    A theme may redirect it — `shape.card_fill` — and may say `none`, which is a theme
    drawing the decoration as an outline rather than opting out of it.
    """
    named = str(theme.shape.get(found.shape_key) or "") if found.shape_key else ""
    return "" if named == "none" else (named or declared)


def paint_for(theme: Theme, name: str, shape: str) -> Paint:
    """The colours a decoration's *panel* resolves to, through the theme.

    The flat answer, and the one a table's row banding needs: it wants a colour to fill a
    cell with, and a cell cannot hold a shadow. `layers_for` is what anything drawing shapes
    should ask.
    """
    found = _for(name, shape)
    if found is None:
        return Paint()
    return Paint(fill=_colour(theme, _role(theme, found, found.fill)),
                 line=_colour(theme, found.line),
                 radius=float(theme.shape.get("radius") or 0.0))


def layers_for(theme: Theme, name: str, shape: str) -> tuple[Paint, ...]:
    """Every shape a decoration draws, resolved against the theme, back to front.

    Order is the contract. `spTree` is painted in document order and so is the preview, so a
    shadow resolved after its panel would be drawn on top of it — a dark ellipse across the
    card rather than underneath it. The list is emitted in the order it must be drawn and
    both writers walk it without sorting.

    A decoration with no layers of its own yields the one flat panel it has always drawn, so
    `card` and `raised` reach the writer as the same kind of thing.
    """
    found = _for(name, shape)
    if found is None:
        return ()
    radius = float(theme.shape.get("radius") or 0.0)
    if not found.layers:
        flat = paint_for(theme, name, shape)
        return (flat,) if flat.visible else ()

    out: list[Paint] = []
    for layer in found.layers:
        # The theme's redirect applies to the *panel* and nowhere else. `card_fill` answers
        # "what is a card filled with"; it does not answer "what colour is shade". Applied to
        # every layer it painted the contact shadow in the surface colour — a near-white
        # ellipse under a near-white card, which is a shape the recipient pays for and cannot
        # see. The preview caught it: the export's renderer drew *something* there, and only
        # comparing the two said what.
        role = _role(theme, found, layer.fill) if layer.place == "panel" else layer.fill
        base = _colour(theme, role)
        if not base:
            continue
        stops = _capped(theme, base, layer.stops) if layer.stops else ()
        if layer.stops and not stops:
            # The theme's own colours cannot carry this layer without pushing text under
            # threshold. Dropping it is the only safe answer, and it is silent by design:
            # every other layer of the decoration still draws.
            continue
        out.append(Paint(
            fill="" if stops else base,
            line=_colour(theme, layer.line),
            radius=radius,
            stops=stops,
            kind=layer.kind if stops else "",
            angle=LIGHT_ANGLE,
            preset=layer.preset,
            place=layer.place,
        ))
    return tuple(p for p in out if p.visible)


def slide_layers(theme: Theme, worn: list[tuple[str, str]]) -> tuple[Paint, ...]:
    """The layers that dress the whole slide rather than one slot, given what it wears.

    `worn` is `(decoration, slot shape)` for every slot on the slide. The first slide-scoped
    layer found wins and the rest are ignored: a wash is a property of the slide, and two
    components that both asked for one would otherwise paint it twice — which is visible,
    because two 13% ellipses are one 24% ellipse.
    """
    for name, shape in worn:
        found = tuple(p for p in layers_for(theme, name, shape) if p.place == "slide")
        if found:
            return found
    return ()
