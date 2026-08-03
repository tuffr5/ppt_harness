"""Editorial review — the axis `lint` does not have.

`lint` answers *does this slide fit*: overflow in px against real font metrics, and it is
never wrong, because it measures. This module answers *does this slide land*, and it can be
wrong, because nothing measures that. Everything about the shape of the file follows from
that one difference.

**Nothing here blocks a write.** A budget refusal is a fact and may refuse. A finding is an
opinion, and an opinion that can refuse a write is a style guide holding a gun. Findings are
returned; the person decides.

**Precision over recall.** The failure mode of an advisory channel is not missing something,
it is crying wolf until someone turns it off. So every rule below decides from the text with
certainty — a list has seven items or it does not — and each one is written to stay silent
when the text cannot tell it. `_case_of` returning `None` on a title too short to classify is
the pattern; a rule that guesses in the dead band would be worse than no rule.

**The interesting judgements are deliberately absent.** Whether three names refer to one
thing, whether slide 6 earns its place, whether the argument holds — none of that is here,
because none of it can be decided by counting. That work belongs to a model reading the deck
with `house-style` or `narrative-arc` in front of it. What is here is the mechanical subset,
which is worth having precisely because it costs nothing and fires without being asked.

Pure functions over a `Deck`. The session-shaped part — which findings have already been
said out loud — lives on the session, because it is a fact about the conversation rather
than about the deck.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from ..state import richtext
from ..state.document import Deck, Mode, Slide

# --------------------------------------------------------------------------- thresholds

#: "Three to five of them. If there are eight, there is a structure hiding in there." The
#: rule fires at seven, which is the first count nobody defends as deliberate.
CROWDED_LIST = 7

#: One run of body text long enough that it is being read rather than glanced at. Prose
#: belongs in the notes, and this is where a bullet stops being one.
WALL_OF_WORDS = 40

#: Below this a bullet set is too small for a mixed convention to mean anything — two items
#: differing is a coin toss, not a house style.
DRIFT_MIN_ITEMS = 3

#: A title needs this many words that carry meaning before its capitalisation can be read.
#: "Q3 results" cannot tell you anything about a deck's convention.
CASE_MIN_WORDS = 3

#: How lopsided the capitalisation has to be to call it. Between these the title is
#: proper-noun heavy — "Churn doubled in EMEA" — and no convention can be inferred.
CASE_HIGH, CASE_LOW = 0.8, 0.2

#: Words that carry no capitalisation signal in either convention.
MINOR = frozenset({"a", "an", "the", "and", "or", "but", "nor", "of", "in", "on", "at",
                   "to", "for", "with", "by", "from", "as", "vs", "via", "per", "into"})

#: Nouns that file a slide rather than state its finding. Matched against the end of a
#: title, which is where the head noun of a noun phrase sits.
LABELS = frozenset({
    "analysis", "overview", "update", "summary", "breakdown", "review", "results",
    "metrics", "roadmap", "agenda", "background", "introduction", "conclusion",
    "takeaways", "next steps", "objectives", "goals", "status", "highlights", "recap",
    "approach", "methodology", "landscape", "considerations", "learnings", "context",
})

#: Last slides that throw the last slide away.
FAREWELLS = re.compile(r"^\s*(any\s+)?(questions|q\s*&\s*a|q\s*and\s*a|thank\s*you|thanks|"
                       r"merci|discussion)\s*[.!?]*\s*$", re.IGNORECASE)

#: A title long enough that a conjunction in it is joining two claims rather than naming one
#: compound thing. "Sales and Marketing" is a department; six words with an "and" is two
#: slides.
CONJUNCTION_MIN_WORDS = 6
CONJUNCTION = re.compile(r"\s(and|&)\s|;", re.IGNORECASE)

WORD = re.compile(r"[A-Za-z][\w'’-]*")

#: Rule order, and the only place it is decided. Findings come back in this order, because
#: the consumer's budget is one — so the first one has to be the one worth spending it on.
#: What changes the deck's meaning outranks what changes its consistency.
ORDER = (
    "topic_title",
    "weak_close",
    "title_conjunction",
    "duplicate_title",
    "crowded_list",
    "wall_of_text",
    "title_case_drift",
    "bullet_stop_mixed",
    "bullet_stop_drift",
    "bullet_case_mixed",
)


# ----------------------------------------------------------------------------- findings


@dataclass(frozen=True)
class Finding:
    """One thing worth saying, and what to do about it.

    `suggestion` is not optional in practice. A review that reports "slide 4's title is a
    topic" and stops has moved the work to the reader; naming the fix is most of the value.
    """

    rule: str
    message: str
    suggestion: str
    slide_id: str | None = None
    """`None` for a finding about the deck rather than about one slide."""
    where: str = ""
    """The block or shape it came from, when a slide has more than one place it could be."""

    @property
    def key(self) -> str:
        """Identity for "have we already said this".

        Includes `where`, so two crowded lists on one slide are two findings rather than one
        that silences the other.
        """
        return f"{self.rule}:{self.slide_id or 'deck'}:{self.where}"

    def as_dict(self) -> dict[str, Any]:
        out = {"rule": self.rule, "message": self.message, "suggestion": self.suggestion}
        if self.slide_id:
            out["slide"] = self.slide_id
        if self.where:
            out["where"] = self.where
        return out


# ------------------------------------------------------------------------ normalisation


@dataclass(frozen=True)
class Read:
    """One slide reduced to the text a rule can judge.

    The two modes store text in entirely different shapes, and a rule that had to know which
    one it was looking at would be written twice and go stale on one of them. So the modes
    are flattened once, here, and every rule below sees the same three fields.
    """

    id: str
    index: int
    title: str
    lists: tuple[tuple[tuple[str, ...], str], ...] = ()
    """Bullet sets as `(items, where)`. A slide can hold several."""
    prose: tuple[tuple[str, str], ...] = ()
    """Body text that is not a list, as `(text, where)`."""


def _plain(value: Any) -> str:
    if isinstance(value, str):
        return richtext.plain(value).strip()
    return ""


def read(slide: Slide) -> Read:
    """Flatten one slide, whichever mode it is in."""
    if slide.mode is Mode.MANAGED:
        return _read_managed(slide)
    return _read_freeform(slide)


def _read_managed(slide: Slide) -> Read:
    title, lists, prose = "", [], []
    for block in slide.blocks:
        for name, value in block.slots.items():
            if name == "title" and not title:
                title = _plain(value)
            elif name == "items" and isinstance(value, list):
                # Only lists of plain strings. A `stat_row`'s items are figures with labels,
                # not bullets, and asking whether they end in a full stop is meaningless.
                items = tuple(_plain(v) for v in value if isinstance(v, str))
                if items:
                    lists.append((items, block.id))
            elif name == "prose":
                text = _plain(value)
                if text:
                    prose.append((text, block.id))
    return Read(id=slide.id, index=slide.index, title=title,
                lists=tuple(lists), prose=tuple(prose))


def _read_freeform(slide: Slide) -> Read:
    title = next((s.text.strip() for s in slide.shapes
                  if s.text and s.role in ("slide_title", "deck_title")), "")
    lists, prose = [], []
    for shape in slide.shapes:
        if shape.opaque or not shape.text or not shape.text.strip():
            continue
        if shape.text.strip() == title:
            continue
        if shape.bullet != "none":
            items = tuple(line.strip() for line in shape.text.splitlines() if line.strip())
            if items:
                lists.append((items, shape.id))
        else:
            prose.append((shape.text.strip(), shape.id))
    # No furniture filter, and none needed: every rule below triggers on length or on a
    # pattern, and a slide number is too short to reach any of them.
    return Read(id=slide.id, index=slide.index, title=title,
                lists=tuple(lists), prose=tuple(prose))


# --------------------------------------------------------------------------- text sense


def _words(text: str) -> list[str]:
    return WORD.findall(text)


def _case_of(text: str) -> str | None:
    """`"title"`, `"sentence"`, or `None` when the text cannot say.

    `None` is the important return. A two-word title, an all-caps one, and a title that is
    mostly proper nouns each look like evidence and are not, and a convention inferred from
    them would be inferred from noise.
    """
    if text.isupper():
        return None
    meaningful = [w for w in _words(text) if w.lower() not in MINOR]
    if len(meaningful) < CASE_MIN_WORDS:
        return None
    rest = meaningful[1:]
    share = sum(1 for w in rest if w[0].isupper()) / len(rest)
    if share >= CASE_HIGH:
        return "title"
    if share <= CASE_LOW:
        return "sentence"
    return None


def _stops(items: Sequence[str]) -> str | None:
    """Whether a bullet set ends its items in a full stop — `None` if it is inconsistent."""
    ends = [item.rstrip().endswith(".") for item in items]
    if all(ends):
        return "stop"
    if not any(ends):
        return "none"
    return None


def _tail(text: str, n: int) -> str:
    return " ".join(_words(text)[-n:]).lower()


# -------------------------------------------------------------------------- slide rules


def _topic_title(r: Read) -> Iterator[Finding]:
    if not r.title:
        return
    if _tail(r.title, 1) in LABELS or _tail(r.title, 2) in LABELS:
        yield Finding(
            rule="topic_title", slide_id=r.id,
            message=f"{r.id}'s title names the topic rather than the finding: "
                    f"{r.title!r}.",
            suggestion="Retitle it with what the slide shows — the sentence someone would "
                       "repeat afterwards.",
        )


def _title_conjunction(r: Read) -> Iterator[Finding]:
    if len(_words(r.title)) >= CONJUNCTION_MIN_WORDS and CONJUNCTION.search(r.title):
        yield Finding(
            rule="title_conjunction", slide_id=r.id,
            message=f"{r.id}'s title joins two claims: {r.title!r}.",
            suggestion="A title that needs 'and' is usually two slides. Split it, or decide "
                       "which half the slide is actually about.",
        )


def _crowded_list(r: Read) -> Iterator[Finding]:
    for items, where in r.lists:
        if len(items) >= CROWDED_LIST:
            yield Finding(
                rule="crowded_list", slide_id=r.id, where=where,
                message=f"{r.id} has a list of {len(items)} items.",
                suggestion="Past about five there is a structure hiding in the list — group "
                           "them, or promote the grouping to the slide and split it.",
            )


def _wall_of_text(r: Read) -> Iterator[Finding]:
    for text, where in r.prose:
        count = len(_words(text))
        if count >= WALL_OF_WORDS:
            yield Finding(
                rule="wall_of_text", slide_id=r.id, where=where,
                message=f"{r.id} carries {count} words of prose in one block.",
                suggestion="At that length it is being read, not glanced at. Move it to the "
                           "notes and leave the claim on the slide.",
            )
    for items, where in r.lists:
        longest = max((len(_words(i)) for i in items), default=0)
        if longest >= WALL_OF_WORDS:
            yield Finding(
                rule="wall_of_text", slide_id=r.id, where=where,
                message=f"{r.id} has a bullet {longest} words long.",
                suggestion="A bullet that long is a paragraph wearing a bullet. Cut it to "
                           "the claim, or move it to the notes.",
            )


def _bullet_stop_mixed(r: Read) -> Iterator[Finding]:
    for items, where in r.lists:
        if len(items) >= DRIFT_MIN_ITEMS and _stops(items) is None:
            yield Finding(
                rule="bullet_stop_mixed", slide_id=r.id, where=where,
                message=f"{r.id}'s bullets end in a full stop only some of the time.",
                suggestion="Pick one and apply it to the set. Fragments usually take none.",
            )


def _bullet_case_mixed(r: Read) -> Iterator[Finding]:
    for items, where in r.lists:
        heads = [item[0] for item in items if item and item[0].isalpha()]
        if len(heads) >= DRIFT_MIN_ITEMS and any(h.isupper() for h in heads) \
                and any(h.islower() for h in heads):
            yield Finding(
                rule="bullet_case_mixed", slide_id=r.id, where=where,
                message=f"{r.id}'s bullets start with a mix of upper and lower case.",
                suggestion="Capitalise the set the same way. Mixed openings usually mean the "
                           "items are not parallel either — check the grammar while you are "
                           "in there.",
            )


SLIDE_RULES = (_topic_title, _title_conjunction, _crowded_list, _wall_of_text,
               _bullet_stop_mixed, _bullet_case_mixed)


# --------------------------------------------------------------------------- deck rules


def _duplicate_title(reads: Sequence[Read]) -> Iterator[Finding]:
    seen: dict[str, str] = {}
    for r in reads:
        norm = " ".join(_words(r.title)).lower()
        if not norm:
            continue
        if norm in seen:
            yield Finding(
                rule="duplicate_title", slide_id=r.id,
                message=f"{r.id} repeats the title on {seen[norm]}: {r.title!r}.",
                suggestion="Two slides with one title read as one slide that overran. Say "
                           "what is different about the second, or merge them.",
            )
        else:
            seen[norm] = r.id


def _weak_close(reads: Sequence[Read]) -> Iterator[Finding]:
    if not reads:
        return
    last = reads[-1]
    text = last.title or next((p for p, _ in last.prose), "")
    if text and FAREWELLS.match(text):
        yield Finding(
            rule="weak_close", slide_id=last.id,
            message=f"The deck ends on {text.strip()!r}.",
            suggestion="The last slide is the one that stays up while people talk. Spend it "
                       "on what you want to happen next, named and owned.",
        )


def _title_case_drift(reads: Sequence[Read]) -> Iterator[Finding]:
    counts = Counter(c for c in (_case_of(r.title) for r in reads) if c)
    if len(counts) < 2:
        return
    winner, top = counts.most_common(1)[0]
    loser = "sentence" if winner == "title" else "title"
    odd = [r.id for r in reads if _case_of(r.title) == loser]
    yield Finding(
        rule="title_case_drift",
        message=f"Slide titles mix conventions: {top} in {winner} case, "
                f"{counts[loser]} in {loser} case ({', '.join(odd[:4])}"
                f"{'…' if len(odd) > 4 else ''}).",
        suggestion=f"Standardise on {winner} case, which is what the deck mostly does — or "
                   "run house-style if the split is closer to even than it looks.",
    )


def _bullet_stop_drift(reads: Sequence[Read]) -> Iterator[Finding]:
    counts: Counter[str] = Counter()
    for r in reads:
        for items, _ in r.lists:
            if len(items) >= DRIFT_MIN_ITEMS:
                style = _stops(items)
                if style:
                    counts[style] += 1
    if len(counts) < 2:
        return
    yield Finding(
        rule="bullet_stop_drift",
        message=f"Bullet sets disagree about full stops: {counts['stop']} end in one, "
                f"{counts['none']} do not.",
        suggestion="One convention across the deck. Fragments usually take none, and that "
                   "is the cheaper direction to standardise in.",
    )


DECK_RULES = (_duplicate_title, _weak_close, _title_case_drift, _bullet_stop_drift)


# ------------------------------------------------------------------------------- review


def review(deck: Deck) -> list[Finding]:
    """Every finding in the deck, ordered by what is worth saying first.

    Always over the whole deck, even when the caller wants one slide: drift is a fact about
    the deck, and a single slide cannot be inconsistent with itself. Filtering is the
    caller's job and happens after.
    """
    reads = [read(s) for s in deck.slides if not s.hidden]
    found: list[Finding] = []
    for r in reads:
        for rule in SLIDE_RULES:
            found.extend(rule(r))
    for deck_rule in DECK_RULES:
        found.extend(deck_rule(reads))

    index = {r.id: r.index for r in reads}
    return sorted(found, key=lambda f: (ORDER.index(f.rule),
                                        index.get(f.slide_id or "", -1)))
