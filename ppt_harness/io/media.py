"""Media resolution — a `media` slot's `asset_id` to the picture behind it. DESIGN §1.5.

A managed `media` slot carries `{"asset_id": ..., "alt": ...}` and nothing else. That is the
catalog's rule holding: a component names *content*, never a file, never a size, never a
coordinate. Somebody still has to turn the name into bytes, and this is where.

**One** thing is behind that name: a **key into the deck's assets** — `DeckStore.assets`,
`Session.assets` — which is where every picture an imported package carried already lives,
and, since `add_asset`, where a picture a generated deck was given lives too.

It used to be two. A key that resolved to nothing was tried as a *path on disk*, because
until `add_asset` existed that was the only way a deck the harness generated could have a
picture at all — nothing ingested bytes, so a `media` slot on a built deck could only name a
file. The convenience was real and the cost was worse: a deck whose content is a path is a
deck that shows a picture on the machine that made it and an empty box everywhere else. The
moment the .pptx is mailed, or the file is moved, or the export runs from another directory,
the slide is missing the image and nothing says so. Assets travel inside the deck; paths do
not, so a path is not an answer to "what is this slide made of". `add_asset` (and
`add_image`, which now ingests rather than merely pointing) reads the bytes once, and from
then on the deck names its own content.

Resolving is deliberately its own thing rather than a few lines inside the writer, because
`eject_slide` freezes the geometry the writer would have produced and can only do that if it
asks the same question about the same picture. Two answers to "how wide is this image" is
the shape of the bug that makes an ejected slide stop matching the managed one it came from.

**Pictures are letterboxed, never cropped and never stretched.** The harness chooses the box
(the expander does) but it has never seen the image: it does not know that the top third is
sky and the face is centre-left. Cropping to fill would therefore be the writer deciding
which part of somebody's photograph is the point — a silent content change, and the same
class of thing DESIGN §7 refuses when it makes adoption a proposal rather than an inference.
Stretching is worse still: it is visibly wrong on any photograph and silently wrong on a
diagram. So the picture keeps its proportions, sits centred, and the bands of slide
background that may be left over are the honest cost of not having been told how to crop.
`Box.fit` in `render/expand.py` is the rectangle that follows, and it is inside the slot box
by construction, which is what makes overflow impossible rather than merely unlikely.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

#: What a `media` slot must carry. Both, always: `asset_id` names the picture and `alt`
#: describes it, and `add_image` has refused a picture without alt text since it shipped —
#: a deck that cannot be read aloud is a deck part of the audience cannot use.
REQUIRED = ("asset_id", "alt")

#: The biggest picture the harness will hold in a deck's assets. Stated here because both
#: ends of the pipe need the same number: `import_pptx` skips a package image larger than
#: this, and `add_asset` refuses to ingest one. A deck whose assets are readable on import
#: but unwritable by the tool would be a rule nobody could act on.
MAX_ASSET_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Asset:
    """One resolved picture: what to hand python-pptx, and how wide it is relative to tall."""

    key: str
    aspect: float
    """Width over height, in the image's own pixels, corrected for non-square DPI."""
    blob: bytes

    def image(self) -> Any:
        """What `add_picture` takes.

        A **fresh** stream every call rather than one held on the instance: python-pptx reads
        the file object to the end, so a second slot naming the same asset would otherwise be
        handed an exhausted buffer and written as a zero-byte picture part.
        """
        return io.BytesIO(self.blob)


@dataclass(frozen=True)
class Probe:
    """What the bytes of an image say about themselves.

    Read through python-pptx's own image reader — the same decoder the package will use when
    it embeds the picture — so what `add_asset` reports and what the writer lays out cannot
    disagree. A second decoder here is how "how wide is this image" acquires two answers.
    """

    content_type: str
    px: tuple[int, int]
    dpi: tuple[int, int]
    aspect: float


def probe(blob: bytes) -> Probe | None:
    """Everything the harness knows about a picture, or `None` when it is not one.

    The media type comes from the **bytes**, never the extension: a `.png` that is really a
    JPEG is an ordinary thing on a laptop full of downloads, and a package that declares the
    wrong content type is one PowerPoint offers to repair rather than open.

    DPI is divided out per axis. It is 72 or 96 on both axes for essentially every image
    anyone puts on a slide, but a scan with 300x150 DPI is a real thing and its pixel ratio
    is twice its shape — the picture would be laid out at the wrong proportions in the one
    case where holding proportions is the entire point.
    """
    from pptx.parts.image import Image

    try:
        image = Image.from_blob(blob)
        px_w, px_h = image.size
        dpi_x, dpi_y = image.dpi
        content_type = image.content_type
    except Exception:
        # Broad on purpose. Anything this cannot read is simply not a picture, and both
        # callers want the same answer to that: `resolve` turns it into the `None` an unknown
        # key gets, and `add_asset` turns it into a refusal naming the formats that work.
        return None
    if px_w <= 0 or px_h <= 0 or not content_type:
        return None
    width = px_w / (dpi_x or 72)
    height = px_h / (dpi_y or 72)
    if height <= 0:
        return None
    return Probe(content_type=content_type, px=(px_w, px_h),
                 dpi=(dpi_x or 72, dpi_y or 72), aspect=width / height)


def is_svg(blob: bytes) -> bool:
    """Does this look like SVG?

    Worth its own answer because SVG is the one format where "PowerPoint supports it" and
    "the harness can use it" come apart. python-pptx cannot read it at all — it sniffs a
    handful of raster headers and has no width, height or content type for anything else —
    so an SVG reaching `add_picture` is either a hard failure or a picture with no
    proportions to letterbox. PowerPoint 2016+ *does* display SVG, but only as a pair: the
    vector part plus a rasterised fallback that Office generates and the harness cannot.
    Detected by sniffing rather than by suffix so the refusal is about the bytes, and named
    separately from `not_an_image` so the answer can be "rasterise it to PNG first", which is
    a thing the caller can actually do.
    """
    head = blob[:1024].lstrip()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in blob[:4096])


def payload(value: Any) -> tuple[str, str] | None:
    """`(asset_id, alt)` from a media slot's value, or `None` if it is not one.

    Returns None rather than raising, and the writer turns that into a `slot_not_written`
    violation. A filled slot that produces nothing has to be *said* — that is the whole
    lesson of the chart-key bug — and an exception thrown from inside the writer would take
    the rest of the deck down with it instead.
    """
    if not isinstance(value, dict):
        return None
    asset_id = str(value.get("asset_id") or "").strip()
    alt = str(value.get("alt") or "").strip()
    if not asset_id or not alt:
        return None
    return asset_id, alt


def resolve(asset_id: str, assets: dict[str, tuple[str, bytes]] | None) -> Asset | None:
    """The picture `asset_id` names, or `None` if nothing is behind the name.

    The deck's own assets, and only those. A key that also happens to exist as a path on the
    machine doing the export is still the deck's asset — the deck is the thing being
    written, and letting the filesystem win would make an export depend on which directory
    it ran in; a key behind *nothing* now resolves to nothing rather than quietly meaning
    "look on this disk", for the reason the module docstring gives.

    `None` rather than an exception: the writer turns it into a `slot_not_written` violation
    naming the asset, which is more use than a traceback that takes the rest of the deck
    down with it.
    """
    if not asset_id:
        return None

    found = (assets or {}).get(asset_id)
    if found is None:
        return None
    _, blob = found
    read = probe(blob)
    return None if read is None else Asset(key=asset_id, aspect=read.aspect, blob=blob)
