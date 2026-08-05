"""Canonical slot rendering — DESIGN §1.5.

One function turning a slot value into the text it will occupy. It lives here, beside the
model, because three layers need the same answer and any disagreement between them is a
bug that only shows up on someone's slide:

- the **budget** measures it to decide whether the content fits;
- the **preview** lays it out;
- the **exporter** writes it.

The bug this replaces: the budget knew how to render a `list` but not a `tabular`, so it
measured a twenty-four-row table as the empty string and let it through. A table of any
size passed a gate whose entire purpose is to catch exactly that.

Note this is what the content *occupies*, not what is written. A `tabular` slot is written
as a real table and a `chart` as a native chart; rendering them to text here is how their
footprint is measured, nothing more.
"""

from __future__ import annotations

from typing import Any

#: How large a stat's label is set relative to its figure. Defined here, with the canonical
#: rendering, because the preview's CSS and the exporter's run sizes must agree on it — the
#: preview *is* the export rendered, and two copies of this number is the shape of the bug
#: that makes them differ.
STAT_LABEL_EM = 0.36


def is_stat(item: Any) -> bool:
    """A `{value, label}` pair — a figure with its subject, set as two lines in one cell."""
    return isinstance(item, dict) and bool(item.get("value")) and bool(item.get("label"))


def is_icon(item: Any) -> bool:
    """An `{icon, label}` pair — a mark with the word it stands for, in one cell.

    The mark is drawn as geometry beside or above the label, so it occupies *no text*: the
    line this item takes up is the label and nothing else. That is why `_line` returns the
    label alone rather than composing something for the icon — a budget that charged an icon
    to the text would refuse labels that render with room to spare, and the mark's own
    rectangle is already taken out of the cell by `render/expand` before the budget sees it.
    """
    return isinstance(item, dict) and bool(item.get("icon")) and bool(item.get("label"))


def slot_text(value: Any) -> str:
    """A slot value as the lines it will take up."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _from_dict(value)
    if isinstance(value, list):
        return "\n".join(_line(item) for item in value)
    return str(value)


def _from_dict(value: dict[str, Any]) -> str:
    if "rows" in value:  # tabular
        grid = ([value.get("headers") or []] if value.get("headers") else []) + list(
            value.get("rows") or [])
        return "\n".join(" ".join(str(cell) for cell in row) for row in grid)
    if "series" in value:
        # A chart occupies its frame whatever its data says: PowerPoint lays out the plot
        # area, and that is the one place it is allowed to (DESIGN §6.1). Measuring the
        # category labels as stacked lines would reject a chart for having many bars, which
        # is a legibility question and belongs in `max_items`, not in a text budget.
        return ""
    return _line(value)


def _line(item: Any) -> str:
    """One list item as the lines it will occupy.

    A `{value, label}` pair is a **stat**: a figure with the thing it measures written under
    it, and both lines belong on the slide. `label or value` — which is what this did — threw
    the figure away, so `stat_row`, the one component whose stated purpose is "three or four
    headline numbers", exported the words and dropped every number. It was invisible because
    the preview reads the same helper: both agreed, and both were wrong.

    An `{icon, label}` pair is the opposite case and falls out of `label or value` correctly:
    the icon is geometry in its own rectangle, so the *text* of the item is the label alone.
    """
    if isinstance(item, dict):
        value, label = item.get("value"), item.get("label")
        if value and label:
            return f"{value}\n{label}"
        head = label or value or ""
        desc = item.get("desc")
        return f"{head} — {desc}" if desc else str(head)
    return str(item)
