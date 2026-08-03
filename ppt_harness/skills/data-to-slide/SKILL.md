---
name: data-to-slide
kind: playbook
description: Turn numbers into a slide that argues something — when someone says "chart this", "put the results on a slide", or hands over a spreadsheet with no stated claim; when a chart is chosen before the point is decided; or when a table has more rows than anyone will read. Use when you have data and do not yet know what to show.
arguments: [claim, dataset]
tools: [load_data, query_data, list_datasets, add_chart, set_chart_data, add_table, set_cell, add_slide, set_text]
---

# Data to slide

A chart is an argument with axes. Choosing one before you know the claim produces a picture
of your data rather than a point about it — which is why the encoding is the *last* decision
here, not the first.

**The claim, if stated:** {{claim}}
**Dataset:** {{dataset}}

## 1. Get the real numbers

`load_data` on the file. It returns the shape, the column types, and five preview rows.

**Do not read numbers off the preview and put them on a slide.** It is a sample, it is not
sorted, and it is there so you can see what the columns hold. Every figure that reaches a
slide comes from `query_data`, which returns the query alongside the answer so the number
can be traced back.

If there is no file — the numbers are in the conversation, or nobody has supplied them —
say so plainly and use what you were given. Never imply a source you do not have.

## 2. Say the claim in one sentence

Before any chart: what should the reader conclude? "AMER carried the quarter at 2.1× EMEA"
is a claim. "Revenue by region" is a subject heading.

Run the query that tests it. If the numbers do not support the claim, **say so and stop** —
that is the most valuable thing this playbook can produce, and the one a chart-first
workflow never surfaces.

## 3. Let the claim pick the encoding

| The claim is about | Reach for | Not |
|---|---|---|
| One thing being bigger | Bars, sorted by value | A pie |
| Change over time | Line, time on x | Bars per period |
| Composition of a whole | Stacked bar, or one big share | A pie beyond ~5 slices |
| Two variables moving together | Scatter | Two lines on twin axes |
| Precise values people will look up | A table | Any chart |
| One number that matters | Large type, alone | A one-bar chart |

Sort bars by value, not alphabetically, unless the categories have a natural order — an
alphabetical bar chart makes the reader do the ranking you were paid to do.

A table earns its place when someone needs to *find* a value. If nobody will look one up,
it is a chart. If it has more than about eight rows on a slide, it is an appendix.

## 4. Honest axes

- **Bar charts start at zero.** A truncated bar axis exaggerates a difference and is the
  single most common way a chart lies. Line charts need not — a trend read against a
  meaningful band is fine.
- **Label the unit and the denominator.** "Revenue" is not a unit. "Revenue, $000, ex-VAT"
  is. A percentage without its base is unreadable.
- **Say n where it changes the reading.** Three customers churning out of five is not the
  same finding as three out of five thousand.
- **Do not put two scales on one chart.** Twin axes can be made to show any relationship
  you like, which is exactly why nobody should trust one.

## 5. Build it, and title it with the claim

Feed `set_chart_data` from the `chartable` block a query returns — categories and series
already shaped — rather than retyping the values. Retyping is where a digit changes.

Then title the slide with the **claim**, not the subject. `add_chart` places the chart; the
title is what makes it an argument. "AMER carried the quarter" beats "Revenue by region"
every time, and if you cannot write that title, go back to step 2.

## What to say when you are done

The claim, the number that carries it, and where it came from — dataset and query. Then the
thing you noticed that nobody asked about: the outlier, the missing quarter, the category
that moved the other way. That is usually the more interesting slide.
