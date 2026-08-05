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

    Sharing it with a running server — or with a second copy of this suite — is safe: the
    cache is keyed by deck content, and every step that *destroys* a file in here holds the
    renderer lock, so the worst case is a redundant conversion. That was not always true.
    `_sweep` used to run unlocked and deleted whatever `.pptx` had no `.pdf` beside it,
    which is exactly what a conversion in flight looks like from another process; two runs
    at once therefore killed each other's renders. Anything added here must keep the rule:
    assert about the versions this session made, never about the contents of the directory.
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
    makes undo — one keystroke — pay for a full re-render.

    Counted over the versions *this* session made, not over the directory. DESIGN §6.1 puts
    every process on the project in one cache directory, so a running server or a second
    test run has renders in here too, and a bare file count would be measuring theirs.
    """
    from ppt_harness.state.document import Author as A

    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    mine: list[str] = []
    for n in range(6):
        with imported.transaction(A.USER) as turn:
            imported.store.write(turn, "set_text", f"{slide.id}/{shape.id}",
                                 {"text": f"revision {n}"}, A.USER)
        cache.page(slide.id, width=200)
        mine.append(cache.version())

    survivors = [v for v in mine if (cache.root / f"{v}.pdf").exists()]
    assert len(survivors) <= 4, "every generation was kept"
    assert mine[-1] in survivors, "the render just made is the one undo will want back"


# --------------------------------------------------------- sharing the directory


def test_debris_is_only_swept_while_the_renderer_lock_is_held(cache, monkeypatch) -> None:
    """A `.pptx` with no `.pdf` is debris *or* a conversion another process is in the
    middle of — the two are identical on disk, and DESIGN §6.1's one shared directory means
    both are ordinary. The renderer lock is what tells them apart, so the sweep has to take
    it; without it the sweep deleted the deck the other process was converting.
    """
    import contextlib as ctx
    import os

    held: list[bool] = []
    real = reference.exclusive

    @ctx.contextmanager
    def watched():
        with real():
            held.append(True)
            yield

    monkeypatch.setattr(reference, "exclusive", watched)

    # Named for this process, so a concurrent run's sweep is not what removes it.
    debris = cache.root / f"{os.getpid():016x}.pptx"
    debris.touch()
    cache._sweep()

    assert held, "the sweep ran without the renderer lock"
    assert not debris.exists(), "a finished deck with no PDF is still debris"


def test_adopting_a_render_counts_as_using_it(cache) -> None:
    """`_prune` keeps the most recently *used* renders. While it kept the most recently
    *written* ones, a second process serving a version it had adopted from disk had that
    PDF deleted from under it by the first process's next prune."""
    import os
    import time

    version = f"ad0pt{os.getpid():011x}"
    pdf = cache.root / f"{version}.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    stale = time.time() - 3600
    os.utime(pdf, (stale, stale))
    try:
        assert cache._adopt(version) is not None
        assert pdf.stat().st_mtime > stale, "an adopted render still looks stale to prune"
    finally:
        pdf.unlink(missing_ok=True)


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

