"""Theme extraction and import — DESIGN §7.

Theme extraction is the highest-value part of import: it makes "add three slides to this
deck" produce slides that match, which needs no adoption at all. The tests that matter are
the honesty ones — that what was *read* is separated from what was *guessed*, and that a
theme which passes validation really does make contrast failure impossible downstream.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from lxml import etree

from ppt_harness.core.session import Session
from ppt_harness.io.theme_extract import extract_theme
from ppt_harness.render import fonts
from ppt_harness.state.document import Mode
from ppt_harness.state.theme_default import PAIRS, contrast, validate_theme

A = "http://schemas.openxmlformats.org/drawingml/2006/main"


# ------------------------------------------------------------------------- theme


def test_extracted_theme_passes_validation(fixture_deck: Path) -> None:
    """The whole point of validating once at load: a passing theme means managed slides
    cannot fail contrast, so per-slide lint never has to check."""
    assert validate_theme(extract_theme(fixture_deck)) == []


def test_every_role_pair_clears_the_contrast_floor(fixture_deck: Path) -> None:
    palette = extract_theme(fixture_deck).palette
    for fg, bg in PAIRS:
        assert contrast(palette[fg], palette[bg]) >= 4.5, f"{fg} on {bg}"


def test_palette_roles_are_read_from_the_colour_scheme(fixture_deck: Path) -> None:
    """Roles must come from the file, not from our defaults."""
    with zipfile.ZipFile(fixture_deck) as z:
        scheme = etree.fromstring(z.read("ppt/theme/theme1.xml")).find(f".//{{{A}}}clrScheme")

    def slot(name: str) -> str | None:
        node = scheme.find(f"{{{A}}}{name}")
        srgb = node.find(f"{{{A}}}srgbClr") if node is not None else None
        sys_clr = node.find(f"{{{A}}}sysClr") if node is not None else None
        if srgb is not None:
            return "#" + srgb.get("val").upper()
        if sys_clr is not None and sys_clr.get("lastClr"):
            return "#" + sys_clr.get("lastClr").upper()
        return None

    theme = extract_theme(fixture_deck)
    assert theme.palette["bg"] == slot("lt1")
    assert theme.palette["ink"] == slot("dk1")
    assert theme.palette["accents"][0] == slot("accent1")


def test_accents_keep_their_order(fixture_deck: Path) -> None:
    """Item N of a list takes accents[N % len], so ordering is what makes colour
    consistency structural rather than remembered."""
    theme = extract_theme(fixture_deck)
    assert len(theme.palette["accents"]) >= 3
    assert theme.accent(0) == theme.palette["accents"][0]
    assert theme.accent(len(theme.palette["accents"])) == theme.palette["accents"][0]


def test_guesses_are_declared_as_guesses(fixture_deck: Path) -> None:
    """`theme1.xml` contains no type scale and no spacing ramp. Presenting derived values
    as read values is the failure this guards."""
    inferred = extract_theme(fixture_deck).inferred
    assert "type.scale" in inferred
    assert "spacing" in inferred
    assert "palette.ink_muted" in inferred


def test_read_values_are_not_marked_inferred(fixture_deck: Path) -> None:
    theme = extract_theme(fixture_deck)
    assert "palette.bg" not in theme.inferred
    assert "palette.ink" not in theme.inferred


def test_canvas_matches_the_declared_slide_size(fixture_deck: Path) -> None:
    with zipfile.ZipFile(fixture_deck) as z:
        pres = etree.fromstring(z.read("ppt/presentation.xml"))
    size = pres.find("{http://schemas.openxmlformats.org/presentationml/2006/main}sldSz")
    ratio = int(size.get("cx")) / int(size.get("cy"))
    theme = extract_theme(fixture_deck)
    assert theme.grid.canvas[0] / theme.grid.canvas[1] == pytest.approx(ratio, rel=0.01)


def test_margin_comes_from_placeholders_not_from_decoration(fixture_deck: Path) -> None:
    """Every `<a:off>` in the master is the wrong set; sub-shapes inside groups sit near
    x=0 and would report a margin of a few pixels."""
    theme = extract_theme(fixture_deck)
    assert theme.grid.margin > 20, "a margin this small means decoration leaked in"
    assert theme.grid.margin < theme.grid.canvas[0] / 4


def test_font_families_resolve_to_installed_faces(fixture_deck: Path) -> None:
    theme = extract_theme(fixture_deck)
    for stack in theme.type.families.values():
        assert fonts.resolve(stack, "latin").exists()


def test_the_stack_carries_cjk_fallbacks(fixture_deck: Path) -> None:
    """Measuring Han text with a Latin face misprices it by roughly 2x, so script-specific
    faces named in the theme must survive into the stack."""
    theme = extract_theme(fixture_deck)
    assert fonts.resolve(theme.type.families["body"], "han") != \
        fonts.resolve("NoSuchFamily", "latin")


def test_a_deck_without_a_theme_part_is_refused(tmp_path: Path) -> None:
    from ppt_harness.io.theme_extract import ThemeExtractionError

    empty = tmp_path / "empty.pptx"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
    with pytest.raises(ThemeExtractionError):
        extract_theme(empty)


# ------------------------------------------------------------------------ import


def test_every_imported_slide_lands_freeform(imported: Session) -> None:
    """Nothing is adopted at import. Adoption reflows a slide and is always a proposal."""
    assert imported.deck.slides
    assert all(s.mode is Mode.FREEFORM for s in imported.deck.slides)


def test_the_original_package_is_remembered(imported: Session, fixture_deck: Path) -> None:
    assert imported.deck.source_path == str(fixture_deck.resolve())


def test_unmodelled_shapes_are_marked_opaque_not_dropped(imported: Session) -> None:
    """An opaque shape is still addressable and still exports; it just cannot be edited."""
    shapes = [s for slide in imported.deck.slides for s in slide.shapes]
    assert shapes, "import produced no shapes at all"
    for shape in shapes:
        assert shape.frame is not None
        if shape.opaque:
            assert shape.text is None


def test_placeholders_carry_a_type_role(imported: Session) -> None:
    """`restyle` and freeform budgets have no component to consult, so the role is what
    supplies the type spec."""
    roles = {s.role for slide in imported.deck.slides for s in slide.shapes if s.role}
    assert roles & {"slide_title", "deck_title", "body"}


def test_shape_ids_are_unique_within_a_slide(imported: Session) -> None:
    for slide in imported.deck.slides:
        ids = [s.id for s in slide.shapes]
        assert len(ids) == len(set(ids))


def test_nothing_is_dirty_immediately_after_import(imported: Session) -> None:
    """`dirty` is what makes mutating export possible; a fresh import must patch nothing."""
    assert not any(s.dirty for slide in imported.deck.slides for s in slide.shapes)
