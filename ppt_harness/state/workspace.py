"""Durable workspace — the deck as it stands, plus how it got there.

Everything else in the harness is in-memory. A crash, a killed terminal, or a laptop that
sleeps badly loses the whole session, and the source `.pptx` is untouched until someone
calls `export` — so the work is not merely unsaved, it is nowhere.

Three files answer three different questions, and the split is what makes recovery
possible rather than merely likely:

- `deck.json` is the **state**. Written whole after each committed turn, so restoring is a
  parse rather than a replay.
- `journal.jsonl` is the **history**. One line per committed turn, carrying the ops *and
  their inverses*, so undo still works after a restart — the log is the only place that
  knows how to walk backwards.
- `assets/` holds the picture bytes, which are deliberately not part of the document model
  (see `DeckStore.assets`) and would otherwise be lost on any restore.

**Corruption is assumed, not feared.** A snapshot is written to a temporary file, flushed,
and then `os.replace`d into position — atomic on POSIX, so a reader sees the old file or
the new one and never a half-written one. The previous snapshot is kept as `deck.bak.json`,
and `restore` walks a ladder: current snapshot, previous snapshot, then the source file
replayed through the journal. A journal whose last line was cut off mid-write is truncated
back to its last complete line rather than rejected whole; losing the interrupted turn is
the correct outcome, since that turn never committed.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .document import Deck
from .ops import Op, Turn
from .store import DeckStore

#: Bumped when the on-disk shape changes in a way an older harness cannot read. A workspace
#: from the future is left alone rather than half-understood.
FORMAT = 1


class WorkspaceError(RuntimeError):
    pass


def _write_atomic(path: Path, data: bytes) -> None:
    """Replace `path` in one step, or not at all.

    The flush and `fsync` are the point: `os.replace` is atomic with respect to *readers*,
    but without forcing the bytes down first a power loss can leave the rename durable and
    the contents not.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> Any | None:
    """Parsed contents, or `None` for anything unreadable.

    Every caller here is trying a candidate it expects might be damaged, so a missing file
    and a corrupt one mean the same thing: move on to the next rung of the ladder.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


class Workspace:
    """One deck's durable state, rooted at a directory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._journal_turns: set[int] = set()

    # -- layout -----------------------------------------------------------------

    @property
    def snapshot(self) -> Path:
        return self.root / "deck.json"

    @property
    def backup(self) -> Path:
        return self.root / "deck.bak.json"

    @property
    def journal(self) -> Path:
        return self.root / "journal.jsonl"

    @property
    def meta(self) -> Path:
        return self.root / "meta.json"

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    @classmethod
    def for_deck(cls, deck_id: str, root: Path | None = None) -> Workspace:
        """The workspace belonging to a deck id, under the harness cache by default.

        Keyed by deck id rather than by source path so that a file moved or renamed on disk
        still finds its own history.
        """
        if root is None:
            from ..render.preview import cache_root

            root = cache_root() / "workspace"
        return cls(Path(root) / deck_id)

    def exists(self) -> bool:
        return self.snapshot.is_file() or self.journal.is_file()

    # -- saving -----------------------------------------------------------------

    def attach(self, store: DeckStore) -> None:
        """Persist after every committed turn, from here on.

        The store fires a callback rather than importing this module: persistence is a
        policy the caller opts into, and a `DeckStore` used in a test or a one-shot CLI
        call should not touch the filesystem at all.
        """
        self.save_assets(store)
        store.on_commit = lambda turn: self.record(store, turn)

    def record(self, store: DeckStore, turn: Turn) -> None:
        """One committed turn: append the history, then replace the state.

        Journal first. If the process dies between the two, the next `restore` finds a
        snapshot one turn stale and a journal that knows how to finish the job — the
        opposite order loses the turn entirely.

        Assets are rewritten only when the turn touched them. They used to be written once
        at `attach` on the grounds that nothing mutates an imported image — true until
        `add_asset` shipped, after which a picture added mid-session lived only in memory and
        a restart resumed a deck whose slides named an asset that was no longer there. The
        op names tell us cheaply, so the common turn still pays nothing.
        """
        self.append(turn)
        if any(op.op in ("add_asset", "delete_asset") for op in turn.ops):
            self.save_assets(store)
        self.save_deck(store.deck)

    def append(self, turn: Turn) -> None:
        if not turn.ops:
            return
        line = json.dumps(
            {"turn": turn.id, "ops": [op.model_dump(mode="json") for op in turn.ops]},
            ensure_ascii=False,
        )
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._journal_turns.add(turn.id)

    def save_deck(self, deck: Deck) -> None:
        # Keep the last good snapshot before overwriting: `os.replace` protects against a
        # torn file, not against writing a deck that was already wrong.
        if self.snapshot.is_file():
            shutil.copy2(self.snapshot, self.backup)
        _write_atomic(self.snapshot,
                      json.dumps(deck.model_dump(mode="json"), ensure_ascii=False).encode())
        _write_atomic(self.meta, json.dumps({
            "format": FORMAT,
            "deck_id": deck.id,
            "title": deck.title,
            "source_path": deck.source_path,
            "slides": len(deck.slides),
        }, ensure_ascii=False).encode())

    def save_assets(self, store: DeckStore) -> None:
        """Picture bytes, and the index that names them.

        Written at attach and again after any turn that added or removed one. Individual
        assets are still immutable — a key always holds the same bytes, which is what makes
        content-addressed keys work — so this rewrites the index and the files it names
        rather than diffing anything.
        """
        if not store.assets and not self.assets_dir.is_dir():
            return
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        index = {}
        for key, (content_type, blob) in store.assets.items():
            name = key.replace("/", "_")
            (self.assets_dir / name).write_bytes(blob)
            index[key] = {"file": name, "content_type": content_type}
        _write_atomic(self.assets_dir / "index.json",
                      json.dumps(index, ensure_ascii=False).encode())

    # -- reading ----------------------------------------------------------------

    def turns(self) -> list[Turn]:
        """Committed history, with a truncated final line dropped.

        A half-written last line is the signature of a crash mid-append. That turn had not
        finished being recorded, so discarding it restores the deck to the last state that
        was actually complete.
        """
        if not self.journal.is_file():
            return []
        out: list[Turn] = []
        for line in self.journal.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                out.append(Turn(id=entry["turn"],
                                ops=[Op.model_validate(o) for o in entry["ops"]],
                                committed=True))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # Only the tail can be damaged by an interrupted append; a bad line in the
                # middle means something else wrote here, and replaying past it would apply
                # ops out of order.
                break
        return out

    def load_assets(self) -> dict[str, tuple[str, bytes]]:
        index = _read_json(self.assets_dir / "index.json")
        if not isinstance(index, dict):
            return {}
        out: dict[str, tuple[str, bytes]] = {}
        for key, entry in index.items():
            try:
                blob = (self.assets_dir / entry["file"]).read_bytes()
            except (OSError, KeyError, TypeError):
                continue
            out[key] = (entry.get("content_type", "image/png"), blob)
        return out

    def _deck_from_disk(self) -> tuple[Deck, str] | None:
        """The newest snapshot that actually parses, and which one it was."""
        for path, origin in ((self.snapshot, "snapshot"), (self.backup, "backup")):
            raw = _read_json(path)
            if raw is None:
                continue
            try:
                return Deck.model_validate(raw), origin
            except Exception:
                continue
        return None

    def restore(self, source: Path | str | None = None) -> tuple[DeckStore, dict[str, Any]]:
        """Rebuild a store, and report honestly how much survived.

        The report is not decoration. A caller that resumed from a backup, or that replayed
        a journal because both snapshots were unreadable, is holding a deck that may be a
        turn behind what the user last saw — and the only wrong move is to present it as if
        nothing happened.
        """
        recovered = self._deck_from_disk()
        history = self.turns()
        report: dict[str, Any] = {"resumed": True, "turns": len(history), "degraded": False}

        if recovered is not None:
            deck, origin = recovered
            report["from"] = origin
            if origin == "backup":
                report["degraded"] = True
                report["note"] = ("the current snapshot was unreadable; resumed from the "
                                  "previous one, so the last turn may be missing")
        elif source is not None:
            # Both snapshots are gone. The source file plus the journal is still a complete
            # description of the deck — that is the whole reason the journal carries ops
            # rather than diffs.
            from ..io.import_pptx import import_pptx

            store = import_pptx(source)
            # Assets before the replay, not after: an `add_asset` op names a digest and
            # leaves the bytes outside the log, so replaying one finds its picture only if
            # what was written to `assets/` is already loaded.
            store.assets.update(self.load_assets())
            for turn in history:
                for op in turn.ops:
                    store._apply(op)
            store.log.restore(history)
            report.update({"from": "journal", "degraded": True,
                           "note": f"no readable snapshot; replayed {len(history)} turns "
                                   "onto the source file"})
            return store, report
        else:
            raise WorkspaceError(
                f"{self.root} has no readable snapshot and no source file to replay onto"
            )

        store = DeckStore(deck)
        store.assets.update(self.load_assets())
        # History restored so undo still reaches back past the restart. The ops carry their
        # own inverses, so walking backwards needs nothing the snapshot could have lost.
        store.log.restore(history)
        self._journal_turns = {t.id for t in history}
        return store, report

    def discard(self) -> None:
        """Forget this workspace. Used when a caller wants the source file as it is."""
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
