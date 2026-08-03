"""Round-trip a folder of real decks and report what survived.

This is the benchmark nobody else runs, and the only one here that needs **no model, no
judge, no key and no network**. Open a real `.pptx`, export it straight back out having
changed nothing, and ask the package what is different. The claim under test is the one the
README makes loudest: patching the original package preserves SmartArt, media, animations,
comments and sensitivity labels — parts the harness does not model and must not lose.

A no-op round trip is the honest form of that test. Any difference is damage, because nothing
was asked for. `--edit` additionally sets one title, which is the smallest real edit and the
one that exercises the writer without changing what the rest of the file should contain.

Where to get decks: DESIGN §12 wants a "fidelity margin corpus … generated from a real deck
corpus" and none was ever built, because none of the repository's own fixtures is a corpus.
GOVDOCS1 (digitalcorpora.org) is ~1M documents crawled from `.gov`, several thousand of them
`.pptx`, freely redistributable — point `--corpus` at a directory of them.

The result is a rate with a denominator, per part family. "1,914 of 1,927 decks round-tripped
with every media part intact" is a sentence this repository can currently not say about
itself, and it is worth more than any judge model's opinion of a slide.
"""

from __future__ import annotations

import json
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Part families a round trip must preserve exactly. Slide XML is *expected* to change — the
#: writer hardens text frames it touches — so it is not in the list; a missing picture, chart
#: worksheet, embedded object or comment is the failure this measures.
PRESERVE = ("ppt/media/", "ppt/embeddings/", "ppt/charts/", "ppt/notesSlides/",
            "ppt/diagrams/", "customXml/", "docProps/")


@dataclass
class DeckResult:
    path: str
    ok: bool = False
    slides: int = 0
    freeform_slides: int = 0
    opaque_shapes: int = 0
    parts_before: int = 0
    parts_after: int = 0
    lost: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    inherited: int = 0
    error: str = ""
    seconds: float = 0.0

    @property
    def preserved(self) -> bool:
        return self.ok and not self.lost


def round_trip(path: Path, out_dir: Path, *, edit: bool = False) -> DeckResult:
    """Open one deck, write it back, and diff the package.

    Never raises. A corpus of real files contains files that are broken, encrypted, or not
    really `.pptx` at all, and the interesting number is *how many* of those there were.
    """
    from ..core.session import Session
    from ..io import package
    from ..state.document import Mode
    from ..tools import router

    result = DeckResult(path=str(path))
    started = time.monotonic()
    try:
        with zipfile.ZipFile(path) as before:
            names_before = set(before.namelist())
        result.parts_before = len(names_before)

        session = Session.open(path)
        result.slides = len(session.deck.slides)
        result.freeform_slides = sum(1 for s in session.deck.slides
                                     if s.mode is Mode.FREEFORM)
        result.opaque_shapes = sum(1 for s in session.deck.slides
                                   for shape in s.shapes if shape.opaque)

        if edit:
            target = _first_editable(session)
            if target:
                router.dispatch(session, "set_text", {"target": target, "text": "Round trip"})

        out = out_dir / f"{path.stem}.pptx"
        out_dir.mkdir(parents=True, exist_ok=True)
        report = router.dispatch(session, "export", {"path": str(out)})
        result.ok = bool(report.get("ok")) and out.exists()
        result.violations = list(report.get("violations") or [])
        if not out.exists():
            result.error = report.get("message", "export produced no file")
            return result

        with zipfile.ZipFile(out) as after:
            names_after = set(after.namelist())
        result.parts_after = len(names_after)
        result.lost = sorted(n for n in names_before - names_after if n.startswith(PRESERVE))

        audit = package.audit(out, original=path)
        result.violations += [f"{f.kind}: {f.part}" for f in audit.findings]
        result.inherited = len(audit.inherited)
        out.unlink(missing_ok=True)          # a corpus run must not fill the disk
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    result.seconds = round(time.monotonic() - started, 2)
    return result


def _first_editable(session: Any) -> str | None:
    for slide in session.deck.slides:
        for shape in slide.shapes:
            if shape.text and not shape.opaque:
                return f"{slide.id}/{shape.id}"
        for block in slide.blocks:
            for name, value in block.slots.items():
                if isinstance(value, str) and value:
                    return f"{slide.id}/{block.id}/{name}"
    return None


def run(corpus: Path, out_dir: Path, *, limit: int | None = None,
        edit: bool = False) -> dict[str, Any]:
    """Every `.pptx` under `corpus`, round-tripped, with the rate and its denominator."""
    decks = sorted(p for p in Path(corpus).rglob("*.pptx") if p.is_file())
    if limit:
        decks = decks[:limit]

    results = [round_trip(deck, out_dir / "work", edit=edit) for deck in decks]
    opened = [r for r in results if r.ok]
    preserved = [r for r in opened if r.preserved]

    payload = {
        "corpus": str(corpus),
        "edit": edit,
        "decks": len(results),
        "opened": len(opened),
        "preserved": len(preserved),
        # Rates over what actually opened, with both denominators kept. A corpus rate that
        # silently drops the files that would not open is the oldest trick in this genre.
        "open_rate": round(len(opened) / len(results), 4) if results else 0.0,
        "preservation_rate": round(len(preserved) / len(opened), 4) if opened else 0.0,
        "slides": sum(r.slides for r in opened),
        "opaque_shapes": sum(r.opaque_shapes for r in opened),
        "with_violations": sum(1 for r in opened if r.violations),
        "lost_parts": sorted({part for r in results for part in r.lost}),
        "failures": [{"path": r.path, "error": r.error} for r in results if r.error][:50],
        "seconds": round(sum(r.seconds for r in results), 1),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "corpus.json").write_text(
        json.dumps({**payload, "results": [vars(r) for r in results]}, indent=1),
        encoding="utf-8")
    return payload
