"""Rich text — the bridge to OOXML runs.

OOXML has no notion of "bold text". A paragraph is a list of **runs**, and each run carries
its own properties:

    <a:p>
      <a:r><a:rPr b="1" i="1" u="sng"/><a:t>emphasised</a:t></a:r>
      <a:r><a:rPr/><a:t> plain</a:t></a:r>
    </a:p>

The harness modelled text as a flat string, so it could only ever write one run — which is
why asking for bold produced literal asterisks in the file. This is the missing layer.

**Emphasis is content, not typography.** Which words are stressed is part of what the author
is saying; what size and face they are set in belongs to the theme. That distinction is why
`bold` is allowed here while `set_font` is still absent from the tool surface — the model
may say *which* words matter, never *how big* they are.

Markup is parsed rather than demanded. A model asked to bold something writes `**this**`
whatever the tool signature says, so the harness accepts that and turns it into real runs.
The alternative — refusing, or writing it through verbatim — is how asterisks ended up on a
slide.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

#: `sng` and `dbl` are the OOXML enum values; the harness exposes the boolean case only.
UNDERLINE_NONE = "none"
UNDERLINE_SINGLE = "sng"

#: `baseline` is thousandths of the font size. These are what PowerPoint itself writes.
SUPERSCRIPT = 30000
SUBSCRIPT = -25000


class Run(BaseModel):
    """A span of characters sharing one set of properties."""

    text: str = ""
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    script: str = ""
    """`super`, `sub`, or empty."""
    link: str = ""
    """A URL. OOXML hangs `<a:hlinkClick>` off the run's properties, so a link is a run
    property like any other rather than a separate object."""

    @property
    def plain(self) -> bool:
        return not (self.bold or self.italic or self.underline or self.strike
                    or self.script or self.link)

    @property
    def baseline(self) -> int:
        return {"super": SUPERSCRIPT, "sub": SUBSCRIPT}.get(self.script, 0)


# ------------------------------------------------------------------------ parsing

#: Ordered longest-first so `**` is never mistaken for two `*`.
_TOKENS = [
    ("bold", re.compile(r"\*\*(?P<t>.+?)\*\*", re.DOTALL)),
    ("bold", re.compile(r"__(?P<t>.+?)__", re.DOTALL)),
    ("strike", re.compile(r"~~(?P<t>.+?)~~", re.DOTALL)),
    ("italic", re.compile(r"(?<!\*)\*(?!\*)(?P<t>[^*\n]+?)\*(?!\*)")),
    ("italic", re.compile(r"(?<!_)_(?!_)(?P<t>[^_\n]+?)_(?!_)")),
]

#: The HTML a model reaches for when markdown feels wrong. Same intent, same treatment.
_TAGS = [
    ("bold", re.compile(r"<(?:b|strong)>(?P<t>.*?)</(?:b|strong)>", re.DOTALL | re.I)),
    ("italic", re.compile(r"<(?:i|em)>(?P<t>.*?)</(?:i|em)>", re.DOTALL | re.I)),
    ("underline", re.compile(r"<u>(?P<t>.*?)</u>", re.DOTALL | re.I)),
    ("strike", re.compile(r"<(?:s|del|strike)>(?P<t>.*?)</(?:s|del|strike)>",
                          re.DOTALL | re.I)),
    ("super", re.compile(r"<sup>(?P<t>.*?)</sup>", re.DOTALL | re.I)),
    ("sub", re.compile(r"<sub>(?P<t>.*?)</sub>", re.DOTALL | re.I)),
]

MARKUP = re.compile(r"\*\*|__|~~|<\/?(?:b|strong|i|em|u|s|del|strike|sup|sub)>", re.I)


def has_markup(text: str) -> bool:
    """Would parsing this change anything? Used to explain a rejection, not to gate one."""
    return bool(MARKUP.search(text or ""))


def parse(text: str) -> list[Run]:
    """Markup to runs. Unrecognised characters stay literal.

    Deliberately not a Markdown implementation: no links, no code spans, no lists. A slide
    is not a document, and every construct accepted here is one the exporter must be able
    to represent as run properties.
    """
    if not text:
        return [Run(text="")]

    runs: list[Run] = []
    _walk(text, Run(), runs)
    merged = _merge(runs)
    return merged or [Run(text="")]


def _walk(text: str, style: Run, out: list[Run]) -> None:
    """Find the earliest emphasis, split around it, recurse into the middle."""
    best: tuple[int, str, re.Match[str]] | None = None
    for kind, pattern in [*_TAGS, *_TOKENS]:
        match = pattern.search(text)
        if match and (best is None or match.start() < best[2].start()):
            best = (match.start(), kind, match)

    if best is None:
        if text:
            out.append(style.model_copy(update={"text": text}))
        return

    _, kind, match = best
    if match.start():
        out.append(style.model_copy(update={"text": text[: match.start()]}))

    inner = style.model_copy()
    if kind in ("super", "sub"):
        inner.script = kind
    else:
        setattr(inner, kind, True)
    _walk(match.group("t"), inner, out)

    _walk(text[match.end():], style, out)


def _merge(runs: list[Run]) -> list[Run]:
    """Fold adjacent runs that share properties. Fewer runs is a smaller file and, more
    importantly, is what a human editing the slide afterwards expects to select."""
    out: list[Run] = []
    for run in runs:
        if not run.text:
            continue
        if out and _same(out[-1], run):
            out[-1].text += run.text
        else:
            out.append(run.model_copy())
    return out


def _same(a: Run, b: Run) -> bool:
    return (a.bold, a.italic, a.underline, a.strike, a.script, a.link) == (
        b.bold, b.italic, b.underline, b.strike, b.script, b.link)


# ---------------------------------------------------------------------- rendering


def to_plain(runs: list[Run]) -> str:
    """The text without its emphasis — what search, budgets and diffs work on."""
    return "".join(r.text for r in runs)


def plain(text: str) -> str:
    """Markup in, words out.

    For everything that must not see the markup: budgets measure the words that render,
    search matches what the reader can see, and an outline reads as a sentence. Asterisks
    counted as characters reject text that fits, and searched as characters hide it.
    """
    return to_plain(parse(text)) if text else (text or "")


def to_markup(runs: list[Run]) -> str:
    """Back to markup, so a model reading a slide sees the emphasis it can edit."""
    parts = []
    for run in runs:
        text = run.text
        if run.script == "super":
            text = f"<sup>{text}</sup>"
        elif run.script == "sub":
            text = f"<sub>{text}</sub>"
        if run.strike:
            text = f"~~{text}~~"
        if run.underline:
            text = f"<u>{text}</u>"
        if run.italic:
            text = f"*{text}*"
        if run.bold:
            text = f"**{text}**"
        parts.append(text)
    return "".join(parts)


def apply_marks(runs: list[Run], span: str, **marks) -> tuple[list[Run], int]:
    """Set properties on every occurrence of `span`. Returns the new runs and a hit count.

    Splitting by *text* rather than by index is deliberate: a model that has just read a
    slide knows the words, not the character offsets, and offsets go stale the moment
    anything else changes.
    """
    if not span:
        return runs, 0

    out: list[Run] = []
    hits = 0
    for run in runs:
        remaining = run.text
        while span in remaining:
            before, _, remaining = remaining.partition(span)
            if before:
                out.append(run.model_copy(update={"text": before}))
            out.append(run.model_copy(update={"text": span, **marks}))
            hits += 1
        if remaining:
            out.append(run.model_copy(update={"text": remaining}))
    return _merge(out), hits


def to_html(runs: list[Run], escape) -> str:
    """For the preview. `escape` is passed in so this module stays free of the renderer."""
    parts = []
    for run in runs:
        text = escape(run.text).replace("\n", "<br>")
        if run.link:
            text = f'<a href="{escape(run.link)}">{text}</a>' 
        if run.script == "super":
            text = f"<sup>{text}</sup>"
        elif run.script == "sub":
            text = f"<sub>{text}</sub>"
        if run.strike:
            text = f"<s>{text}</s>"
        if run.underline:
            text = f"<u>{text}</u>"
        if run.italic:
            text = f"<em>{text}</em>"
        if run.bold:
            text = f"<strong>{text}</strong>"
        parts.append(text)
    return "".join(parts)
