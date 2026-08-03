"""Session — what a conversation holds.

One deck, one store, one lock. The session is where the tool layer, the render layer, and
the export layer meet, and it is deliberately thin: it owns no policy of its own, it just
gives tools a place to stand.
"""

from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path
from typing import Any

from ..io.import_pptx import import_pptx
from ..render import budget, expand
from ..state import richtext
from ..state.document import Author, Deck, Mode, Shape, Slide, Theme
from ..state.store import DeckStore
from ..state.theme_default import default_theme, validate_theme


def _flatten(value: Any) -> str:
    """Slot content as searchable text, matching how it renders.

    Markup comes off: a search for "quarterly" should find a slide that bolds the word, and
    a snippet reading `**Quarterly**` describes the storage rather than the slide.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return richtext.plain(value)
    if isinstance(value, list):
        return "\n".join(_flatten(v) for v in value)
    if isinstance(value, dict):
        return " ".join(richtext.plain(str(v))
                        for v in value.values() if isinstance(v, (str, int, float)))
    return richtext.plain(str(value))


def _snippets(target: str, text: str, needle: str, width: int = 44) -> list[dict[str, str]]:
    """Every occurrence, with enough surrounding text to recognise it."""
    lowered = text.lower()
    out, start = [], 0
    while True:
        at = lowered.find(needle, start)
        if at < 0:
            return out
        left = max(0, at - width // 2)
        right = min(len(text), at + len(needle) + width // 2)
        out.append({
            "target": target,
            "snippet": ("…" if left else "") + text[left:right].replace("\n", " ")
                       + ("…" if right < len(text) else ""),
        })
        start = at + len(needle)


def _overflow_px(result, max_lines: int, line: float) -> float:
    """How far past its budget a slot is, in canvas px.

    Two things can be over: the *height*, when the text takes more lines than the slot has,
    and the *width capacity*, when it needs more advance width than the box provides. They
    are not the same, and reporting only the first let a slot come back `fits: False` on a
    slide reported `clean: True` — a contradiction the repair ladder walked straight into.

    A slot that does not fit is over by at least one line, whichever measure caught it.
    """
    lines_over = max(0.0, result.lines - max_lines) * line
    if lines_over:
        return lines_over
    return line if not result.ok else 0.0


class Session:
    def __init__(self, store: DeckStore, *, vision: bool = False,
                 freeze_geometry: bool = False) -> None:
        self.store = store
        #: Set by `resume`. `None` means edits live only in memory, which is right for a
        #: one-shot CLI call and wrong for anything with a person waiting on it.
        self.workspace: Any = None
        #: Datasets opened this session, by name. Held here rather than on the deck: the
        #: numbers are what the deck is *about*, not part of it, and they must not travel
        #: into the exported file.
        self.datasets: dict[str, Any] = {}
        #: What this person has been observed to prefer (DESIGN §8.2). Loaded lazily so a
        #: test or a one-shot call never touches the profile on disk.
        self._preferences: Any = None
        #: Editorial findings already handed out this session, by key. Repetition is how an
        #: advisory channel gets switched off, so a finding is offered once and then stays
        #: quiet — held here rather than on the deck because it is a fact about the
        #: conversation, and a reopened deck deserves to be told again.
        self.raised: set[str] = set()
        self.vision = vision
        #: Lay slides out in a real browser and export those numbers. Off by default: it
        #: costs ~200ms a slide and a Chromium install, and the analytic pass agrees with it
        #: on every box of the fixture corpus.
        self.freeze_geometry = freeze_geometry
        self._frozen: dict[str, Any] = {}
        problems = validate_theme(store.deck.theme)
        # Validated once, here. A theme that passes means managed slides cannot fail
        # contrast, which is what lets per-slide lint skip the check entirely (DESIGN §2).
        self.theme_problems = problems

    # -- construction -----------------------------------------------------------

    @classmethod
    def open(cls, path: Path | str, **kw: Any) -> Session:
        return cls(import_pptx(path), **kw)

    @classmethod
    def resume(cls, path: Path | str, *, root: Path | None = None, fresh: bool = False,
               **kw: Any) -> tuple[Session, dict[str, Any]]:
        """Open a deck, picking up any work a previous process left behind.

        Returns the session *and* what happened, because "resumed 6 turns from a backup
        snapshot" and "opened the file" are different situations for the person at the
        keyboard, and only the caller knows how to say so.

        The workspace is keyed by deck id, which import derives from the file, so reopening
        the same deck finds the same history. `fresh=True` throws that history away and
        starts from the file as it is on disk.
        """
        from ..state.workspace import Workspace

        store = import_pptx(path)
        workspace = Workspace.for_deck(store.deck.id, root)
        report: dict[str, Any] = {"resumed": False, "turns": 0, "degraded": False}

        if fresh:
            workspace.discard()
        elif workspace.exists():
            try:
                store, report = workspace.restore(path)
            except Exception as exc:
                # The source file is always a valid fallback. Losing the resumed edits is
                # bad; refusing to open the deck at all is worse.
                report = {"resumed": False, "turns": 0, "degraded": True,
                          "note": f"could not resume ({exc}); opened the source file"}
                store = import_pptx(path)

        session = cls(store, **kw)
        session.workspace = workspace
        workspace.attach(store)
        return session, report

    @classmethod
    def blank(cls, title: str = "Untitled", theme: Theme | None = None,
              theme_from: str | None = None, **kw: Any) -> Session:
        deck = Deck(id=uuid.uuid4().hex[:8], title=title,
                    theme=theme or default_theme(), theme_from=theme_from)
        return cls(DeckStore(deck), **kw)

    @classmethod
    def from_builtin(cls, name: str, title: str = "Untitled", **kw: Any) -> Session:
        """A new deck on one of the themes that ship with the harness.

        The sibling of `from_template` for the case with nothing to borrow from. It states
        every value rather than extracting some and inferring the rest, so `theme.inferred`
        comes back empty and `get_theme` has nothing to ask the user to correct.
        """
        from ..state import templates

        return cls.blank(title, theme=templates.load(name),
                         theme_from=f"{name} (built in)", **kw)

    @classmethod
    def from_template(cls, template: Path | str, title: str = "Untitled",
                      **kw: Any) -> Session:
        """A new deck wearing another deck's theme, and none of its slides.

        The way most decks actually start: nobody opens a blank canvas, they open the company
        template. `new --from` has done this offline since v0 — it belongs here so the chat
        client can do it too, because writing the slides is the part that needs a model and
        `new` deliberately has none.

        Nothing is copied. The palette, the type scale and the grid come across; the source
        file is never opened again, so there is no package to patch and no imported slide to
        inherit. That is why the result is all-managed and every component is available on it.
        """
        from ..io.theme_extract import extract_theme

        template = Path(template)
        return cls.blank(title, theme=extract_theme(template), theme_from=template.name, **kw)

    # -- preferences ------------------------------------------------------------

    @property
    def preferences(self) -> Any:
        """The profile, kept current with the op log.

        Observed on read rather than on write: a correction is only visible as a *pair* of
        ops, so the cheapest correct moment to notice one is when somebody asks. The log is
        the record; this is a view over it.
        """
        from .preferences import PreferenceProfile

        if self._preferences is None:
            self._preferences = PreferenceProfile.load(self._preferences_path())
        self._preferences.observe(self.store.log)
        return self._preferences

    def _preferences_path(self) -> Path | None:
        if self.workspace is not None:
            return Path(self.workspace.root) / "preferences.json"
        return None

    def remember_preferences(self) -> None:
        if self._preferences is not None:
            self._preferences.save(self._preferences_path())

    # -- convenience ------------------------------------------------------------

    @property
    def deck(self) -> Deck:
        return self.store.deck

    @property
    def theme(self) -> Theme:
        return self.store.deck.theme

    def slide(self, slide_id: str) -> Slide:
        return self.store.slide(slide_id)

    def slide_size_emu(self) -> tuple[int, int]:
        return expand.slide_size_emu(self.theme)

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:6]}"

    # -- assets -----------------------------------------------------------------

    @property
    def assets(self) -> dict[str, tuple[str, bytes]]:
        return self.store.assets

    def asset_data_uri(self, key: str) -> str | None:
        """An image inlined into the markup.

        Needed wherever the HTML has to stand alone — the measurement pass loads it with
        `set_content`, so there is no origin for a relative URL to resolve against.
        """
        found = self.assets.get(key)
        if found is None:
            return None
        content_type, blob = found
        return f"data:{content_type};base64,{base64.b64encode(blob).decode()}"

    def render_html(self, slide_id: str, *, inline_assets: bool = True,
                    asset_url: str = "/api/asset", for_display: bool = True) -> str:
        """One slide as HTML, with its pictures.

        `inline_assets=False` points at an endpoint instead, which keeps the markup small
        for a page that is re-fetched on every edit.
        """
        from ..render import html as renderer

        slide = self.slide(slide_id)
        cx, cy = self.slide_size_emu()
        if inline_assets:
            src = self.asset_data_uri
        else:
            def src(key: str) -> str | None:
                return f"{asset_url}/{key}" if key in self.assets else None
        return renderer.render_slide(self.theme, slide, cx, cy, asset_src=src,
                                     compensate_autofit=for_display).html

    # -- search -----------------------------------------------------------------

    def search(self, query: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Find text across the deck, one entry per matching slide.

        Searches the editable text of every shape and slot, plus speaker notes. Inherited
        layout art is excluded: a logo caption on the master would otherwise match on every
        slide and drown the answers the user asked for.

        Case-insensitive substring, not a ranked index. A deck is tens of slides, so the
        useful thing is *where* a phrase occurs, not which occurrence scores highest.
        """
        needle = query.strip().lower()
        if not needle:
            return []

        found: list[dict[str, Any]] = []
        for slide in self.deck.slides:
            hits: list[dict[str, str]] = []

            for shape in slide.shapes:
                if shape.text:
                    hits += _snippets(f"{slide.id}/{shape.id}", shape.text, needle)
            for block in slide.blocks:
                for name, value in block.slots.items():
                    hits += _snippets(f"{slide.id}/{block.id}/{name}",
                                      _flatten(value), needle)
            if slide.notes:
                hits += _snippets(f"{slide.id}/notes", slide.notes, needle)

            if hits:
                found.append({"slide": slide.id, "index": slide.index,
                              "hits": hits[:limit]})
        return found

    # -- budgets ----------------------------------------------------------------

    def budget_for(self, target: str) -> budget.Budget:
        """The budget guarding one text target, whichever mode it lives in."""
        slide, obj, slot = self.store.resolve_text_target(target)
        if slot is None:
            assert isinstance(obj, Shape)
            cx, cy = self.slide_size_emu()
            return budget.for_shape(self.theme, obj, cx, cy)
        for laid_out in expand.expand_slide(self.theme, slide):
            if laid_out.block_id == obj.id and laid_out.slot == slot:
                b = budget.for_slot(self.theme, laid_out)
                return budget.Budget(
                    target=b.target,
                    capacity_em=b.capacity_em,
                    max_lines=b.max_lines,
                    width_px=b.width_px,
                    spec=b.spec,
                    stack=b.stack,
                    context={**b.context, "component": obj.component,
                             "variant": obj.variant, "region": obj.region},
                )
        raise KeyError(f"slot {target!r} has no geometry; is it filled?")

    # -- measurement ------------------------------------------------------------

    def measure_slide(self, slide_id: str, *, freeze: bool | None = None) -> dict[str, Any]:
        """The render half of every mutating tool's result.

        Measurement, not a picture. Overflow and capacity are numbers; a model does not
        need to look at anything to act on them.

        Analytic by default: real font metrics and the expander's boxes, ~1ms, no
        renderer involved. That matters because the model's loop runs on this and must keep
        working where no Office or browser is installed.

        `freeze=True` additionally lays the slide out in headless Chrome and reports where
        the two disagree. Not a runtime dependency — a check. A persistent disagreement
        means the line breaker has drifted from what a real engine does, which is the drift
        that later shows up as "looked fine in preview, overflowed in PowerPoint".
        """
        slide = self.slide(slide_id)
        analytic = (self._measure_managed(slide) if slide.mode is Mode.MANAGED
                    else self._measure_freeform(slide))

        want = self.freeze_geometry if freeze is None else freeze
        if not want:
            return analytic

        from ..render import browser

        try:
            cx, cy = self.slide_size_emu()
            frozen = browser.freeze(self.theme, slide, cx, cy,
                                    asset_src=self.asset_data_uri)
        except browser.BrowserUnavailable as exc:
            # Say so rather than silently reporting analytic numbers as frozen geometry.
            analytic["frozen"] = False
            analytic["freeze_error"] = str(exc)
            return analytic

        self._frozen[slide_id] = frozen
        result = frozen.as_result()
        result["frozen"] = True
        result["analytic_overflow_px"] = analytic["overflow_px"]
        disagreements = browser.compare_with_analytic(frozen, analytic)
        if disagreements:
            result["measurer_disagreement"] = disagreements
        return result

    def frozen(self, slide_id: str):
        """The last frozen geometry for a slide, if the browser has laid it out."""
        return self._frozen.get(slide_id)

    def _measure_managed(self, slide: Slide) -> dict[str, Any]:
        slots = []
        overflow = 0.0
        for laid_out in expand.expand_slide(self.theme, slide):
            block = slide.block(laid_out.block_id)
            if block is None:
                continue
            b = budget.for_slot(self.theme, laid_out)
            result = budget.check_value(block.slots.get(laid_out.slot), b, self.theme)
            over = _overflow_px(result, b.max_lines, laid_out.spec.line)
            overflow += over
            slots.append({
                "target": f"{slide.id}/{laid_out.block_id}/{laid_out.slot}",
                "box": [round(laid_out.box.x), round(laid_out.box.y),
                        round(laid_out.box.w), round(laid_out.box.h)],
                "lines": result.lines,
                "max_lines": b.max_lines,
                "used_em": round(result.used_em, 1),
                "capacity_em": round(b.capacity_em, 1),
                "fits": result.ok,
            })
        return {"slide": slide.id, "mode": "managed", "slots": slots,
                "overflow_px": round(overflow, 1),
                # Derived from the per-slot verdicts, not from the total. A slide is clean
                # when every slot fits, and nothing else.
                "clean": all(s["fits"] for s in slots)}

    def _measure_freeform(self, slide: Slide) -> dict[str, Any]:
        cx, cy = self.slide_size_emu()
        canvas_w, canvas_h = self.theme.grid.canvas
        shapes, overflow = [], 0.0
        for shape in slide.shapes:
            if shape.opaque or not shape.text:
                continue
            b = budget.for_shape(self.theme, shape, cx, cy)
            result = budget.check(shape.text, b, self.theme)
            over = _overflow_px(result, b.max_lines, b.spec.line)
            overflow += over
            entry = {
                "target": f"{slide.id}/{shape.id}",
                # Canvas px, the same coordinate system the expander and the overlay use.
                "box": [round(shape.frame.x / cx * canvas_w),
                        round(shape.frame.y / cy * canvas_h),
                        round(shape.frame.cx / cx * canvas_w),
                        round(shape.frame.cy / cy * canvas_h)],
                "role": shape.role,
                "lines": result.lines,
                "max_lines": b.max_lines,
                "fits": result.ok,
                "overflow_px": round(over, 1),
            }
            if not result.ok and shape.autofit_scale is not None:
                # Say why a slide that "looks fine in PowerPoint" measures as overflowing:
                # the source file is shrinking the text to hide it.
                entry["source_autofit"] = round(shape.autofit_scale, 3)
                entry["note"] = (
                    f"the original file shrinks this text to "
                    f"{shape.autofit_scale:.0%} via normAutofit; it does not fit at its "
                    "declared size"
                )
            shapes.append(entry)
        return {"slide": slide.id, "mode": "freeform", "shapes": shapes,
                "overflow_px": round(overflow, 1),
                "clean": all(s["fits"] for s in shapes)}

    # -- outline ----------------------------------------------------------------

    def outline(self) -> dict[str, Any]:
        """Level 1 of the context pyramid — always on, so it must stay small."""
        slides = []
        for slide in self.deck.slides:
            entry: dict[str, Any] = {"id": slide.id, "index": slide.index,
                                     "mode": slide.mode.value}
            if slide.mode is Mode.MANAGED:
                entry["layout"] = slide.layout
                entry["blocks"] = [f"{b.region}:{b.component}" for b in slide.blocks]
                entry["gist"] = richtext.plain(next(
                    (v for b in slide.blocks for v in [b.slots.get("title")] if isinstance(v, str)),
                    "",
                ))
            else:
                entry["shapes"] = len(slide.shapes)
                entry["gist"] = _gist(slide)
            slides.append(entry)
        return {
            "deck": self.deck.title,
            "theme": self.theme.id,
            "slides": slides,
            "source": self.deck.source_path,
            "template": self.deck.theme_from,
        }

    # -- writing ----------------------------------------------------------------

    def transaction(self, author: Author = Author.MODEL):
        return self.store.transaction(author)


#: Text that identifies a slide's furniture rather than its content. Slide numbers and
#: auto-dates sit early in the shape list, so "first shape with text" reliably picks them.
_FURNITURE = re.compile(r"^\s*(\d{1,3}|[\d/.\-]{6,10}|page \d+)\s*$", re.IGNORECASE)


def _gist(slide: Slide) -> str:
    """A one-line summary of a freeform slide for the always-on outline.

    Prefer a real title placeholder; otherwise the longest line of body-length text, which
    beats "first shape with text" on decks whose date and slide-number placeholders come
    first in z-order.
    """
    titled = [s.text for s in slide.shapes
              if s.text and s.role in ("slide_title", "deck_title")]
    if titled:
        return titled[0][:80]
    candidates = [s.text.strip() for s in slide.shapes
                  if s.text and s.text.strip() and not _FURNITURE.match(s.text.strip())]
    if not candidates:
        return ""
    return max(candidates, key=lambda t: min(len(t), 90))[:80]
