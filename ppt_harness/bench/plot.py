"""Charts and tables from benchmark JSON — SVG written by hand, no plotting dependency.

A benchmark's output is evidence, and evidence has to be *looked at*. Four dimensions across
two conditions is a picture in three seconds and a squint in thirty, so the numbers get a
chart; a preservation rate over three decks is one fact and gets a sentence. The rule this
file follows: **a chart when the shape matters, a table when the value does.**

Written as literal SVG rather than through matplotlib because the alternative is a 30 MB
dependency and a font stack of its own for four bar charts a year. The output is text, so it
diffs, and a chart that changed because a number changed shows exactly which pixel moved.

Two things are load-bearing in how these are drawn:

**Every score is drawn against its ceiling.** SlidesBench's `color` dimension scores 0–37
comparing their own reference to *itself*, so a bar drawn on a 0–100 axis with no ceiling
mark would say "catastrophic" about a result that is at the maximum. The identity baseline is
a tick on every row, and the legend names it.

**Colour never carries identity alone.** Every bar is direct-labelled with its value and the
legend is always present, so the chart survives greyscale, colour blindness, and a reader who
prints it. The palette is the data-viz reference instance, validated: worst adjacent pair
ΔE 24.7 under protanopia, 33.6 normal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Categorical slots 1 and 2 of the validated reference palette, plus its ink and surface
# roles. Fixed order, never cycled: high-level is always blue, detailed always orange, so a
# chart with one condition missing does not repaint the other.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
INK_MUTED = "#8a8983"
GRID = "#e6e5e0"
SERIES = ("#2a78d6", "#eb6834")


def _esc(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _bar(x: float, y: float, width: float, height: float, fill: str, radius: float = 4) -> str:
    """A bar with its *data end* rounded and its baseline end square.

    Both ends rounded would float the bar off its axis, which reads as a floating range
    rather than a magnitude measured from zero.
    """
    radius = max(0.0, min(radius, width))
    if width <= 0.5:
        return ""
    right = x + width
    return (f'<path d="M{x:.1f},{y:.1f} H{right - radius:.1f} '
            f'Q{right:.1f},{y:.1f} {right:.1f},{y + radius:.1f} '
            f'V{y + height - radius:.1f} '
            f'Q{right:.1f},{y + height:.1f} {right - radius:.1f},{y + height:.1f} '
            f'H{x:.1f} Z" fill="{fill}"/>')


@dataclass
class Series:
    label: str
    values: dict[str, float]


def grouped_bars(title: str, subtitle: str, dimensions: list[str], series: list[Series],
                 ceiling: dict[str, float] | None = None) -> str:
    """Score per dimension, one bar per condition, with the achievable ceiling marked."""
    left, right, top = 132, 56, 76
    row, bar_h, gap = 46, 14, 4
    width = 760
    height = top + row * len(dimensions) + 62

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="ui-sans-serif,-apple-system,'
        f'\'Segoe UI\',Helvetica,Arial,sans-serif" role="img" '
        f'aria-label="{_esc(title)}. {_esc(subtitle)}">',
        f'<rect width="{width}" height="{height}" fill="{SURFACE}"/>',
        f'<text x="24" y="34" font-size="17" font-weight="650" fill="{INK}">{_esc(title)}</text>',
        f'<text x="24" y="55" font-size="12.5" fill="{INK_2}">{_esc(subtitle)}</text>',
    ]

    plot = width - left - right
    for tick in (0, 25, 50, 75, 100):
        x = left + plot * tick / 100
        parts.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" '
                     f'y2="{top + row * len(dimensions) - 12}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{top + row * len(dimensions) + 6}" font-size="11" '
                     f'fill="{INK_MUTED}" text-anchor="middle">{tick}</text>')

    for index, dimension in enumerate(dimensions):
        y0 = top + row * index
        parts.append(f'<text x="{left - 14}" y="{y0 + 18}" font-size="13" fill="{INK}" '
                     f'text-anchor="end">{_esc(dimension)}</text>')

        for slot, one in enumerate(series):
            value = one.values.get(dimension)
            if value is None:
                continue
            y = y0 + slot * (bar_h + gap)
            length = plot * max(0.0, min(value, 100.0)) / 100
            parts.append(_bar(left, y, length, bar_h, SERIES[slot % len(SERIES)]))
            # Direct-labelled, always: identity and magnitude must both survive greyscale.
            parts.append(f'<text x="{left + length + 7:.1f}" y="{y + bar_h - 3}" font-size="11.5" '
                         f'fill="{INK_2}">{value:.1f}</text>')

        if ceiling and dimension in ceiling:
            x = left + plot * max(0.0, min(ceiling[dimension], 100.0)) / 100
            span = len(series) * (bar_h + gap) - gap
            parts.append(f'<line x1="{x:.1f}" y1="{y0 - 4}" x2="{x:.1f}" y2="{y0 + span + 4}" '
                         f'stroke="{INK_MUTED}" stroke-width="2" stroke-dasharray="3 2"/>')

    legend_y = height - 22
    x = 24
    for slot, one in enumerate(series):
        parts.append(f'<rect x="{x}" y="{legend_y - 9}" width="11" height="11" rx="2.5" '
                     f'fill="{SERIES[slot % len(SERIES)]}"/>')
        parts.append(f'<text x="{x + 17}" y="{legend_y}" font-size="12" fill="{INK_2}">'
                     f'{_esc(one.label)}</text>')
        x += 22 + 7.1 * len(one.label)
    if ceiling:
        parts.append(f'<line x1="{x + 3}" y1="{legend_y - 10}" x2="{x + 3}" y2="{legend_y + 2}" '
                     f'stroke="{INK_MUTED}" stroke-width="2" stroke-dasharray="3 2"/>')
        parts.append(f'<text x="{x + 12}" y="{legend_y}" font-size="12" fill="{INK_2}">'
                     f'reference scored against itself (the achievable ceiling)</text>')

    parts.append("</svg>")
    return "\n".join(parts)


def strip(title: str, subtitle: str, rows: list[tuple[str, list[float], float]]) -> str:
    """One dot per example, per dimension — the spread a mean hides.

    A mean over five examples and a mean over thirty look identical in a bar chart and are
    not the same claim. This is the chart that says which one you are reading.
    """
    left, right, top = 132, 74, 76
    row = 40
    width = 760
    height = top + row * len(rows) + 58

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="ui-sans-serif,-apple-system,'
        f'\'Segoe UI\',Helvetica,Arial,sans-serif" role="img" '
        f'aria-label="{_esc(title)}. {_esc(subtitle)}">',
        f'<rect width="{width}" height="{height}" fill="{SURFACE}"/>',
        f'<text x="24" y="34" font-size="17" font-weight="650" fill="{INK}">{_esc(title)}</text>',
        f'<text x="24" y="55" font-size="12.5" fill="{INK_2}">{_esc(subtitle)}</text>',
    ]

    plot = width - left - right
    for tick in (0, 25, 50, 75, 100):
        x = left + plot * tick / 100
        parts.append(f'<line x1="{x:.1f}" y1="{top - 10}" x2="{x:.1f}" '
                     f'y2="{top + row * len(rows) - 14}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{top + row * len(rows) + 4}" font-size="11" '
                     f'fill="{INK_MUTED}" text-anchor="middle">{tick}</text>')

    for index, (label, values, mean) in enumerate(rows):
        y = top + row * index
        parts.append(f'<text x="{left - 14}" y="{y + 5}" font-size="13" fill="{INK}" '
                     f'text-anchor="end">{_esc(label)}</text>')
        parts.append(f'<line x1="{left}" y1="{y}" x2="{left + plot}" y2="{y}" '
                     f'stroke="{GRID}" stroke-width="1"/>')
        for value in values:
            x = left + plot * max(0.0, min(value, 100.0)) / 100
            # A surface-coloured ring, so overlapping dots stay countable.
            parts.append(f'<circle cx="{x:.1f}" cy="{y}" r="4" fill="{SERIES[0]}" '
                         f'fill-opacity="0.55" stroke="{SURFACE}" stroke-width="1.5"/>')
        x = left + plot * max(0.0, min(mean, 100.0)) / 100
        parts.append(f'<line x1="{x:.1f}" y1="{y - 11}" x2="{x:.1f}" y2="{y + 11}" '
                     f'stroke="{SERIES[1]}" stroke-width="2"/>')
        parts.append(f'<text x="{left + plot + 8}" y="{y + 4}" font-size="11.5" '
                     f'fill="{INK_2}">{mean:.0f}</text>')

    legend_y = height - 20
    parts.append(f'<circle cx="30" cy="{legend_y - 4}" r="4" fill="{SERIES[0]}" '
                 f'fill-opacity="0.55" stroke="{SURFACE}" stroke-width="1.5"/>')
    parts.append(f'<text x="42" y="{legend_y}" font-size="12" fill="{INK_2}">one example</text>')
    parts.append(f'<line x1="150" y1="{legend_y - 10}" x2="150" y2="{legend_y + 2}" '
                 f'stroke="{SERIES[1]}" stroke-width="2"/>')
    parts.append(f'<text x="158" y="{legend_y}" font-size="12" fill="{INK_2}">mean</text>')
    parts.append("</svg>")
    return "\n".join(parts)


# ------------------------------------------------------------------------- assembling


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(out)


def publish(runs: dict[str, Any], out_dir: Path) -> Path:
    """Write the JSON snapshots, the charts, and a report that reads them.

    `runs` is whatever was found: a SlidesBench run per variant, a corpus run, a task-suite
    scorecard. Missing pieces are omitted rather than faked — a report that shows an empty
    chart for a benchmark nobody ran is how a placeholder becomes a claim.
    """
    charts = out_dir / "charts"
    results = out_dir / "results"
    charts.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)

    sections: list[str] = []
    slides = {name: run for name, run in runs.items()
              if str(run.get("benchmark", "")).startswith("SlidesBench")}

    if slides:
        dimensions = ["match", "text", "position", "color"]
        series = [Series(label=name, values={d: (run["ours"].get(d) or 0.0) for d in dimensions})
                  for name, run in sorted(slides.items())]
        first = next(iter(sorted(slides.items())))[1]
        ceiling = {d: (first["identity_baseline"].get(d) or 0.0) for d in dimensions}
        n = max(run["scored"] for run in slides.values())

        svg = grouped_bars(
            "SlidesBench · scored by their evaluator",
            f"domain food · n={n} · every bar against the ceiling their own reference reaches",
            dimensions, series, ceiling)
        (charts / "slidesbench-dimensions.svg").write_text(svg, encoding="utf-8")

        rows = []
        for dimension in dimensions:
            values = [r["scores"][dimension] * 100 for run in slides.values()
                      for r in run["results"] if r["ok"] and dimension in r["scores"]]
            if values:
                rows.append((dimension, values, sum(values) / len(values)))
        if rows:
            (charts / "slidesbench-spread.svg").write_text(
                strip("SlidesBench · every example, not just the mean",
                      "each dot is one slide; the mean is the vertical rule", rows),
                encoding="utf-8")

        sections.append("## SlidesBench\n\n![](charts/slidesbench-dimensions.svg)\n")
        sections.append("![](charts/slidesbench-spread.svg)\n")
        sections.append(_table(
            ["dimension", *sorted(slides), "ceiling", "reading"],
            [[d, *[f"{slides[k]['ours'].get(d) or 0:.1f}" for k in sorted(slides)],
              f"{ceiling[d]:.1f}", _READING[d]] for d in dimensions]) + "\n")
        for name, run in sorted(slides.items()):
            results_path = results / f"slidesbench-{run['domain']}-{name}.json"
            results_path.write_text(json.dumps(run, indent=1), encoding="utf-8")

    corpus = runs.get("corpus")
    if corpus:
        (results / "corpus.json").write_text(json.dumps(corpus, indent=1), encoding="utf-8")
        sections.append("## Round trip · no model, no judge\n\n" + _table(
            ["corpus", "decks", "opened", "preserved", "slides", "opaque shapes"],
            [[Path(corpus["corpus"]).name, str(corpus["decks"]),
              f"{corpus['opened']} ({corpus['open_rate']:.0%})",
              f"**{corpus['preserved']}/{corpus['opened']}** "
              f"({corpus['preservation_rate']:.0%})",
              str(corpus["slides"]), str(corpus["opaque_shapes"])]]) + "\n")

    tasks = runs.get("tasks")
    if tasks:
        (results / "tasks.json").write_text(json.dumps(tasks, indent=1), encoding="utf-8")
        card = tasks["scorecard"]
        sections.append("## Task suite · measured, ours\n\n" + _table(
            ["tasks", "met brief", "fit rate", "refused writes", "violations"],
            [[f"{card['ran']}/{card['tasks']}", f"{card['met']}/{card['tasks']}",
              f"**{card['fit_rate']:.3f}** ({card['clean_slides']}/{card['slides']} slides)",
              f"{card['refusals']} of {card['tool_calls']}", str(card["violations"])]]) + "\n")

    # Caveats travel with the benchmark that needs them. A report carrying warnings about a
    # benchmark nobody ran reads as boilerplate, and boilerplate is what people skip.
    caveats = _CAVEATS + (_SLIDESBENCH_CAVEATS if slides else "")
    report = out_dir / "README.md"
    report.write_text(_PREAMBLE + "\n" + "\n".join(sections) + caveats, encoding="utf-8")
    return report


_READING = {
    "match": "fair — did the right blocks get built",
    "text": "fair — variant-dependent, see below",
    "position": "diagnostic — we derive geometry, never copy coordinates",
    "color": "diagnostic — their metric compares `FillFormat` objects",
}

_PREAMBLE = """# Benchmark results

Generated by `ppt-harness bench publish`. Every figure is drawn against the ceiling that
figure can actually reach, and every JSON that produced a chart is in `results/`.

"""

_CAVEATS = """
## What these numbers do not say

- **Nothing here is a comparison to another system.** These scores came from one model;
  published baselines used others. Until the same model is run through a no-harness control,
  a gap measures the model and the harness together and cannot tell you which moved.
- **A measured number is not a good deck.** Fit, preservation and refusals say a deck is
  sound. Whether it argues anything is a judgement, and no figure here makes it.
"""

_SLIDESBENCH_CAVEATS = """\
- **`color` and `position` are diagnostics, not grades.** SlidesBench scores similarity to a
  reference slide's coordinates and fills; this harness derives geometry from components and
  exposes no tool that accepts a coordinate, so a low score restates a design decision.
- **The ceiling is not 100.** Their reference deck scored against itself reaches 0-37 on
  colour, which is why every chart draws it.
"""
