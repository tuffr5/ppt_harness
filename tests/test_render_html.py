"""HTML rendering and freeze-geometry — DESIGN §6.1.

The claim under test is that the preview and the export describe the same slide. Two things
have to hold for that: the HTML must be laid out in the same coordinate system the exporter
writes, and the browser's numbers must be the ones that win when the analytic measurer
disagrees.

Browser tests skip when Playwright is absent. That is the intended posture — freeze-geometry
is optional, and the harness must be honest about running without it rather than reporting
analytic numbers as if a browser had produced them.
"""

from __future__ import annotations

import re

import pytest

from ppt_harness.core.session import Session
from ppt_harness.render import browser, html
from ppt_harness.state.document import Block, Mode, Slide
from ppt_harness.state.theme_default import default_theme

THEME = default_theme()
CX, CY = 12192000, 6858000

needs_browser = pytest.mark.skipif(not browser.available(), reason="playwright not installed")


# ------------------------------------------------------------------------- markup


def test_a_slide_renders_to_a_complete_document(managed_slide: Slide) -> None:
    out = html.render_slide(THEME, managed_slide, CX, CY)
    assert out.html.startswith("<!doctype html>")
    assert out.canvas == THEME.grid.canvas
    assert out.targets, "nothing was marked for measurement"


def test_a_fragment_carries_its_styles_but_no_document(managed_slide: Slide) -> None:
    out = html.render_slide(THEME, managed_slide, CX, CY, fragment=True)
    assert "<!doctype" not in out.html
    assert "<style>" in out.html and 'class="slide"' in out.html


def test_every_measurable_slot_is_probeable(managed_slide: Slide) -> None:
    out = html.render_slide(THEME, managed_slide, CX, CY)
    found = re.findall(rf'{html.PROBE_ATTR}="([^"]+)"', out.html)
    assert sorted(found) == sorted(out.targets)


def test_text_is_escaped(managed_slide: Slide) -> None:
    managed_slide.blocks[0].slots["title"] = '<script>alert("x")</script>'
    out = html.render_slide(THEME, managed_slide, CX, CY)
    assert "<script>" not in out.html
    assert "&lt;script&gt;" in out.html


def test_slot_geometry_matches_the_expander(managed_slide: Slide) -> None:
    """The preview must be laid out where the exporter will put it, or it is a picture of a
    different slide."""
    from ppt_harness.render.expand import expand_slide

    out = html.render_slide(THEME, managed_slide, CX, CY)
    for laid_out in expand_slide(THEME, managed_slide):
        target = f"{managed_slide.id}/{laid_out.block_id}/{laid_out.slot}"
        if target not in out.targets:
            continue
        assert f"left:{laid_out.box.x:.2f}px" in out.html
        assert f"width:{laid_out.box.w:.2f}px" in out.html


def test_a_decorated_variant_draws_its_panel_where_the_expander_puts_it() -> None:
    """The card is part of the file now, so it is part of the picture of the file.

    A preview that drew `carded` as `flat` would put the divergence back where it started,
    one layer down: the writer and the browser describing different slides. The pad is
    spent as padding on the cell rather than on a smaller box, which `box-sizing: border-box`
    makes the same content rectangle the writer measures against.
    """
    from ppt_harness.render.expand import expand_slide

    slide = Slide(id="s_card", index=0, mode=Mode.MANAGED, layout="stack", blocks=[
        Block(id="b", region="body", component="stat_row", variant="carded",
              slots={"items": [{"value": "8.3%", "label": "Churn"},
                               {"value": "+41%", "label": "Expansion"}]})])
    out = html.render_slide(THEME, slide, CX, CY)
    laid_out = next(s for s in expand_slide(THEME, slide) if s.slot == "items")

    assert out.html.count('class="panel"') == 2, "a card behind each figure, or none at all"
    for panel in laid_out.panels(2):
        assert f"left:{panel.x:.2f}px" in out.html
    assert f"padding:{laid_out.pad:.1f}px" in out.html


def test_line_height_is_absolute_never_a_ratio(managed_slide: Slide) -> None:
    """Same reason the writer emits `spcPts` and never `spcPct`."""
    out = html.render_slide(THEME, managed_slide, CX, CY)
    for match in re.findall(r"line-height:([^;\"]+)", out.html):
        assert match.strip().endswith("px"), match


def test_insets_are_zero_because_the_export_zeroes_them(managed_slide: Slide) -> None:
    out = html.render_slide(THEME, managed_slide, CX, CY)
    assert "padding: 0" in out.html


def test_the_measured_element_can_outgrow_its_box(managed_slide: Slide) -> None:
    """`scrollHeight` on a fixed-height box is clamped to that box and would report every
    slot as exactly full — hiding underflow and overflow alike."""
    out = html.render_slide(THEME, managed_slide, CX, CY)
    assert 'class="ink"' in out.html
    assert "height: auto" in out.html


def test_opaque_shapes_are_drawn_not_dropped(imported: Session) -> None:
    """A preview that omitted the SmartArt would suggest the slide is emptier than it is."""
    slide = next((s for s in imported.deck.slides if any(x.opaque for x in s.shapes)), None)
    if slide is None:
        pytest.skip("fixture has no opaque shapes")
    cx, cy = imported.slide_size_emu()
    out = html.render_slide(imported.theme, slide, cx, cy)
    assert 'class="opaque"' in out.html


def test_imported_text_uses_the_files_own_type(imported: Session) -> None:
    """An imported slide is previewed, not restyled. Substituting the theme's scale would
    render a slide the file does not contain — and budget it wrongly too."""
    slide, shape = next(
        ((s, x) for s in imported.deck.slides for x in s.shapes if x.type_spec and x.text),
        (None, None),
    )
    if shape is None:
        pytest.skip("fixture states no explicit font sizes")
    cx, cy = imported.slide_size_emu()
    out = html.render_slide(imported.theme, slide, cx, cy)
    assert f"font-size:{shape.type_spec.size:.2f}px" in out.html


def test_render_deck_covers_every_slide(imported: Session) -> None:
    cx, cy = imported.slide_size_emu()
    page = html.render_deck(imported.theme, imported.deck.slides, cx, cy)
    for slide in imported.deck.slides:
        assert slide.id in page


# ------------------------------------------------------------------------ browser


@needs_browser
def test_freezing_reads_back_every_probe(imported: Session) -> None:
    cx, cy = imported.slide_size_emu()
    slide = imported.deck.slides[0]
    frozen = browser.freeze(imported.theme, slide, cx, cy)
    expected = html.render_slide(imported.theme, slide, cx, cy).targets
    assert sorted(b.target for b in frozen.boxes) == sorted(expected)


@needs_browser
def test_content_height_is_the_text_not_the_box(imported: Session) -> None:
    """The bug this guards: measuring the fixed-height slot reports content_h == h for
    every slot, so nothing ever underflows and nothing ever overflows."""
    cx, cy = imported.slide_size_emu()
    frozen = browser.freeze(imported.theme, imported.deck.slides[0], cx, cy)
    assert any(b.content_h < b.h - 1 for b in frozen.boxes), \
        "no slot measured shorter than its box — content_h is tracking the box"


@needs_browser
def test_the_two_measurers_agree(imported: Session) -> None:
    """Analytic line counts must match what a real engine does.

    A disagreement is the drift that later shows up as "looked fine in preview, overflowed
    in PowerPoint", so it is worth failing on rather than merely reporting.
    """
    cx, cy = imported.slide_size_emu()
    notes = []
    for slide in imported.deck.slides:
        frozen = browser.freeze(imported.theme, slide, cx, cy)
        notes += browser.compare_with_analytic(
            frozen, imported.measure_slide(slide.id, freeze=False))
    assert len(notes) <= 1, notes


@needs_browser
def test_a_screenshot_is_a_png_of_the_slide(imported: Session) -> None:
    cx, cy = imported.slide_size_emu()
    png = browser.screenshot_slide(imported.theme, imported.deck.slides[0], cx, cy)
    assert png.startswith(b"\x89PNG")


@needs_browser
def test_measure_slide_reports_when_it_froze(imported: Session) -> None:
    result = imported.measure_slide(imported.deck.slides[0].id, freeze=True)
    assert result["frozen"] is True
    assert result["source"] == "browser"
    assert "analytic_overflow_px" in result


def test_measure_slide_says_so_when_it_could_not_freeze(imported: Session, monkeypatch) -> None:
    """Silently returning analytic numbers as frozen geometry would be the worst outcome:
    the caller believes a browser laid the slide out when none did."""
    def boom(*a, **kw):
        raise browser.BrowserUnavailable("no chromium here")

    monkeypatch.setattr(browser, "freeze", boom)
    result = imported.measure_slide(imported.deck.slides[0].id, freeze=True)
    assert result["frozen"] is False
    assert "no chromium here" in result["freeze_error"]


def test_source_autofit_explains_a_surprising_overflow(imported: Session) -> None:
    """A deck that "looks fine in PowerPoint" may only look fine because PowerPoint shrank
    the text. Say that, rather than reporting a bare overflow the user cannot reconcile."""
    noted = [s for slide in imported.deck.slides
             for s in imported.measure_slide(slide.id).get("shapes", [])
             if s.get("source_autofit")]
    if not noted:
        pytest.skip("fixture has no autofit-compensated overflow")
    assert all("normAutofit" in s["note"] for s in noted)


# ------------------------------------------------------------------------ assets


def test_pictures_are_extracted_at_import(imported: Session) -> None:
    """A preview of a deck whose images are grey boxes is not much of a preview."""
    if not any(s.asset for slide in imported.deck.slides for s in slide.shapes):
        pytest.skip("fixture has no pictures")
    assert imported.assets
    for content_type, blob in imported.assets.values():
        assert content_type.startswith("image/")
        assert blob


def test_assets_are_kept_out_of_the_document_model(imported: Session) -> None:
    """`Deck` is dumped whole for every invertible op; image bytes must not ride along."""
    dumped = imported.deck.model_dump(mode="json")
    assert "assets" not in dumped


def test_a_picture_renders_as_an_image_when_inlined(imported: Session) -> None:
    slide = next((s for s in imported.deck.slides
                  if any(x.asset and x.asset in imported.assets for x in s.shapes)), None)
    if slide is None:
        pytest.skip("fixture has no extractable pictures")
    markup = imported.render_html(slide.id, inline_assets=True)
    assert "<img src=\"data:image/" in markup


def test_by_url_markup_is_smaller_than_inlined(imported: Session) -> None:
    """The preview is re-fetched after every edit; re-sending base64 each time is waste."""
    slide = next((s for s in imported.deck.slides
                  if any(x.asset and x.asset in imported.assets for x in s.shapes)), None)
    if slide is None:
        pytest.skip("fixture has no extractable pictures")
    inline = imported.render_html(slide.id, inline_assets=True)
    by_url = imported.render_html(slide.id, inline_assets=False)
    assert "/api/asset/" in by_url
    assert len(by_url) < len(inline)


def test_a_missing_asset_falls_back_to_a_named_placeholder(imported: Session) -> None:
    """The shape is in the file whether or not the preview can draw it."""
    slide = next((s for s in imported.deck.slides
                  if any(x.text is None and not x.opaque for x in s.shapes)), None)
    if slide is None:
        pytest.skip("fixture has no asset shapes")
    cx, cy = imported.slide_size_emu()
    markup = html.render_slide(imported.theme, slide, cx, cy, asset_src=lambda k: None).html
    assert 'class="asset"' in markup
    assert "<img" not in markup


def test_oversized_pictures_are_not_held_in_memory() -> None:
    """A deck of photographs would otherwise make import unbounded."""
    from ppt_harness.io.import_pptx import MAX_ASSET_BYTES

    assert 0 < MAX_ASSET_BYTES <= 16 * 1024 * 1024


# ---------------------------------------------------------------------- geometry


def test_scheme_colours_resolve_through_their_transforms() -> None:
    """`<a:schemeClr val="accent6"><a:lumMod val="75000"/>` is not a colour until the theme
    and the transform stack have both been applied."""
    from lxml import etree

    from ppt_harness.io import colors

    scheme = {"accent6": "#4EA72E", "dk1": "#000000", "lt1": "#FFFFFF"}
    node = etree.fromstring(
        '<solidFill xmlns="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<schemeClr val="accent6"><lumMod val="75000"/></schemeClr></solidFill>')
    resolved, alpha = colors.resolve(node, scheme)
    assert resolved.startswith("#") and resolved != "#4EA72E", "lumMod was not applied"
    assert alpha == 1.0


def test_tx_and_bg_slots_alias_onto_the_scheme() -> None:
    from lxml import etree

    from ppt_harness.io import colors

    node = etree.fromstring(
        '<solidFill xmlns="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<schemeClr val="tx1"/></solidFill>')
    assert colors.resolve(node, {"dk1": "#123456"}) == ("#123456", 1.0)


def test_preset_geometry_becomes_a_path() -> None:
    from ppt_harness.render import svg

    markup = svg.shape_svg("rightArrow", 200, 60, ("#AEAEAE", 1.0), None)
    assert markup.startswith("<svg")
    assert 'viewBox="0 0 100 100"' in markup
    assert 'preserveAspectRatio="none"' in markup
    assert "<path" in markup


def test_an_unknown_preset_falls_back_to_a_rectangle() -> None:
    """A named fallback is honest; silently drawing nothing is not."""
    from ppt_harness.render import svg

    assert "<path" in svg.shape_svg("someShapeNobodyHas", 100, 100, ("#000000", 1.0), None)


def test_a_shape_with_neither_fill_nor_outline_draws_nothing() -> None:
    from ppt_harness.render import svg

    assert svg.shape_svg("rect", 100, 100, None, None) == ""


def test_geometry_is_captured_with_resolved_colours(imported: Session) -> None:
    drawn = [s for slide in imported.deck.slides for s in slide.shapes
             if s.geometry and s.geometry.visible]
    if not drawn:
        pytest.skip("fixture has no filled shapes")
    for shape in drawn:
        colour = shape.geometry.fill or shape.geometry.line
        assert colour.startswith("#") and len(colour) == 7, colour


def test_shapes_are_drawn_in_the_preview(imported: Session) -> None:
    """Text-only rendering shows a slide of floating words where the file has arrows."""
    slide = next((s for s in imported.deck.slides
                  if any(x.geometry and x.geometry.visible for x in s.shapes)), None)
    if slide is None:
        pytest.skip("fixture has no filled shapes")
    cx, cy = imported.slide_size_emu()
    markup = html.render_slide(imported.theme, slide, cx, cy).html
    assert 'class="geom"' in markup


def test_a_native_chart_is_drawn_but_never_replaces_its_data(imported: Session) -> None:
    """The preview is a rendering; the authoritative worksheet stays in the package."""
    charted = [s for slide in imported.deck.slides for s in slide.shapes if s.chart]
    if not charted:
        pytest.skip("fixture has no native chart")
    shape = charted[0]
    assert shape.chart.categories and shape.chart.series
    from ppt_harness.render import svg

    markup = svg.chart_svg(shape.chart.kind, shape.chart.categories, shape.chart.series,
                           600, 400)
    assert markup.startswith("<svg") and "<rect" in markup


def test_a_chart_with_no_data_draws_nothing() -> None:
    from ppt_harness.render import svg

    assert svg.chart_svg("bar", [], [], 400, 300) == ""


def test_video_posters_are_extracted(imported: Session) -> None:
    """A movie's poster frame is what PowerPoint shows before playback, and it is right
    there in the package — better than a grey box labelled "media"."""
    movies = [s for slide in imported.deck.slides for s in slide.shapes
              if s.type == "media"]
    if not movies:
        pytest.skip("fixture has no video")
    assert any(s.asset in imported.assets for s in movies)


def test_connectors_are_drawn_rather_than_hatched(imported: Session) -> None:
    """Hatching them as unmodelled hides rules and arrows the slide is partly made of."""
    lines = [s for slide in imported.deck.slides for s in slide.shapes if s.type == "line"]
    if not lines:
        pytest.skip("fixture has no connectors")
    assert not any(s.opaque for s in lines)


# --------------------------------------------------------------------- inheritance


def test_layout_and_master_art_is_carried_onto_the_slide(imported: Session) -> None:
    """Logos and footer bars live on the layout. Omitting them makes every preview look
    bare in a way the real deck is not."""
    if not any(s.inherited for s in imported.deck.slides):
        pytest.skip("fixture layout has no non-placeholder art")
    slide = next(s for s in imported.deck.slides if s.inherited)
    cx, cy = imported.slide_size_emu()
    markup = html.render_slide(imported.theme, slide, cx, cy).html
    for shape in slide.inherited:
        if shape.text:
            assert shape.text[:12] in markup or "asset" in markup


def test_inherited_shapes_are_drawn_but_never_measured(imported: Session) -> None:
    """They belong to the layout, not this slide; budgeting them would report overflow
    nobody can act on."""
    slide = next((s for s in imported.deck.slides if s.inherited), None)
    if slide is None:
        pytest.skip("fixture layout has no inherited art")
    cx, cy = imported.slide_size_emu()
    targets = html.render_slide(imported.theme, slide, cx, cy).targets
    for shape in slide.inherited:
        assert f"{slide.id}/{shape.id}" not in targets


def test_inherited_shapes_are_not_exported(imported: Session, tmp_path) -> None:
    """They are already in the file, on the layout. Writing copies onto the slide would
    duplicate the logo on every export."""
    from ppt_harness.io.export_mutate import export

    report = export(imported.deck, tmp_path / "out.pptx")
    assert report.shapes_added == 0


def test_placeholder_fill_is_inherited(imported: Session) -> None:
    """A slide's footer states no fill of its own; the coloured band lives on the layout."""
    filled = [s for slide in imported.deck.slides for s in slide.shapes
              if s.geometry and s.geometry.fill]
    if not filled:
        pytest.skip("fixture has no filled placeholders")
    assert all(s.geometry.fill.startswith("#") for s in filled)


def test_alignment_and_anchor_come_from_the_file(imported: Session) -> None:
    """A title the file centres and the preview left-aligns is the most visible way a
    preview can be wrong."""
    values = {(s.align, s.anchor) for slide in imported.deck.slides for s in slide.shapes
              if s.text}
    assert values, "fixture has no text"
    assert any(align != "left" or anchor != "top" for align, anchor in values), \
        "nothing resolved past the defaults — inheritance is not being read"


def test_autofit_is_compensated_for_display_but_not_for_measurement(
    imported: Session,
) -> None:
    """Looking right and being right are different questions.

    The preview reproduces the shrink PowerPoint applies, so it matches what the audience
    sees. The measurement does not, because the harness exports with autofit *off* and the
    honest overflow is the one at the declared size.
    """
    slide = next((s for s in imported.deck.slides
                  if any(x.autofit_scale and x.autofit_scale < 1 for x in s.shapes)), None)
    if slide is None:
        pytest.skip("fixture has no autofit-compensated text")
    cx, cy = imported.slide_size_emu()
    measured = html.render_slide(imported.theme, slide, cx, cy).html
    displayed = html.render_slide(imported.theme, slide, cx, cy,
                                  compensate_autofit=True).html
    assert measured != displayed
    assert not imported.measure_slide(slide.id)["clean"], \
        "measurement must still report the overflow"
