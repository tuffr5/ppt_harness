"""Mode transitions — DESIGN §1.1, §7, PLAN B5.

The two doors between the tiers.

**Eject** is one-way and always safe: a managed slide already knows its own geometry, so
freezing it into absolute shapes loses nothing. It exists because the catalog will never
cover everything, and a harness with no exit is one people work around.

**Adopt** is the hard direction, and it is a *proposal*. Recognising four evenly-spaced
boxes each holding a big number and a small label as a `stat_row` is pattern-matching, not
understanding — and acting on it reflows the slide. So the classifier reports what it
thinks and how sure it is, and a person decides. DESIGN §7: never a silent inference.

The classifier reads **arrangement**, not meaning: how many shapes, how regular their sizes,
whether they line up, how their type sizes relate. That is deliberately shallow. A deeper
one would be right more often and wrong less legibly, and the failure mode of a confident
wrong reflow is a slide someone has to rebuild.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..components import decoration, overrides, registry
from ..state.document import (
    Block,
    Geometry,
    GradientStop,
    Mode,
    Shape,
    Slide,
    Theme,
)

#: Below this, the classifier says it does not know rather than guessing.
MIN_CONFIDENCE = 0.55

#: Two lengths within this ratio count as "the same size" to the eye.
SAME_SIZE = 0.12

#: Two edges within this fraction of the slide count as aligned.
ALIGNED = 0.02


@dataclass
class Guess:
    component: str
    variant: str
    confidence: float
    because: list[str] = field(default_factory=list)
    slots: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"component": self.component, "variant": self.variant,
                "confidence": round(self.confidence, 2), "because": self.because}


# ------------------------------------------------------------------------ eject


def eject(slide: Slide, theme: Theme, cx: int, cy: int,
          assets: dict[str, tuple[str, bytes]] | None = None) -> list[Shape]:
    """Freeze a managed slide's blocks into absolute shapes.

    Lossless by construction: the expander already decides every box, so this writes down
    what it decided. What is lost is the *ability to re-derive* it — which is exactly what
    makes the door one-way.

    "Lossless" has to mean everything the slide *drew*, not everything it said. This used to
    freeze text and only text, so ejecting a slide built from a decorated variant — `carded`,
    `zebra`, `table` — silently dropped its panels, and ejecting an `image_full` or
    `image_split` slide dropped the picture the slide was about. Both are one-way losses: the
    blocks are gone by the time anyone notices, and there is nothing left to re-derive them
    from. A door that quietly costs you the cards is a door people stop trusting.

    Emitted in the order the writer writes: the panel, then the picture or the words in front
    of it. `spTree` is painted in document order, so a card frozen after its text would cover
    the words it is behind.
    """
    from ..render import expand

    canvas_w, canvas_h = theme.grid.canvas
    frozen: list[Shape] = []
    laid_out_slots = expand.expand_slide(theme, slide)

    # The slide-wide layers first, and once — they are behind everything, and a wash frozen
    # per slot would be as many washes as the slide has slots.
    worn = [(item.decoration, item.shape) for item in laid_out_slots if item.decoration]
    for layer in decoration.slide_layers(theme, worn):
        box = expand.decoration_box(theme, expand.content_box(theme), layer.place)
        frozen.append(_frozen_panel(box, layer, theme, canvas_w, canvas_h, cx, cy,
                                    slide.id))

    for laid_out in laid_out_slots:
        block = slide.block(laid_out.block_id)
        if block is None:
            continue
        value = block.slots.get(laid_out.slot)
        if not value:
            continue

        if laid_out.shape == "media":
            frozen.extend(_frozen_picture(laid_out, value, theme, cx, cy, assets))
            continue

        # The same cells the writer emits, from the same function, because a slide that
        # ejected to a different arrangement from the one it exports would make this door a
        # reflow rather than a freeze.
        cells = expand.written_cells(laid_out, value)
        panels = laid_out.panels(len(cells))
        layers = decoration.layers_for(theme, laid_out.decoration, laid_out.shape)
        for index, ((text, cell), panel) in enumerate(zip(cells, panels, strict=True)):
            if not text:
                continue
            name = f"{laid_out.block_id}_{laid_out.slot}"
            if len(cells) > 1:
                name = f"{name}_{index}"
            for layer in layers:
                if layer.place == "slide":
                    continue  # frozen once for the slide, above
                box = expand.decoration_box(theme, panel, layer.place)
                frozen.append(_frozen_panel(box, layer, theme, canvas_w, canvas_h,
                                            cx, cy, name))
            x, y, w, h = cell.emu(canvas_w, canvas_h, cx, cy)
            frozen.append(Shape(
                id=name,
                ooxml_id=0,
                type="text_box",
                frame={"x": x, "y": y, "cx": w, "cy": h},
                role=laid_out.role,
                text=text,
                align=laid_out.align,
                type_spec=laid_out.spec,
            ))
    return frozen


def _frozen_panel(panel, paint: decoration.Paint, theme: Theme, canvas_w: int,
                  canvas_h: int, cx: int, cy: int, name: str) -> Shape:
    """One decoration layer as a real autoshape.

    A `Geometry`, which is the model's word for "a shape that is drawn" — so the frozen panel
    is the same kind of thing an imported autoshape is, and the preview and the writer that
    already handle those need nothing new. Frozen rather than refused: the alternative is
    telling a model that used `carded` that it may not leave managed mode, and the pressure
    valve exists precisely so that answer is never necessary.

    The gradient is written down here, stop for stop, for the same reason the box is: eject
    is one-way. A frozen panel that kept only its flat fill would leave managed mode as a
    grey rectangle where the slide had a lit one, and there would be nothing to compare it
    against afterwards.
    """
    x, y, w, h = panel.emu(canvas_w, canvas_h, cx, cy)
    return Shape(
        id=f"{name}_{paint.place}",
        ooxml_id=0,
        type="shape",
        frame={"x": x, "y": y, "cx": w, "cy": h},
        geometry=Geometry(preset=paint.preset, fill=paint.fill or None,
                          line=paint.line or None,
                          line_width_pt=decoration.LINE_PX * 0.75,
                          gradient=paint.kind or "",
                          gradient_angle=paint.angle,
                          stops=[GradientStop(at=s.at, colour=s.colour, alpha=s.alpha)
                                 for s in paint.stops]),
    )


def _frozen_picture(laid_out, value, theme: Theme, cx: int, cy: int,
                    assets: dict[str, tuple[str, bytes]] | None) -> list[Shape]:
    """A `media` slot as a real picture shape, in the rectangle the writer would have used.

    The frame carries the picture's own proportions — `Box.fit`, the same call the writer
    makes — so the frozen shape is stretched into a rectangle that already fits it and the
    ejected slide is pixel-for-pixel the managed one. An asset nothing is behind freezes
    nothing: there is no picture to lose, and inventing a placeholder would put a shape on
    the slide that the managed version never had.
    """
    from . import media as media_mod

    found = media_mod.payload(value)
    if found is None:
        return []
    asset_id, alt = found
    asset = media_mod.resolve(asset_id, assets)
    if asset is None:
        return []

    placed = (laid_out.box
              .scaled(overrides.media_factor(laid_out.overrides))
              .fit(asset.aspect))
    x, y, w, h = placed.emu(*theme.grid.canvas, cx, cy)
    return [Shape(
        id=f"{laid_out.block_id}_{laid_out.slot}",
        ooxml_id=0,
        type="picture",
        frame={"x": x, "y": y, "cx": w, "cy": h},
        asset=asset_id,
        alt=alt,
        # No `source`. The picture is the deck's asset and has no path on this machine —
        # it may never have had one — and `_write_ejected` resolves `asset` first for
        # exactly that reason. A frozen shape carrying a path would make an ejected slide
        # the one part of the deck whose content lives outside it.
    )]


# -------------------------------------------------------------------- classifier


def _spread(values: list[float]) -> float:
    """How unlike each other these numbers are, 0 = identical."""
    if not values:
        return 1.0
    biggest = max(values)
    return 0.0 if biggest == 0 else (biggest - min(values)) / biggest


def _text_shapes(slide: Slide) -> list[Shape]:
    return [s for s in slide.shapes if s.text and not s.opaque]


def _signals(slide: Slide, theme: Theme) -> dict[str, Any]:
    """What the arrangement looks like. Numbers only — no reading of the words."""
    shapes = _text_shapes(slide)
    canvas_w = theme.grid.canvas[0] or 1
    return {
        "count": len(shapes),
        "size_spread": _spread([float(s.frame.cx) for s in shapes]),
        "height_spread": _spread([float(s.frame.cy) for s in shapes]),
        "same_row": len({round(s.frame.y / max(1, canvas_w) / ALIGNED) for s in shapes}) == 1
        if shapes else False,
        "short_text": all(len(s.text or "") <= 24 for s in shapes) if shapes else False,
        "has_title": any((s.role or "").endswith("title") for s in slide.shapes),
    }


def classify(slide: Slide, theme: Theme) -> Guess | None:
    """What this slide's arrangement resembles, and how sure that is.

    Returns `None` below `MIN_CONFIDENCE` — "I do not know" is a better answer than a
    confident reflow of a slide nobody asked to change.
    """
    if slide.mode is not Mode.FREEFORM:
        return None
    shapes = _text_shapes(slide)
    if len(shapes) < 2:
        return None

    signals = _signals(slide, theme)
    body = [s for s in shapes if not (s.role or "").endswith("title")]
    because: list[str] = []
    score = 0.0

    regular = signals["size_spread"] <= SAME_SIZE and signals["height_spread"] <= SAME_SIZE
    if regular:
        score += 0.4
        because.append(f"{len(body)} shapes of near-identical size")
    if signals["same_row"]:
        score += 0.2
        because.append("aligned on one row")
    if signals["short_text"]:
        score += 0.15
        because.append("every label is short")

    if len(body) in (3, 4) and regular and signals["short_text"]:
        component, variant = "stat_row", "flat"
        score += 0.15
        because.append("three or four short figures reads as a stat row")
    elif len(body) in (4, 6) and regular:
        component, variant = "card_grid", "2x2" if len(body) == 4 else "2x3"
        because.append("an even grid of equal blocks")
        score += 0.1
    elif len(body) >= 3 and not regular:
        component, variant = "bullets", "plain"
        score += 0.25
        because.append("a stack of unequal text blocks reads as a list")
    else:
        return None

    ordered = sorted(body, key=lambda s: (s.frame.y, s.frame.x))
    return Guess(component=component, variant=variant, confidence=min(score, 0.95),
                 because=because,
                 slots={"items": [s.text or "" for s in ordered]})


def proposal(slide: Slide, theme: Theme) -> dict[str, Any] | None:
    """The before/after a person is shown before anything moves."""
    guess = classify(slide, theme)
    if guess is None or guess.confidence < MIN_CONFIDENCE:
        return None
    comp = registry.COMPONENTS.get(guess.component)
    if comp is None:
        return None
    return {
        **guess.as_dict(),
        "before": [{"id": s.id, "text": s.text} for s in _text_shapes(slide)],
        "after": {"component": guess.component, "variant": guess.variant,
                  "slots": guess.slots},
        "warning": ("adoption reflows the slide: shapes move to where the component puts "
                    "them, and anything the harness does not model is dropped"),
    }


def adopted_blocks(slide: Slide, theme: Theme, new_id) -> list[Block] | None:
    """The blocks an adoption would produce, or `None` if it is not confident enough."""
    guess = classify(slide, theme)
    if guess is None or guess.confidence < MIN_CONFIDENCE:
        return None

    blocks: list[Block] = []
    title = next((s for s in slide.shapes if (s.role or "").endswith("title") and s.text),
                 None)
    if title is not None:
        blocks.append(Block(id=new_id("bk"), region="header", component="slide_title",
                            variant="plain", slots={"title": title.text}))

    comp = registry.COMPONENTS.get(guess.component)
    region = "body"
    if comp and region not in comp.regions:
        region = comp.regions[0]
    blocks.append(Block(id=new_id("bk"), region=region, component=guess.component,
                        variant=guess.variant, slots=guess.slots))
    return blocks
