"""The vendored icon set — DESIGN §1.4, §1.5.

`icon_row` is named for a mark it did not draw. Its two variants were called `icon_top` and
`icon_left`, it had a `title` slot and a list of labels, and nothing anywhere in the pipeline
put a glyph on a slide — so the two variants differed in `per_row` and alignment and in
nothing a reader would recognise as an icon being above a word rather than beside it. Same
class of defect as the variants that rendered identically and the `media` slot the writer
skipped: the catalog offering a rendering the writer cannot produce.

**An icon is geometry, not a picture.** The paths here are written into the file as OOXML
`a:custGeom` — the same primitive as any autoshape — for three reasons that all point the
same way:

- **It can be coloured through a theme role.** A shape's stroke resolves from the palette at
  write time, so an icon is on-brand on every theme the deck is retargeted to. A PNG is one
  colour forever, and a set of PNGs pre-tinted per palette is a build step that goes stale.
- **It scales and prints.** A vector mark is resolution-free in PowerPoint, in Keynote, and
  on paper. A raster at slide size is a raster at four times slide size on a projector.
- **python-pptx cannot read SVG at all.** `io/media.is_svg` exists precisely because the
  obvious route — drop the icon in as a picture — is closed: python-pptx sniffs raster
  headers and has no width, height or content type for anything else, so `add_asset` refuses
  SVG with `svg_unsupported`. Rasterising at build time would be possible and would cost the
  first two points.

**Stroked, not filled.** Tabler's outline style draws with a 2-unit stroke on a 24-unit box
and leaves its paths open, which suits `a:custGeom` exactly: `<a:path fill="none">` with an
`<a:ln>` carrying the colour. Nothing here depends on how a renderer resolves the winding of
overlapping subpaths, which is the one part of filled custom geometry where DrawingML and SVG
are not obviously the same — an outline icon has no counters to get wrong.

**Read back, not just written.** Export puts an icon on the slide as `a:custGeom`, and
import (DESIGN §7) lands every slide as `freeform` — so a deck the harness exported and then
reopened used to come back holding shapes it no longer recognised as marks it had drawn, and
the preview rendered a stroked rectangle for each one. `identify` closes that, matching an
incoming path against this table rather than against a marker written beside it: no name to
be edited, no extension element to be dropped, and nothing added to the exported file, which
is what keeps §6.2's round trip free rather than something the writer has to work around.

The data is `iconset/paths.json`, normalised at vendor time to `M`/`L`/`C`/`Z` only. See
`iconset/NOTICE.md` for provenance and `iconset/LICENSE` for the MIT text; the curation list
and the normaliser are `scripts/vendor_icons.py`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA = Path(__file__).parent / "iconset" / "paths.json"

#: One command of a normalised path: the letter, then its absolute coordinates in view-box
#: units. `M`/`L` carry a point, `C` carries two controls and an end point, `Z` carries none.
Segment = tuple[str, tuple[float, ...]]

#: The same command, with its coordinates snapped to whole units of a path space — what the
#: writer emits as `a:pt` and what the importer reads back off one. See `placed`.
Placed = tuple[str, tuple[int, ...]]

#: How many coordinates each command carries. One table, read by the parser here and by the
#: importer's arity check, so a malformed `a:cubicBezTo` carrying two points cannot be
#: mistaken for something `a:lnTo`-shaped on the way back in.
ARITY = {"M": 2, "L": 2, "C": 6, "Z": 0}


@lru_cache(maxsize=1)
def _table() -> dict[str, Any]:
    return json.loads(_DATA.read_text())


def view_box() -> float:
    """The side of the square the paths are drawn in. 24, as Tabler authors them."""
    return float(_table()["view_box"])


def stroke_units() -> float:
    """Line width in view-box units — 2 of 24, which is the weight the set was drawn at.

    Kept as a proportion rather than a length because an icon is drawn at whatever size the
    expander gives it, and a stroke fixed in px would be hairline at hero size and a blot in
    a footer band. The px conversion is `render/expand.icon_stroke_px`, because that module
    is the only one allowed to produce a coordinate.
    """
    return float(_table()["stroke"])


@lru_cache(maxsize=1)
def names() -> tuple[str, ...]:
    """Every icon a model may name, sorted. This is what the gate quotes back."""
    return tuple(sorted(_table()["paths"]))


def path(name: str) -> str:
    """The SVG path data for `name`, or `""` when nothing is behind it.

    Empty rather than an exception, for the same reason `media.resolve` returns None: the
    unknown name is refused at the tool gate, and a writer that raised here would take the
    rest of the deck down over one misspelt word on one slide.
    """
    return str(_table()["paths"].get(name, ""))


def attribution() -> dict[str, str]:
    """Where the artwork came from, for anything that has to say so."""
    table = _table()
    return {"source": table["source"], "commit": table["commit"],
            "style": table["style"], "licence": table["licence"]}


def suggest(name: str, limit: int = 5) -> list[str]:
    """The closest names to a miss, nearest first.

    A refusal that lists a hundred and forty-five names is technically complete and
    practically unreadable; the near misses are what a model actually acts on, and the full
    list follows them for the case where the guess was not close to anything.

    The cutoff is deliberately high. A loose one turns `unicorn` into "did you mean coin",
    which is worse than saying nothing: it reads as the harness having understood the
    request, and the next guess is spent on a suggestion nobody meant.
    """
    import difflib

    return difflib.get_close_matches(str(name).strip().lower(), names(), n=limit,
                                     cutoff=0.65)


@lru_cache(maxsize=256)
def segments(name: str) -> tuple[Segment, ...]:
    """One icon's path, parsed into commands the writer can emit as `a:custGeom`.

    The data is normalised to absolute `M`/`L`/`C`/`Z` at vendor time, so this parser has no
    relative forms, no shorthand curves and — the point of the exercise — no elliptic arcs.
    SVG's `A` and OOXML's `a:arcTo` describe the same curve from opposite ends (endpoint plus
    flags against centre plus sweep), and converting between them at write time would put
    trigonometry in the exporter and give the preview a second chance to disagree with it.
    """
    out: list[Segment] = []
    tokens = path(name).split()
    index = 0
    while index < len(tokens):
        command = tokens[index]
        take = ARITY.get(command)
        if take is None:  # pragma: no cover - the vendored data is normalised
            index += 1
            continue
        values = tuple(float(v) for v in tokens[index + 1:index + 1 + take])
        out.append((command, values))
        index += 1 + take
    return tuple(out)


@lru_cache(maxsize=512)
def placed(name: str, units: int) -> tuple[Placed, ...]:
    """One icon's commands snapped to whole units of a `units`-square path space.

    The single rounding in the system, and it has to be: the writer emits these integers into
    `a:custGeom` and the importer matches an incoming path against them, so a second copy of
    `round(v * units / 24)` anywhere would be a copy that can disagree. It can, too —
    `round(v * (units / 24))` and `round(v * units / 24)` differ by one whenever the product
    lands on a half, and one unit in 21600 is invisible on the slide and the whole distance
    between recognising an icon and not.
    """
    scale = units / view_box()
    return tuple((command, tuple(round(v * scale) for v in values))
                 for command, values in segments(name))


def geometry_key(commands: Iterable[Placed]) -> str:
    """Canonical text for one path, in whole units of whatever space it is drawn in.

    A string rather than a tuple of tuples because it is only ever hashed and compared, never
    walked — which is what makes recognition a single `dict` lookup rather than a scan over
    145 candidates.
    """
    out: list[str] = []
    for command, values in commands:
        out.append(command)
        out.extend(str(value) for value in values)
    return " ".join(out)


@lru_cache(maxsize=4)
def _by_geometry(units: int) -> dict[str, str]:
    """Every icon keyed by its path in a `units`-square space, minus anything ambiguous.

    Built per path space rather than once, because the space is a *precision* the writer
    chose (`export_mutate.ICON_PATH_UNITS`, 21600) and a file that has been through another
    tool may state a different one. `lru_cache(maxsize=4)` bounds that: `units` comes off an
    imported file, so an unbounded cache would be a memory sink an adversarial deck controls.

    Colliding keys are **deleted, not overwritten**. Two icons that round to the same integers
    at some coarse `units` cannot be told apart there, and answering with whichever one was
    indexed last would be exactly the false positive this is built to avoid. At the 21600 the
    writer uses all 145 are distinct — they stay distinct down to the 24-unit view box — so
    this drops nothing in practice; it is the guard for the resolution nobody has seen yet.
    """
    index: dict[str, str] = {}
    collisions: set[str] = set()
    for name in names():
        key = geometry_key(placed(name, units))
        if key in index:
            collisions.add(key)
        index[key] = name
    for key in collisions:  # pragma: no cover - no collision exists in the vendored set
        del index[key]
    return index


def identify(key: str, units: int) -> str:
    """Which icon has *exactly* this path in a `units`-square space, or `""` for none.

    The reverse of `path`, and the reason an exported icon survives being reopened as more
    than a nameless freeform — DESIGN §7 lands every imported slide as shapes, and without
    this the preview draws a stroked rectangle where the file holds a chevron.

    **Matched on the geometry, not on a label beside it.** A shape name is the obvious cheap
    channel and it is not a channel at all: PowerPoint's Selection Pane lets anyone rename a
    shape, the harness's own names are `slide:block:slot` targeting strings (§3) rather than
    content, and a name is a claim about a shape that nothing verifies — a rectangle called
    `...growth.icon` would be believed. The path *is* the icon. It also costs the writer
    nothing: no marker is added to the file, so the bytes an exported deck contains are the
    bytes it contained before recognition existed, which is what keeps §6.2's round-trip
    guarantee free rather than something this has to be careful around.

    **Exact, never approximate.** The coordinates being compared are integers the writer
    produced by rounding the same table this reads, so an icon we wrote matches on the nose;
    anything needing a tolerance was not drawn from this table, and a threshold would only
    decide how often a hand-drawn arrow gets relabelled `growth`. A miss is cheap — the shape
    stays a freeform, which is what it is today — and a false positive silently rewrites what
    the preview draws and what `eject_slide`'s writer would re-emit.
    """
    if units <= 0:
        return ""
    return _by_geometry(int(units)).get(key, "")
