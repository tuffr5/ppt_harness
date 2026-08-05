"""Pictures, tables, charts and speaker notes — PLAN A4.

The objects a deck is actually made of, beyond text. Each is placed by **region**, so
nothing here takes a coordinate, and each produces the structure the importer already
treats as first-class — a deck the harness writes is one it can read back and keep editing.

Two commitments worth naming:

- **A chart is data, not a picture.** `add_chart` writes a native chart with its embedded
  worksheet, so the recipient can edit the numbers. DESIGN §1.5 is explicit that the harness
  never degrades one into an image, and there is no path back if it did.
- **Alt text is required, not optional.** A deck that cannot be read aloud is a deck part of
  the audience cannot use, and the moment to supply it is the moment the picture is added.
- **A picture is read, not referenced.** `add_image` ingests the file into the deck's assets
  (`tools/assets_.py`) and the shape names the key. A deck whose content is a path on the
  machine that built it is a deck that shows an empty box the moment it is opened anywhere
  else, and the preview — which draws from the store — could not show it either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..components import registry
from ..core.session import Session
from ..render import expand
from ..state.document import Author, ChartSpec, Mode, Shape, Slide, TableSpec
from . import assets_
from .base import Diff, ToolError, integer, obj, string, tool

REGIONS = sorted({name for frame in registry.LAYOUTS.values() for name in frame.regions})

#: Chart kinds the exporter can write and the importer can read back.
CHART_KINDS = ("bar", "column", "line", "pie")

#: A table wider than this stops being a table and starts being a spreadsheet nobody reads
#: from the back of a room.
MAX_COLUMNS = 8
MAX_ROWS = 20

# What counts as an image is no longer a set of suffixes here. `tools/assets_.read_image`
# decides it from the bytes, because a `.png` that is really a JPEG is an ordinary thing on
# a laptop full of downloads and a package that declares the wrong content type is one
# PowerPoint offers to repair rather than open.


def _freeform(session: Session, slide_id: str) -> Slide:
    slide = session.slide(slide_id)
    if slide.mode is not Mode.FREEFORM:
        raise ToolError(
            "wrong_mode",
            f"{slide_id} is managed — it is built from components. Use add_block with an "
            "image, chart or table component instead.",
            mode=slide.mode.value,
        )
    return slide


def _placed(session: Session, region: str) -> tuple[int, int, int, int]:
    box = expand.region_by_name(session.theme, region)
    if box is None:
        raise ToolError("unknown_region",
                        f"no region {region!r} on the theme grid; it has {REGIONS}")
    cx, cy = session.slide_size_emu()
    return box.emu(*session.theme.grid.canvas, cx, cy)


def _new(session: Session, slide: Slide, kind: str, region: str, **fields: Any) -> Shape:
    x, y, w, h = _placed(session, region)
    return Shape(id=session.new_id("sh"), ooxml_id=0, type=kind,
                 frame={"x": x, "y": y, "cx": w, "cy": h}, **fields)


def _add(session: Session, slide: Slide, shape: Shape, author: Author) -> None:
    with session.transaction(author) as turn:
        session.store.write(turn, "add_shape", f"{slide.id}/{shape.id}",
                            {"slide_id": slide.id, "index": len(slide.shapes),
                             "shape": shape.model_dump(mode="json")}, author)


# ------------------------------------------------------------------------ pictures


@tool("add_image", "Place a picture on an imported slide. Alt text is required.",
      obj({"slide_id": string("Slide id"),
           "region": string("Where to place it", REGIONS),
           "path": string("Path to an image file"),
           "alt": string("What the picture shows, for screen readers")},
          ["slide_id", "region", "path", "alt"]),
      gate="freeform", mutating=True)
def add_image(session: Session, slide_id: str, region: str, path: str, alt: str,
              author: Author = Author.MODEL) -> dict[str, Any]:
    """A picture on a slide, and its bytes in the deck.

    The file is **ingested**, not merely pointed at: the same `add_asset` path reads it,
    validates it by its bytes, dedupes it against what the deck already holds, and files it
    under a key the shape then names. Two things follow that did not hold when this recorded
    a path and nothing else.

    First, the preview shows it. `render/html.py` draws `shape.asset` — a store key — and had
    nothing to draw for a shape carrying only `source`, so a picture added here was in the
    exported file and absent from every render the model was verifying against. That is
    precisely the divergence the harness exists to close.

    Second, the deck stops depending on the machine that built it. `source` is still recorded
    because it is the provenance a later `replace_image` is traced through, and because the
    writer's own dispatch still keys picture-ness off it — but it is no longer where the
    picture *is*.
    """
    slide = _freeform(session, slide_id)
    # Alt text before the file is read: it is the check most likely to fail and the cheapest
    # to run, and refusing after ingesting would mean bytes in the store for a picture no
    # slide ended up carrying.
    if not alt.strip():
        raise ToolError("alt_required",
                        "alt text is required: a picture with none is invisible to anyone "
                        "using a screen reader")
    source = Path(path).expanduser()

    with session.transaction(author) as turn:
        added = assets_.ingest(session, turn, path, author=author)
        shape = _new(session, slide, "picture", region, alt=alt.strip(),
                     asset=added.key, source=str(source.resolve()))
        session.store.write(turn, "add_shape", f"{slide.id}/{shape.id}",
                            {"slide_id": slide.id, "index": len(slide.shapes),
                             "shape": shape.model_dump(mode="json")}, author)

    px_w, px_h = added.probe.px
    return Diff(summary=f"added a picture in {region}", target=f"{slide.id}/{shape.id}",
                after={"id": shape.id, "alt": alt, "asset_id": added.key,
                       "media_type": added.probe.content_type,
                       "width_px": px_w, "height_px": px_h, "source": shape.source},
                render=session.measure_slide(slide.id)).as_result()


@tool("replace_image", "Swap the picture in an existing image shape, keeping its frame.",
      obj({"shape_id": string("Shape id"), "path": string("Path to the new image")},
          ["shape_id", "path"]),
      gate="freeform", mutating=True)
def replace_image(session: Session, shape_id: str, path: str,
                  author: Author = Author.MODEL) -> dict[str, Any]:
    slide, shape = _locate(session, shape_id)
    if shape.type not in ("picture", "media"):
        raise ToolError("not_an_image", f"{shape_id} is a {shape.type}")

    before = {"asset_id": shape.asset, "source": shape.source}
    with session.transaction(author) as turn:
        # Ingested, then repointed, in one turn — so undo restores the shape's old picture
        # rather than leaving it naming an asset that a half-undone turn removed. The old
        # asset is deliberately *not* deleted: another shape or slot may name the same key,
        # and the store has no reference count to tell.
        added = assets_.ingest(session, turn, path, author=author)
        session.store.write(turn, "set_props", f"{slide.id}/{shape.id}",
                            {"asset": added.key,
                             "source": str(Path(path).expanduser().resolve())}, author)

    return Diff(summary=f"replaced the picture in {shape_id}",
                target=f"{slide.id}/{shape.id}", before=before,
                after={"asset_id": shape.asset, "source": shape.source,
                       "media_type": added.probe.content_type},
                render=session.measure_slide(slide.id)).as_result()


def _locate(session: Session, shape_id: str) -> tuple[Slide, Shape]:
    for slide in session.deck.slides:
        shape = slide.shape(shape_id)
        if shape is not None:
            return slide, shape
    raise ToolError("no_shape", f"no shape {shape_id!r} in this deck")


# -------------------------------------------------------------------------- tables


@tool("add_table", "Add a real table — not a picture of one.",
      obj({"slide_id": string("Slide id"),
           "region": string("Where to place it", REGIONS),
           "headers": {"type": "array", "items": {"type": "string"},
                       "description": "Header row; omit for a headerless table"},
           "rows": {"type": "array",
                    "items": {"type": "array", "items": {"type": "string"}},
                    "description": "Body rows, each a list of cell strings"}},
          ["slide_id", "region", "rows"]),
      gate="freeform", mutating=True)
def add_table(session: Session, slide_id: str, region: str, rows: list[list[str]],
              headers: list[str] | None = None,
              author: Author = Author.MODEL) -> dict[str, Any]:
    slide = _freeform(session, slide_id)
    spec = TableSpec(headers=[str(h) for h in (headers or [])],
                     rows=[[str(c) for c in row] for row in rows])
    if not spec.rows and not spec.headers:
        raise ToolError("empty_table", "a table needs at least one row")
    if spec.columns > MAX_COLUMNS:
        raise ToolError("too_wide",
                        f"{spec.columns} columns will not read from the back of a room; "
                        f"{MAX_COLUMNS} is the practical limit. Split it, or show the "
                        "columns that carry the point.")
    if len(spec.rows) > MAX_ROWS:
        raise ToolError("too_tall",
                        f"{len(spec.rows)} rows exceeds {MAX_ROWS}; split across slides")

    shape = _new(session, slide, "table", region, table=spec)
    _add(session, slide, shape, author)
    return Diff(summary=f"added a {spec.columns}x{len(spec.rows)} table in {region}",
                target=f"{slide.id}/{shape.id}",
                after={"id": shape.id, "columns": spec.columns, "rows": len(spec.rows)},
                render=session.measure_slide(slide.id)).as_result()


@tool("set_cell", "Change one cell of a table.",
      obj({"shape_id": string("Table shape id"),
           "row": integer("0-based row; row 0 is the header when there is one"),
           "col": integer("0-based column"),
           "text": string("New cell text")},
          ["shape_id", "row", "col", "text"]),
      gate="freeform", mutating=True)
def set_cell(session: Session, shape_id: str, row: int, col: int, text: str,
             author: Author = Author.MODEL) -> dict[str, Any]:
    slide, shape = _locate(session, shape_id)
    if shape.table is None:
        raise ToolError("not_a_table", f"{shape_id} is a {shape.type}, not a table")

    spec = shape.table.model_copy(deep=True)
    grid = [spec.headers, *spec.rows] if spec.headers else spec.rows
    try:
        before = spec.cell(row, col)
    except IndexError as exc:
        raise ToolError("out_of_range", str(exc)) from exc

    line = grid[row]
    line.extend([""] * (spec.columns - len(line)))
    line[col] = str(text)

    with session.transaction(author) as turn:
        session.store.write(turn, "set_payload", f"{slide.id}/{shape.id}",
                            {"slide_id": slide.id, "shape_id": shape.id,
                             "table": spec.model_dump(mode="json")}, author)

    return Diff(summary=f"set cell ({row}, {col}) of {shape_id}",
                target=f"{slide.id}/{shape.id}", before={"text": before},
                after={"text": text},
                render=session.measure_slide(slide.id)).as_result()


# -------------------------------------------------------------------------- charts


def _series(series: list[dict[str, Any]], categories: list[str]) -> list[dict[str, Any]]:
    cleaned = []
    for entry in series:
        values = entry.get("values") or []
        if len(values) != len(categories):
            raise ToolError(
                "series_mismatch",
                f"series {entry.get('name', '?')!r} has {len(values)} values for "
                f"{len(categories)} categories; they must line up",
            )
        cleaned.append({"name": str(entry.get("name") or ""),
                        "values": [float(v) for v in values]})
    return cleaned


@tool("add_chart",
      "Add a native chart with its own worksheet, so the recipient can edit the numbers.",
      obj({"slide_id": string("Slide id"),
           "region": string("Where to place it", REGIONS),
           "kind": string("Chart type", list(CHART_KINDS)),
           "categories": {"type": "array", "items": {"type": "string"},
                          "description": "Category labels, one per data point"},
           "series": {"type": "array",
                      "description": "Each: {name, values} with one value per category",
                      "items": {"type": "object"}}},
          ["slide_id", "region", "kind", "categories", "series"]),
      gate="freeform", mutating=True)
def add_chart(session: Session, slide_id: str, region: str, kind: str,
              categories: list[str], series: list[dict[str, Any]],
              author: Author = Author.MODEL) -> dict[str, Any]:
    slide = _freeform(session, slide_id)
    if kind not in CHART_KINDS:
        raise ToolError("unknown_chart", f"{kind!r} is not one of {list(CHART_KINDS)}")
    if not categories:
        raise ToolError("no_categories", "a chart needs at least one category")
    if not series:
        raise ToolError("no_series", "a chart needs at least one series")

    spec = ChartSpec(kind=kind, categories=[str(c) for c in categories],
                     series=_series(series, list(categories)))
    shape = _new(session, slide, "chart", region, chart=spec)
    _add(session, slide, shape, author)
    return Diff(summary=f"added a {kind} chart in {region}",
                target=f"{slide.id}/{shape.id}",
                after={"id": shape.id, "kind": kind, "series": len(spec.series)},
                render=session.measure_slide(slide.id)).as_result()


@tool("set_chart_data", "Replace a chart's numbers, worksheet included.",
      obj({"shape_id": string("Chart shape id"),
           "categories": {"type": "array", "items": {"type": "string"},
                          "description": "Category labels"},
           "series": {"type": "array", "items": {"type": "object"},
                      "description": "Each: {name, values}"}},
          ["shape_id", "categories", "series"]),
      gate="freeform", mutating=True)
def set_chart_data(session: Session, shape_id: str, categories: list[str],
                   series: list[dict[str, Any]],
                   author: Author = Author.MODEL) -> dict[str, Any]:
    slide, shape = _locate(session, shape_id)
    if shape.chart is None:
        raise ToolError(
            "not_a_chart",
            f"{shape_id} is a {shape.type}. If it is a picture of a chart, the harness "
            "cannot change the numbers in it — the data is not there to change.",
        )

    spec = ChartSpec(kind=shape.chart.kind, categories=[str(c) for c in categories],
                    series=_series(series, list(categories)))
    with session.transaction(author) as turn:
        session.store.write(turn, "set_payload", f"{slide.id}/{shape.id}",
                            {"slide_id": slide.id, "shape_id": shape.id,
                             "chart": spec.model_dump(mode="json")}, author)

    return Diff(summary=f"replaced the data in {shape_id}",
                target=f"{slide.id}/{shape.id}",
                after={"categories": len(categories), "series": len(spec.series)},
                render=session.measure_slide(slide.id)).as_result()


# --------------------------------------------------------------------------- notes


@tool("set_notes", "Set a slide's speaker notes.",
      obj({"slide_id": string("Slide id"), "notes": string("Notes text")},
          ["slide_id", "notes"]),
      mutating=True)
def set_notes(session: Session, slide_id: str, notes: str,
              author: Author = Author.MODEL) -> dict[str, Any]:
    """Notes are content and carry no budget: nothing renders them on the slide."""
    slide = session.slide(slide_id)
    before = slide.notes

    with session.transaction(author) as turn:
        session.store.write(turn, "set_notes", slide_id,
                            {"slide_id": slide_id, "notes": notes}, author)

    return Diff(summary=f"set notes on {slide_id}", target=slide_id,
                before={"notes": before[:120]}, after={"notes": notes[:120]}).as_result()
