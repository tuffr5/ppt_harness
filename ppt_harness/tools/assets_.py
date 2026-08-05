"""Getting a picture *into* a deck — DESIGN §1.5.

Until this existed, `Session.assets` had exactly one filler: import. Open a `.pptx` and its
media came across; build a deck from nothing and there was no path, no tool and no API that
put bytes in the store. A generated deck could therefore never hold a picture — the only
reason one ever showed a photograph was that `io/media.resolve` used to fall back to reading
`asset_id` as a path on disk, which made the deck's content depend on a file outside it. That
fallback is gone (see `io/media.py`); this is what replaces it.

`add_asset` reads an image once, files it under a key, and hands the key back. From then on
the key is the picture's name everywhere: `media` slots take it as `asset_id`, `add_image`
and `replace_image` register their file the same way, the preview inlines it from the store,
and the exporter embeds those same bytes. One picture, one set of bytes, one answer.

Three decisions, each with a cost worth stating:

**Keys are content-addressed, and legible.** The default key is the filename's stem plus
eight hex of the bytes' sha1 — `q3-revenue-4f1c2ab9`. A pure hash would dedupe perfectly and
read like line noise in an op log a human is trying to follow; a bare filename reads well and
collides the moment two directories both hold `chart.png`. The suffix settles collisions and
the stem carries the meaning. Identity, though, is the *bytes*: adding the same file twice —
under either name, from either directory — returns the key that already holds it rather than
storing a second copy, so re-adding a picture cannot grow the deck. A caller who supplies
`key` gets it verbatim, because a script that wants `logo` in its slot payloads should be
able to say `logo`; a name already taken by different bytes is refused rather than silently
rebound.

**The bytes never enter the op log.** The op names a digest and the store's pool holds the
picture (`DeckStore._blobs`). An op carrying base64 would put megabytes into every
`journal.jsonl` line and hold them in memory for the life of the session, twice over, for
something the workspace already writes to `assets/`. Undo still works: the inverse of
`add_asset` is `delete_asset`, which drops the key and keeps the bytes so redo can put them
back.

**Refusal is cheap; a wrong picture is not.** Everything is checked before the write —
readable, non-empty, within the same size limit the importer applies, actually an image by
its *bytes* rather than its extension, and specifically not SVG, which PowerPoint renders
only as a vector part plus a raster fallback that the harness cannot generate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.session import Session
from ..io import media as media_mod
from ..state.document import Author
from ..state.ops import Turn
from .base import Diff, ToolError, obj, string, tool

#: What the refusal names when the bytes are not a picture. The formats python-pptx can
#: measure and embed — the list is short because it is the *intersection* of what it reads
#: and what PowerPoint draws, and naming a format the writer would choke on is worse than
#: naming none.
READABLE = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff")

#: Key charset. Lowercase, digits and hyphens: a key appears in a slot payload, in an op log,
#: and as a filename under the workspace's `assets/` — the intersection of what those three
#: read back unambiguously is narrow, and a key with a slash in it would escape the directory.
_SLUG = re.compile(r"[^a-z0-9]+")
KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class Ingested:
    """One picture in the store, and what reading it taught us."""

    key: str
    probe: media_mod.Probe
    size: int
    reused: bool
    """True when these exact bytes were already in the deck; `key` is where they were."""


def read_image(path: str) -> tuple[bytes, media_mod.Probe]:
    """The bytes of an image file, validated, or a `ToolError` saying precisely what is wrong.

    Every check is here rather than spread across callers so `add_asset` and `add_image`
    refuse identically — a picture the tool accepts and the slot rejects (or vice versa) is
    the kind of inconsistency a model burns turns on.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise ToolError("no_such_image", f"no file at {source}")
    try:
        blob = source.read_bytes()
    except OSError as exc:
        raise ToolError("unreadable_image", f"{source} could not be read: {exc}") from exc

    if not blob:
        raise ToolError("not_an_image", f"{source.name} is empty")
    if len(blob) > media_mod.MAX_ASSET_BYTES:
        raise ToolError(
            "image_too_large",
            f"{source.name} is {len(blob) / 1e6:.1f} MB; the limit is "
            f"{media_mod.MAX_ASSET_BYTES / 1e6:.0f} MB. Resize or re-encode it — a slide is "
            "about 1600px wide at most, so a photograph larger than that is bytes the "
            "audience cannot see.",
            bytes=len(blob),
            limit=media_mod.MAX_ASSET_BYTES,
        )
    if media_mod.is_svg(blob):
        raise ToolError(
            "svg_unsupported",
            f"{source.name} is SVG. PowerPoint shows an SVG only as a vector part paired "
            "with a rasterised fallback that Office itself generates, and the harness can "
            "produce neither — it cannot even measure the image to letterbox it. Rasterise "
            "it to PNG at the size it will appear and add that.",
        )

    read = media_mod.probe(blob)
    if read is None:
        raise ToolError(
            "not_an_image",
            f"{source.name} is not an image PowerPoint reads. Its bytes do not start like "
            f"one — the extension is not what was checked — so use one of "
            f"{list(READABLE)}.",
        )
    return blob, read


def _default_key(stem: str, digest: str) -> str:
    """`q3-revenue-4f1c2ab9` — the filename's meaning, the bytes' identity."""
    slug = _SLUG.sub("-", stem.lower()).strip("-")[:40]
    return f"{slug or 'image'}-{digest[:8]}"


def register(session: Session, turn: Turn, blob: bytes, probe: media_mod.Probe, *,
             stem: str = "image", key: str | None = None,
             author: Author = Author.MODEL) -> Ingested:
    """File `blob` in the deck's assets inside an open turn, and say what key it went under.

    Takes the turn rather than opening its own, so ingesting and the write that uses the
    key — `add_image` adds a shape naming it — are one undoable unit. Half of that pair
    landing would leave either a picture nothing points at or a shape pointing at nothing.
    """
    digest = session.store.stage_blob(blob)
    existing = session.store.asset_named(digest)

    if existing is not None and key in (None, existing):
        # Same bytes, and no other name insisted on: hand back what is already there and
        # write no op at all. This is the dedupe promise — adding the same file twice must
        # not grow the deck — and it also makes the tool idempotent, which matters for a
        # model that retries a call it is unsure landed.
        return Ingested(key=existing, probe=probe, size=len(blob), reused=True)

    # A caller-supplied key that is free while the bytes already sit under another key is
    # honoured as an *alias*: two names, one `bytes` object in memory, and one image part in
    # the package, because python-pptx keys its media parts by content hash. It costs a
    # second file under the workspace's `assets/`, which is a fair price for letting a script
    # say `logo` and mean it.
    key = key or _default_key(stem, digest)
    session.store.write(turn, "add_asset", f"asset/{key}",
                        {"key": key, "content_type": probe.content_type, "sha1": digest},
                        author)
    return Ingested(key=key, probe=probe, size=len(blob), reused=False)


def ingest(session: Session, turn: Turn, path: str, key: str | None = None,
           author: Author = Author.MODEL) -> Ingested:
    """`read_image` then `register` — what a caller holding a path and a turn wants."""
    blob, probe = read_image(path)
    check_key(session, key, blob)
    return register(session, turn, blob, probe,
                    stem=Path(path).expanduser().stem, key=key, author=author)


def check_key(session: Session, key: str | None, blob: bytes | None = None) -> None:
    """Refuse an unusable or already-taken key, before anything is written.

    `blob` is the bytes about to be filed; without it only the *shape* of the key is checked,
    which is what a caller does before reading a file it may not need. A key already pointing
    at these exact bytes is never a conflict — that is the dedupe case, and refusing it would
    make re-adding the same picture under its own name an error.
    """
    if key is None:
        return
    if not KEY_PATTERN.match(key):
        raise ToolError(
            "bad_key",
            f"{key!r} is not usable as an asset key. Lowercase letters, digits, '.', '-' "
            "and '_', starting with a letter or digit, up to 64 characters — a key is also "
            "a filename in the workspace and a value in a slot payload.",
        )
    held = session.assets.get(key)
    if blob is not None and held is not None and held[1] != blob:
        raise ToolError(
            "key_taken",
            f"{key!r} already names a different picture in this deck. Pick another name, or "
            "omit `key` and take the content-addressed one.",
        )


@tool("add_asset",
      "Read an image into the deck so slides can name it. Returns the key to use as "
      "`asset_id` in a media slot, and what the image turned out to be.",
      obj({"path": string("Path to an image file on this machine"),
           "key": string("Name to file it under; omit to derive one from the filename")},
          ["path"]),
      mutating=True)
def add_asset(session: Session, path: str, key: str | None = None,
              author: Author = Author.MODEL) -> dict[str, Any]:
    """The way bytes get into a deck. Shared, because an imported deck may want a picture too.

    Returns the pixel dimensions and the aspect ratio as well as the key, because the caller
    is about to choose a slot for it and those are the facts that decide: a 3:1 panorama in a
    square `image_full` slot letterboxes to two thick bands of background, and the moment to
    learn that is before the slide is built rather than from the render afterwards.

    The one mutating tool with an empty `render`, and honestly so: this touches no slide, so
    there is nothing to measure. The measurement arrives with the write that *uses* the key —
    `add_slide`, `set_slot`, `add_image` — which is the write that can actually overflow.
    """
    # Shape of the key first, before the file is even opened: a caller who mistyped the name
    # should not have 8 MB read to be told so, and a rejected write is the cheapest failure
    # this system has.
    check_key(session, key)
    with session.transaction(author) as turn:
        added = ingest(session, turn, path, key, author)

    px_w, px_h = added.probe.px
    return Diff(
        summary=(f"{'reused' if added.reused else 'added'} asset {added.key} "
                 f"({px_w}x{px_h} {added.probe.content_type})"),
        target=f"asset/{added.key}",
        after={"asset_id": added.key, "media_type": added.probe.content_type,
               "width_px": px_w, "height_px": px_h,
               "aspect": round(added.probe.aspect, 4),
               "bytes": added.size, "reused": added.reused,
               "use": f'a media slot takes {{"asset_id": "{added.key}", "alt": ...}}'},
    ).as_result()
