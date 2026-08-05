"""HTML rendering — DESIGN §6.1.

A slide as one self-contained HTML document at the theme's canvas size.

Two jobs, and it is worth being clear which is which.

**Managed slides** — the ones the harness generates — render here for real. It controls the
component set and therefore the CSS, and the result measures 0.007 mean difference against
PowerPoint: layout-identical, the residual being font substitution.

**Imported slides** render here only as a *fallback* for machines with no Office renderer
installed. Measured against PowerPoint that path reaches 0.041 and still lacks bullets,
curved connectors, gradients and rotation — reimplementing PowerPoint is an endless tail, so
the real preview goes through `render/preview.py` instead. Degraded and labelled beats
absent.

`render_overlay` serves the primary path: a raster from a real renderer with the harness's
own measurement boxes drawn over it, in canvas coordinates.

The CSS is restricted to what OOXML can express. Anything the exporter cannot reproduce is
not allowed here, because showing something the export cannot is worse than showing
nothing.
"""

from __future__ import annotations

import html as html_escape
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..components import decoration, icons, overrides
from ..state import richtext
from ..state.document import Mode, Slide, Theme
from ..state.slots import STAT_LABEL_EM, is_stat
from . import svg
from .expand import (
    Box,
    LaidOutSlot,
    content_box,
    decoration_box,
    expand_slide,
    icon_stroke_px,
    written_icons,
)

#: Slot elements carry this so the measurement pass can find them without guessing.
PROBE_ATTR = "data-target"


@dataclass(frozen=True)
class RenderedSlide:
    slide_id: str
    mode: str
    html: str
    canvas: tuple[int, int]
    targets: list[str]


def _esc(text: Any) -> str:
    return html_escape.escape(str(text if text is not None else ""))


def _font(theme: Theme, family: str) -> str:
    return theme.type.families.get(family, family)


def _emphasised(text: Any) -> str:
    """One line of slot text as HTML, with its emphasis.

    Slots hold markup the same way `set_text` accepts it on a freeform shape; the words a
    model wants stressed are content, and a slide composed by the harness has no less right
    to them than one it imported. Everything outside the emphasis stays escaped words.
    """
    return richtext.to_html(richtext.parse(str(text)), _esc)


def _text_html(value: Any, accent: str = "") -> str:
    """Slot content to markup.

    A `list` slot becomes one line per item, matching `_slot_text` in the exporter, so the
    preview cannot disagree with the file about how many lines there are.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return "<br>".join(_emphasised(line) for line in value.split("\n"))
    if isinstance(value, list):
        return "<br>".join(_item_html(item, accent) for item in value)
    return _item_html(value, accent)


def _item_html(item: Any, accent: str = "") -> str:
    """One list item as markup.

    Shared by the stacked and the multi-column renderings. It has to be, and the reason is a
    bug that shipped: the grid path passed each item back through `_text_html`, which only
    unpacked a dict when it found one *inside a list*, so every stat cell rendered the
    literal `{'value': '8.3%', 'label': 'EMEA churn'}` on the slide.
    """
    if not isinstance(item, dict):
        return _emphasised(item)
    figure, label = item.get("value"), item.get("label")
    if is_stat(item):
        # The figure carries the slot's own type; the label is set small beneath it *on a
        # full line box*, so the analytic pass and the render still agree on the line count.
        # The figure takes the block's accent — a role from the theme's ordered ramp, never
        # a colour anyone chose here. It is the one place a generated deck shows the palette
        # it borrowed, and without it every theme renders as black text on white.
        tint = f' style="color:{accent}"' if accent else ""
        return (f'<span class="stat"{tint}>{_emphasised(figure)}</span>'
                f'<br><span class="stat-label">{_emphasised(label)}</span>')
    head = label or figure or ""
    desc = item.get("desc")
    return _emphasised(f"{head} — {desc}" if desc else head)


# ---------------------------------------------------------------------- styling


def _base_css(theme: Theme) -> str:
    w, h = theme.grid.canvas
    bg = theme.palette.get("bg", "#FFFFFF")
    ink = theme.palette.get("ink", "#111111")
    return f"""
*, *::before, *::after {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: #6b7280; }}
.slide {{
  position: relative;
  width: {w}px; height: {h}px;
  background: {bg}; color: {ink};
  overflow: hidden;
  /* The canvas is a fixed pixel stage. The page scales it with a transform so one set of
     numbers serves the preview, the measurement pass, and the export. */
  transform-origin: top left;
}}
.slot {{
  position: absolute;
  /* Insets are zero here because they are zero in the emitted OOXML. Any padding would
     make the preview lie about capacity. */
  padding: 0; margin: 0;
  overflow: hidden;
  /* A column flexbox is how `bodyPr@anchor` is honoured. PowerPoint bottom-anchors most
     title placeholders, and top-anchoring everything puts titles in the wrong place. */
  display: flex; flex-direction: column;
}}
.slot.anchor-top {{ justify-content: flex-start; }}
.slot.anchor-center {{ justify-content: center; }}
.slot.anchor-bottom {{ justify-content: flex-end; }}
/* Lists are drawn with real markers rather than typed-in characters, so the words the
   harness measures are the words on the slide. */
.ink.list-bullet, .ink.list-number {{ margin: 0; padding-left: 1.4em; }}
.ink.list-bullet {{ list-style: disc outside; }}
.ink.list-number {{ list-style: decimal outside; }}
.ink a {{ color: inherit; text-decoration: underline; }}
.ink {{
  /* The measured element. It must be free to grow past its parent, because `scrollHeight`
     on a fixed-height box is clamped to that height and would report every slot as exactly
     full — hiding underflow and overflow alike. */
  display: block;
  height: auto;
  white-space: normal;
  overflow-wrap: break-word;
  word-break: normal;
}}
.slot.overflowing {{ outline: 2px solid #dc2626; outline-offset: 2px; }}
/* The panel a decorated variant draws — `stat_row/carded`, `comparison/table`. Behind the
   words rather than part of them, so it is never probed: the measured element is still the
   slot's text, and the box it is drawn in is the one the exporter paints. */
.panel {{ position: absolute; }}
/* A stat is a figure with its subject written under it. The label is set small and quiet but
   keeps a full line box, so what the browser lays out and what the analytic pass counts stay
   the same number of lines. */
.stat-label {{
  font-size: {STAT_LABEL_EM}em; font-weight: 500; letter-spacing: .02em; opacity: .68;
}}
.opaque {{
  position: absolute;
  border: 1px dashed rgba(0,0,0,.28);
  background: repeating-linear-gradient(45deg,
      rgba(0,0,0,.03) 0 8px, rgba(0,0,0,.06) 8px 16px);
  font: 500 12px/1.3 ui-sans-serif, system-ui, sans-serif;
  color: rgba(0,0,0,.5);
  display: flex; align-items: center; justify-content: center;
  text-align: center; padding: 4px;
}}
.asset {{
  position: absolute;
  background: rgba(0,0,0,.05);
  border: 1px solid rgba(0,0,0,.12);
  display: flex; align-items: center; justify-content: center;
  font: 500 12px/1.3 ui-sans-serif, system-ui, sans-serif;
  color: rgba(0,0,0,.45);
}}
.geom {{
  position: absolute; inset: 0;
  width: 100%; height: 100%;
  overflow: visible; pointer-events: none;
}}
.chart {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
.shape {{ position: absolute; }}
.shape > .ink {{ position: relative; }}
.asset img {{
  /* `fill`, because OOXML stretches a picture into its frame unless it is cropped, and the
     frame is the geometry the exporter writes. `contain` would preview a different crop
     from the one the file actually has. */
  width: 100%; height: 100%; object-fit: fill; display: block;
}}
/* A managed `media` slot. `contain` rather than `fill`, and it is the writer's rule rather
   than a second one: the exporter letterboxes the picture inside this box with `Box.fit`,
   and `contain` *is* that rectangle — the largest box of the image's own proportions,
   centred. The div is already the slot box shrunk by `media_scale`, so preview and file
   agree without the preview ever decoding the image to find out how wide it is. */
.media {{ position: absolute; }}
.media img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
/* An `asset_id` with nothing behind it. Named rather than blank, for the same reason an
   opaque shape is: the slot is filled, and a preview that drew nothing would look exactly
   like a slot nobody filled — which is the silence the export report exists to break. */
.media.missing {{
  border: 1px dashed rgba(0,0,0,.28); background: rgba(0,0,0,.04);
  display: flex; align-items: center; justify-content: center; text-align: center;
  font: 500 12px/1.3 ui-sans-serif, system-ui, sans-serif; color: rgba(0,0,0,.45);
}}
/* An `icon_row` mark. Absolutely placed, in the rectangle `written_icons` gives the
   exporter, and stroked rather than filled — the same `<a:ln>` on the same open path. No
   `stroke-width` here: it is an attribute on each element, in view-box units, so the browser
   scales it with the artwork exactly as PowerPoint scales a line width with its shape. A
   value in this stylesheet would override that and be a second opinion about the weight. */
.icon {{
  position: absolute;
  fill: none; stroke: currentColor;
  stroke-linecap: round; stroke-linejoin: round;
  overflow: visible;
}}
""".strip()


def _slot_style(theme: Theme, laid_out: LaidOutSlot) -> str:
    spec = laid_out.spec
    box = laid_out.box
    return "; ".join([
        f"left:{box.x:.2f}px",
        f"top:{box.y:.2f}px",
        f"width:{box.w:.2f}px",
        f"height:{box.h:.2f}px",
        f"font-family:{_font(theme, spec.family)}",
        f"font-size:{spec.size}px",
        f"font-weight:{spec.weight}",
        # Absolute line height, never a ratio — the same reason the writer emits `spcPts`
        # and never `spcPct`.
        f"line-height:{spec.line}px",
        f"letter-spacing:{spec.track}em",
        f"text-align:{laid_out.align}",
        f"color:{overrides.ink_for(theme, laid_out.overrides)}",
    ])


# ---------------------------------------------------------------------- managed


def _rgba(colour: str, alpha: float) -> str:
    if alpha >= 1.0:
        return colour
    r, g, b = (int(colour.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha:.3f})"


def _gradient_css(paint: decoration.Paint) -> str:
    """A decoration layer's ramp as CSS, drawing what `<a:gradFill>` draws.

    The two coordinate systems differ by a quarter turn and nothing else: OOXML measures
    clockwise from due east, CSS clockwise from straight up, so the angle is converted once,
    here. Both are unscaled, so a 45° light stays 45° on a wide panel in both.

    `closest-side` on the radial, because OOXML's `path=circle` with the focus collapsed to
    the centre runs its ramp from the middle of the shape to its edge — and the shape is an
    ellipse inscribed in the box, so its edge is exactly the closest side on each axis.
    """
    stops = ", ".join(f"{_rgba(stop.colour, stop.alpha)} {stop.at * 100:.0f}%"
                      for stop in paint.stops)
    if paint.kind == "radial":
        return f"background:radial-gradient(ellipse closest-side at 50% 50%, {stops});"
    return f"background:linear-gradient({paint.angle + 90:.0f}deg, {stops});"


def _panel_html(box: Box, paint: decoration.Paint) -> str:
    """One decoration layer, in the box the writer paints the same rectangle in.

    `box-sizing: border-box` is what makes the border legitimate here: it is drawn inside the
    rectangle, so a card in the preview covers exactly the pixels the exported autoshape
    does rather than a hairline more.

    Nothing here is a CSS effect the file cannot hold. A `filter: blur()` under the panel
    would look better in a browser and would be rasterised or dropped on the way to OOXML —
    the shadow is a gradient on an ellipse in both, because that is the only version of it
    that survives.
    """
    if paint.stops:
        fill = _gradient_css(paint)
    else:
        fill = f"background:{paint.fill};" if paint.fill else ""
    edge = f"border:{decoration.LINE_PX}px solid {paint.line};" if paint.line else ""
    # An ellipse is a rectangle with its corners rounded all the way, which is the same
    # `prstGeom` the writer emits — one shape, described twice, and never two shapes.
    radius = "50%" if paint.preset == "ellipse" else f"{paint.radius:.0f}px"
    return (f'<div class="panel" style="left:{box.x:.2f}px; top:{box.y:.2f}px; '
            f'width:{box.w:.2f}px; height:{box.h:.2f}px; {fill}{edge}'
            f'border-radius:{radius}"></div>')


def _cell_style(laid_out: LaidOutSlot) -> str:
    """The padding one grid cell spends, so its content box is the exporter's text box.

    Two things come out of the cell before the words do — the decoration's pad, on every
    side, and the icon's square, on the side the variant puts it. Both are already subtracted
    in `LaidOutSlot.cells`, which is what the writer and the budget read; here they are spent
    as padding because `box-sizing: border-box` makes that the identical rectangle while
    leaving the grid track where the exporter paints its card.
    """
    pad = laid_out.pad
    take = (laid_out.icon_side + laid_out.icon_gap) if laid_out.icon_side else 0.0
    top = pad + (laid_out.icon_offset + take if laid_out.icon_place == "top" else 0.0)
    left = pad + (take if laid_out.icon_place == "left" else 0.0)
    if not (pad or take):
        return ""
    return (f' style="padding:{top:.1f}px {pad:.1f}px {pad:.1f}px {left:.1f}px"')


def _icons_html(laid_out: LaidOutSlot, value: Any, colour: str) -> str:
    """Every mark an icon slot draws, in the squares the exporter draws them in.

    The same path string the writer turns into `a:custGeom`, from the same table, positioned
    by the same `written_icons`. That is the point of normalising the artwork at vendor time:
    the preview *is* the export rendered, and the only way to keep that true of a curve is for
    both of them to be reading one list of coordinates rather than two derivations of it.

    Absolutely positioned rather than laid out inside the cell, because the exporter writes
    the mark as its own shape at its own rectangle — a mark that flowed with the text here
    would be a preview of a different slide, and the difference would show up exactly where
    the label wrapped.
    """
    marks = []
    for name, box in written_icons(laid_out, value):
        path = icons.path(name)
        if not path:
            continue
        marks.append(
            f'<svg class="icon" viewBox="0 0 {icons.view_box():g} {icons.view_box():g}" '
            f'style="left:{box.x:.2f}px; top:{box.y:.2f}px; '
            f'width:{box.w:.2f}px; height:{box.h:.2f}px; color:{colour}" '
            # Stated in *view-box* units, not px, so the browser scales the stroke with the
            # artwork exactly as PowerPoint scales `a:ln` with the shape. Both therefore land
            # on the same weight, and `icon_stroke_px` stays the one place the ratio lives.
            f'stroke-width="{icon_stroke_px(box) / box.w * icons.view_box():.3f}">'
            f'<path d="{path}"></path></svg>'
        )
    return "".join(marks)


def _media_html(laid_out: LaidOutSlot, value: Any, src: str | None) -> str:
    """A managed `media` slot as the picture the exporter writes there.

    The box is the slot's, shrunk by `media_scale` about its centre — the same
    `Box.scaled` the writer calls — and the picture is letterboxed inside it by the
    stylesheet. The preview is the file rendered, and a media slot the preview left blank is
    how `image_full/full` and `image_full/inset` came to look identical in *both*.
    """
    box = laid_out.box.scaled(overrides.media_factor(laid_out.overrides))
    geom = (f'left:{box.x:.2f}px; top:{box.y:.2f}px; '
            f'width:{box.w:.2f}px; height:{box.h:.2f}px')
    alt = _esc(str(value.get("alt") or "") if isinstance(value, dict) else "")
    if not src:
        name = _esc(str(value.get("asset_id") or "") if isinstance(value, dict) else "")
        return f'<div class="media missing" style="{geom}">{name or "image"}</div>'
    return f'<div class="media" style="{geom}"><img src="{_esc(src)}" alt="{alt}"></div>'


def _managed_body(theme: Theme, slide: Slide,
                  asset_src: AssetSrc | None = None) -> tuple[str, list[str]]:
    parts, targets = [], []
    laid_out_slots = expand_slide(theme, slide)

    # Slide-wide layers first and once, exactly as the writer emits them — the preview is
    # the file rendered, and that includes which shape is behind which.
    worn = [(item.decoration, item.shape) for item in laid_out_slots if item.decoration]
    for layer in decoration.slide_layers(theme, worn):
        parts.append(_panel_html(decoration_box(theme, content_box(theme), layer.place),
                                 layer))

    for laid_out in laid_out_slots:
        block = slide.block(laid_out.block_id)
        if block is None:
            continue
        value = block.slots.get(laid_out.slot)

        # Before the text check, because a picture has no text and would fall out of the
        # loop there. It is also not a probe: the measurement contract is about words
        # overflowing a box, and a slot whose content is an image has none to overflow.
        if laid_out.shape == "media":
            if value:
                key = str(value.get("asset_id") or "") if isinstance(value, dict) else ""
                parts.append(_media_html(laid_out, value,
                                         asset_src(key) if asset_src and key else None))
            continue

        accent = overrides.accent_for(theme, laid_out.overrides)
        content = _text_html(value, accent)
        if not content:
            continue
        target = f"{slide.id}/{laid_out.block_id}/{laid_out.slot}"
        targets.append(target)

        # A row of cells is one probe, not several. The measurement contract is per *slot* —
        # `set_text` addresses a slot, lint reports a slot — so the box the prober measures
        # stays the slot's box, and the cells live inside it as a grid. The alternative,
        # a probe per cell, would have made every target in the deck ambiguous to buy a
        # geometry the budget already computes analytically.
        grid = laid_out.columns > 1 and isinstance(value, list) and len(value) > 1
        # Same count the writer emits, and drawn before the slot so document order puts it
        # behind — the preview is the file rendered, down to the z-order. Layers within a
        # panel keep the decoration's own order too: ground, shade, then the panel itself.
        layers = decoration.layers_for(theme, laid_out.decoration, laid_out.shape)
        for panel in laid_out.panels(len(value) if grid else 1):
            for layer in layers:
                if layer.place == "slide":
                    continue  # drawn once for the slide, above
                parts.append(_panel_html(decoration_box(theme, panel, layer.place), layer))

        if grid:
            # The pad is the writer's inset, spent as padding rather than as a smaller box:
            # `box-sizing: border-box` makes the two the same content rectangle, and the
            # grid track stays the cell the exporter paints its card in. An icon's square is
            # spent the same way, on the one side it is taken from, so the content rectangle
            # left over is exactly `LaidOutSlot.cells()` — the box the exporter writes the
            # text into and the box the budget measured it against.
            style = _cell_style(laid_out)
            cells = "".join(f'<span class="cell"{style}>{_item_html(item, accent)}</span>'
                            for item in value)
            across = min(laid_out.columns, len(value))
            # Rows in **px, from the expander**, not left to the content. A grid whose tracks
            # size themselves packs its rows against the top of the slot while the exporter
            # gives each row an equal share of it, so a two-row `card_grid` or `stat_row`
            # previewed as two rows crowded at the top of a box it filled in the file. The
            # icons are what made it undeniable — they are placed absolutely, at the
            # expander's rectangles, so the second row's marks sat a long way below the
            # second row's words.
            #
            # `minmax(track, auto)`, never a flat height: a track that could not grow would
            # hide overflow, and the browser probe finds overflow precisely by the ink box
            # growing past the slot.
            rows = laid_out.panels(len(value))
            inner = (f'<span class="ink grid" style="'
                     f'display:grid;'
                     f'grid-template-columns:repeat({across},1fr);'
                     f'grid-template-rows:repeat({math.ceil(len(value) / across)},'
                     f'minmax({rows[0].h:.2f}px,auto));'
                     f'gap:{laid_out.cell_gap:.1f}px">{cells}</span>')
        else:
            inner = f'<span class="ink">{content}</span>'

        parts.append(
            f'<div class="slot" {PROBE_ATTR}="{_esc(target)}" '
            f'data-max-lines="{laid_out.max_lines}" '
            f'style="{_slot_style(theme, laid_out)}">{inner}</div>'
        )
        # After the slot, matching the writer's own order — and outside it, because the marks
        # are positioned on the slide rather than inside the probed element. A shape inside
        # the probe would be measured as part of the text it labels, and the measurement
        # contract is about words overflowing a box.
        parts.append(_icons_html(laid_out, value, accent))
    return "\n".join(parts), targets


# --------------------------------------------------------------------- freeform


AssetSrc = Callable[[str], str | None]


def _freeform_body(theme: Theme, slide: Slide, cx: int, cy: int,
                   asset_src: AssetSrc | None = None,
                   compensate_autofit: bool = False) -> tuple[str, list[str]]:
    """Render imported shapes as the file draws them, scaled to the canvas.

    Order matters and mirrors OOXML: geometry underneath, then the picture, then the text.
    Shapes are emitted in document order so z-order survives.

    Nothing is silently dropped. An opaque shape becomes a labelled placeholder, because a
    preview that omitted the SmartArt would suggest the slide is emptier than it is — the
    shape is still in the file either way.
    """
    w, h = theme.grid.canvas
    sx, sy = w / cx if cx else 0, h / cy if cy else 0
    parts, targets = [], []

    inherited_ids = {id(x) for x in slide.inherited}
    for shape in [*slide.inherited, *slide.shapes]:
        own = id(shape) not in inherited_ids
        f = shape.frame
        box_w, box_h = f.cx * sx, f.cy * sy
        geom = (f"left:{f.x * sx:.2f}px; top:{f.y * sy:.2f}px; "
                f"width:{box_w:.2f}px; height:{box_h:.2f}px")

        if shape.opaque:
            parts.append(f'<div class="opaque" style="{geom}">{_esc(shape.type)}</div>')
            continue

        layers: list[str] = []

        if shape.geometry is not None and shape.geometry.icon:
            # A frozen mark, from `eject_slide`. Drawn from the same path table and at the
            # same weight the writer gives it, so an ejected `icon_row` previews as the
            # managed one it came from — which is the only way anyone can check that the
            # one-way door did not cost them anything.
            g = shape.geometry
            layers.append(
                f'<svg class="icon" style="position:static; width:100%; height:100%; '
                f'color:{_esc(g.line or theme.palette.get("ink", "#111"))}" '
                f'viewBox="0 0 {icons.view_box():g} {icons.view_box():g}" '
                f'stroke-width="{icons.stroke_units():.3f}">'
                f'<path d="{icons.path(g.icon)}"></path></svg>'
            )
        elif shape.geometry is not None:
            g = shape.geometry
            ramp = ((g.gradient, g.gradient_angle,
                     tuple((s.at, s.colour, s.alpha) for s in g.stops))
                    if g.stops else None)
            layers.append(svg.shape_svg(
                g.preset, box_w, box_h,
                (g.fill, g.fill_alpha) if g.fill else None,
                (g.line, g.line_alpha, g.line_width_pt) if g.line else None,
                g.flip_h, g.flip_v, ramp,
            ))

        if shape.chart is not None:
            layers.append(svg.chart_svg(shape.chart.kind, shape.chart.categories,
                                        shape.chart.series, box_w, box_h))

        src = asset_src(shape.asset) if (asset_src and shape.asset) else None
        if src:
            layers.append(f'<img src="{_esc(src)}" alt="{_esc(shape.type)}">')

        style = geom
        if shape.geometry is not None and shape.geometry.rotation:
            style += f"; transform:rotate({shape.geometry.rotation:.2f}deg)"

        if shape.text is None:
            if layers:
                parts.append(f'<div class="asset" style="{style}">{"".join(layers)}</div>')
            else:
                # Named, not blank: the shape is in the file whether or not we can draw it.
                parts.append(f'<div class="asset" style="{style}">{_esc(shape.type)}</div>')
            continue

        # The file's own type wins; the theme role is the fallback. A preview that restyled
        # the original author's slide would not be a preview of it.
        spec = shape.type_spec or theme.type.scale[
            shape.role if shape.role in theme.type.scale else "body"
        ]
        size, line = spec.size, spec.line
        if compensate_autofit and shape.autofit_scale and shape.autofit_scale < 1:
            # Show what PowerPoint shows. The source shrinks this text to make it fit, so a
            # preview at the declared size clips text the audience will actually see.
            #
            # Never done when measuring: the harness exports with autofit *off*, so the
            # honest overflow is the one at the declared size. Looking right and being right
            # are different questions, and this flag is where they are kept apart.
            size *= shape.autofit_scale
            line *= shape.autofit_scale
        # Inherited shapes are drawn but never *probed*: they are the layout's, not this
        # slide's. Leaving the probe attribute on them would have the browser measure the
        # logo and report it as overflow nobody can act on.
        probe = ""
        if own:
            target = f"{slide.id}/{shape.id}"
            targets.append(target)
            probe = f' {PROBE_ATTR}="{_esc(target)}"'
        text_style = "; ".join([
            style,
            f"font-family:{_font(theme, spec.family)}",
            f"font-size:{size:.2f}px",
            f"font-weight:{spec.weight}",
            f"line-height:{line:.2f}px",
            f"font-style:{'italic' if spec.italic else 'normal'}",
            f"text-align:{shape.align}",
            f"color:{shape.color or theme.palette.get('ink', '#111')}",
        ])
        body = (richtext.to_html(shape.runs, _esc) if shape.runs
                else _text_html(shape.text))
        if shape.bullet in ("bullet", "number"):
            tag = "ol" if shape.bullet == "number" else "ul"
            items = "".join(f"<li>{line}</li>" for line in body.split("<br>") if line)
            ink = f'<{tag} class="ink list-{shape.bullet}">{items}</{tag}>'
        else:
            ink = f'<span class="ink">{body}</span>'
        parts.append(
            f'<div class="slot shape anchor-{shape.anchor}"{probe} style="{text_style}">'
            f'{"".join(layers)}{ink}</div>'
        )
    return "\n".join(parts), targets


# ----------------------------------------------------------------------- render


def render_slide(theme: Theme, slide: Slide, cx: int, cy: int,
                 fragment: bool = False,
                 asset_src: AssetSrc | None = None,
                 compensate_autofit: bool = False) -> RenderedSlide:
    """One slide as HTML.

    `fragment=True` returns just the stage plus its styles, for embedding. The default is a
    complete document, which is what the measurement pass loads.

    `compensate_autofit` reproduces the shrink the *source* applies to text that does not
    fit, so the preview matches PowerPoint. It is off by default because the measurement
    pass must see the declared size — the harness exports with autofit disabled, and the
    overflow it reports is the real one.
    """
    if slide.mode is Mode.MANAGED:
        body, targets = _managed_body(theme, slide, asset_src)
    else:
        body, targets = _freeform_body(theme, slide, cx, cy, asset_src, compensate_autofit)

    w, h = theme.grid.canvas
    stage = f'<div class="slide" id="slide">\n{body}\n</div>'
    css = _base_css(theme)

    if fragment:
        markup = f"<style>{css}</style>\n{stage}"
    else:
        markup = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{_esc(slide.id)}</title><style>{css}</style></head>"
            f"<body>{stage}</body></html>"
        )
    return RenderedSlide(slide_id=slide.id, mode=slide.mode.value, html=markup,
                         canvas=(w, h), targets=targets)


def render_deck(theme: Theme, slides: list[Slide], cx: int, cy: int,
                asset_src: AssetSrc | None = None) -> str:
    """Every slide stacked, for a scroll-through contact sheet."""
    css = _base_css(theme)
    stages = []
    for slide in slides:
        rendered = render_slide(theme, slide, cx, cy, fragment=True, asset_src=asset_src)
        stage = rendered.html.split("</style>\n", 1)[-1]
        stages.append(f'<figure style="margin:0 0 24px"><figcaption '
                      f'style="font:600 12px ui-sans-serif;color:#fff;padding:4px 0">'
                      f'{_esc(slide.id)}</figcaption>{stage}</figure>')
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            f"<style>{css}</style></head><body style='padding:24px'>"
            + "\n".join(stages) + "</body></html>")


__all__ = ["PROBE_ATTR", "AssetSrc", "RenderedSlide", "render_deck", "render_slide"]


# ----------------------------------------------------------------------- overlay


def render_overlay(theme: Theme, slide: Slide, png_url: str,
                   measurement: dict[str, Any] | None = None) -> str:
    """A rastered slide with the harness's measurements drawn on top.

    The picture comes from a real renderer, so it is faithful by construction. The boxes
    come from the expander and the measurer, in canvas coordinates — the same numbers the
    exporter writes. Overlaying rather than redrawing is what lets the preview be somebody
    else's rendering while the inspector still knows where everything is.
    """
    w, h = theme.grid.canvas
    boxes = []
    found = measurement or {}
    for item in found.get("boxes") or found.get("slots") or found.get("shapes") or []:
        box = item.get("box")
        if not box:
            continue
        x, y, bw, bh = box
        state = "fits" if item.get("fits", True) else "over"
        label = _esc(item.get("target", "").rsplit("/", 1)[-1])
        boxes.append(
            f'<div class="probe {state}" style="left:{x}px; top:{y}px; '
            f'width:{bw}px; height:{bh}px" title="{label}"></div>'
        )

    return f"""<!doctype html><html><head><meta charset='utf-8'>
<title>{_esc(slide.id)}</title><style>
*, *::before, *::after {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: #6b7280; }}
.slide {{ position: relative; width: {w}px; height: {h}px; overflow: hidden;
          transform-origin: top left; background: #fff; }}
.slide > img {{ width: 100%; height: 100%; display: block; }}
.probe {{ position: absolute; pointer-events: none; border: 1px solid transparent; }}
body.show-probes .probe.fits {{ border-color: rgba(79,142,247,.55); }}
body.show-probes .probe.over {{ border-color: #f85149; background: rgba(248,81,73,.12); }}
</style></head><body>
<div class="slide" id="slide"><img src="{_esc(png_url)}" alt="{_esc(slide.id)}">
{"".join(boxes)}
</div></body></html>"""
