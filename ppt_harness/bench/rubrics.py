"""The prompts and level definitions for the judged half — PPTEval, from the PPTAgent paper.

Kept here rather than inline for the same reason `core/loop.py` keeps `SYSTEM` as one named
block: a prompt is *the specification of the measurement*, and a rubric spliced into f-strings
at three call sites is a rubric that has silently forked. Not a skill under `ppt_harness/skills`
either — that directory is a user-facing surface with a frontmatter contract, listed to MCP
hosts as prompts a person can ask for by name, and an internal eval rubric is neither a
playbook nor an invariant. It is prompt text belonging to one module, so it lives beside that
module's code, which is the `loop.py` convention.

**The method, which is the whole point.** PPTEval scores in two calls, never one:

1. a vision model *describes* a rendered slide along fixed axes and is told not to judge;
2. a separate text-only call scores that description against the levels below, and never
   sees the image.

Asking one call to look and judge is what makes LLM-as-judge scores wobble between runs. The
split is what buys the paper's human correlation — Content 0.70, Design 0.90 — and it is why
`describe` and `score_prompt` are two functions producing two prompts rather than one.

**Coherence is deliberately not here.** See `WHY_NO_COHERENCE`.

Levels are the paper's, edited only where its wording assumes a conference talk. Editing one
is safe with the cache warm: only *descriptions* are cached, and every run scores them
afresh, so new levels are applied to old descriptions — which is the intended behaviour and
the reason the split earns its keep.
"""

from __future__ import annotations

from dataclasses import dataclass

#: A manual escape hatch, and deliberately *not* the thing that keeps the cache honest.
#:
#: `quality._cache_path` hashes the png, the `describe` text and the describer's model, so
#: editing a describe block, changing model, or re-rendering at a different width each mint
#: a new key on their own — verified, not assumed. Nobody has to remember anything for the
#: common cases, which is the only kind of invalidation worth relying on.
#:
#: What it is still for: a change that alters what a description *means* without altering
#: any of those three — a swap in the render pipeline that produces a byte-identical image
#: from different geometry, say. Rare, and worth having a lever for. Scoring levels are not
#: such a change: only descriptions are cached, so new levels reach old descriptions by
#: design.
VERSION = "1"

DESCRIBE_SYSTEM = (
    "You are describing a single presentation slide for a colleague who cannot see it. "
    "Report only what is visibly there. Do not rate, score, praise, criticise, or suggest "
    "improvements — a later step does that, and it will be reading your description as if it "
    "were the slide itself. Anything you leave out does not exist."
)

SCORE_SYSTEM = (
    "You are scoring a written description of a presentation slide against a fixed rubric. "
    "You cannot see the slide and must not imagine anything the description does not state. "
    "If the description does not mention something a level requires, that thing is absent. "
    'Reply with one JSON object and nothing else: {"score": <1-5>, "reason": "<one sentence>"}.'
)


@dataclass(frozen=True)
class Rubric:
    """One axis: what to describe, and what each 1-5 level means.

    The invariant this type exists to hold: `describe` never mentions a level and
    `score_prompt` never mentions an image. If either leaked into the other the two calls
    would collapse back into one judgement made while looking, which is the thing the design
    is spending a second API call to avoid.
    """

    axis: str
    describe: str
    levels: dict[int, str]
    human_r: float
    """Pearson correlation with human raters reported by the PPTAgent paper for this axis.

    Carried on the rubric rather than in prose because it is the number that says how much
    weight a reader may put on the score — and it must travel with the score into the report.
    """

    def score_prompt(self, description: str) -> str:
        """The text-only half. Takes a description; the caller must not add an image."""
        levels = "\n".join(f"  {n}. {text}" for n, text in sorted(self.levels.items()))
        return (
            f"Rubric — {self.axis}:\n{levels}\n\n"
            "Description of the slide:\n"
            f"\"\"\"\n{description.strip()}\n\"\"\"\n\n"
            "Score the described slide 1-5. Judge only what the description states."
        )


CONTENT = Rubric(
    axis="content",
    human_r=0.70,
    describe=(
        "Describe the content of this slide:\n"
        "- The text: what it says, roughly how much of it there is, and whether it reads as "
        "complete sentences, headline fragments, or a wall of prose.\n"
        "- Whether the amount of text is comfortable for a slide or crowded.\n"
        "- Any images, charts, diagrams, tables or icons, what each one shows, and whether it "
        "relates to what the text says.\n"
        "- Anything unreadable, truncated, cut off at an edge, or overlapping another element.\n"
        "Do not evaluate. Six sentences at most."
    ),
    levels={
        1: "The text is unreadable, truncated, or says nothing a reader could use.",
        2: "The text is legible but incomplete or unclear — it does not stand on its own.",
        3: "The text is clear and complete, but the slide lacks visual aids.",
        4: "Clear text with a relevant image or chart, with minor flaws — the visual is only "
           "loosely related, or the slide is text-heavy.",
        5: "Images and text complement each other: the visual carries part of the meaning.",
    },
)

DESIGN = Rubric(
    axis="design",
    human_r=0.90,
    describe=(
        "Describe the visual style of this slide:\n"
        "- Visual consistency: do any elements overlap or collide, is anything cropped by an "
        "edge, and is the contrast between text and its background strong enough to read?\n"
        "- Colour scheme: name the colours actually used, and say whether the slide is "
        "monochrome (black, white and greys) or colourful, and whether the colours agree with "
        "each other.\n"
        "- Supplementary visual elements: state explicitly whether there are background "
        "colours or images, textures, patterns, geometric shapes, rules or dividers, icons, "
        "logos, or photographs — and say so plainly if there are none and the slide is plain "
        "text on a plain background.\n"
        "Do not evaluate. Six sentences at most."
    ),
    levels={
        1: "Elements overlap or collide, or contrast is so low that parts cannot be read.",
        2: "Monotonous colours — black and white — readable but with no visual appeal.",
        3: "A basic colour scheme, but no supplementary visual elements such as icons, "
           "backgrounds, images or geometric shapes, which makes it look plain.",
        4: "A harmonious colour scheme plus some visual elements, with minor flaws.",
        5: "Harmonious and engaging: the visual elements enhance the slide's appeal.",
    },
)

RUBRICS = {r.axis: r for r in (CONTENT, DESIGN)}

WHY_NO_COHERENCE = """\
PPTEval's third axis is not implemented, and this is the reason rather than an oversight.

Two objections, either of which is enough:

1. **It is the weakest axis the paper reports.** Content correlates with human raters at
   r=0.70 and Design at 0.90; Coherence manages 0.55. A number that agrees with a human
   half the time, printed in the same table and the same units as one that agrees nine
   times in ten, gets quoted as though they were the same kind of evidence.

2. **Its top two levels describe a conference talk.** Level 4 requires the speaker, the date
   and the institution; level 5 additionally requires acknowledgements. A board update that
   scored 5 on that rubric would be a worse board update. Shipping it unchanged would
   measure genre conformity and call it quality.

Rewriting the levels for business decks was the alternative, and it was rejected: the paper's
human correlation belongs to the paper's wording, so a rewrite ships an unvalidated rubric
wearing a validated one's reputation — with no human ratings of this repository's decks to
re-establish what it is worth.

What is measured instead, and it is not nothing: the deck-level half of `core/review.py` —
`weak_close`, `duplicate_title`, `title_case_drift`, `bullet_stop_drift` — decides narrative
mechanics by counting rather than by opinion, costs no model call, and is reported beside the
judged axes by `quality.structure`. It is a narrower claim, honestly the same one: a deck that
ends on a bullet list and repeats a title twice has a structural problem, and nothing here
pretends that a deck without those has a good argument.
"""
