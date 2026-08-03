"""Reference rendering — the fidelity oracle.

Drives a **real renderer** — PowerPoint where installed, LibreOffice otherwise — to PDF,
then rasterises pages. `PPT_HARNESS_RENDERER` pins one, and a PowerPoint that is installed
but will not convert falls back rather than failing: selection used to ask only whether the
application existed on disk, which is not the same question as whether it works.

Two callers, and the distinction matters.

`render/preview.py` uses it to *produce* the preview: the export, rendered, so the picture
cannot drift from the file.

`fidelity/compare.py` uses it as *ground truth* to score anything the harness draws itself.
PowerPoint is preferred because it is the engine the recipient will use, so a disagreement
with it is a real defect rather than a third opinion. LibreOffice is a usable stand-in on
CI, where neither Office nor a Mac is available — faithful to *a* renderer, and the
`available()` name is reported so nobody mistakes one for the other.

Costs seconds and opens an application, so nothing the **model** does may depend on it.
Measurement never calls this.
"""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SOFFICE_CANDIDATES = [
    "soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
]


class NoReferenceRenderer(RuntimeError):
    """Neither PowerPoint nor LibreOffice is available."""


#: The renderer is one application on the machine, not one per caller — and not one per
#: *process*. A running server and a test run are the ordinary case, and two AppleScript
#: sessions driving PowerPoint at once fail with error -9074; LibreOffice headless has the
#: same problem with its user profile. So the lock has to be a file, not just an object.
_ENGINE_LOCK = threading.Lock()

#: Waiting is right here: the other holder is converting a deck and will be done in seconds.
#: Failing instead would surface as "PowerPoint got an error", which explains nothing.
_LOCK_TIMEOUT = 300


def _lock_path() -> Path:
    from ..render.preview import cache_root

    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / "renderer.lock"


#: Depth of nested `exclusive()` blocks on this thread. A caller that legitimately holds
#: the lock across a sequence must not deadlock against a function inside it that takes the
#: lock too.
_DEPTH = threading.local()


@contextmanager
def exclusive() -> Iterator[None]:
    """Exclusive use of the renderer, across threads and across processes.

    Reentrant, and named `exclusive` rather than `engine` because `engine` is already the
    name of a parameter meaning *which* renderer — a collision that silently resolves to
    `None` and fails as "NoneType is not callable".

    Hold it across a whole write-then-convert sequence. Two processes on the same deck
    version compute the same filename, so locking only the conversion lets one rewrite the
    file while the other reads it — which PowerPoint reports as "can't read", blaming the
    file rather than the timing.
    """
    depth = getattr(_DEPTH, "value", 0)
    if depth:
        _DEPTH.value = depth + 1
        try:
            yield
        finally:
            _DEPTH.value -= 1
        return

    _DEPTH.value = 1
    try:
        with _hold():
            yield
    finally:
        _DEPTH.value = 0


@contextmanager
def _hold() -> Iterator[None]:
    with _ENGINE_LOCK, open(_lock_path(), "w") as handle:
        deadline = time.monotonic() + _LOCK_TIMEOUT
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise NoReferenceRenderer(
                            "another process has been using the renderer for "
                            f"{_LOCK_TIMEOUT}s; is a dialog waiting for input?"
                        ) from None
                    time.sleep(0.25)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass(frozen=True)
class Reference:
    engine: str
    pdf: Path
    pages: list[Path]


# ------------------------------------------------------------------ powerpoint


def _powerpoint_available() -> bool:
    return Path("/Applications/Microsoft PowerPoint.app").exists()


#: Set once we have decided who owns the application's lifetime, so the decision is made
#: from the state of the machine *before* the first render rather than between two of them.
_LIFETIME_DECIDED = False


def _keep_powerpoint_open() -> None:
    """Leave PowerPoint running between renders, and quit it at exit if we started it.

    The alternative — quit after every render — is what made `-9074` a routine event here.
    Leaving the machine as we found it is still the goal; it just belongs at the end of the
    process rather than after each of its hundred renders.
    """
    global _LIFETIME_DECIDED
    if _LIFETIME_DECIDED:
        return
    _LIFETIME_DECIDED = True

    probe = subprocess.run(
        ["osascript", "-e", 'application "Microsoft PowerPoint" is running'],
        capture_output=True, text=True, timeout=30)
    if probe.stdout.strip() == "true":
        return          # someone else's session; never close their windows

    atexit.register(_quit_powerpoint)


def _quit_powerpoint() -> None:
    with contextlib.suppress(Exception):
        subprocess.run(
            ["osascript", "-e",
             'tell application "Microsoft PowerPoint" to quit saving no'],
            capture_output=True, text=True, timeout=60)


def _powerpoint_to_pdf(source: Path, out_pdf: Path) -> None:
    """Export via the application itself.

    `saving no` on close is load-bearing: the deck must be left exactly as found, and an
    accidental save through a different PowerPoint build would rewrite parts the harness is
    trying to prove it preserves.
    """
    _keep_powerpoint_open()

    # Close a presentation of the same name before opening. A run killed mid-script — a
    # test timeout, a stopped server — leaves the file open, and every later `open` then
    # fails with a bare -9074 that says nothing about why.
    #
    # It does **not** quit at the end. It used to, whenever it had launched the app itself,
    # which is polite exactly once: a suite or a recording renders dozens of decks back to
    # back, so every render paid a launch, and each launch raced the previous render's quit.
    # That race is the -9074 this file kept reporting, and once the app lands in that state
    # it stays there — running, answering `get name`, refusing to open a document. Quitting
    # is now done once, at process exit, by `_keep_powerpoint_open`.
    script = f'''
    set src to POSIX file "{source}"
    set dst to POSIX file "{out_pdf}"
    tell application "Microsoft PowerPoint"
        repeat with p in (get every presentation)
            try
                if (name of p) is "{source.name}" then close p saving no
            end try
        end repeat
        open src
        delay 1
        save active presentation in dst as save as PDF
        close active presentation saving no
    end tell
    '''
    # Retried once, and only on -9074. That error means "the application is not in a state
    # to answer" — most often because a previous render's `quit` is still tearing the app
    # down while this one opens it, which is what a session doing many renders back to back
    # produces. A second attempt after the teardown finishes succeeds; a wedged install
    # fails twice and is reported, which is the case worth telling someone about.
    for attempt in (1, 2):
        done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                              timeout=600)
        if done.returncode == 0 and out_pdf.exists():
            return
        if attempt == 1 and "-9074" in done.stderr:
            time.sleep(4.0)
            continue
        break

    raise NoReferenceRenderer(
        f"PowerPoint export failed: {done.stderr.strip() or 'no PDF produced'}"
        + (". -9074 twice: PowerPoint is running but will not open a document — bring it to "
           "the front and dismiss whatever dialog it is showing, or quit it."
           if "-9074" in done.stderr else "")
    )


# ------------------------------------------------------------------ libreoffice


def _soffice() -> str | None:
    for candidate in SOFFICE_CANDIDATES:
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    return None


def _libreoffice_to_pdf(source: Path, out_pdf: Path) -> None:
    binary = _soffice()
    if binary is None:
        raise NoReferenceRenderer("LibreOffice not found")
    done = subprocess.run(
        [binary, "--headless", "--convert-to", "pdf", "--outdir", str(out_pdf.parent),
         str(source)],
        capture_output=True, text=True, timeout=600,
    )
    produced = out_pdf.parent / (source.stem + ".pdf")
    if done.returncode != 0 or not produced.exists():
        raise NoReferenceRenderer(f"LibreOffice export failed: {done.stderr.strip()}")
    if produced != out_pdf:
        produced.replace(out_pdf)


# ------------------------------------------------------------------ rasterising


def _pdf_to_pngs(pdf: Path, out_dir: Path, width: int) -> list[Path]:
    binary = shutil.which("pdftoppm") or shutil.which("pdftocairo")
    if binary is None:
        raise NoReferenceRenderer(
            "no PDF rasteriser: install poppler (`brew install poppler`)"
        )
    prefix = out_dir / "page"
    subprocess.run([binary, "-png", "-scale-to-x", str(width), "-scale-to-y", "-1",
                    str(pdf), str(prefix)],
                   capture_output=True, text=True, check=True, timeout=600)
    return sorted(out_dir.glob("page*.png"))


# ----------------------------------------------------------------------- public


def render(source: Path | str, out_dir: Path | str, *, width: int = 1280,
           engine: str | None = None) -> Reference:
    """Rasterise `source` with a real renderer. One PNG per slide, in order."""
    source, out_dir = Path(source).resolve(), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / f"{source.stem}.pdf"

    chosen = engine or ("powerpoint" if _powerpoint_available() else "libreoffice")
    with exclusive():
        if pdf.exists():
            # Another caller produced it while we waited. Converting again would cost a
            # second and change nothing.
            return Reference(engine=chosen, pdf=pdf,
                             pages=_pdf_to_pngs(pdf, out_dir, width))
        if chosen == "powerpoint":
            _powerpoint_to_pdf(source, pdf)
        else:
            _libreoffice_to_pdf(source, pdf)

    return Reference(engine=chosen, pdf=pdf, pages=_pdf_to_pngs(pdf, out_dir, width))


def page_count(pdf: Path) -> int:
    """Pages in a PDF, without rasterising any of them.

    `render` produces a PNG per page, which is exactly what the preview cache does *not*
    want: it needs the count so it can map slide index to page, and rasterises one page on
    demand at the size actually being displayed.
    """
    binary = shutil.which("pdfinfo")
    if binary:
        done = subprocess.run([binary, str(pdf)], capture_output=True, text=True,
                              timeout=60)
        for line in done.stdout.splitlines():
            if line.lower().startswith("pages:"):
                return int(line.split(":", 1)[1].strip())
    # No poppler-utils `pdfinfo`: fall back to counting page objects in the file itself.
    blob = pdf.read_bytes()
    return max(1, blob.count(b"/Type /Page") - blob.count(b"/Type /Pages"))


def to_pdf(source: Path | str, out_pdf: Path | str, *, engine: str | None = None) -> str:
    """Convert without rasterising. Returns the engine used.

    Falls back to LibreOffice when PowerPoint is installed but will not convert. Preference
    was decided by `_powerpoint_available()`, which asks only whether the *application
    exists on disk* — so a Mac whose PowerPoint has stopped answering (a modal dialog it is
    holding, a licence prompt, an instance that has been driven for hours) failed every
    render on a machine that had a working LibreOffice sitting beside it.

    The fidelity note in the README still holds and matters more after a fallback than
    before: the two renderers substitute fonts and break lines differently, so a figure
    measured against one is not comparable to a figure measured against the other. This
    returns the engine it actually used, and every caller records it.
    """
    source, out_pdf = Path(source).resolve(), Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    forced = engine or ((os.environ.get("PPT_HARNESS_RENDERER") or "").strip().lower()
                        or None)
    chosen = forced or preferred() or "libreoffice"

    with exclusive():
        if chosen != "powerpoint":
            _libreoffice_to_pdf(source, out_pdf)
            return chosen
        try:
            _powerpoint_to_pdf(source, out_pdf)
            return "powerpoint"
        except NoReferenceRenderer:
            # Only when the caller did not name an engine: someone who asked for PowerPoint
            # explicitly wants PowerPoint's answer or none, because the whole point of
            # naming it is that it is the engine the recipient will use.
            if forced or not _soffice():
                raise
            _libreoffice_to_pdf(source, out_pdf)
            return "libreoffice"


def preferred() -> str | None:
    """Which engine `to_pdf` will reach for, honouring `PPT_HARNESS_RENDERER`.

    The environment variable exists because "installed" and "usable" are different states
    and only a person can tell them apart. A PowerPoint holding a modal dialog answers
    `is running` and refuses every `open`; nothing this module can measure distinguishes
    that from a healthy one without paying seconds to try. So the override is the escape
    hatch, and `to_pdf` falls back on failure for everyone who has not set it.
    """
    forced = (os.environ.get("PPT_HARNESS_RENDERER") or "").strip().lower()
    if forced in ("powerpoint", "libreoffice"):
        return forced
    if _powerpoint_available():
        return "powerpoint"
    if _soffice():
        return "libreoffice"
    return None


def available() -> str | None:
    """An engine that can actually convert, or None.

    Reports the *fallback* when the preferred engine is not installed — a machine with a
    forced-but-absent LibreOffice has no renderer, and saying "libreoffice" there would put
    the name of a missing program into a fidelity report.
    """
    engine = preferred()
    if engine == "libreoffice" and not _soffice():
        return "powerpoint" if _powerpoint_available() else None
    if engine == "powerpoint" and not _powerpoint_available():
        return "libreoffice" if _soffice() else None
    return engine
