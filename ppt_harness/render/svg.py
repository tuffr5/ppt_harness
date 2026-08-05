"""Preset geometry to SVG.

OOXML draws shapes from named presets — `rightArrow`, `ellipse`, `bentArrow` — each with a
parametric outline. A preview that renders only text shows a slide of floating words where
the file has arrows and callouts, which is not a preview of that slide.

Every path is emitted in a **0..100 viewBox** and stretched to the shape's frame with
`preserveAspectRatio="none"`, exactly as PowerPoint stretches a preset into its extent. That
keeps one number — the frame — governing both the preview and the export.

Presets not listed fall back to a rectangle. A named fallback is honest; silently drawing
nothing is not.
"""

from __future__ import annotations

from collections.abc import Callable

#: Arrow head proportions, as fractions of the box. OOXML exposes these as adjust values;
#: the defaults cover the overwhelming majority of real decks.
_HEAD = 40.0   # length of the arrow head along the arrow's axis
_TAIL = 25.0   # half-height of the shaft


def _right_arrow() -> str:
    body_top, body_bottom = 50 - _TAIL, 50 + _TAIL
    x = 100 - _HEAD
    return (f"M0,{body_top} L{x},{body_top} L{x},0 L100,50 L{x},100 L{x},{body_bottom} "
            f"L0,{body_bottom} Z")


def _left_arrow() -> str:
    body_top, body_bottom = 50 - _TAIL, 50 + _TAIL
    x = _HEAD
    return (f"M100,{body_top} L{x},{body_top} L{x},0 L0,50 L{x},100 L{x},{body_bottom} "
            f"L100,{body_bottom} Z")


def _up_arrow() -> str:
    left, right = 50 - _TAIL, 50 + _TAIL
    y = _HEAD
    return (f"M{left},100 L{left},{y} L0,{y} L50,0 L100,{y} L{right},{y} L{right},100 Z")


def _down_arrow() -> str:
    left, right = 50 - _TAIL, 50 + _TAIL
    y = 100 - _HEAD
    return (f"M{left},0 L{left},{y} L0,{y} L50,100 L100,{y} L{right},{y} L{right},0 Z")


def _bent_arrow() -> str:
    """Up, then right. The elbow is squared off; the curve OOXML draws is close enough at
    preview scale that the difference is invisible."""
    return ("M0,100 L0,55 L55,55 L55,30 L100,62 L55,95 L55,80 L22,80 L22,100 Z")


def _chevron() -> str:
    return "M0,0 L70,0 L100,50 L70,100 L0,100 L30,50 Z"


def _pentagon_arrow() -> str:
    return "M0,0 L70,0 L100,50 L70,100 L0,100 Z"


def _triangle() -> str:
    return "M50,0 L100,100 L0,100 Z"


def _diamond() -> str:
    return "M50,0 L100,50 L50,100 L0,50 Z"


def _plus() -> str:
    return ("M35,0 L65,0 L65,35 L100,35 L100,65 L65,65 L65,100 L35,100 L35,65 "
            "L0,65 L0,35 L35,35 Z")


def _star5() -> str:
    return ("M50,0 L61,35 L98,35 L68,57 L79,91 L50,70 L21,91 L32,57 L2,35 L39,35 Z")


def _parallelogram() -> str:
    return "M25,0 L100,0 L75,100 L0,100 Z"


def _trapezoid() -> str:
    return "M25,0 L75,0 L100,100 L0,100 Z"


def _hexagon() -> str:
    return "M25,0 L75,0 L100,50 L75,100 L25,100 L0,50 Z"


def _can_rect() -> str:
    return "M0,0 L100,0 L100,100 L0,100 Z"


#: Presets whose outline is a path. Ellipses and rounded rectangles use dedicated SVG
#: elements instead, because a path approximation of a circle looks wrong at preview scale.
PATHS: dict[str, Callable[[], str]] = {
    "rect": _can_rect,
    "rightArrow": _right_arrow,
    "leftArrow": _left_arrow,
    "upArrow": _up_arrow,
    "downArrow": _down_arrow,
    "bentArrow": _bent_arrow,
    "chevron": _chevron,
    "homePlate": _pentagon_arrow,
    "triangle": _triangle,
    "diamond": _diamond,
    "mathPlus": _plus,
    "plus": _plus,
    "star5": _star5,
    "parallelogram": _parallelogram,
    "trapezoid": _trapezoid,
    "hexagon": _hexagon,
}

#: Straight or curved connectors, drawn corner to corner across the frame.
CONNECTORS = {"line", "straightConnector1", "bentConnector2", "bentConnector3",
              "curvedConnector2", "curvedConnector3", "curvedConnector4", "curvedConnector5"}

ROUNDED = {"roundRect", "round1Rect", "round2SameRect", "round2DiagRect", "snip1Rect"}
ELLIPTIC = {"ellipse", "circle", "oval"}


def _paint(fill: tuple[str, float] | None, line: tuple[str, float, float] | None) -> str:
    bits = []
    if fill:
        bits.append(f'fill="{fill[0]}"')
        if fill[1] < 1:
            bits.append(f'fill-opacity="{fill[1]:.3f}"')
    else:
        bits.append('fill="none"')
    if line:
        colour, alpha, width_pt = line
        bits.append(f'stroke="{colour}" stroke-width="{max(0.5, width_pt):.2f}"')
        bits.append('vector-effect="non-scaling-stroke"')
        if alpha < 1:
            bits.append(f'stroke-opacity="{alpha:.3f}"')
    return " ".join(bits)


#: A gradient fill, as the model holds one: (kind, angle in OOXML degrees, stops), where a
#: stop is (position 0..1, `#RRGGBB`, alpha).
Gradient = tuple[str, float, tuple[tuple[float, str, float], ...]]

def _gradient_id(gradient: Gradient) -> str:
    """A stable `<defs>` id for one ramp.

    Content-addressed, and deterministic across processes: two shapes with the same gradient
    share one definition, and the same slide rendered twice produces the same bytes. A
    counter would break that, and `hash()` is salted per run.
    """
    from hashlib import blake2b

    return "g" + blake2b(repr(gradient).encode(), digest_size=5).hexdigest()


def _gradient_defs(gradient: Gradient, ident: str) -> str:
    """A `<linearGradient>` or `<radialGradient>` matching what `<a:gradFill>` draws.

    OOXML measures its linear angle clockwise from due east; SVG states the vector's two
    endpoints instead, so the angle is turned back into a unit vector across the 0..1
    object box. The radial is centred with its ramp running out to the shape's edge, which
    is what `path=circle` with a collapsed `fillToRect` means.
    """
    import math

    kind, angle, stops = gradient
    body = "".join(
        f'<stop offset="{at * 100:.1f}%" stop-color="{colour}" '
        f'stop-opacity="{alpha:.3f}"/>'
        for at, colour, alpha in stops
    )
    if kind == "radial":
        return (f'<defs><radialGradient id="{ident}" cx="50%" cy="50%" r="50%">'
                f'{body}</radialGradient></defs>')
    dx, dy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    x1, y1 = 0.5 - dx / 2, 0.5 - dy / 2
    return (f'<defs><linearGradient id="{ident}" x1="{x1:.3f}" y1="{y1:.3f}" '
            f'x2="{x1 + dx:.3f}" y2="{y1 + dy:.3f}">{body}</linearGradient></defs>')


def shape_svg(
    preset: str,
    width: float,
    height: float,
    fill: tuple[str, float] | None,
    line: tuple[str, float, float] | None,
    flip_h: bool = False,
    flip_v: bool = False,
    gradient: Gradient | None = None,
) -> str:
    """An `<svg>` element that fills its container, drawing `preset`.

    Returns an empty string when there is nothing to draw — a shape with neither fill nor
    outline is a text box in all but name, and emitting an invisible SVG over it would only
    swallow clicks.

    `gradient` is a ramp rather than a flat colour, and it is what an ejected decoration
    panel carries. Without it a slide that left managed mode drew its lit cards as nothing
    at all in the preview while the exported file still had them — the preview claiming a
    loss the file did not have is the same bug as the reverse, one direction over.
    """
    if not fill and not line and not gradient:
        return ""

    defs = ""
    if gradient is not None and gradient[2]:
        ident = _gradient_id(gradient)
        defs = _gradient_defs(gradient, ident)
        fill = (f"url(#{ident})", 1.0)

    paint = _paint(fill, line)
    if preset in ELLIPTIC:
        body = f'<ellipse cx="50" cy="50" rx="50" ry="50" {paint}/>'
    elif preset in ROUNDED:
        # A radius in viewBox units would stretch with the box; 8% of the shorter side
        # tracks what PowerPoint draws closely enough at this scale.
        radius = 100 * 0.08 * (min(width, height) / max(width, height, 1))
        body = f'<rect x="0" y="0" width="100" height="100" rx="{radius:.1f}" {paint}/>'
    elif preset in CONNECTORS:
        stroke = line or ("#000000", 1.0, 1.0)
        body = (f'<line x1="0" y1="0" x2="100" y2="100" stroke="{stroke[0]}" '
                f'stroke-width="{max(0.5, stroke[2]):.2f}" vector-effect="non-scaling-stroke"/>')
    else:
        body = f'<path d="{PATHS.get(preset, _can_rect)()}" {paint}/>'

    transform = ""
    if flip_h or flip_v:
        sx, sy = (-1 if flip_h else 1), (-1 if flip_v else 1)
        tx, ty = (100 if flip_h else 0), (100 if flip_v else 0)
        transform = f' transform="translate({tx},{ty}) scale({sx},{sy})"'

    return (
        '<svg class="geom" viewBox="0 0 100 100" preserveAspectRatio="none" '
        f'aria-hidden="true">{defs}<g{transform}>{body}</g></svg>'
    )


def supported() -> set[str]:
    return set(PATHS) | CONNECTORS | ROUNDED | ELLIPTIC


# ------------------------------------------------------------------------ charts

#: Enough of a palette that a chart is readable without the theme having to supply one.
SERIES_COLOURS = ["#4472C4", "#ED7D31", "#A5A5A5", "#FFC000", "#5B9BD5", "#70AD47"]


def _nice_max(value: float) -> float:
    """A round number at or above `value`, so the axis does not end mid-tick."""
    if value <= 0:
        return 1.0
    import math

    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


def chart_svg(kind: str, categories: list[str], series: list[dict], width: float,
              height: float) -> str:
    """A native chart, drawn for the preview.

    Deliberately a *rendering*, never a replacement. DESIGN §1.5 is explicit that a chart
    the harness holds data for exports as a native pptx chart with its worksheet; this only
    exists so the frame can be verified and the slide can be looked at.
    """
    values = [v for s in series for v in s.get("values", []) if isinstance(v, (int, float))]
    if not categories or not values:
        return ""

    pad_l, pad_r, pad_t, pad_b = 46, 8, 10, 34
    w, h = max(width, 120), max(height, 90)
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    if plot_w <= 0 or plot_h <= 0:
        return ""

    top = _nice_max(max(values))
    bottom = min(0.0, min(values))
    span = top - bottom or 1.0

    def y_of(value: float) -> float:
        return pad_t + plot_h * (1 - (value - bottom) / span)

    parts = [f'<svg class="chart" viewBox="0 0 {w:.0f} {h:.0f}" '
             f'preserveAspectRatio="xMidYMid meet" aria-hidden="true">']

    # gridlines and value labels
    for i in range(5):
        value = bottom + span * i / 4
        y = y_of(value)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}" '
                     f'stroke="#E4E7EC" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 6}" y="{y + 3:.1f}" font-size="9" fill="#7A828F" '
                     f'text-anchor="end">{value:g}</text>')

    slot = plot_w / max(1, len(categories))
    if kind.startswith("pie") or kind.startswith("doughnut"):
        parts.append(_pie(series, categories, w, h))
    elif kind.startswith("line") or kind.startswith("xy") or kind.startswith("scatter"):
        for index, one in enumerate(series):
            points = " ".join(
                f"{pad_l + slot * (i + 0.5):.1f},{y_of(float(v)):.1f}"
                for i, v in enumerate(one.get("values", []))
                if isinstance(v, (int, float))
            )
            colour = SERIES_COLOURS[index % len(SERIES_COLOURS)]
            parts.append(f'<polyline points="{points}" fill="none" stroke="{colour}" '
                         f'stroke-width="2"/>')
    else:
        count = max(1, len(series))
        bar_w = slot * 0.72 / count
        for index, one in enumerate(series):
            colour = SERIES_COLOURS[index % len(SERIES_COLOURS)]
            for i, value in enumerate(one.get("values", [])):
                if not isinstance(value, (int, float)):
                    continue
                x = pad_l + slot * i + slot * 0.14 + bar_w * index
                y = y_of(float(value))
                parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                             f'height="{max(0.0, y_of(bottom) - y):.1f}" fill="{colour}"/>')

    # category labels, thinned so they never collide
    every = max(1, round(len(categories) / max(1, plot_w // 52)))
    for i, name in enumerate(categories):
        if i % every:
            continue
        x = pad_l + slot * (i + 0.5)
        label = name if len(name) <= 11 else name[:10] + "…"
        parts.append(f'<text x="{x:.1f}" y="{h - pad_b + 14:.0f}" font-size="9" '
                     f'fill="#5A6472" text-anchor="middle">{label}</text>')

    # legend
    for index, one in enumerate(series[:4]):
        colour = SERIES_COLOURS[index % len(SERIES_COLOURS)]
        x = pad_l + index * 78
        parts.append(f'<rect x="{x}" y="{h - 10}" width="8" height="8" fill="{colour}"/>')
        parts.append(f'<text x="{x + 12}" y="{h - 3}" font-size="9" fill="#5A6472">'
                     f'{(one.get("name") or "")[:12]}</text>')

    parts.append("</svg>")
    return "".join(parts)


def _pie(series: list[dict], categories: list[str], w: float, h: float) -> str:
    import math

    values = [v for v in (series[0].get("values", []) if series else [])
              if isinstance(v, (int, float)) and v > 0]
    total = sum(values)
    if not total:
        return ""
    cx, cy = w / 2, h / 2 - 6
    r = min(w, h) / 2 - 22
    angle = -math.pi / 2
    out = []
    for index, value in enumerate(values):
        sweep = 2 * math.pi * value / total
        x1, y1 = cx + r * math.cos(angle), cy + r * math.sin(angle)
        angle += sweep
        x2, y2 = cx + r * math.cos(angle), cy + r * math.sin(angle)
        large = 1 if sweep > math.pi else 0
        colour = SERIES_COLOURS[index % len(SERIES_COLOURS)]
        out.append(f'<path d="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} '
                   f'A{r:.1f},{r:.1f} 0 {large},1 {x2:.1f},{y2:.1f} Z" fill="{colour}"/>')
    return "".join(out)
