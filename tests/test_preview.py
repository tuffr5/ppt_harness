"""Preview by round-trip — DESIGN §6.1.

The claim: the preview is not an approximation of the export, it *is* the export, rendered.
Two things have to hold for that to be worth anything.

**Invalidation.** The cache is keyed by deck state, so any edit produces a new render
without anyone remembering to say so. A stale preview is worse than none — it shows the
user a slide that no longer exists and invites them to act on it.

**Separation.** Measurement must not acquire a dependency on the renderer. The model's loop
runs at ~1ms and has to keep working on a machine with no Office installed; only the
person looking at a picture pays the ~1s.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ppt_harness.adapters.web import create_app
from ppt_harness.core.session import Session
from ppt_harness.fidelity import reference
from ppt_harness.render import html, preview
from ppt_harness.state.document import Author

needs_renderer = pytest.mark.skipif(reference.available() is None,
                                    reason="no PowerPoint or LibreOffice")


@pytest.fixture
def cache(imported: Session):
    """The **same** directory the application uses.

    Not `tmp_path`, and not a test-only folder either. PowerPoint is sandboxed and raises a
    permission dialog for every directory it has not been granted, so any new path means a
    modal nobody is there to click — which surfaces as a bare "PowerPoint got an error".
    One directory for everything means one grant, ever.

    Sharing it with a running server is safe: renders are serialised by a file lock, and
    the cache is keyed by deck content, so the worst case is a redundant conversion.
    """
    made = preview.PreviewCache(imported, root=preview.cache_root() / "preview")
    yield made


# ------------------------------------------------------------------ invalidation


def test_the_version_tracks_deck_state(cache, imported: Session) -> None:
    before = cache.version()
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    with imported.transaction(Author.MODEL) as turn:
        imported.store.write(turn, "set_text", f"{slide.id}/{shape.id}",
                             {"text": "Changed"}, Author.MODEL)
    assert cache.version() != before, "an edit must produce a new render key"


def test_undo_restores_the_previous_version(cache, imported: Session) -> None:
    """The same deck state must render to the same key, or every undo re-renders."""
    before = cache.version()
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    with imported.transaction(Author.MODEL) as turn:
        imported.store.write(turn, "set_text", f"{slide.id}/{shape.id}",
                             {"text": "Changed"}, Author.MODEL)
    imported.store.undo()
    assert cache.version() == before


def test_reading_the_deck_does_not_change_the_version(cache, imported: Session) -> None:
    before = cache.version()
    imported.measure_slide(imported.deck.slides[0].id)
    imported.outline()
    assert cache.version() == before


# -------------------------------------------------------------------- separation


def test_measurement_never_needs_a_renderer(imported: Session, monkeypatch) -> None:
    """The model's loop must keep working where no Office is installed."""
    monkeypatch.setattr(reference, "available", lambda: None)
    result = imported.measure_slide(imported.deck.slides[0].id)
    assert "overflow_px" in result
    assert "clean" in result


def test_a_missing_renderer_is_reported_not_hidden(imported: Session, tmp_path,
                                                   monkeypatch) -> None:
    """With nothing cached and nothing installed, say so. A blank pane with no explanation
    is the one outcome that leaves the user with nowhere to go.

    `tmp_path` is safe here precisely because no renderer runs — nothing will ask the
    sandbox for permission to a folder it has not seen.
    """
    monkeypatch.setattr(reference, "available", lambda: None)
    made = preview.PreviewCache(imported, root=tmp_path / "empty")
    assert made.available is False
    with pytest.raises(preview.PreviewUnavailable, match="no reference renderer"):
        made.page(imported.deck.slides[0].id)


@needs_renderer
def test_an_existing_render_is_served_without_a_renderer(cache, imported: Session,
                                                         monkeypatch) -> None:
    """Adoption happens before the availability check, and should.

    A render already on disk is just as good whether or not Office is still installed —
    refusing to show it would be pedantry at the user's expense.
    """
    cache.page(imported.deck.slides[0].id, width=240)
    monkeypatch.setattr(reference, "available", lambda: None)

    revived = preview.PreviewCache(imported, root=cache.root)
    assert revived.available is False
    assert revived.page(imported.deck.slides[0].id, width=240).png.startswith(b"\x89PNG")


# ---------------------------------------------------------------------- overlay


def test_the_overlay_carries_a_box_per_measured_target(imported: Session) -> None:
    slide = imported.deck.slides[0]
    measurement = imported.measure_slide(slide.id)
    markup = html.render_overlay(imported.theme, slide, "/x.png", measurement)
    expected = len([i for i in (measurement.get("shapes") or measurement.get("slots") or [])
                    if i.get("box")])
    assert markup.count('class="probe') == expected
    assert "<img" in markup


def test_overflow_is_marked_distinctly(imported: Session) -> None:
    slide = next((s for s in imported.deck.slides
                  if not imported.measure_slide(s.id)["clean"]), None)
    if slide is None:
        pytest.skip("fixture has no overflowing slide")
    markup = html.render_overlay(imported.theme, slide, "/x.png",
                                 imported.measure_slide(slide.id))
    assert "probe over" in markup


def test_overlay_boxes_are_in_canvas_coordinates(imported: Session) -> None:
    """The same numbers the exporter writes. Anything else and the inspector points at the
    wrong place on someone else's rendering."""
    slide = imported.deck.slides[0]
    width, height = imported.theme.grid.canvas
    markup = html.render_overlay(imported.theme, slide, "/x.png",
                                 imported.measure_slide(slide.id))
    assert f"width: {width}px" in markup or f"width:{width}px" in markup
    assert f"height: {height}px" in markup or f"height:{height}px" in markup


def test_an_empty_measurement_still_renders_the_picture(imported: Session) -> None:
    markup = html.render_overlay(imported.theme, imported.deck.slides[0], "/x.png", None)
    assert "<img" in markup
    assert 'class="probe' not in markup


# ----------------------------------------------------------------------- render


@needs_renderer
def test_a_page_is_a_png_of_the_right_slide(cache, imported: Session) -> None:
    page = cache.page(imported.deck.slides[0].id, width=640)
    assert page.png.startswith(b"\x89PNG")
    assert page.index == 0


@needs_renderer
def test_every_slide_can_be_rendered(cache, imported: Session) -> None:
    for index, slide in enumerate(imported.deck.slides):
        page = cache.page(slide.id, width=320)
        assert page.index == index
        assert page.png.startswith(b"\x89PNG")


@needs_renderer
def test_an_unknown_slide_is_a_key_error(cache) -> None:
    with pytest.raises(KeyError):
        cache.page("no-such-slide")


@needs_renderer
def test_the_pdf_is_converted_once_per_version(cache, imported: Session) -> None:
    """Conversion is the expensive step; rasterising is not. Re-converting per request
    would make every zoom cost a second."""
    import time

    cache.page(imported.deck.slides[0].id, width=320)
    started = time.time()
    cache.page(imported.deck.slides[0].id, width=640)
    assert time.time() - started < 1.0, "a second size re-ran the conversion"


def test_rendering_never_creates_a_new_directory(cache, imported: Session) -> None:
    """A directory per version means a sandbox permission dialog on every edit."""
    from ppt_harness.state.document import Author as A

    cache.page(imported.deck.slides[0].id, width=240)
    before = {p.name for p in cache.root.iterdir() if p.is_dir()}

    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    with imported.transaction(A.USER) as turn:
        imported.store.write(turn, "set_text", f"{slide.id}/{shape.id}",
                             {"text": "Forces a re-render"}, A.USER)
    cache.page(slide.id, width=240)

    assert {p.name for p in cache.root.iterdir() if p.is_dir()} == before
    assert list(cache.root.glob("*.pdf")), "the render must still be on disk"


@needs_renderer
def test_old_renders_are_pruned(cache, imported: Session) -> None:
    """Keeping every generation fills a disk with copies of a 174 MB deck; keeping none
    makes undo — one keystroke — pay for a full re-render."""
    from ppt_harness.state.document import Author as A

    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    for n in range(6):
        with imported.transaction(A.USER) as turn:
            imported.store.write(turn, "set_text", f"{slide.id}/{shape.id}",
                                 {"text": f"revision {n}"}, A.USER)
        cache.page(slide.id, width=200)

    assert len(list(cache.root.glob("*.pdf"))) <= 4


# --------------------------------------------------------------------------- web


def test_the_preview_pane_uses_the_renderer_when_present(imported: Session) -> None:
    client = TestClient(create_app(imported))
    body = client.get("/api/outline").json()
    markup = client.get(f"/api/slide/{imported.deck.slides[0].id}").text
    if body["renderer"]:
        assert "<img" in markup and "preview.png" in markup
    else:
        assert "class=\"slot" in markup, "should fall back to the HTML renderer"


def test_the_outline_says_which_renderer_is_in_use(imported: Session) -> None:
    """Degraded and honest beats silently different."""
    client = TestClient(create_app(imported))
    body = client.get("/api/outline").json()
    assert "renderer" in body
    assert body["renderer"] in (None, "powerpoint", "libreoffice")


@needs_renderer
def test_the_png_endpoint_serves_an_image(imported: Session) -> None:
    client = TestClient(create_app(imported))
    slide_id = imported.deck.slides[0].id
    response = client.get(f"/api/slide/{slide_id}/preview.png?width=480")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


@needs_renderer
def test_the_preview_url_changes_when_the_deck_does(imported: Session) -> None:
    """The version is in the URL, so the image can be cached immutably and an edit still
    busts it."""
    client = TestClient(create_app(imported))
    slide = imported.deck.slides[0]
    first = client.get(f"/api/slide/{slide.id}").text

    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    client.post("/api/edit", json={"target": f"{slide.id}/{shape.id}", "text": "Different"})

    assert client.get(f"/api/slide/{slide.id}").text != first


def test_a_missing_slide_is_still_404(imported: Session) -> None:
    client = TestClient(create_app(imported))
    assert client.get("/api/slide/nope/preview.png").status_code == 404

