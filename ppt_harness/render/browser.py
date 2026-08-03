"""Browser measurement and freeze-geometry — DESIGN §6.1, ARCHITECTURE "Load-bearing".

A second opinion on layout, and the reason to have one.

This lays a slide out in headless Chrome and reads back the **computed** rects. It is not
in the request path: previews come from a real renderer (`render/preview.py`) and budgets
from real font metrics (`render/measure.py`). What it provides is disagreement — and
disagreement is how measurement bugs are found at all.

It earned its place. Three silent defects surfaced within minutes of having a second engine
to compare against: box widths converted to points while text was measured in canvas px,
over-long tokens never broken, and imported text sized from the theme instead of the file.
Each made the harness confidently wrong rather than visibly broken, and a single
self-consistent measurer could not have caught any of them.

Optional by design — Playwright plus a Chromium download is a real cost, and nothing a user
does depends on it.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from ..state.document import Slide, Theme
from .html import PROBE_ATTR, render_slide


class BrowserUnavailable(RuntimeError):
    """Playwright or its Chromium build is not installed."""


@dataclass(frozen=True)
class FrozenBox:
    """One slot's geometry as the browser actually laid it out, in canvas px."""

    target: str
    x: float
    y: float
    w: float
    h: float
    content_h: float
    lines: int
    max_lines: int | None = None

    @property
    def overflow_px(self) -> float:
        """How far the text exceeds its box. Positive means it will clip in PowerPoint."""
        return max(0.0, self.content_h - self.h)

    @property
    def fits(self) -> bool:
        return self.overflow_px <= 0.5  # sub-pixel slack; anything more is real


@dataclass
class FrozenSlide:
    slide_id: str
    canvas: tuple[int, int]
    boxes: list[FrozenBox] = field(default_factory=list)
    screenshot: bytes | None = None

    @property
    def overflow_px(self) -> float:
        return sum(b.overflow_px for b in self.boxes)

    @property
    def clean(self) -> bool:
        return all(b.fits for b in self.boxes)

    def as_result(self) -> dict[str, Any]:
        return {
            "slide": self.slide_id,
            "source": "browser",
            "canvas": list(self.canvas),
            "boxes": [
                {"target": b.target, "box": [round(b.x, 1), round(b.y, 1),
                                             round(b.w, 1), round(b.h, 1)],
                 "lines": b.lines, "content_h": round(b.content_h, 1),
                 "overflow_px": round(b.overflow_px, 1), "fits": b.fits}
                for b in self.boxes
            ],
            "overflow_px": round(self.overflow_px, 1),
            "clean": self.clean,
        }


#: Read back the laid-out truth. `scrollHeight` exceeds `clientHeight` exactly when the text
#: does not fit, and dividing by the resolved line-height gives the real line count — the
#: browser's, not our line breaker's guess at it.
_PROBE = f"""
() => {{
  const out = [];
  for (const el of document.querySelectorAll('[{PROBE_ATTR}]')) {{
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.2;
    // Measure the inner auto-height element, never the fixed-height slot: scrollHeight on
    // a fixed box is clamped to that box and reports every slot as exactly full.
    const ink = el.querySelector('.ink') || el;
    const ih = ink.getBoundingClientRect().height;
    out.push({{
      target: el.getAttribute('{PROBE_ATTR}'),
      x: r.left, y: r.top, w: r.width, h: r.height,
      content_h: ih,
      lines: Math.max(1, Math.round(ih / lh)),
      max_lines: el.dataset.maxLines ? parseInt(el.dataset.maxLines, 10) : null,
    }});
  }}
  return out;
}}
"""


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


@contextmanager
def _page(canvas: tuple[int, int]):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "playwright is not installed. `pip install 'ppt-harness[render]'` then "
            "`playwright install chromium`. Measurement falls back to render/measure.py "
            "until then."
        ) from exc

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(args=["--force-color-profile=srgb",
                                               "--font-render-hinting=none"])
        except Exception as exc:
            raise BrowserUnavailable(f"could not launch chromium: {exc}") from exc
        try:
            # deviceScaleFactor 1 keeps CSS px equal to canvas px, so nothing has to be
            # divided back out before it becomes EMU.
            page = browser.new_page(viewport={"width": canvas[0], "height": canvas[1]},
                                    device_scale_factor=1)
            yield page
        finally:
            browser.close()


def freeze(theme: Theme, slide: Slide, cx: int, cy: int, *,
           screenshot: bool = False, asset_src=None) -> FrozenSlide:
    """Lay the slide out in a browser and read back the numbers that will be exported."""
    rendered = render_slide(theme, slide, cx, cy, asset_src=asset_src)
    with _page(rendered.canvas) as page:
        page.set_content(rendered.html, wait_until="load")
        # Web fonts are not used — everything resolves to installed faces — but layout still
        # settles a frame late on first paint.
        page.wait_for_timeout(60)
        probed = page.evaluate(_PROBE)
        shot = page.locator("#slide").screenshot(type="png") if screenshot else None

    return FrozenSlide(
        slide_id=slide.id,
        canvas=rendered.canvas,
        boxes=[FrozenBox(**{k: raw[k] for k in
                            ("target", "x", "y", "w", "h", "content_h", "lines", "max_lines")})
               for raw in probed],
        screenshot=shot,
    )


def screenshot_slide(theme: Theme, slide: Slide, cx: int, cy: int,
                     width: int | None = None, asset_src=None,
                     compensate_autofit: bool = True) -> bytes:
    """A PNG of one slide.

    `width` downscales for a model: DESIGN §10 puts model-facing renders at ~800px (~480
    tokens), with full resolution reserved for the canvas a person looks at.
    """
    # A screenshot is for looking at, so it shows what PowerPoint shows. `freeze` above
    # deliberately does not — it measures the declared size, which is what gets exported.
    rendered = render_slide(theme, slide, cx, cy, asset_src=asset_src,
                            compensate_autofit=compensate_autofit)
    scale = (width / rendered.canvas[0]) if width else 1.0
    with _page(rendered.canvas) as page:
        page.set_content(rendered.html, wait_until="load")
        page.wait_for_timeout(60)
        if scale != 1.0:
            page.evaluate(
                "s => { const el = document.getElementById('slide');"
                "el.style.transform = `scale(${s})`;"
                "document.body.style.width = `${el.offsetWidth * s}px`;"
                "document.body.style.height = `${el.offsetHeight * s}px`; }",
                scale,
            )
            page.set_viewport_size({"width": max(1, round(rendered.canvas[0] * scale)),
                                    "height": max(1, round(rendered.canvas[1] * scale))})
            page.wait_for_timeout(30)
            return page.screenshot(type="png")
        return page.locator("#slide").screenshot(type="png")


def compare_with_analytic(frozen: FrozenSlide, analytic: dict[str, Any]) -> list[str]:
    """Where the two measurers disagree.

    Not a test failure — a signal. A persistent disagreement means `render/measure.py`'s
    line breaker has drifted from what a real engine does, which is exactly the drift that
    later shows up as "looked fine in preview, overflowed in PowerPoint".
    """
    by_target = {b["target"]: b for b in
                 (analytic.get("slots") or analytic.get("shapes") or [])}
    notes = []
    for box in frozen.boxes:
        other = by_target.get(box.target)
        if other is None:
            continue
        if other.get("lines") != box.lines:
            notes.append(f"{box.target}: analytic {other['lines']} lines, browser {box.lines}")
    return notes


__all__ = ["BrowserUnavailable", "FrozenBox", "FrozenSlide", "available",
           "compare_with_analytic", "freeze", "screenshot_slide"]