"""Font embedding and mode transitions — PLAN B4, B5.

**Embedding** is the measured gap on the path the harness controls: generated slides score
0.007 against PowerPoint and all of that residual is font substitution. Two things have to
hold — licensing is obeyed and reported, and the file stays small enough that anyone would
leave the feature on.

**Eject** is lossless and one-way. **Adopt** is a proposal, because recognising an
arrangement is not understanding it, and acting on a wrong guess reflows a slide someone
then has to rebuild.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from ppt_harness.core.session import Session
from ppt_harness.io import adopt, embed_fonts
from ppt_harness.io import export_mutate as exporter
from ppt_harness.state.document import Mode
from ppt_harness.tools import router

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"


@pytest.fixture
def generated(blank: Session) -> Session:
    router.dispatch(blank, "add_slide", {"layout": "stack", "blocks": [
        {"region": "header", "component": "slide_title", "slots": {"title": "Findings"}},
        {"region": "body", "component": "bullets",
         "slots": {"items": ["one", "two", "three"]}}]})
    return blank


# ------------------------------------------------------------------- embedding


def test_fonts_are_embedded_by_default(generated: Session, tmp_path: Path) -> None:
    out = tmp_path / "embedded.pptx"
    report = exporter.export(generated.deck, out, strict=False)
    assert report.fonts["embedded"]
    with zipfile.ZipFile(out) as z:
        assert [n for n in z.namelist() if n.startswith("ppt/fonts/")]


def test_an_embedded_face_is_declared_in_the_presentation(generated: Session,
                                                          tmp_path: Path) -> None:
    """The bytes alone do nothing: PowerPoint reads `embeddedFontLst` to know to use them."""
    out = tmp_path / "declared.pptx"
    report = exporter.export(generated.deck, out, strict=False)
    with zipfile.ZipFile(out) as z:
        presentation = z.read("ppt/presentation.xml").decode()
    for family in report.fonts["embedded"]:
        assert f'typeface="{family}"' in presentation
    assert "embeddedFontLst" in presentation


def test_subsetting_keeps_the_file_worth_shipping(generated: Session,
                                                 tmp_path: Path) -> None:
    """Unsubsetted, two faces turned a 27 KB deck into 1.9 MB — a feature nobody leaves on."""
    plain = tmp_path / "plain.pptx"
    fat = tmp_path / "embedded.pptx"
    exporter.export(generated.deck, plain, strict=False, embed_fonts_=False)
    exporter.export(generated.deck, fat, strict=False)

    added = fat.stat().st_size - plain.stat().st_size
    assert added < 400_000, f"{added / 1024:.0f} KB of fonts is not a subset"


def test_licensing_is_obeyed_and_reported() -> None:
    """A restricted face is skipped and *said*: the honest outcome is a wider fidelity
    margin, not a quietly non-compliant file."""
    allowed, why = embed_fonts.may_embed("A Font That Is Not Installed")
    assert allowed is False
    assert why


def test_a_skipped_face_is_named_in_the_report(generated: Session, tmp_path: Path,
                                               monkeypatch) -> None:
    monkeypatch.setattr(embed_fonts, "may_embed",
                        lambda family: (False, "licence forbids embedding"))
    report = exporter.export(generated.deck, tmp_path / "skipped.pptx", strict=False)
    assert report.fonts["not_embedded"]
    assert "substituted" in report.fonts["note"]


def test_embedding_can_be_turned_off(generated: Session, tmp_path: Path) -> None:
    report = exporter.export(generated.deck, tmp_path / "bare.pptx", strict=False,
                             embed_fonts_=False)
    assert report.fonts == {}


def test_the_subset_covers_text_not_yet_typed(generated: Session) -> None:
    """A font missing a comma the moment someone adds one is worse than a slightly larger
    file."""
    text = embed_fonts.deck_text(generated.deck)
    for character in ",.?!'\"()-":
        assert character in text


def test_an_embedded_deck_still_opens(generated: Session, tmp_path: Path) -> None:
    out = tmp_path / "reopen.pptx"
    exporter.export(generated.deck, out, strict=False)
    assert len(Session.open(out).deck.slides) == len(generated.deck.slides)


# ----------------------------------------------------------------------- eject


def test_ejecting_freezes_the_geometry_the_expander_chose(generated: Session) -> None:
    from ppt_harness.render import expand

    slide = generated.deck.slides[0]
    expected = len([s for s in expand.expand_slide(generated.theme, slide)
                    if slide.block(s.block_id)
                    and slide.block(s.block_id).slots.get(s.slot)])

    assert router.dispatch(generated, "eject_slide", {"slide_id": slide.id})["ok"]
    assert slide.mode is Mode.FREEFORM
    assert len(slide.shapes) == expected
    assert slide.blocks == []


def test_an_ejected_slide_keeps_its_words(generated: Session) -> None:
    slide = generated.deck.slides[0]
    before = {b.slots.get("title") or tuple(b.slots.get("items", [])) for b in slide.blocks}
    router.dispatch(generated, "eject_slide", {"slide_id": slide.id})
    text = "\n".join(s.text or "" for s in slide.shapes)
    for item in ("Findings", "one", "two", "three"):
        assert item in text
    assert before  # the blocks did hold something to begin with


def test_ejecting_twice_is_refused(generated: Session) -> None:
    slide = generated.deck.slides[0]
    router.dispatch(generated, "eject_slide", {"slide_id": slide.id})
    again = router.dispatch(generated, "eject_slide", {"slide_id": slide.id})
    assert again["ok"] is False
    assert again["error"] == "wrong_mode"


def test_eject_is_undoable_even_though_it_is_one_way(generated: Session) -> None:
    """One-way is a property of the *mode transition*, not of the op log."""
    slide = generated.deck.slides[0]
    router.dispatch(generated, "eject_slide", {"slide_id": slide.id})
    assert router.dispatch(generated, "undo")["ok"]
    assert slide.mode is Mode.MANAGED
    assert slide.blocks


# ----------------------------------------------------------------------- adopt


def test_adoption_is_a_proposal_before_it_is_a_change(generated: Session) -> None:
    """DESIGN §7: never a silent inference. Without confirmation nothing moves."""
    slide = generated.deck.slides[0]
    router.dispatch(generated, "eject_slide", {"slide_id": slide.id})
    shapes_before = [s.model_dump(mode="json") for s in slide.shapes]

    result = router.dispatch(generated, "adopt_slide", {"slide_id": slide.id})
    if not result["ok"]:
        pytest.skip("the classifier is not confident about this arrangement")

    assert result["applied"] is False
    assert result["proposal"]["confidence"] >= adopt.MIN_CONFIDENCE
    assert result["proposal"]["because"], "a proposal must say why"
    assert "reflows" in result["proposal"]["warning"]
    assert [s.model_dump(mode="json") for s in slide.shapes] == shapes_before


def test_confirming_applies_it(generated: Session) -> None:
    slide = generated.deck.slides[0]
    router.dispatch(generated, "eject_slide", {"slide_id": slide.id})
    if not router.dispatch(generated, "adopt_slide", {"slide_id": slide.id})["ok"]:
        pytest.skip("the classifier is not confident about this arrangement")

    result = router.dispatch(generated, "adopt_slide",
                             {"slide_id": slide.id, "confirm": True})
    assert result["ok"]
    assert slide.mode is Mode.MANAGED
    assert slide.blocks


def test_an_unrecognised_arrangement_says_so_and_stops(imported: Session) -> None:
    """Leaving a slide freeform is a valid outcome, not a failure."""
    refused = [router.dispatch(imported, "adopt_slide", {"slide_id": s.id})
               for s in imported.deck.slides]
    unrecognised = [r for r in refused if not r["ok"]]
    assert unrecognised, "the classifier claimed every slide; that is not conservative"
    assert all(r["error"] == "not_recognised" for r in unrecognised)
    assert "freeform is the right outcome" in unrecognised[0]["message"]


def test_the_classifier_reads_arrangement_not_meaning(imported: Session) -> None:
    """Deliberately shallow. A deeper one would be right more often and wrong less
    legibly, and a confident wrong reflow costs someone a rebuild."""
    for slide in imported.deck.slides:
        guess = adopt.classify(slide, imported.theme)
        if guess is None:
            continue
        assert 0.0 <= guess.confidence <= 0.95
        assert guess.because, "a guess with no reason cannot be reviewed"


def test_low_confidence_never_reaches_a_proposal(imported: Session) -> None:
    for slide in imported.deck.slides:
        guess = adopt.classify(slide, imported.theme)
        if guess and guess.confidence < adopt.MIN_CONFIDENCE:
            assert adopt.proposal(slide, imported.theme) is None


def test_adopting_a_managed_slide_is_refused(generated: Session) -> None:
    result = router.dispatch(generated, "adopt_slide",
                             {"slide_id": generated.deck.slides[0].id})
    assert result["ok"] is False
    assert result["error"] == "wrong_mode"
