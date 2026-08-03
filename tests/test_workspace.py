"""Durable workspace and recovery — `state/workspace.py`.

Everything else in the harness is in memory, so these tests are about one question: what
survives a process that does not come back? The interesting cases are all failure cases,
because a persistence layer that only works when nothing goes wrong is decoration.

The deck is rebuilt through a *second* `Workspace` object throughout, never by reading the
first one's attributes. Recovery that only works while the original process is alive is not
recovery.
"""

from __future__ import annotations

import json

import pytest

from ppt_harness.core.session import Session
from ppt_harness.state.document import Author
from ppt_harness.state.workspace import Workspace, WorkspaceError
from ppt_harness.tools import router


def _edit(session: Session, text: str) -> None:
    slide = session.deck.slides[0]
    router.dispatch(session, "set_text",
                    {"target": f"{slide.id}/{slide.blocks[0].id}/title", "text": text})


def _deck_with_a_title(tmp_path) -> tuple[Session, Workspace]:
    session = Session.blank("Persisted")
    workspace = Workspace(tmp_path / "ws")
    workspace.attach(session.store)
    router.dispatch(session, "add_slide", {
        "layout": "stack",
        "blocks": [{"region": "header", "component": "slide_title",
                    "slots": {"title": "First"}}]})
    return session, workspace


# ------------------------------------------------------------------- the happy path


def test_a_committed_turn_reaches_disk_immediately(tmp_path) -> None:
    """Not on exit, not on a timer. A crash gets no chance to run cleanup code."""
    _, workspace = _deck_with_a_title(tmp_path)
    assert workspace.snapshot.is_file()
    assert json.loads(workspace.snapshot.read_text())["slides"][0]["blocks"]


def test_a_restored_deck_is_the_deck_that_was_saved(tmp_path) -> None:
    session, _ = _deck_with_a_title(tmp_path)
    _edit(session, "Second")

    store, report = Workspace(tmp_path / "ws").restore()
    assert report["resumed"] is True
    assert store.deck.slides[0].blocks[0].slots["title"] == "Second"
    assert report["degraded"] is False


def test_history_survives_the_restart_so_undo_still_reaches_back(tmp_path) -> None:
    """The point of journalling ops rather than diffs. A resumed session whose undo stack
    starts empty has lost something the user can see."""
    session, _ = _deck_with_a_title(tmp_path)
    _edit(session, "Second")

    store, _ = Workspace(tmp_path / "ws").restore()
    assert store.undo() is True
    assert store.deck.slides[0].blocks[0].slots["title"] == "First"


def test_a_turn_that_never_committed_is_not_saved(tmp_path) -> None:
    """A rolled-back transaction leaves no trace on disk, because it left none in the deck."""
    session, workspace = _deck_with_a_title(tmp_path)
    before = workspace.journal.read_text()

    with pytest.raises(RuntimeError):
        with session.store.transaction(Author.MODEL) as turn:
            session.store.write(turn, "set_text",
                                f"{session.deck.slides[0].id}/"
                                f"{session.deck.slides[0].blocks[0].id}/title",
                                {"text": "doomed"}, Author.MODEL)
            raise RuntimeError("the turn fails after writing")

    assert workspace.journal.read_text() == before


# ---------------------------------------------------------------------- corruption


def test_a_torn_snapshot_falls_back_to_the_previous_one(tmp_path) -> None:
    """`os.replace` cannot help if the deck written was already wrong, so the last good one
    is kept. Losing the most recent turn beats losing the deck."""
    session, workspace = _deck_with_a_title(tmp_path)
    _edit(session, "Second")
    workspace.snapshot.write_text('{"id": "half-written", "sli')

    store, report = Workspace(tmp_path / "ws").restore()
    assert report["from"] == "backup"
    assert report["degraded"] is True, "a caller must be able to tell it lost something"
    assert "previous" in report["note"]
    assert store.deck.slides


def test_a_half_written_journal_line_is_dropped_not_fatal(tmp_path) -> None:
    """The signature of a crash mid-append. That turn had not finished being recorded, so
    it is the one thing that *should* be lost."""
    session, workspace = _deck_with_a_title(tmp_path)
    _edit(session, "Second")
    with open(workspace.journal, "a", encoding="utf-8") as handle:
        handle.write('{"turn": 9, "ops": [{"op": "set_te')

    turns = Workspace(tmp_path / "ws").turns()
    assert len(turns) == 2, "the two complete turns survive, the fragment does not"
    assert all(t.committed for t in turns)


def test_both_snapshots_gone_replays_the_journal_onto_the_source(tmp_path,
                                                                 fixture_deck) -> None:
    """The last rung of the ladder. Ops are journalled rather than diffs precisely so the
    source file plus the history is still a complete description of the deck."""
    session, report = Session.resume(fixture_deck, root=tmp_path)
    workspace = session.workspace
    slide = next(s for s in session.deck.slides for sh in s.shapes
                 if sh.text and not sh.opaque)
    shape = next(sh for sh in slide.shapes if sh.text and not sh.opaque)
    router.dispatch(session, "set_text",
                    {"target": f"{slide.id}/{shape.id}", "text": "Replayed"})

    workspace.snapshot.unlink()
    workspace.backup.unlink(missing_ok=True)

    store, report = Workspace(workspace.root).restore(fixture_deck)
    assert report["from"] == "journal"
    assert report["degraded"] is True
    assert store.deck.slide(slide.id).shape(shape.id).text == "Replayed"


def test_nothing_readable_and_no_source_is_an_error_not_a_lie(tmp_path) -> None:
    """The one case with no honest answer. Returning an empty deck here would present data
    loss as a fresh start."""
    workspace = Workspace(tmp_path / "empty")
    workspace.journal.write_text("")
    with pytest.raises(WorkspaceError):
        workspace.restore()


def test_a_broken_workspace_never_blocks_opening_the_deck(tmp_path, fixture_deck) -> None:
    """Losing resumed edits is bad; refusing to open the file at all is worse."""
    session, _ = Session.resume(fixture_deck, root=tmp_path)
    _ = router.dispatch(session, "get_outline")
    session.workspace.snapshot.write_text("{ not json")
    session.workspace.backup.write_text("{ also not json")

    resumed, report = Session.resume(fixture_deck, root=tmp_path)
    assert resumed.deck.slides, "the source file is always a valid fallback"
    assert report["degraded"] is True


# ------------------------------------------------------------------------ resuming


def test_resume_picks_up_where_the_last_process_left_off(tmp_path, fixture_deck) -> None:
    first, report = Session.resume(fixture_deck, root=tmp_path)
    assert report["resumed"] is False, "nothing to resume the first time"

    slide = first.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    router.dispatch(first, "set_text",
                    {"target": f"{slide.id}/{shape.id}", "text": "Survives"})
    del first

    second, report = Session.resume(fixture_deck, root=tmp_path)
    assert report["resumed"] is True
    assert report["turns"] == 1
    assert second.deck.slide(slide.id).shape(shape.id).text == "Survives"


def test_fresh_discards_the_history_on_purpose(tmp_path, fixture_deck) -> None:
    first, _ = Session.resume(fixture_deck, root=tmp_path)
    slide = first.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    original = shape.text
    router.dispatch(first, "set_text",
                    {"target": f"{slide.id}/{shape.id}", "text": "Discarded"})

    second, report = Session.resume(fixture_deck, root=tmp_path, fresh=True)
    assert report["resumed"] is False
    assert second.deck.slide(slide.id).shape(shape.id).text == original


def test_assets_survive_a_restore(tmp_path, fixture_deck) -> None:
    """Picture bytes live outside the document model, so they are exactly the thing a
    naive snapshot would silently drop."""
    session, _ = Session.resume(fixture_deck, root=tmp_path)
    if not session.assets:
        pytest.skip("fixture has no pictures")
    slide = session.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    router.dispatch(session, "set_text",
                    {"target": f"{slide.id}/{shape.id}", "text": "x"})

    store, _ = Workspace(session.workspace.root).restore(fixture_deck)
    assert set(store.assets) == set(session.assets)
    for key, (_, blob) in store.assets.items():
        assert blob == session.assets[key][1]


# ------------------------------------------------- writes that were escaping the log


@pytest.mark.parametrize("tool,args,check", [
    ("set_variant", {"variant": "two_col"}, lambda b: b.variant),
    ("set_component", {"component": "bullets"}, lambda b: b.component),
])
def test_block_level_writes_are_journalled(tmp_path, tool, args, check) -> None:
    """These three mutated the deck in place and recorded nothing, so they were invisible
    to undo *and* to this file — an edit that vanished on restart while the UI showed it
    had landed."""
    session = Session.blank("Blocks")
    workspace = Workspace(tmp_path / "ws")
    workspace.attach(session.store)
    router.dispatch(session, "add_slide", {
        "layout": "stack",
        "blocks": [{"region": "body", "component": "bullets", "slots": {"items": ["a"]}}]})
    slide = session.deck.slides[0]
    block = slide.blocks[0]

    result = router.dispatch(session, tool,
                             {"slide_id": slide.id, "block_id": block.id, **args})
    assert result["ok"], result

    store, _ = Workspace(tmp_path / "ws").restore()
    assert check(store.deck.slides[0].blocks[0]) == next(iter(args.values()))


def test_removing_a_block_survives_and_reverses(tmp_path) -> None:
    session = Session.blank("Blocks")
    workspace = Workspace(tmp_path / "ws")
    workspace.attach(session.store)
    router.dispatch(session, "add_slide", {
        "layout": "stack",
        "blocks": [
            {"region": "header", "component": "slide_title", "slots": {"title": "T"}},
            {"region": "body", "component": "bullets", "slots": {"items": ["a"]}}]})
    slide = session.deck.slides[0]
    router.dispatch(session, "remove_block",
                    {"slide_id": slide.id, "block_id": slide.blocks[1].id})

    store, _ = Workspace(tmp_path / "ws").restore()
    assert len(store.deck.slides[0].blocks) == 1
    assert store.undo() is True
    assert len(store.deck.slides[0].blocks) == 2, "and it comes back in its own region"
    assert store.deck.slides[0].blocks[1].region == "body"
