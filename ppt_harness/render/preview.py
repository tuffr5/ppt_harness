"""Preview by round-trip — DESIGN §6.1.

The preview is not an approximation of the export; it **is** the export, rendered. Deck
state goes out through the ordinary exporter, a real renderer turns that file into a PDF,
and pages are rasterised on demand.

That collapses a whole class of bug. "What you previewed is what PowerPoint renders" stops
being a property the fidelity work has to defend and becomes true by construction — and
SmartArt, gradients, WordArt, tables, curved connectors and video posters all render
correctly for free, because nothing is reimplementing them.

The cost is latency, so the split that matters is who is waiting:

- **measurement** feeds the model and stays analytic, ~1ms, no renderer involved;
- **preview** feeds a person, costs ~1s a slide, and is cached.

DESIGN's ~10ms budget was for the verification loop, which is measurement. A person looking
at a slide can wait a second; a repair loop cannot.

PDF is the cached artifact because it is vector: one expensive conversion per deck version,
then any page at any DPI for tens of milliseconds. Rasterising is cheap enough to do per
request; converting is not.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..fidelity import reference

if TYPE_CHECKING:  # pragma: no cover
    from ..core.session import Session


def cache_root() -> Path:
    """Where the harness keeps working files.

    Project-local by default rather than the system temp directory: on macOS that lives
    under `/private`, which sits behind a permission prompt, and asking someone to approve
    a folder they never chose is a bad first impression. `PPT_HARNESS_CACHE` overrides it.
    """
    override = os.environ.get("PPT_HARNESS_CACHE")
    return Path(override) if override else Path.cwd() / ".harness"


class PreviewUnavailable(RuntimeError):
    """No renderer is installed, so there is nothing to rasterise."""


@dataclass(frozen=True)
class Page:
    slide_id: str
    index: int
    png: bytes
    width: int


def _rasterise(pdf: Path, page: int, width: int, out_dir: Path) -> bytes:
    """One page of a PDF to PNG. Tens of milliseconds — cheap enough to do per request."""
    binary = shutil.which("pdftoppm") or shutil.which("pdftocairo")
    if binary is None:
        raise PreviewUnavailable("no PDF rasteriser: install poppler (`brew install poppler`)")
    prefix = out_dir / f"{pdf.stem}-p{page}-{width}"
    subprocess.run(
        [binary, "-png", "-f", str(page), "-l", str(page), "-singlefile",
         "-scale-to-x", str(width), "-scale-to-y", "-1", str(pdf), str(prefix)],
        capture_output=True, check=True, timeout=120,
    )
    produced = prefix.with_suffix(".png")
    if not produced.exists():
        raise PreviewUnavailable(f"page {page} produced no image")
    return produced.read_bytes()


class PreviewCache:
    """Renders a session's deck through the real export path, and remembers the result.

    Keyed by a hash of deck state, so any edit — by the model, by the user, by a repair —
    invalidates it without anyone having to remember to say so. Getting invalidation wrong
    shows a stale slide, which is worse than showing none.
    """

    def __init__(self, session: Session, root: Path | None = None) -> None:
        self.session = session
        self.root = Path(root) if root else cache_root() / "preview"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._version: str | None = None
        self._pdf: Path | None = None
        self._pages: int = 0

    # -- versioning -------------------------------------------------------------

    def version(self) -> str:
        """A fingerprint of everything that would change the rendering."""
        payload = self.session.deck.model_dump(mode="json")
        return hashlib.sha256(
            repr(payload).encode("utf-8", "replace")
        ).hexdigest()[:16]

    @property
    def available(self) -> bool:
        return reference.available() is not None

    # -- rendering --------------------------------------------------------------

    def _sweep(self) -> None:
        """Drop renders that never finished.

        A `.pptx` with no `.pdf` beside it is the debris of an interrupted conversion, and
        reading one back produces a zlib error rather than anything actionable.
        """
        for deck in self.root.glob("*.pptx"):
            if not deck.with_suffix(".pdf").exists():
                with contextlib.suppress(OSError):
                    deck.unlink()

    def _adopt(self, current: str) -> tuple[Path, int] | None:
        """Reuse a render left on disk by an earlier process.

        The filename *is* the version, so restarting the server — or opening the same deck
        in a second process — costs nothing. Without this, every restart drives the renderer
        again for a file that is already correct.
        """
        pdf = self.root / f"{current}.pdf"
        if not pdf.exists():
            self._sweep()
            return None
        self._version, self._pdf = current, pdf
        self._pages = reference.page_count(pdf)
        return pdf, self._pages

    def _ensure(self) -> tuple[Path, int]:
        """Export and convert if the deck has changed since last time."""
        current = self.version()
        with self._lock:
            if self._version == current and self._pdf and self._pdf.exists():
                return self._pdf, self._pages

            adopted = self._adopt(current)
            if adopted is not None:
                return adopted

            if not self.available:
                raise PreviewUnavailable(
                    "no reference renderer: install LibreOffice, or run where PowerPoint "
                    "is available. Measurement is unaffected — it never needed one."
                )

            deck_path = self.root / f"{current}.pptx"
            pdf_path = self.root / f"{current}.pdf"

            # Held across the write *and* the conversion. Same deck version means same
            # filename, so without this a second process can replace the file while this
            # one is still reading it — which surfaces as "PowerPoint can't read", a
            # message that blames the file rather than the timing.
            with reference.exclusive():
                return self._render(current, deck_path, pdf_path)

    def _render(self, current: str, deck_path: Path, pdf_path: Path) -> tuple[Path, int]:
        from ..io.export_mutate import export

        # Written aside and renamed, because the rename is atomic and the write is not.
        scratch = self.root / f".{current}.{os.getpid()}.pptx"
        try:
            # `strict=False`: a preview must render even when the deck has a fidelity
            # problem. Refusing to draw the thing the user is trying to look at would hide
            # the very slide they need to see.
            export(self.session.deck, scratch, strict=False,
                   assets=self.session.assets)
            scratch.replace(deck_path)
        finally:
            scratch.unlink(missing_ok=True)

        reference.to_pdf(deck_path, pdf_path)

        self._version = current
        self._pdf = pdf_path
        self._pages = reference.page_count(pdf_path)
        self._prune(keep=current)
        return pdf_path, self._pages

    def _prune(self, keep: str, generations: int = 3) -> None:
        """Drop old renders, newest first.

        Undo is one keystroke, so the render a user just left is very likely the one they
        are about to want again. Keeping a few generations makes undo instant; keeping all
        of them fills a disk with copies of a 174 MB deck.
        """
        pdfs = sorted(self.root.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in pdfs[generations:]:
            version = stale.stem
            if version == keep:
                continue
            for path in self.root.glob(f"{version}*"):
                with contextlib.suppress(OSError):
                    path.unlink()

    def page(self, slide_id: str, width: int = 1280) -> Page:
        pdf, count = self._ensure()
        index = next((i for i, s in enumerate(self.session.deck.slides)
                      if s.id == slide_id), None)
        if index is None:
            raise KeyError(f"no slide {slide_id!r}")
        if index >= count:
            raise PreviewUnavailable(
                f"the rendered deck has {count} pages but {slide_id} is at index {index}"
            )
        return Page(slide_id=slide_id, index=index, width=width,
                    png=_rasterise(pdf, index + 1, width, self.root))

    def warm(self) -> None:
        """Render ahead of the first request, so opening a deck is not a stall.

        Best effort by definition: it runs on a background thread nobody is waiting on, and
        whatever goes wrong here — no renderer, a locked file, an export that fails — the
        correct outcome is the same, which is that the next request renders on demand and
        reports its own error to somebody who can see it. Letting an exception escape a
        daemon thread only produces a warning nobody can act on.
        """
        try:
            self._ensure()
        except Exception:
            self.invalidate()

    def invalidate(self) -> None:
        with self._lock:
            self._version = None

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
