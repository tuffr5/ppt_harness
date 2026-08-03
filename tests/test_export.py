"""Mutating export and the writer assertions — DESIGN §6.2.

Two separate claims are under test.

**Preservation**: patching the original package leaves everything unmodeled untouched —
media, sensitivity labels, change-tracking parts — and confines the blast radius of an edit
to the slide that changed. `test_roundtrip.py` proves that for python-pptx generally; this
proves it for the harness's own exporter.

**Fidelity**: every text box the harness writes carries `noAutofit`, `spcPts`, and explicit
insets. These are asserted against the emitted XML, not the object model, because the XML is
what the recipient opens.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from lxml import etree
from pptx import Presentation

from ppt_harness.core.session import Session
from ppt_harness.io import writer_assertions as fidelity
from ppt_harness.io.export_mutate import ExportError, export
from ppt_harness.state.document import Author, Mode, Slide
from ppt_harness.tools import router

A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def _c14n(raw: bytes) -> bytes:
    return etree.tostring(etree.fromstring(raw), method="c14n2")


def _add_managed(session: Session) -> str:
    result = router.dispatch(session, "add_slide", {
        "layout": "stack",
        "blocks": [
            {"region": "header", "component": "slide_title", "slots": {"title": "Findings"}},
            {"region": "body", "component": "bullets",
             "slots": {"items": ["First", "Second", "Third"]}},
        ],
    })
    assert result["ok"], result
    return result["target"]


# ------------------------------------------------------------------ preservation


@pytest.fixture
def exported(imported: Session, tmp_path: Path) -> Path:
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    with imported.transaction(Author.MODEL) as turn:
        imported.store.write(turn, "set_text", f"{slide.id}/{shape.id}",
                             {"text": "Edited"}, Author.MODEL)
    _add_managed(imported)
    out = tmp_path / "exported.pptx"
    export(imported.deck, out)
    return out


def test_binary_parts_are_untouched(exported: Path, fixture_deck: Path) -> None:
    """Media is the expensive thing an export must never rewrite."""
    before, after = _parts(fixture_deck), _parts(exported)
    for name, blob in before.items():
        if name.startswith("ppt/media") or name.endswith((".jpeg", ".png", ".mp4")):
            assert after[name] == blob, f"{name} was rewritten"


def test_unmodelled_parts_survive(exported: Path, fixture_deck: Path) -> None:
    """Change-tracking, revision info, and any sensitivity label the organisation requires."""
    before, after = _parts(fixture_deck), _parts(exported)
    for name in before:
        if name.startswith(("ppt/changesInfos", "docMetadata")) or name in (
            "ppt/revisionInfo.xml", "docProps/custom.xml"
        ):
            assert after.get(name) == before[name], f"{name} lost or rewritten"


def test_export_only_adds_parts_it_needs(exported: Path, fixture_deck: Path) -> None:
    before, after = set(_parts(fixture_deck)), set(_parts(exported))
    assert not before - after, f"dropped {sorted(before - after)}"
    added = after - before
    assert all("slide" in name for name in added), f"unexpected additions: {sorted(added)}"


def test_untouched_slides_are_not_rewritten(imported: Session, tmp_path: Path,
                                            fixture_deck: Path) -> None:
    """Editing slide 1 must not disturb slide 3.

    Canonical, not byte, equality — python-pptx reserializes every XML declaration
    (`"` to `'`, CRLF to LF). That is the contract DESIGN §6.2 states, and asserting bytes
    here would fail on serialization noise while proving nothing extra about content.
    """
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    with imported.transaction(Author.MODEL) as turn:
        imported.store.write(turn, "set_text", f"{slide.id}/{shape.id}",
                             {"text": "Only this one"}, Author.MODEL)
    out = tmp_path / "one.pptx"
    export(imported.deck, out)

    before, after = _parts(fixture_deck), _parts(out)
    others = [n for n in before if n.startswith("ppt/slides/slide") and "slide1." not in n]
    assert others, "fixture has only one slide; this proves nothing"
    for name in others:
        assert _c14n(after[name]) == _c14n(before[name]), \
            f"{name} changed but nothing on it did"


def test_the_edited_slide_is_the_only_one_that_differs(imported: Session, tmp_path: Path,
                                                       fixture_deck: Path) -> None:
    """The converse: the edit must actually reach the XML, or the test above is vacuous."""
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    with imported.transaction(Author.MODEL) as turn:
        imported.store.write(turn, "set_text", f"{slide.id}/{shape.id}",
                             {"text": "UNIQUE-MARKER-9f2a"}, Author.MODEL)
    out = tmp_path / "marked.pptx"
    export(imported.deck, out)

    after = _parts(out)
    carrying = [n for n, blob in after.items()
                if n.startswith("ppt/slides/slide") and b"UNIQUE-MARKER-9f2a" in blob]
    assert carrying == ["ppt/slides/slide1.xml"]


def test_the_edit_actually_lands(exported: Path) -> None:
    text = " ".join(
        shape.text_frame.text
        for shape in Presentation(str(exported)).slides[0].shapes
        if shape.has_text_frame
    )
    assert "Edited" in text


def test_exporting_over_the_original_is_refused(imported: Session,
                                                fixture_deck: Path) -> None:
    with pytest.raises(ExportError, match="refusing"):
        export(imported.deck, fixture_deck)


def test_an_imported_slide_that_lost_its_original_fails_loudly(imported: Session,
                                                               tmp_path: Path) -> None:
    """A freeform slide on an *imported* deck is an overlay on real OOXML.

    Narrowed from "a freeform slide always needs an original", which was not true and had
    not been true since `eject_slide` shipped: ejection produces a freeform slide on a deck
    that never had a package, and the old rule made such a deck unexportable for ever.

    The invariant worth keeping is this one. If the deck came from a file, a slide with no
    part in that file is corruption, and rebuilding it from the model would quietly drop
    every shape the harness does not understand — which is the one thing patching the
    original package exists to prevent.
    """
    imported.deck.slides.append(Slide(id="orphan", index=99, mode=Mode.FREEFORM, shapes=[]))
    with pytest.raises(ExportError, match="no original"):
        export(imported.deck, tmp_path / "x.pptx")


# --------------------------------------------------------------------- fidelity


def test_a_written_slide_satisfies_every_assertion(exported: Path) -> None:
    prs = Presentation(str(exported))
    written = [s for s in prs.slides[-1].shapes if s.has_text_frame]
    assert written, "the managed slide produced no text boxes"
    for shape in written:
        assert fidelity.check_frame(shape.text_frame._bodyPr,
                                    shape.text_frame.paragraphs) == []


def _added_slide_xml(exported: Path, source: Path) -> str:
    """The slide part the harness appended.

    Derived, never hardcoded: the index depends on how many slides the fixture already had,
    and asserting against `slide6.xml` silently inspects an imported slide the moment the
    fixture changes size.
    """
    before, after = set(_parts(source)), _parts(exported)
    added = sorted(n for n in set(after) - before
                   if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
    assert len(added) == 1, f"expected exactly one new slide part, got {added}"
    return after[added[0]].decode()


def test_no_percent_line_spacing_is_emitted(exported: Path, fixture_deck: Path) -> None:
    """`spcPct` resolves against font ascent/descent, so it cannot match CSS line-height."""
    slide = _added_slide_xml(exported, fixture_deck)
    assert "spcPct" not in slide
    assert "spcPts" in slide


def test_autofit_is_off_and_stated_positively(exported: Path, fixture_deck: Path) -> None:
    slide = _added_slide_xml(exported, fixture_deck)
    assert "noAutofit" in slide
    assert "normAutofit" not in slide


def test_insets_are_present_not_merely_zero(exported: Path, fixture_deck: Path) -> None:
    """Omitting them does not mean zero — PowerPoint defaults eat 14.4pt of width."""
    root = etree.fromstring(_added_slide_xml(exported, fixture_deck).encode())
    checked = sum(1 for body in root.iter(f"{{{A}}}bodyPr")
                  if all(body.get(k) is not None for k in ("lIns", "tIns", "rIns", "bIns")))
    assert checked > 0


def test_the_post_export_check_catches_a_planted_violation(exported: Path,
                                                           tmp_path: Path) -> None:
    """Prove the check can fail. A gate that never fires is not a gate."""
    prs = Presentation(str(exported))
    shape = next(s for s in prs.slides[-1].shapes if s.has_text_frame)
    body = shape.text_frame._bodyPr
    for key in ("lIns", "tIns", "rIns", "bIns"):
        if body.get(key) is not None:
            del body.attrib[key]
    broken = tmp_path / "broken.pptx"
    prs.save(str(broken))

    violations = fidelity.assert_fidelity(broken)
    assert any(v.rule == "implicit_insets" for v in violations)
    with pytest.raises(fidelity.FidelityError):
        fidelity.raise_for_violations(violations)


def test_the_check_is_scoped_to_shapes_we_wrote(exported: Path) -> None:
    """An imported deck's untouched boxes are the original author's and are not ours to
    hold to this contract; failing on them would make the check unusable."""
    everything = fidelity.assert_fidelity(exported)
    ours = fidelity.assert_fidelity(exported, only_shapes=set())
    assert ours == []
    assert len(everything) > 0, "the fixture's own slides should not satisfy our contract"


def test_a_no_op_export_is_clean(imported: Session, tmp_path: Path) -> None:
    """Nothing written means nothing to check. An empty written-set must not be read as
    "audit everything", or every untouched import fails on the original author's shapes.
    """
    report = export(imported.deck, tmp_path / "noop.pptx")
    assert report.shapes_patched == 0 and report.shapes_added == 0
    assert report.violations == []
    assert report.ok


def test_shape_ids_are_scoped_per_slide(exported: Path) -> None:
    """Shape ids repeat across slides, so a flat id set would pull in untouched shapes."""
    prs = Presentation(str(exported))
    seen: dict[int, int] = {}
    collisions = 0
    for index, slide in enumerate(prs.slides):
        for shape in slide.shapes:
            sid = int(shape.shape_id)
            if sid in seen and seen[sid] != index:
                collisions += 1
            seen[sid] = index
    assert collisions > 0, "fixture no longer exercises cross-slide id reuse"


# ------------------------------------------------------------------- from blank


def test_a_deck_with_no_original_exports_at_theme_size(blank: Session,
                                                       tmp_path: Path) -> None:
    _add_managed(blank)
    out = tmp_path / "new.pptx"
    report = export(blank.deck, out)
    assert report.ok

    prs = Presentation(str(out))
    expected_w, expected_h = blank.slide_size_emu()
    assert int(prs.slide_width) == expected_w
    assert int(prs.slide_height) == expected_h
    assert len(prs.slides.__iter__.__self__._sldIdLst) == 1


def test_managed_geometry_lands_inside_the_slide(blank: Session, tmp_path: Path) -> None:
    _add_managed(blank)
    out = tmp_path / "geom.pptx"
    export(blank.deck, out)
    prs = Presentation(str(out))
    for shape in prs.slides[0].shapes:
        assert shape.left >= 0 and shape.top >= 0
        assert shape.left + shape.width <= int(prs.slide_width)
        assert shape.top + shape.height <= int(prs.slide_height)


# ------------------------------------------------- managed objects and multi-column slots


def _managed_chart_slide(session: Session) -> dict:
    return router.dispatch(session, "add_slide", {
        "layout": "stack",
        "blocks": [
            {"region": "header", "component": "slide_title",
             "slots": {"title": "Churn doubled in EMEA"}},
            {"region": "body", "component": "chart", "variant": "wide", "slots": {"chart": {
                "kind": "bar", "categories": ["Q1", "Q2", "Q3"],
                "series": [{"name": "EMEA", "values": [4.1, 6.0, 8.3]}]}}},
        ],
    })


def test_a_managed_chart_reaches_the_file(blank: Session, tmp_path: Path) -> None:
    """The writer read `labels` where every other layer says `categories`.

    A chart slot filled exactly as the schema documents produced an empty `ChartSpec`, was
    dropped, and `add_slide` still answered ok — the slide measured clean and exported a
    title with nothing under it. Nothing disagreed, because the one component that knew was
    silent.
    """
    assert _managed_chart_slide(blank)["ok"]
    out = tmp_path / "chart.pptx"
    report = export(blank.deck, out)

    graphics = [s for s in Presentation(str(out)).slides[0].shapes if s.has_chart]
    assert graphics, "the chart slot exported no chart"
    assert [c for c in graphics[0].chart.plots[0].categories] == ["Q1", "Q2", "Q3"]
    assert not report.violations


def test_a_slot_that_writes_nothing_is_reported(blank: Session, tmp_path: Path) -> None:
    """Silence is the failure mode that let the chart bug live.

    A filled slot the writer cannot build is content the user will not find in the file, so
    it has to reach the export report rather than being skipped.
    """
    assert _managed_chart_slide(blank)["ok"]
    # Past the tool gate, straight into the model: a payload the writer cannot use.
    slide = blank.deck.slides[0]
    block = next(b for b in slide.blocks if b.component == "chart")
    block.slots["chart"] = {"kind": "bar", "categories": ["Q1"], "series": []}

    # `strict=False`, as the `export` tool calls it: the point is that the report
    # *says so*, not that the writer refuses to finish.
    report = export(blank.deck, tmp_path / "dropped.pptx", strict=False)
    assert [v for v in report.violations if v.rule == "slot_not_written"], \
        "a dropped slot exported silently"


def test_a_row_of_stats_is_written_across_not_down(blank: Session, tmp_path: Path) -> None:
    """`per_row` was declared by twelve variants and read by nothing.

    Every list component rendered as one vertical column, so `stat_row` and `bullets` were
    the same slide. The check is on the *file*, because the preview is the file rendered.
    """
    assert router.dispatch(blank, "add_slide", {
        "layout": "stack",
        "blocks": [{"region": "body", "component": "stat_row", "variant": "flat", "slots": {
            "items": [{"value": "8.3%", "label": "EMEA churn"},
                      {"value": "+41%", "label": "Expansion"},
                      {"value": "108%", "label": "Retention"}]}}],
    })["ok"]

    out = tmp_path / "stats.pptx"
    export(blank.deck, out)
    boxes = [s for s in Presentation(str(out)).slides[0].shapes if s.has_text_frame]
    assert len(boxes) == 3, "a row of three stats is three boxes, not one"
    assert len({b.left for b in boxes}) == 3, "the cells share an x; they were stacked"
    assert len({b.top for b in boxes}) == 1, "the cells are a row, so they share a y"

    figures = [b.text_frame.paragraphs[0].runs[0].text for b in boxes]
    assert "8.3%" in figures, "the figure was dropped; only the label survived"


def test_a_slot_exports_in_the_face_its_role_asks_for(blank: Session, tmp_path: Path) -> None:
    """The budget measures the display stack, so the file has to render in it.

    Runs carried no face at all, and a bare text box inherits PowerPoint's *minor* font — so
    every `display` role exported in the body face while the preview drew it in the display
    one. On a serif-titled theme the harness was measuring Georgia and shipping Inter, which
    makes "measured against the font that will actually render it" false in the one place it
    is load-bearing.
    """
    from ppt_harness.state import templates

    session = Session.blank("Serif", theme=templates.load("editorial"))
    assert router.dispatch(session, "add_slide", {
        "layout": "stack",
        "blocks": [
            {"region": "header", "component": "slide_title", "slots": {"title": "A title"}},
            {"region": "body", "component": "bullets", "slots": {"items": ["One", "Two"]}},
        ],
    })["ok"]

    out = tmp_path / "faces.pptx"
    export(session.deck, out)
    faces = {shape.name.split(":")[-1]:
             shape.text_frame.paragraphs[0].runs[0].font.name
             for shape in Presentation(str(out)).slides[0].shapes if shape.has_text_frame}

    families = session.deck.theme.type.families
    assert faces["title"] == families["display"].split(",")[0].strip("'\"")
    assert faces["items"] == families["body"].split(",")[0].strip("'\"")
    assert faces["title"] != faces["items"], "the theme distinguishes them; the file must too"


def test_an_ejected_slide_can_still_be_exported(blank: Session, tmp_path: Path) -> None:
    """`eject_slide` used to make a generated deck permanently unexportable.

    Ejection is shipped, documented, and advertised as the way back from managed. On a deck
    the harness generated there is no original package to patch, and the writer raised —
    `ExportError`, past the router, as an exception rather than a refusal. The user's work
    became unrecoverable through the only door that leads out of the harness.

    The geometry ejection produces is absolute, so there was never anything missing; the
    writer simply had no branch for it.
    """
    assert router.dispatch(blank, "add_slide", {
        "layout": "stack",
        "blocks": [
            {"region": "header", "component": "slide_title", "slots": {"title": "A title"}},
            {"region": "body", "component": "bullets", "slots": {"items": ["One", "Two"]}},
        ],
    })["ok"]
    slide_id = blank.deck.slides[0].id
    assert router.dispatch(blank, "eject_slide", {"slide_id": slide_id})["ok"]

    out = tmp_path / "ejected.pptx"
    report = export(blank.deck, out)

    assert report.ok, [str(v) for v in report.violations]
    texts = {s.text_frame.text for s in Presentation(str(out)).slides[0].shapes
             if s.has_text_frame}
    assert "A title" in texts, "the ejected title never reached the file"
    assert any("One" in t for t in texts)


def test_a_duplicated_ejected_slide_still_exports(blank: Session, tmp_path: Path) -> None:
    """`duplicate_slide` + `eject_slide` are both shipped; together they broke export.

    The clone path exists for *imported* slides, where the OOXML holds what the harness
    cannot rebuild. On a generated deck there is no package to clone from, and the writer
    raised — the same hole as the ejected-slide branch, one level in. Found by a benchmark
    run, which it killed at task N of 32.
    """
    assert router.dispatch(blank, "add_slide", {
        "layout": "stack",
        "blocks": [{"region": "header", "component": "slide_title",
                    "slots": {"title": "A title"}}],
    })["ok"]
    slide_id = blank.deck.slides[0].id
    assert router.dispatch(blank, "eject_slide", {"slide_id": slide_id})["ok"]
    assert router.dispatch(blank, "duplicate_slide", {"slide_id": slide_id})["ok"]

    out = tmp_path / "dup.pptx"
    report = export(blank.deck, out)
    assert report.ok, [str(v) for v in report.violations]
    assert len(Presentation(str(out)).slides) == 2


def test_an_export_the_writer_refuses_is_a_result_not_an_exception(imported: Session,
                                                                   tmp_path: Path) -> None:
    """`ExportError` used to escape the router.

    Every other refusal comes back as `{ok: False, error: ...}`; this one propagated as an
    exception, so a caller had to know that one tool throws differently. Under MCP it would
    surface as a transport error rather than a tool result, and it took down a benchmark run.
    """
    imported.deck.slides.append(Slide(id="orphan", index=99, mode=Mode.FREEFORM, shapes=[]))
    result = router.dispatch(imported, "export", {"path": str(tmp_path / "x.pptx")})

    assert result["ok"] is False
    assert result["error"] == "export_failed"
    assert "no original" in result["message"]
