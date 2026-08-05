"""PPTEval — what a model thinks the slides look like, and what a program can check without one.

`metrics.py` measures whether a deck is *sound*: text fits, the package survived, nothing was
refused that should not have been. It says at the top that none of it says the deck is any
good. This file is the other half of that sentence, and it is built to be distrusted:

**Describe, then score, in two calls.** A vision model writes a neutral description of a
rendered slide; a separate text-only call scores that description against `rubrics`. The
scorer never sees the image. That decoupling is not decoration — it is what the PPTAgent paper
(EMNLP 2025) validated against human raters, at r=0.90 for design and 0.70 for content, and
one call asked to look and judge does not reproduce it.

**Descriptions are cached on disk, scores are not.** The description is the expensive half and
is a pure function of (image bytes, prompt, model), so it is keyed on exactly those three and
survives the process. Re-scoring an unchanged deck after a rubric edit then costs a handful of
text tokens instead of a vision pass over every slide.

**The image scored is the real file.** Slides are rendered through `render/preview`, which
exports through the ordinary exporter and rasterises what a real renderer made of it —
DESIGN §6.1, "the preview *is* the export, rendered". Nothing here draws its own approximation
of a slide and then scores the approximation.

**A missing score is never a low score.** No vision model, no LibreOffice, an unparseable
reply: each produces `score=None` with the reason attached, and every mean carries the count
of slides it is actually over. The one thing a quality metric must never do is average a
failure to measure into the measurement.

`verify` is the free half: page count, aspect ratio and output script, read straight off the
exported `.pptx`. No model, no network, deterministic — and it catches the embarrassments a
judge scores 4/5 without noticing, like a deck that came out 4:3, or in the wrong writing
system, or one slide long.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.session import Session
from ..render.preview import PreviewCache, PreviewUnavailable, cache_root
from . import rubrics
from .rubrics import Rubric
from .vision import Judge, VisionUnavailable

#: Anything that changes what a description *is* goes in the cache key. The rubric version
#: covers the prompt text; this covers how this module frames the call around it.
PROMPT_VERSION = f"v{rubrics.VERSION}"

#: 16:9. `tools/deck.SLIDE_SIZES` calls it the default, and a deck that quietly came out 4:3
#: is the classic silent export failure.
WIDESCREEN = 16 / 9
ASPECT_TOLERANCE = 0.1

#: Enough characters for a dominant-script reading to mean anything. Below it a deck is
#: numbers and acronyms, which have no script worth reporting.
SCRIPT_MIN_CHARS = 20

_SLIDE_PART = re.compile(r"^ppt/slides/slide\d+\.xml$")
_TEXT = re.compile(r"<a:t>(.*?)</a:t>", re.DOTALL)
_JSON = re.compile(r"\{.*?\}", re.DOTALL)
#: "score: 4", "Score = 5", "**Score:** 3" — the shapes a model reaches for when it ignores
#: the JSON instruction. Deliberately anchored on the word: a bare digit in prose is not a
#: score, and reading one out of "the rule of 3" is how a judge invents a measurement.
_LOOSE = re.compile(r"score\W{0,6}([1-5])\b", re.IGNORECASE)


# ------------------------------------------------------------------------ the judged half


@dataclass
class AxisScore:
    """One axis on one slide. `score is None` means *not measured*, and says why.

    Kept distinct from a low score throughout. A deck nobody could render and a deck rendered
    and found plain are opposite findings; collapsing them onto the same `0` is the failure
    mode this whole file is arranged around.
    """

    axis: str
    score: int | None = None
    reason: str = ""
    description: str = ""
    error: str = ""
    cached: bool = False
    """The description came off disk — no vision call was made for this axis on this slide."""

    @property
    def measured(self) -> bool:
        return self.score is not None

    def as_dict(self) -> dict[str, Any]:
        return {"axis": self.axis, "score": self.score, "reason": self.reason,
                "error": self.error, "cached": self.cached,
                "description": self.description}


@dataclass
class SlideQuality:
    slide_id: str
    index: int
    axes: dict[str, AxisScore] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"slide_id": self.slide_id, "index": self.index,
                "axes": {k: v.as_dict() for k, v in self.axes.items()}}


@dataclass
class DeckQuality:
    """Every slide's scores, plus what could not be scored and why.

    `means` is over the slides that produced a score, and reports the denominator beside it.
    A mean over "the slides that worked" without saying how many worked is the same lie as a
    fit rate without its slide count — see `report.py`, rule one.
    """

    slides: list[SlideQuality] = field(default_factory=list)
    judge: dict[str, Any] = field(default_factory=dict)
    structure: dict[str, int] = field(default_factory=dict)
    verification: Verification | None = None
    unmeasured: list[str] = field(default_factory=list)
    """One line per slide-axis that produced no score, each naming its cause."""

    def scored(self, axis: str) -> list[int]:
        return [s.axes[axis].score for s in self.slides
                if axis in s.axes and s.axes[axis].score is not None]

    def mean(self, axis: str) -> float | None:
        """`None`, never `0.0`, when nothing on this axis was measured."""
        values = self.scored(axis)
        return round(sum(values) / len(values), 2) if values else None

    def as_dict(self) -> dict[str, Any]:
        axes = sorted({a for s in self.slides for a in s.axes})
        return {
            "means": {a: self.mean(a) for a in axes},
            "scored_slides": {a: [len(self.scored(a)), len(self.slides)] for a in axes},
            "human_correlation": {a: rubrics.RUBRICS[a].human_r for a in axes
                                  if a in rubrics.RUBRICS},
            "slides": [s.as_dict() for s in self.slides],
            "judge": self.judge,
            "structure": self.structure,
            "coherence": {"scored": False, "why": rubrics.WHY_NO_COHERENCE},
            "verification": self.verification.as_dict() if self.verification else None,
            "unmeasured": self.unmeasured,
        }


def _cache_path(root: Path, png: bytes, rubric: Rubric, model: str) -> Path:
    """Keyed on the image, the prompt and the model — the three things a description answers.

    The image hash rather than the slide id: a slide edited into a different picture is a
    different question, and keying on the id would serve the answer to the old one. Which is
    also why nothing has to remember to invalidate this.
    """
    key = hashlib.sha256(
        png + b"\0" + rubric.describe.encode() + b"\0"
        + f"{model}\0{PROMPT_VERSION}".encode()
    ).hexdigest()[:32]
    return root / f"{rubric.axis}-{key}.json"


def describe(judge: Judge, png: bytes, rubric: Rubric, cache: Path) -> tuple[str, bool]:
    """The vision half. Returns (description, came_from_cache)."""
    path = _cache_path(cache, png, rubric, judge.describer.model)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))["description"], True
        except (OSError, ValueError, KeyError):
            # A truncated cache entry is debris, not a fatal error: re-describe and overwrite.
            path.unlink(missing_ok=True)
    text = judge.describe(png, rubric.describe, rubrics.DESCRIBE_SYSTEM)
    cache.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"axis": rubric.axis, "model": judge.describer.model,
                                "prompt_version": PROMPT_VERSION, "description": text},
                               indent=1), encoding="utf-8")
    return text, False


def parse_score(reply: str) -> tuple[int | None, str, str]:
    """(score, reason, error) from whatever the scorer actually said.

    Lenient about *shape* — fenced JSON, prose around it, a bare "Score: 4" — and strict about
    substance: a number outside 1-5, or none at all, yields `None` and the reply that could
    not be read. There is no default and no midpoint, because a rubric level is a claim about
    the slide and inventing one is worse than admitting the call failed.
    """
    text = (reply or "").strip()
    for match in _JSON.finditer(text):
        try:
            blob = json.loads(match.group(0))
        except ValueError:
            continue
        if not isinstance(blob, dict) or "score" not in blob:
            continue
        raw = blob.get("score")
        reason = str(blob.get("reason") or "").strip()
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            return None, reason, f"score was not a number: {raw!r}"
        if not 1 <= value <= 5:
            return None, reason, f"score {value} is outside the 1-5 rubric"
        return value, reason, ""

    loose = _LOOSE.search(text)
    if loose:
        return int(loose.group(1)), text[:200], ""
    return None, "", ("the scorer's reply carried no 1-5 score: "
                      + (text[:200] or "<empty reply>"))


def score_axis(judge: Judge, rubric: Rubric, png: bytes, cache: Path) -> AxisScore:
    """One axis on one slide: describe (or reuse a description), then score it, separately."""
    out = AxisScore(axis=rubric.axis)
    description, out.cached = describe(judge, png, rubric, cache)
    out.description = description
    if not description.strip():
        out.error = "the vision model returned an empty description"
        return out
    reply = judge.score(rubric.score_prompt(description), rubrics.SCORE_SYSTEM)
    out.score, out.reason, out.error = parse_score(reply)
    return out


def measure_quality(session: Session, judge: Judge, *,
                    axes: tuple[str, ...] = ("content", "design"),
                    width: int = 1280, cache: Path | None = None,
                    preview: Any = None) -> DeckQuality:
    """Score every slide in the deck. Raises only where *nothing* is measurable.

    `preview` is any object with `page(slide_id, width)` — `PreviewCache` in production, a
    stub in the tests, which is how this suite scores slides on a machine with no LibreOffice
    without ever pretending to have scored one.

    `PreviewUnavailable` propagates rather than being folded into a per-slide error: a missing
    renderer is missing for all twelve slides, and twelve identical failures dressed as
    results read as a deck that scored badly. The caller reports it as the one fact it is.
    """
    preview = preview or PreviewCache(session)
    cache = cache or (cache_root() / "ppteval")
    chosen = [rubrics.RUBRICS[a] for a in axes]
    out = DeckQuality(structure=structure(session))

    for index, slide in enumerate(session.deck.slides):
        record = SlideQuality(slide_id=slide.id, index=index)
        out.slides.append(record)
        page = preview.page(slide.id, width=width)
        for rubric in chosen:
            try:
                record.axes[rubric.axis] = score_axis(judge, rubric, page.png, cache)
            except VisionUnavailable:
                raise
            except Exception as exc:   # one endpoint hiccup must not lose the whole run
                record.axes[rubric.axis] = AxisScore(
                    axis=rubric.axis, error=f"{type(exc).__name__}: {exc}")
        for axis, result in record.axes.items():
            if not result.measured:
                out.unmeasured.append(f"{slide.id} {axis}: {result.error}")

    out.judge = judge.as_dict()
    return out


def structure(session: Session) -> dict[str, int]:
    """The deck-level findings from `core/review.py`, counted by rule.

    This is what stands in for PPTEval's coherence axis — see `rubrics.WHY_NO_COHERENCE`. It
    is a narrower claim than the paper's and an honest one: `weak_close` and `duplicate_title`
    are decided by counting, cost nothing, and cannot be wrong about themselves. Only the deck
    rules, because the per-slide ones are already reported by `metrics.measure_shape`.
    """
    from ..core import review

    reads = [review.read(s) for s in session.deck.slides if not s.hidden]
    out: dict[str, int] = {}
    for rule in review.DECK_RULES:
        for finding in rule(reads):
            out[finding.rule] = out.get(finding.rule, 0) + 1
    return out


# --------------------------------------------------------------------- the free half


@dataclass
class Verification:
    """Deterministic checks on the exported file. No model, no network, no opinion.

    Every check is tri-state: `True` passed, `False` failed, `None` *nobody asked*. A caller
    that gave no expected page count must not be told the page count was fine — the same
    reason `Friction`'s rates read `None` rather than zero when the endpoint billed nothing.
    """

    path: str = ""
    pages: int = 0
    expected_pages: tuple[int, int] | None = None
    pages_ok: bool | None = None
    aspect: float = 0.0
    expected_aspect: float | None = None
    aspect_ok: bool | None = None
    script: str | None = None
    expected_script: str | None = None
    script_ok: bool | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def asserted(self) -> int:
        return sum(1 for v in (self.pages_ok, self.aspect_ok, self.script_ok)
                   if v is not None)

    @property
    def passed(self) -> bool | None:
        """`None` when nothing was asserted — an unasked question did not pass."""
        return None if not self.asserted else not self.problems

    def as_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in vars(self).items()}
        out["asserted"] = self.asserted
        out["passed"] = self.passed
        return out


def script_of(text: str) -> str | None:
    """The deck's dominant writing system, or `None` when there is too little text to tell.

    Reuses `render/fonts.script_of`, which buckets into han / kana / hangul / latin because
    that is all a font fallback needs — so this check catches "the deck came out in Chinese
    when English was asked for" and *not* "Russian instead of English", both of which land in
    `latin`. Stated rather than fixed: a second, finer script table maintained here would drift
    from the one that actually picks the faces, and the coarse answer is the one that matches
    what the renderer did.
    """
    from ..render import fonts

    counts: dict[str, int] = {}
    for char in text:
        if not char.isalpha():
            continue
        kind = fonts.script_of(char)
        counts[kind] = counts.get(kind, 0) + 1
    if sum(counts.values()) < SCRIPT_MIN_CHARS:
        return None
    return max(counts, key=lambda k: counts[k])


def verify(path: Path, *, expected_pages: tuple[int, int] | None = None,
           expected_aspect: float | None = WIDESCREEN,
           expected_script: str | None = None) -> Verification:
    """Read the exported package and check the things a judge does not look at.

    Read from the `.pptx` itself rather than from the session that wrote it: the file is what
    a recipient opens, and a check run against the in-memory deck could only ever confirm that
    the harness agrees with itself.
    """
    out = Verification(path=str(path), expected_pages=expected_pages,
                       expected_aspect=expected_aspect, expected_script=expected_script)
    with zipfile.ZipFile(path) as package:
        names = package.namelist()
        out.pages = sum(1 for n in names if _SLIDE_PART.match(n))
        size = re.search(rb'<p:sldSz[^>]*cx="(\d+)"[^>]*cy="(\d+)"',
                         package.read("ppt/presentation.xml"))
        text = " ".join(
            m for n in sorted(names) if _SLIDE_PART.match(n)
            for m in _TEXT.findall(package.read(n).decode("utf-8", "replace")))

    if size:
        out.aspect = round(int(size.group(1)) / int(size.group(2)), 4)
    out.script = script_of(text)

    if expected_pages is not None:
        low, high = expected_pages
        out.pages_ok = low <= out.pages <= high
        if not out.pages_ok:
            out.problems.append(f"{out.pages} slides, expected {low}-{high}")
    if expected_aspect is not None:
        out.aspect_ok = bool(out.aspect) and abs(out.aspect - expected_aspect) <= ASPECT_TOLERANCE
        if not out.aspect_ok:
            out.problems.append(
                f"aspect ratio {out.aspect or 'unreadable'}, expected "
                f"{round(expected_aspect, 3)} ±{ASPECT_TOLERANCE}")
    if expected_script is not None:
        out.script_ok = out.script == expected_script
        if not out.script_ok:
            out.problems.append(
                f"dominant script {out.script or 'undetermined'}, expected {expected_script}")
    return out


__all__ = ["AxisScore", "DeckQuality", "PreviewUnavailable", "SlideQuality", "Verification",
           "VisionUnavailable", "measure_quality", "parse_score", "script_of", "structure",
           "verify"]
