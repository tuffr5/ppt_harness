"""Ops, transactions, and undo — DESIGN §1.6.

The property that matters is that undo is *turn*-scoped. A user who says "undo that" means
the request, not whichever tool call the model happened to make last, and a turn that fails
halfway must leave nothing behind.
"""

from __future__ import annotations

import pytest

from ppt_harness.core.session import Session
from ppt_harness.state.document import Author, Mode, Slide
from ppt_harness.state.store import DeckStore, Locked, StoreError


def _slide(store: DeckStore, index: int) -> dict:
    return Slide(id=f"x{index}", index=index, mode=Mode.MANAGED, layout="stack",
                 blocks=[]).model_dump(mode="json")


def test_a_turn_is_the_unit_of_undo(populated: Session) -> None:
    """Three writes in one turn undo together, not one at a time."""
    store = populated.store
    slide = populated.deck.slides[0]
    target = f"{slide.id}/bk_title/title"

    with store.transaction(Author.MODEL) as turn:
        store.write(turn, "set_text", target, {"text": "one"}, Author.MODEL)
        store.write(turn, "set_text", target, {"text": "two"}, Author.MODEL)
        store.write(turn, "set_text", target, {"text": "three"}, Author.MODEL)

    assert slide.block("bk_title").slots["title"] == "three"
    assert store.undo() is True
    assert slide.block("bk_title").slots["title"] == "A reasonable title"


def test_a_failed_turn_leaves_nothing_behind(populated: Session) -> None:
    store = populated.store
    slide = populated.deck.slides[0]
    original = slide.block("bk_title").slots["title"]

    with pytest.raises(RuntimeError, match="deliberate"):
        with store.transaction(Author.MODEL) as turn:
            store.write(turn, "set_text", f"{slide.id}/bk_title/title", {"text": "half"},
                        Author.MODEL)
            raise RuntimeError("deliberate")

    assert slide.block("bk_title").slots["title"] == original
    assert len(store.log) == 0


def test_redo_restores_an_undone_turn(populated: Session) -> None:
    store = populated.store
    slide = populated.deck.slides[0]
    with store.transaction(Author.MODEL) as turn:
        store.write(turn, "set_text", f"{slide.id}/bk_title/title", {"text": "changed"},
                    Author.MODEL)
    store.undo()
    assert store.redo() is True
    assert slide.block("bk_title").slots["title"] == "changed"


def test_add_slide_inverts_to_delete(blank: Session) -> None:
    """An op whose inverse is a *different* op still round-trips."""
    store = blank.store
    with store.transaction(Author.MODEL) as turn:
        store.write(turn, "add_slide", "deck", {"index": 0, "slide": _slide(store, 0)},
                    Author.MODEL)
    assert len(blank.deck.slides) == 1
    store.undo()
    assert blank.deck.slides == []
    store.redo()
    assert len(blank.deck.slides) == 1


def test_delete_slide_inverts_to_add_at_the_same_index(blank: Session) -> None:
    store = blank.store
    for i in range(3):
        with store.transaction(Author.MODEL) as turn:
            store.write(turn, "add_slide", "deck", {"index": i, "slide": _slide(store, i)},
                        Author.MODEL)
    with store.transaction(Author.USER) as turn:
        store.write(turn, "delete_slide", "x1", {"slide_id": "x1"}, Author.USER)
    assert [s.id for s in blank.deck.slides] == ["x0", "x2"]
    store.undo()
    assert [s.id for s in blank.deck.slides] == ["x0", "x1", "x2"]


def test_reorder_reindexes(blank: Session) -> None:
    store = blank.store
    for i in range(3):
        with store.transaction(Author.MODEL) as turn:
            store.write(turn, "add_slide", "deck", {"index": i, "slide": _slide(store, i)},
                        Author.MODEL)
    with store.transaction(Author.USER) as turn:
        store.write(turn, "reorder", "x2", {"slide_id": "x2", "index": 0}, Author.USER)
    assert [s.id for s in blank.deck.slides] == ["x2", "x0", "x1"]
    assert [s.index for s in blank.deck.slides] == [0, 1, 2]
    store.undo()
    assert [s.id for s in blank.deck.slides] == ["x0", "x1", "x2"]


def test_writer_lock_is_exclusive(populated: Session) -> None:
    """A user dragging a shape mid-turn must not corrupt the model's assumptions."""
    store = populated.store
    with store.transaction(Author.MODEL):
        with pytest.raises(Locked):
            with store.transaction(Author.USER):
                pass


def test_an_op_without_an_inverse_cannot_be_written(populated: Session) -> None:
    store = populated.store
    with pytest.raises(StoreError, match="no inverse"):
        with store.transaction(Author.MODEL) as turn:
            store.write(turn, "invent_something", "x", {}, Author.MODEL)


def test_corrections_are_a_query_not_instrumentation(populated: Session) -> None:
    """The observed channel of the preference profile — DESIGN §8.2."""
    store = populated.store
    slide = populated.deck.slides[0]
    target = f"{slide.id}/bk_title/title"

    with store.transaction(Author.MODEL) as turn:
        store.write(turn, "set_text", target, {"text": "Model's phrasing"}, Author.MODEL)
    with store.transaction(Author.USER) as turn:
        store.write(turn, "set_text", target, {"text": "What I actually wanted"}, Author.USER)

    corrections = store.log.corrections()
    assert len(corrections) == 1
    model_op, user_op = corrections[0]
    assert model_op.patch["text"] == "Model's phrasing"
    assert user_op.patch["text"] == "What I actually wanted"


def test_a_model_write_alone_is_not_a_correction(populated: Session) -> None:
    store = populated.store
    with store.transaction(Author.MODEL) as turn:
        store.write(turn, "set_text", f"{populated.deck.slides[0].id}/bk_title/title",
                    {"text": "x"}, Author.MODEL)
    assert store.log.corrections() == []


def test_malformed_targets_are_refused(populated: Session) -> None:
    with pytest.raises(StoreError, match="malformed"):
        populated.store.resolve_text_target("no-slashes-here")
