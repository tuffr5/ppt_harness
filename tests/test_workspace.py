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


def _add_slide(session: Session, title: str) -> None:
    router.dispatch(session, "add_slide", {
        "layout": "stack",
        "blocks": [{"region": "header", "component": "slide_title",
                    "slots": {"title": title}}]})


def _titles(store) -> list[str]:
    return [s.blocks[0].slots["title"] for s in store.deck.slides]


def _deck_with_a_title(tmp_path) -> tuple[Session, Workspace]:
    session = Session.blank("Persisted")
    workspace = Workspace(tmp_path / "ws")
    workspace.attach(session.store)
    _add_slide(session, "First")
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


# ------------------------------------------------- undo, which the deck also has to survive


def test_an_undone_turn_does_not_come_back_after_a_restart(tmp_path) -> None:
    """The bug this whole section exists for. `undo` moved the deck and notified nothing, so
    the journal and the snapshot both still described the turn as live — and a user who
    undid a mistake, closed the tab and reopened found the mistake waiting for them."""
    session, _ = _deck_with_a_title(tmp_path)
    _add_slide(session, "Second")
    assert router.dispatch(session, "undo")["ok"]

    store, report = Workspace(tmp_path / "ws").restore()
    assert _titles(store) == ["First"], "the undone slide came back"
    assert report["turns"] == 1, "the live turn, not the one that was taken back off"
    assert report["undone"] == 1, "'resumed 2 turns' would describe the file, not the deck"


def test_undo_then_redo_lands_where_the_user_left_it(tmp_path) -> None:
    """Redo is durable for the same reason undo is: it moved the deck. A restart that
    honoured the undo and forgot the redo would be a different kind of the same bug."""
    session, _ = _deck_with_a_title(tmp_path)
    _add_slide(session, "Second")
    assert router.dispatch(session, "undo")["ok"]
    assert router.dispatch(session, "redo")["ok"]

    store, report = Workspace(tmp_path / "ws").restore()
    assert _titles(store) == ["First", "Second"]
    assert report["turns"] == 2
    assert "undone" not in report, "nothing is undone, so nothing is worth reporting"


def test_repeated_undo_survives_all_the_way_back(tmp_path) -> None:
    """Not just the last one. Two undos leave two turns on file that must not be replayed,
    and a fix that only tracked the most recent would restore the middle one."""
    session, _ = _deck_with_a_title(tmp_path)
    _add_slide(session, "Second")
    _add_slide(session, "Third")
    assert router.dispatch(session, "undo")["ok"]
    assert router.dispatch(session, "undo")["ok"]

    store, report = Workspace(tmp_path / "ws").restore()
    assert _titles(store) == ["First"]
    assert report["turns"] == 1
    assert report["undone"] == 2


def test_the_journal_replay_path_honours_an_undo(tmp_path, fixture_deck) -> None:
    """The case a re-snapshot alone silently gets wrong. With neither snapshot readable the
    journal is the only source of truth, and one carrying just the forward turns replays the
    undone edit straight back onto the source file."""
    session, _ = Session.resume(fixture_deck, root=tmp_path)
    workspace = session.workspace
    slide = next(s for s in session.deck.slides for sh in s.shapes
                 if sh.text and not sh.opaque)
    shape = next(sh for sh in slide.shapes if sh.text and not sh.opaque)
    original = shape.text
    router.dispatch(session, "set_text",
                    {"target": f"{slide.id}/{shape.id}", "text": "Undone"})
    assert router.dispatch(session, "undo")["ok"]

    workspace.snapshot.unlink()
    workspace.backup.unlink(missing_ok=True)

    store, report = Workspace(workspace.root).restore(fixture_deck)
    assert report["from"] == "journal"
    assert store.deck.slide(slide.id).shape(shape.id).text == original
    assert report["turns"] == 0, "nothing was replayed, and the note says so"
    assert "replayed 0 turns" in report["note"]


def test_the_redo_stack_survives_the_restart_too(tmp_path) -> None:
    """Redo history is not the price of making undo durable. The undone turn is still on
    file with its ops — only the record of it being live changed — so a resumed session can
    still put it back, and `restore` derives the stack from exactly that."""
    session, _ = _deck_with_a_title(tmp_path)
    _add_slide(session, "Second")
    assert router.dispatch(session, "undo")["ok"]

    store, _ = Workspace(tmp_path / "ws").restore()
    assert _titles(store) == ["First"]
    assert store.redo() is True
    assert _titles(store) == ["First", "Second"]


def test_repeated_undo_redoes_in_the_order_it_was_undone(tmp_path) -> None:
    """The restored stack is a stack, not a set: redo puts the *oldest* undone turn back
    first, or the deck is rebuilt out of order."""
    session, _ = _deck_with_a_title(tmp_path)
    _add_slide(session, "Second")
    _add_slide(session, "Third")
    assert router.dispatch(session, "undo")["ok"]
    assert router.dispatch(session, "undo")["ok"]

    store, _ = Workspace(tmp_path / "ws").restore()
    assert store.redo() is True
    assert _titles(store) == ["First", "Second"]
    assert store.redo() is True
    assert _titles(store) == ["First", "Second", "Third"]
    assert store.redo() is False


def test_a_turn_committed_after_an_undo_closes_the_redo_stack(tmp_path) -> None:
    """The case a *stored* undo stack would get wrong. Committing over an undone turn
    discards it for good (`transaction` clears the stack), so a restore must not offer it
    back — and deriving the stack from the journal's live frontier gets that for free."""
    session, _ = _deck_with_a_title(tmp_path)
    _add_slide(session, "Second")
    assert router.dispatch(session, "undo")["ok"]
    _add_slide(session, "Third")

    store, _ = Workspace(tmp_path / "ws").restore()
    assert _titles(store) == ["First", "Third"]
    assert store.redo() is False, "the undone turn was written over, not parked"
    assert store.undo() is True, "and undo still reaches the turn that replaced it"
    assert _titles(store) == ["First"]


def test_a_half_written_undo_record_is_dropped_like_any_other(tmp_path) -> None:
    """A crash mid-append, on the record saying a turn came off the deck. Losing it means
    the undo never happened — which is where the snapshot is too, because the journal is
    written first and the snapshot after."""
    session, workspace = _deck_with_a_title(tmp_path)
    _add_slide(session, "Second")
    with open(workspace.journal, "a", encoding="utf-8") as handle:
        handle.write('{"event": "un')

    turns = Workspace(tmp_path / "ws").turns()
    assert len(turns) == 2
    assert all(t.committed for t in turns), "an unfinished undo did not happen"


def test_an_undo_record_naming_an_unknown_turn_stops_replay(tmp_path) -> None:
    """Same rule as a bad turn line. A record that cannot be placed in the history means
    something else wrote here, and honouring what follows it would apply ops out of order."""
    session, workspace = _deck_with_a_title(tmp_path)
    _add_slide(session, "Second")
    first, second = workspace.journal.read_text().splitlines()
    workspace.journal.write_text(
        f'{first}\n{{"event": "undo", "turn": 77}}\n{second}\n', encoding="utf-8")

    turns = Workspace(tmp_path / "ws").turns()
    assert len(turns) == 1, "replay stopped at the record it could not place"
    assert turns[0].committed


def test_undoing_an_added_asset_removes_it_from_disk_too(tmp_path) -> None:
    """Pictures live outside the document model, so re-snapshotting the deck says nothing
    about them. An `assets/index.json` still naming an undone key resurrects the picture on
    the next restore, exactly as the journal would resurrect the turn."""
    from PIL import Image

    session = Session.blank("Assets")
    workspace = Workspace(tmp_path / "ws")
    workspace.attach(session.store)
    path = tmp_path / "shot.png"
    # Generated rather than committed, as in `test_assets`: a binary in the repository is a
    # thing to explain, and this test cares only that the bytes are a real image.
    Image.new("RGB", (32, 20), (0x15, 0x60, 0x82)).save(path)
    key = router.dispatch(session, "add_asset", {"path": str(path)})["after"]["asset_id"]
    assert router.dispatch(session, "undo")["ok"]

    store, _ = Workspace(tmp_path / "ws").restore()
    assert key not in store.assets, "the index still named a picture the user took out"


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
