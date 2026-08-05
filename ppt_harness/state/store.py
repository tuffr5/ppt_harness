"""Deck store — DESIGN §1.6.

The single writer. Every mutation goes through `transaction()`, which acquires the lock,
collects invertible ops, and commits or rolls back as a unit. Undo therefore matches what a
user thinks they did ("undo that request") rather than what the model happened to call.

Ops are applied by a registry of named appliers rather than by arbitrary patch merging, so
an op with no inverse cannot be written in the first place.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

from . import richtext
from .document import (
    Author,
    Block,
    ChartSpec,
    Deck,
    Frame,
    Shape,
    Slide,
    TableSpec,
    Theme,
    TypeSpec,
)
from .ops import Op, OpLog, Turn


class StoreError(RuntimeError):
    pass


class Locked(StoreError):
    """Another author holds the writer lock."""


#: op name -> name of the op that undoes it. Absent means it inverts to itself.
INVERSE_OF = {"add_slide": "delete_slide", "delete_slide": "add_slide",
              "add_shape": "delete_shape", "delete_shape": "add_shape",
              "add_block": "delete_block", "delete_block": "add_block",
              "add_asset": "delete_asset", "delete_asset": "add_asset"}


class DeckStore:
    def __init__(self, deck: Deck) -> None:
        self.deck = deck
        self.log = OpLog()
        self._undone: list[Turn] = []
        #: Called with each committed turn. A hook rather than a direct call into the
        #: workspace: persistence is a policy the caller opts into, and a store used in a
        #: test or a one-shot CLI call should not touch the filesystem at all.
        self.on_commit: Callable[[Turn], None] | None = None
        #: key -> (content type, bytes) for pictures pulled out of the package, and for
        #: pictures `add_asset` ingested. Deliberately *not* on `Deck`: the document model is
        #: dumped whole for every invertible op, and carrying image bytes through undo would
        #: be absurd.
        self.assets: dict[str, tuple[str, bytes]] = {}
        #: sha1 -> bytes for every picture this store has been handed, whether or not a key
        #: currently points at it. The pool is what lets an `add_asset` op be **invertible
        #: without being enormous**: the op names a digest, the bytes live here, and the log
        #: — which is appended to `journal.jsonl` verbatim and held in memory for the life of
        #: the session — carries a 40-character string instead of a megabyte of base64 per
        #: add. It also survives undo: the bytes stay after the key is removed, so redo has
        #: something to put back.
        self._blobs: dict[str, bytes] = {}

    # -- transactions -----------------------------------------------------------

    @contextmanager
    def transaction(self, author: Author) -> Iterator[Turn]:
        if not self.log.acquire(author):
            raise Locked("another author is writing")
        turn = self.log.begin()
        try:
            yield turn
        except Exception:
            for inverse in self.log.discard(turn):
                self._apply(inverse)
            raise
        else:
            self.log.commit(turn)
            self._undone.clear()
            # After the commit and inside the lock, so what reaches disk is a turn that
            # actually landed and no second writer can interleave with the write. A
            # persistence failure fails the turn rather than leaving the deck ahead of its
            # own history — silently diverging is the one outcome worth crashing over.
            if self.on_commit is not None and turn.ops:
                self.on_commit(turn)
        finally:
            self.log.release()

    def write(
        self, turn: Turn, op: str, target: str, patch: dict[str, Any], author: Author
    ) -> None:
        """Apply a mutation and record its inverse.

        The inverse is computed from live state *before* the patch lands — the only moment
        it is knowable.
        """
        record = Op(
            seq=-1,
            op=op,
            target=target,
            patch=patch,
            inverse=self._invert(op, target, patch),
            author=author,
            turn=turn.id,
            inverse_op=INVERSE_OF.get(op),
        )
        self._apply(record)
        self.log.append(turn, record)

    # -- undo / redo ------------------------------------------------------------

    def undo(self) -> bool:
        turn = next((t for t in reversed(self.log.turns) if t.committed and t.ops), None)
        if turn is None:
            return False
        for inverse in self.log.inverses(turn):
            self._apply(inverse)
        turn.committed = False
        self._undone.append(turn)
        return True

    def redo(self) -> bool:
        if not self._undone:
            return False
        turn = self._undone.pop()
        for op in turn.ops:
            self._apply(op)
        turn.committed = True
        return True

    # -- resolution -------------------------------------------------------------

    def slide(self, slide_id: str) -> Slide:
        slide = self.deck.slide(slide_id)
        if slide is None:
            raise StoreError(f"no slide {slide_id!r}")
        return slide

    def resolve_text_target(self, target: str) -> tuple[Slide, Shape | Block, str | None]:
        """`set_text` addresses `slide/shape` on freeform and `slide/block/slot` on managed.

        Both resolve here so no tool walks the document model itself.
        """
        parts = target.split("/")
        # Shape of the address first. Resolving the slide first turns "you forgot the
        # slash" into "no such slide", which sends the caller looking in the wrong place.
        if len(parts) not in (2, 3):
            raise StoreError(
                f"malformed target {target!r}; expected slide/shape or slide/block/slot"
            )
        slide = self.slide(parts[0])
        if len(parts) == 2:
            shape = slide.shape(parts[1])
            if shape is None:
                raise StoreError(f"no shape {parts[1]!r} on slide {slide.id}")
            return slide, shape, None
        if len(parts) == 3:
            block = slide.block(parts[1])
            if block is None:
                raise StoreError(f"no block {parts[1]!r} on slide {slide.id}")
            if parts[2] not in block.slots:
                raise StoreError(f"block {block.id} has no slot {parts[2]!r}")
            return slide, block, parts[2]
        raise AssertionError("unreachable")

    # -- op dispatch ------------------------------------------------------------

    def _apply(self, op: Op) -> None:
        handler = getattr(self, f"_apply_{op.op}", None)
        if handler is None:
            raise StoreError(f"no applier for op {op.op!r}")
        handler(op.target, op.patch)

    def _invert(self, op: str, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_invert_{op}", None)
        if handler is None:
            raise StoreError(f"op {op!r} has no inverse; it may not be written")
        return handler(target, patch)

    # set_text ------------------------------------------------------------------

    def _apply_set_text(self, target: str, patch: dict[str, Any]) -> None:
        _, obj, slot = self.resolve_text_target(target)
        if slot is None:
            assert isinstance(obj, Shape)
            # Markup becomes runs; `text` keeps the plain words. A model asked to bold
            # something writes `**this**` whatever the signature says, and writing that
            # through verbatim is how asterisks end up on a slide.
            runs = richtext.parse(patch["text"])
            obj.runs = [] if len(runs) == 1 and runs[0].plain else runs
            obj.text = richtext.to_plain(runs)
            # `dirty` is derived from `text`, so undo un-dirties by itself.
        else:
            assert isinstance(obj, Block)
            obj.slots[slot] = patch["text"]

    def _invert_set_text(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        _, obj, slot = self.resolve_text_target(target)
        if slot is not None:
            return {"text": obj.slots.get(slot)}
        # Round-trip through markup so undo restores the emphasis too, not just the words.
        return {"text": richtext.to_markup(obj.runs) if obj.runs else obj.text}

    # add_slide / delete_slide --------------------------------------------------

    def _apply_add_slide(self, target: str, patch: dict[str, Any]) -> None:
        self.deck.slides.insert(patch["index"], Slide.model_validate(patch["slide"]))
        self._reindex()

    def _invert_add_slide(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        return {"slide_id": patch["slide"]["id"]}

    def _apply_delete_slide(self, target: str, patch: dict[str, Any]) -> None:
        self.deck.slides = [s for s in self.deck.slides if s.id != patch["slide_id"]]
        self._reindex()

    def _invert_delete_slide(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        slide = self.slide(patch["slide_id"])
        return {"index": self.deck.slides.index(slide), "slide": slide.model_dump(mode="json")}

    # add_shape / delete_shape --------------------------------------------------

    def _apply_add_shape(self, target: str, patch: dict[str, Any]) -> None:
        slide = self.slide(patch["slide_id"])
        slide.shapes.insert(patch["index"], Shape.model_validate(patch["shape"]))

    def _invert_add_shape(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        return {"slide_id": patch["slide_id"], "shape_id": patch["shape"]["id"]}

    def _apply_delete_shape(self, target: str, patch: dict[str, Any]) -> None:
        slide = self.slide(patch["slide_id"])
        slide.shapes = [s for s in slide.shapes if s.id != patch["shape_id"]]

    def _invert_delete_shape(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        slide = self.slide(patch["slide_id"])
        shape = slide.shape(patch["shape_id"])
        if shape is None:
            raise StoreError(f"no shape {patch['shape_id']!r} to delete")
        # The whole model, and its position. A shape the harness does not understand must
        # come back exactly as it was, and `index` is what keeps z-order intact.
        return {"slide_id": slide.id, "index": slide.shapes.index(shape),
                "shape": shape.model_dump(mode="json")}

    # paragraph and run properties ----------------------------------------------

    def _apply_set_props(self, target: str, patch: dict[str, Any]) -> None:
        """Set any of a shape's presentation properties in one op.

        One op rather than four because they are one *decision* — "make this a centred
        bulleted list" is a single thing a user did, and undo should treat it that way.
        """
        _, obj, slot = self.resolve_text_target(target)
        if slot is not None:
            raise StoreError("managed slots take their properties from the component")
        assert isinstance(obj, Shape)
        for name, value in patch.items():
            # Model fields have to be rebuilt, not assigned. A raw dict here reads back as
            # a dict everywhere downstream — the budget asks it for `.family` and gets an
            # AttributeError far from the write that caused it.
            if name == "runs":
                obj.runs = [richtext.Run.model_validate(r) for r in value]
            elif name == "type_spec":
                obj.type_spec = TypeSpec.model_validate(value) if value else None
            else:
                setattr(obj, name, value)

    def _invert_set_props(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        _, obj, slot = self.resolve_text_target(target)
        if slot is not None:
            raise StoreError("managed slots take their properties from the component")
        assert isinstance(obj, Shape)
        return {
            name: ([r.model_dump(mode="json") for r in obj.runs] if name == "runs"
                   else getattr(obj, name))
            for name in patch
        }

    # blocks --------------------------------------------------------------------

    def _block(self, slide_id: str, block_id: str) -> Block:
        slide = self.slide(slide_id)
        block = slide.block(block_id)
        if block is None:
            raise StoreError(f"no block {block_id!r} on slide {slide.id}")
        return block

    #: Addressing, not content. Everything else in a `set_block_props` patch is a property
    #: to set, so these two names are the only ones a block may not have.
    _BLOCK_ADDRESS = ("slide_id", "block_id")

    def _apply_set_block_props(self, target: str, patch: dict[str, Any]) -> None:
        """Set any of a block's properties — component, variant, region, overrides.

        One op for all of them because they are one *decision*: a component swap carries a
        variant reset with it, and undoing half of that leaves a block whose variant its
        component has never heard of.
        """
        block = self._block(patch["slide_id"], patch["block_id"])
        for name, value in patch.items():
            if name not in self._BLOCK_ADDRESS:
                setattr(block, name, value)

    def _invert_set_block_props(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        block = self._block(patch["slide_id"], patch["block_id"])
        return {"slide_id": patch["slide_id"], "block_id": patch["block_id"],
                **{name: getattr(block, name) for name in patch
                   if name not in self._BLOCK_ADDRESS}}

    def _apply_add_block(self, target: str, patch: dict[str, Any]) -> None:
        slide = self.slide(patch["slide_id"])
        slide.blocks.insert(patch["index"], Block.model_validate(patch["block"]))

    def _invert_add_block(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        return {"slide_id": patch["slide_id"], "block_id": patch["block"]["id"]}

    def _apply_delete_block(self, target: str, patch: dict[str, Any]) -> None:
        slide = self.slide(patch["slide_id"])
        slide.blocks = [b for b in slide.blocks if b.id != patch["block_id"]]

    def _invert_delete_block(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        block = self._block(patch["slide_id"], patch["block_id"])
        slide = self.slide(patch["slide_id"])
        # Position included: a block restored to the end of the list is a different slide
        # from the one that was deleted, because region order drives the layout.
        return {"slide_id": slide.id, "index": slide.blocks.index(block),
                "block": block.model_dump(mode="json")}

    # geometry ------------------------------------------------------------------

    def _apply_set_frames(self, target: str, patch: dict[str, Any]) -> None:
        """Move or resize several shapes at once.

        One op for the whole selection because it is one *decision* — "align these left" is
        a single thing the user did, and undo has to put all of them back together.
        """
        slide = self.slide(patch["slide_id"])
        for shape_id, frame in patch["frames"].items():
            shape = slide.shape(shape_id)
            if shape is None:
                raise StoreError(f"no shape {shape_id!r} on {slide.id}")
            shape.frame = Frame.model_validate(frame)

    def _invert_set_frames(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        slide = self.slide(patch["slide_id"])
        before: dict[str, Any] = {}
        for shape_id in patch["frames"]:
            shape = slide.shape(shape_id)
            if shape is None:
                raise StoreError(f"no shape {shape_id!r} on {slide.id}")
            before[shape_id] = shape.frame.model_dump(mode="json")
        return {"slide_id": slide.id, "frames": before}

    # slide properties ----------------------------------------------------------

    #: Slide fields that hold models rather than plain values, and the model to rebuild
    #: each from. A raw dict assigned here reads back as a dict everywhere downstream.
    _REBUILD: ClassVar[dict[str, type]] = {"shapes": Shape, "blocks": Block}

    def _apply_set_slide_props(self, target: str, patch: dict[str, Any]) -> None:
        slide = self.slide(patch["slide_id"])
        for name, value in patch.items():
            if name == "slide_id":
                continue
            # Model fields are rebuilt, not assigned: a raw dict reads back as a dict
            # everywhere downstream and fails far from the write that caused it.
            model = self._REBUILD.get(name)
            setattr(slide, name,
                    [model.model_validate(v) for v in value] if model else value)

    def _invert_set_slide_props(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        slide = self.slide(patch["slide_id"])
        out: dict[str, Any] = {"slide_id": slide.id}
        for name in patch:
            if name == "slide_id":
                continue
            current = getattr(slide, name)
            out[name] = ([m.model_dump(mode="json") for m in current]
                         if name in self._REBUILD else current)
        return out

    # theme ---------------------------------------------------------------------

    def _apply_set_theme(self, target: str, patch: dict[str, Any]) -> None:
        """Replace the whole theme.

        Whole rather than field-by-field because the theme is validated as a unit — a
        palette role and the contrast pair it belongs to cannot be checked separately.
        """
        self.deck.theme = Theme.model_validate(patch["theme"])

    def _invert_set_theme(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        return {"theme": self.deck.theme.model_dump(mode="json")}

    # notes ---------------------------------------------------------------------

    def _apply_set_notes(self, target: str, patch: dict[str, Any]) -> None:
        self.slide(patch["slide_id"]).notes = patch["notes"]

    def _invert_set_notes(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        return {"slide_id": patch["slide_id"],
                "notes": self.slide(patch["slide_id"]).notes}

    # assets --------------------------------------------------------------------

    def stage_blob(self, blob: bytes) -> str:
        """Put bytes in the pool and return the digest an op can name them by.

        Called before the write, because the op has to be able to name something that is
        already there: `_apply_add_asset` runs during the transaction *and* again on redo,
        and the second time nobody is holding the file.
        """
        digest = hashlib.sha1(blob).hexdigest()
        self._blobs[digest] = blob
        return digest

    def asset_named(self, digest: str) -> str | None:
        """The key already holding exactly these bytes, or `None`.

        A linear scan that re-hashes every asset rather than an index kept up to date,
        because `assets` is assigned wholesale by the importer and updated in place by the
        workspace — an index maintained here would be stale exactly when it mattered, and a
        stale answer means the same picture stored twice. Ingesting is a rare, interactive
        act on a dict of tens of entries; the scan costs milliseconds and cannot be wrong.
        """
        for key, (_, blob) in self.assets.items():
            if hashlib.sha1(blob).hexdigest() == digest:
                return key
        return None

    def _asset_blob(self, key: str, digest: str) -> bytes:
        """The bytes an `add_asset` op refers to.

        The pool first, then whatever is already filed under the key. The second case is a
        restore: `Workspace` writes assets to disk beside the journal, so a replayed
        `add_asset` finds its picture already loaded and must not insist on a pool that this
        process never filled.
        """
        blob = self._blobs.get(digest)
        if blob is not None:
            return blob
        found = self.assets.get(key)
        if found is not None:
            return found[1]
        raise StoreError(f"no bytes for asset {key!r} ({digest[:8]}); they were never staged")

    def _apply_add_asset(self, target: str, patch: dict[str, Any]) -> None:
        self.assets[patch["key"]] = (patch["content_type"],
                                     self._asset_blob(patch["key"], patch["sha1"]))

    def _invert_add_asset(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        return {"key": patch["key"]}

    def _apply_delete_asset(self, target: str, patch: dict[str, Any]) -> None:
        found = self.assets.pop(patch["key"], None)
        if found is not None:
            # Keep the bytes. This is what undo runs, and a redo immediately after has to be
            # able to put the same picture back — including after a restore, where the pool
            # was empty because the bytes came off disk rather than through `stage_blob`.
            self.stage_blob(found[1])

    def _invert_delete_asset(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        found = self.assets.get(patch["key"])
        if found is None:
            raise StoreError(f"no asset {patch['key']!r} to delete")
        content_type, blob = found
        return {"key": patch["key"], "content_type": content_type,
                "sha1": self.stage_blob(blob)}

    # object payloads -----------------------------------------------------------

    def _apply_set_payload(self, target: str, patch: dict[str, Any]) -> None:
        """Replace a shape's table or chart data.

        One op for both because they are the same kind of change — the numbers behind an
        object, not the object itself — and a single op keeps undo aligned with what the
        user thinks they did.
        """
        slide = self.slide(patch["slide_id"])
        shape = slide.shape(patch["shape_id"])
        if shape is None:
            raise StoreError(f"no shape {patch['shape_id']!r}")
        if "table" in patch:
            shape.table = TableSpec.model_validate(patch["table"]) if patch["table"] else None
        if "chart" in patch:
            shape.chart = ChartSpec.model_validate(patch["chart"]) if patch["chart"] else None

    def _invert_set_payload(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        slide = self.slide(patch["slide_id"])
        shape = slide.shape(patch["shape_id"])
        if shape is None:
            raise StoreError(f"no shape {patch['shape_id']!r}")
        out: dict[str, Any] = {"slide_id": slide.id, "shape_id": shape.id}
        if "table" in patch:
            out["table"] = shape.table.model_dump(mode="json") if shape.table else None
        if "chart" in patch:
            out["chart"] = shape.chart.model_dump(mode="json") if shape.chart else None
        return out

    # set_z_order ---------------------------------------------------------------

    def _apply_set_z_order(self, target: str, patch: dict[str, Any]) -> None:
        slide = self.slide(patch["slide_id"])
        shape = slide.shape(patch["shape_id"])
        if shape is None:
            raise StoreError(f"no shape {patch['shape_id']!r}")
        slide.shapes.remove(shape)
        slide.shapes.insert(max(0, min(patch["index"], len(slide.shapes))), shape)

    def _invert_set_z_order(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        slide = self.slide(patch["slide_id"])
        shape = slide.shape(patch["shape_id"])
        if shape is None:
            raise StoreError(f"no shape {patch['shape_id']!r}")
        return {"slide_id": slide.id, "shape_id": shape.id,
                "index": slide.shapes.index(shape)}

    # reorder -------------------------------------------------------------------

    def _apply_reorder(self, target: str, patch: dict[str, Any]) -> None:
        slide = self.slide(patch["slide_id"])
        self.deck.slides.remove(slide)
        self.deck.slides.insert(patch["index"], slide)
        self._reindex()

    def _invert_reorder(self, target: str, patch: dict[str, Any]) -> dict[str, Any]:
        slide = self.slide(patch["slide_id"])
        return {"slide_id": slide.id, "index": self.deck.slides.index(slide)}

    # ---------------------------------------------------------------------------

    def _reindex(self) -> None:
        for i, slide in enumerate(self.deck.slides):
            slide.index = i
