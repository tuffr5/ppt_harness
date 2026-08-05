"""Media resolution — a `media` slot's `asset_id` to the picture behind it. DESIGN §1.5.

A managed `media` slot carries `{"asset_id": ..., "alt": ...}` and nothing else. That is the
catalog's rule holding: a component names *content*, never a file, never a size, never a
coordinate. Somebody still has to turn the name into bytes, and this is where.

Two things can be behind that name, and both have to work or the slot is decorative:

- a **key into the deck's assets** — `DeckStore.assets`, `Session.assets` — which is where
  every picture an imported package carried already lives; and
- a **path on disk**, which is the only way a deck the harness *generated* has ever had a
  picture put on it, because `add_image` takes one and there is no tool that ingests bytes.

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
from pathlib import Path
from typing import Any

#: What a `media` slot must carry. Both, always: `asset_id` names the picture and `alt`
#: describes it, and `add_image` has refused a picture without alt text since it shipped —
#: a deck that cannot be read aloud is a deck part of the audience cannot use.
REQUIRED = ("asset_id", "alt")


@dataclass(frozen=True)
class Asset:
    """One resolved picture: what to hand python-pptx, and how wide it is relative to tall."""

    key: str
    aspect: float
    """Width over height, in the image's own pixels, corrected for non-square DPI."""
    path: str | None = None
    blob: bytes | None = None

    def image(self) -> Any:
        """What `add_picture` takes.

        A **fresh** stream every call rather than one held on the instance: python-pptx reads
        the file object to the end, so a second slot naming the same asset would otherwise be
        handed an exhausted buffer and written as a zero-byte picture part.
        """
        if self.blob is not None:
            return io.BytesIO(self.blob)
        return self.path


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

    The deck's own assets are consulted first. A key that also happens to exist as a relative
    path on the machine doing the export is still the deck's asset — the deck is the thing
    being written, and letting the filesystem win would make an export depend on which
    directory it ran in.
    """
    if not asset_id:
        return None

    found = (assets or {}).get(asset_id)
    if found is not None:
        _, blob = found
        aspect = _aspect_of_blob(blob)
        return None if aspect is None else Asset(key=asset_id, aspect=aspect, blob=blob)

    path = Path(asset_id).expanduser()
    if not path.is_file():
        return None
    aspect = _aspect_of_blob(path.read_bytes())
    return None if aspect is None else Asset(key=asset_id, aspect=aspect,
                                             path=str(path.resolve()))


def _aspect_of_blob(blob: bytes) -> float | None:
    """Width over height of an image, or `None` when it is not one this can read.

    Measured through python-pptx's own image reader rather than a second decoder, so the
    proportions the writer lays out are the ones the package will report for the picture part
    it is about to embed.

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
    except Exception:
        # Broad on purpose. Anything this cannot read is simply not a picture, and the one
        # correct answer to that is the same `None` an unknown key gets — the writer turns
        # it into a `slot_not_written` violation naming the asset, which is more use than a
        # traceback that takes the rest of the deck down with it.
        return None
    if px_w <= 0 or px_h <= 0:
        return None
    width = px_w / (dpi_x or 72)
    height = px_h / (dpi_y or 72)
    return None if height <= 0 else width / height
