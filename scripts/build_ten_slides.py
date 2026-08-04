"""Build a ten-slide deck through the harness's own managed-slide tools."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ppt_harness.core.session import Session
from ppt_harness.tools import managed, shared

DECK = Path(sys.argv[1])

SLIDES = [
    # 1 — opener
    dict(layout="title", notes="Ten slides, built without a model: every block below is a "
                               "component the harness placed and measured.",
         blocks=[dict(component="title_slide", region="hero", variant="left", slots=dict(
             title="ppt-harness",
             prose="Editing real presentations by talking to a model — "
                   "without throwing the original file away."))]),

    # 2 — agenda
    dict(layout="stack", notes="Four claims, in the order they build on each other.",
         blocks=[
             dict(component="slide_title", region="header", slots=dict(title="What we'll cover")),
             dict(component="agenda", region="body", variant="list", slots=dict(items=[
                 "Why rebuilding a deck loses the deck",
                 "Two kinds of slide, two tool sets",
                 "Measurement instead of guesswork",
                 "What ships today, and what doesn't",
             ]))]),

    # 3 — the problem
    dict(layout="stack", notes="The case for patching the package rather than regenerating it.",
         blocks=[
             dict(component="slide_title", region="header",
                  slots=dict(title="A rebuilt deck is a different deck")),
             dict(component="bullets", region="body", variant="plain", slots=dict(items=[
                 "Most tools rebuild the file from the text they parsed",
                 "SmartArt, media, animations and comments do not survive",
                 "What lands no longer belongs to its author",
                 "ppt-harness patches the original package instead",
             ]))]),

    # 4 — the two modes
    dict(layout="stack", notes="The mode split is the load-bearing decision in the system.",
         blocks=[
             dict(component="slide_title", region="header",
                  slots=dict(title="Every slide carries a mode")),
             dict(component="comparison", region="body", variant="split", slots=dict(
                 title="The tool set is gated on it",
                 left=[
                     "freeform — an imported slide",
                     "Holds the author's own shapes",
                     "Moved by align, distribute, snap",
                     "adopt_slide promotes it",
                 ],
                 right=[
                     "managed — a generated slide",
                     "Built from the component catalog",
                     "Geometry derived from the theme",
                     "eject_slide sends it back",
                 ]))]),

    # 5 — no coordinates
    dict(layout="stack", notes="A schema test fails if a coordinate ever appears in a tool.",
         blocks=[
             dict(component="slide_title", region="header",
                  slots=dict(title="No tool takes a coordinate")),
             dict(component="card_grid", region="body", variant="1x3", slots=dict(
                 title="What a model is allowed to say",
                 items=[
                     "Components own geometry. The model picks a component, not a rectangle.",
                     "The theme owns type. No font sizes, no colour values, no point offsets.",
                     "Shapes move by relationship — align, distribute, snap — never by a number.",
                 ]))]),

    # 6 — measurement
    dict(layout="hero_plus_row",
         notes="Advance width is shaped with HarfBuzz against the font that will render.",
         blocks=[
             dict(component="slide_title", region="header",
                  slots=dict(title="Text is measured, not estimated")),
             dict(component="card_grid", region="hero", variant="1x3", slots=dict(items=[
                 "A write is checked before it lands, and returns its own measurement.",
                 "Counting characters misprices CJK, which runs wider per character than Latin.",
                 "A rejection carries the capacity, the overage, and the ways out.",
             ])),
             dict(component="stat_row", region="footer_row", variant="flat", slots=dict(items=[
                 dict(value="2.2x", label="CJK vs Latin width"),
                 dict(value="1280", label="canvas width, px"),
                 dict(value="3", label="repair rungs"),
             ]))]),

    # 7 — the repair ladder
    dict(layout="stack", notes="Repair reshapes a slide; it never rewrites the author's words.",
         blocks=[
             dict(component="slide_title", region="header",
                  slots=dict(title="When a slide overflows anyway")),
             dict(component="timeline", region="body", variant="vertical", slots=dict(
                 title="repair climbs, and stops short of the last rung",
                 items=[
                     "Variant — another arrangement, same component",
                     "Density — tighten before touching content",
                     "Degradation — a component that holds less",
                     "Refuse — reshape a slide, never cut text",
                 ]))]),

    # 8 — checks
    dict(layout="stack", notes="lint is measured and blocking; review is judged and advisory.",
         blocks=[
             dict(component="slide_title", region="header",
                  slots=dict(title="Two kinds of check, kept apart")),
             dict(component="data_table", region="body", variant="zebra", slots=dict(
                 tabular=dict(
                     headers=["Check", "Asks", "Blocks a write?"],
                     rows=[
                         ["lint", "Does the text overflow its box?", "Yes — measured"],
                         ["review", "Does the title state a finding?", "No — advisory"],
                         ["export", "Did fidelity survive the round trip?", "Yes — asserted"],
                     ]),
                 prose="Measured and judged never share a verdict."))]),

    # 9 — section break
    dict(layout="full_bleed", notes="Divider before the closing status.",
         blocks=[dict(component="section_break", region="canvas", variant="centered", slots=dict(
             title="Where it stands",
             prose="Four themes ship, the CLI is offline and deterministic, "
                   "and writing slides is the part that needs a model."))]),

    # 10 — takeaway
    dict(layout="stack", notes="The one sentence to leave with.",
         blocks=[
             dict(component="slide_title", region="header", slots=dict(title="The bet")),
             dict(component="takeaway", region="body", variant="bar", slots=dict(
                 title="Constrain the model to the operations a designer would "
                       "recognise, and check every one of them against real font metrics.",
                 items=[
                     "The original file survives the edit",
                     "Nothing lands that does not fit",
                 ]))]),
]


def main() -> int:
    session = Session.from_builtin("slate", title="ppt-harness")
    for existing in reversed(shared.get_outline(session)["slides"]):
        shared.delete_slide(session, existing["id"])

    for n, spec in enumerate(SLIDES, 1):
        try:
            res = managed.add_slide(session, layout=spec["layout"], blocks=spec["blocks"],
                                    notes=spec["notes"])
        except Exception as exc:                      # noqa: BLE001 - report and keep going
            print(f"slide {n:2}  FAILED  {type(exc).__name__}: {exc}")
            continue
        print(f"slide {n:2}  ok  {spec['layout']:14} {res['target']}")

    report = shared.export(session, str(DECK))
    print("export:", json.dumps(report)[:300])
    print("lint:  ", json.dumps(shared.lint(session))[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
