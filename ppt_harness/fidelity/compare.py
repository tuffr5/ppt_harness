"""Preview versus ground truth.

Renders every slide twice — once through the harness's HTML, once through a real renderer —
and reports where they differ. The point is not a pass/fail score; it is a picture a person
can look at, plus a number that tracks whether things are getting better.

The score is a coarse per-pixel difference on a downscaled greyscale image. It is
deliberately crude: anti-aliasing and font hinting differ between engines no matter what, so
a precise metric would only encode that noise. What it is good for is *ranking* — which
slide is worst, and whether a change helped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops

from ..core.session import Session
from ..render import browser
from . import reference

#: Compared at this width. Small enough that hinting differences wash out, large enough that
#: a missing shape does not.
COMPARE_WIDTH = 320


@dataclass(frozen=True)
class SlideDiff:
    slide_id: str
    index: int
    difference: float
    """0 = identical, 1 = maximally different."""
    ours: Path
    theirs: Path
    side_by_side: Path

    @property
    def verdict(self) -> str:
        if self.difference < 0.04:
            return "close"
        if self.difference < 0.12:
            return "drifting"
        return "wrong"


def _score(a: Path, b: Path) -> float:
    left = Image.open(a).convert("L").resize((COMPARE_WIDTH, COMPARE_WIDTH * 9 // 16))
    right = Image.open(b).convert("L").resize(left.size)
    diff = ImageChops.difference(left, right)
    histogram = diff.histogram()
    total = sum(histogram)
    if not total:
        return 0.0
    # Mean absolute difference, normalised. Ignores where the difference is, which is why
    # the side-by-side image matters more than this number.
    return sum(i * n for i, n in enumerate(histogram)) / (total * 255)


def _stack(ours: Path, theirs: Path, out: Path, label: str) -> Path:
    """Ours above, theirs below, so the eye can scan one column."""
    a, b = Image.open(ours).convert("RGB"), Image.open(theirs).convert("RGB")
    width = max(a.width, b.width)
    a = a.resize((width, round(a.height * width / a.width)))
    b = b.resize((width, round(b.height * width / b.width)))
    gap = 28
    canvas = Image.new("RGB", (width, a.height + b.height + gap * 2), "#1b1e24")
    canvas.paste(a, (0, gap))
    canvas.paste(b, (0, a.height + gap * 2))
    canvas.save(out)
    del label
    return out


def compare(deck: Path | str, out_dir: Path | str, *, slides: list[str] | None = None,
            width: int = 1280) -> list[SlideDiff]:
    """Render `deck` both ways and report the difference per slide."""
    deck, out_dir = Path(deck), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ours_dir, theirs_dir = out_dir / "harness", out_dir / "reference"
    ours_dir.mkdir(exist_ok=True)
    theirs_dir.mkdir(exist_ok=True)

    ref = reference.render(deck, theirs_dir, width=width)
    session = Session.open(deck)
    cx, cy = session.slide_size_emu()

    results: list[SlideDiff] = []
    for index, slide in enumerate(session.deck.slides):
        if slides and slide.id not in slides:
            continue
        if index >= len(ref.pages):
            break
        mine = ours_dir / f"{slide.id}.png"
        mine.write_bytes(browser.screenshot_slide(session.theme, slide, cx, cy, width,
                                                  asset_src=session.asset_data_uri))
        theirs = ref.pages[index]
        results.append(SlideDiff(
            slide_id=slide.id,
            index=index,
            difference=_score(mine, theirs),
            ours=mine,
            theirs=theirs,
            side_by_side=_stack(mine, theirs, out_dir / f"{slide.id}_compare.png", slide.id),
        ))
    return results


def report(results: list[SlideDiff]) -> str:
    lines = [f"{'slide':<8} {'diff':>7}  verdict"]
    for item in results:
        lines.append(f"{item.slide_id:<8} {item.difference:>7.3f}  {item.verdict}")
    if results:
        worst = max(results, key=lambda r: r.difference)
        mean = sum(r.difference for r in results) / len(results)
        lines.append("")
        lines.append(f"mean {mean:.3f} · worst {worst.slide_id} ({worst.difference:.3f})")
    return "\n".join(lines)
