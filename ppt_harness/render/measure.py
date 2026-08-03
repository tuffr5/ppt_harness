"""Text measurement — DESIGN §3.1, §5.1 gate 1.

Advance width against real font metrics, shaped with HarfBuzz so kerning, ligatures, and
mark positioning are the ones the renderer will actually apply. Character counting is not
an approximation of this; it misprices CJK by roughly 2x and never sees kerning at all.

This is the only measurement in the request path, and it deliberately depends on nothing
but font files: the model's loop has to work where no browser and no Office are installed.
Previews are slower and live elsewhere.

Widths are returned in **em** — multiples of the font size — because a budget must survive
a change to the type scale.

`Measurement.width` is em times the size you passed in, so **its unit is whatever unit that
size was in**. The theme's type scale is in canvas px, so everything here is canvas px
unless a caller deliberately hands in points. Mixing the two silently measures every box a
quarter too narrow, which shows up as phantom line breaks rather than as an error.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

import uharfbuzz as hb

from ..state.document import TypeSpec, Typography
from . import fonts


@dataclass(frozen=True)
class Measurement:
    """One measured string."""

    width_em: float
    width: float
    """em times the size passed to `measure` — same unit as that size."""
    scripts: tuple[str, ...]

    @property
    def dominant_script(self) -> str:
        return self.scripts[0] if self.scripts else "latin"


@functools.lru_cache(maxsize=64)
def _hb_font(path: Path, family: str | None) -> tuple[hb.Font, int]:
    """A HarfBuzz font plus its units-per-em. Cached — face creation reads the whole file."""
    blob = hb.Blob.from_file_path(str(path))
    face = hb.Face(blob)
    font = hb.Font(face)
    upem = face.upem or 1000
    return font, upem


def _advance(text: str, path: Path, family: str | None) -> float:
    """Shaped advance width in em."""
    if not text:
        return 0.0
    font, upem = _hb_font(path, family)
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf, {"kern": True, "liga": True})
    return sum(pos.x_advance for pos in buf.glyph_positions) / upem


def measure(text: str, stack: str, size: float, tracking: float = 0.0) -> Measurement:
    """Measure `text` as the renderer will set it.

    Each script run is measured with the face that will render it — `resolve` picks per
    script, so a mixed Latin/Han string is not measured entirely in a Latin face.
    """
    total = 0.0
    seen: list[str] = []
    for script, run in fonts.runs(text):
        path = fonts.resolve(stack, script)
        family = next((f for f in fonts.parse_stack(stack) if fonts.find(f) == path), None)
        total += _advance(run, path, family)
        if script not in seen:
            seen.append(script)
    total += tracking * len(text)  # CSS letter-spacing is in em and applies per character
    return Measurement(width_em=total, width=total * size, scripts=tuple(seen))


def measure_spec(text: str, spec: TypeSpec, typography: Typography) -> Measurement:
    stack = typography.families.get(spec.family, spec.family)
    return measure(text, stack, spec.size, spec.track)


# ------------------------------------------------------------------------ wrapping


#: Scripts that break between characters rather than at spaces.
CHARACTER_BREAKING = {"han", "kana", "hangul"}

#: Never start a line with these; never end one with an opening bracket. A minimal kinsoku
#: rule set — enough that CJK line counts match a real renderer's.
NO_LINE_START = "、。，．！？；：）」』】〉》”’%,.!?;:)]}"
NO_LINE_END = "（「『【〈《“‘([{"


def _atoms(text: str) -> list[str]:
    """Split into the smallest units a line break may fall between.

    Latin breaks after whitespace; CJK breaks between characters, subject to the kinsoku
    rules above. Getting this wrong changes the line count, which changes whether a slot
    overflows — so it is shared with the fidelity harness rather than reimplemented there.
    """
    out: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            out.append(buf)
            buf = ""

    for ch in text:
        if fonts.script_of(ch) in CHARACTER_BREAKING:
            flush()
            # Glue rather than break: a closing mark may not open a line, and an opening
            # bracket may not close one.
            if out and (ch in NO_LINE_START or out[-1][-1:] in NO_LINE_END):
                out[-1] += ch
            else:
                out.append(ch)
        elif ch.isspace():
            buf += ch
            flush()
        elif ch in NO_LINE_START and out and not buf:
            out[-1] += ch
        else:
            buf += ch
    flush()
    return out


def _longest_prefix(text: str, stack: str, size: float, tracking: float, width: float) -> int:
    """How many characters of `text` fit in `width`. Binary search over the measurer."""
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid], stack, size, tracking).width <= width:
            lo = mid
        else:
            hi = mid - 1
    return lo


def wrap(
    text: str, stack: str, size: float, width: float, tracking: float = 0.0
) -> list[str]:
    """Greedy line breaking at the measured width. Returns the lines.

    `size` and `width` must be in the same unit — both canvas px, or both points.
    """
    if width <= 0:
        return [text] if text else []
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for atom in _atoms(paragraph):
            candidate = current + atom
            too_wide = measure(candidate.rstrip(), stack, size, tracking).width > width
            if current and too_wide:
                lines.append(current.rstrip())
                current = atom.lstrip() if atom.strip() else ""
            else:
                current = candidate
            # A single token wider than the line breaks inside itself. Both Chromium
            # (`overflow-wrap: break-word`) and PowerPoint (`wrap="square"`) do this;
            # refusing to would undercount lines on identifiers and URLs — the strings most
            # likely to be too long in the first place.
            while measure(current, stack, size, tracking).width > width and len(current) > 1:
                cut = _longest_prefix(current, stack, size, tracking, width)
                lines.append(current[:cut])
                current = current[cut:]
        lines.append(current.rstrip())
    return lines


def line_count(text: str, spec: TypeSpec, typography: Typography, width_px: float) -> int:
    stack = typography.families.get(spec.family, spec.family)
    return len(wrap(text, stack, spec.size, width_px, spec.track))


def height_px(text: str, spec: TypeSpec, typography: Typography, width_px: float) -> float:
    """Rendered height at absolute line spacing, in canvas px.

    `spec.line` is an absolute length, not a ratio, for the reason in
    `spcPct` resolves against font ascent/descent and will not match
    CSS `line-height`.
    """
    return line_count(text, spec, typography, width_px) * spec.line
