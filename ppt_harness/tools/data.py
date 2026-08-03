"""Data tools — getting real numbers onto a slide.

These exist to close the gap that made every data skill dishonest: `set_chart_data` takes
literal values, so a model asked to chart something it had not been given could only invent
plausible figures, and nothing downstream could tell. The budget check passes either way; a
fabricated chart fits as well as a true one.

The shape is deliberate. `load_data` returns a *description* — shape, column types, a few
preview rows — never the table. `query_data` answers one question at a time and returns a
result small enough for a slide, carrying the dataset and query that produced it. A model
that wants a number has to ask for it, and what comes back is traceable.

No coordinates here either: a query names columns and aggregates, never a position.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.session import Session
from ..state.dataset import AGGREGATES, DataError, Dataset
from .base import ToolError, integer, obj, string, tool


def _dataset(session: Session, name: str | None) -> Dataset:
    loaded = session.datasets
    if not loaded:
        raise ToolError(
            "no_data_loaded",
            "no dataset is open. Call load_data with a path to a .csv, .tsv or .json "
            "first — or, if the numbers only exist in the conversation, say so and use "
            "them directly rather than implying a source you do not have.")
    if name is None:
        if len(loaded) > 1:
            raise ToolError("ambiguous_dataset",
                            f"several datasets are open: {sorted(loaded)}. Name one.")
        return next(iter(loaded.values()))
    found = loaded.get(name)
    if found is None:
        raise ToolError("unknown_dataset",
                        f"no dataset {name!r}; loaded: {sorted(loaded)}")
    return found


@tool("load_data",
      "Open a .csv, .tsv or .json file as a named dataset. Returns its shape, column types "
      "and a few preview rows — never the whole table.",
      obj({"path": string("Path to the file"),
           "name": string("Name to refer to it by; defaults to the filename")},
          ["path"]),
      mutating=True)
def load_data(session: Session, path: str, name: str | None = None) -> dict[str, Any]:
    try:
        dataset = Dataset.load(Path(path), name)
    except DataError as exc:
        raise ToolError("bad_dataset", str(exc)) from exc
    session.datasets[dataset.name] = dataset
    summary = dataset.summary()
    summary["ok"] = True
    summary["next"] = ("ask query_data for the numbers you need; do not read them off the "
                       "preview, which is a sample and may not be representative")
    return summary


@tool("list_datasets", "Datasets currently open, with their shape.", obj({}))
def list_datasets(session: Session) -> dict[str, Any]:
    return {"datasets": [{"name": d.name, "rows": len(d.rows),
                          "columns": [c for c in d.columns], "source": d.source_path}
                         for d in session.datasets.values()]}


@tool("query_data",
      "Ask one question of a loaded dataset and get a slide-sized answer back. Filter, "
      "group, aggregate, sort, limit. The result carries the query that produced it.",
      obj({
          "dataset": string("Which dataset; omit when only one is open"),
          "select": {"type": "array", "items": {"type": "string"},
                     "description": "Columns to return; omit for all"},
          "where": string("One condition: `revenue > 100`, `region = EMEA`, "
                          "`name contains north`"),
          "group_by": string("Column to group by"),
          "aggregate": {"type": "object",
                        "description": f"Column -> one of {list(AGGREGATES)}"},
          "sort": string("Column of the result to sort by"),
          "descending": {"type": "boolean", "description": "Sort high to low"},
          "limit": integer("Maximum rows to return"),
      }))
def query_data(session: Session, dataset: str | None = None,
               select: list[str] | None = None, where: str | None = None,
               group_by: str | None = None, aggregate: dict[str, str] | None = None,
               sort: str | None = None, descending: bool = False,
               limit: int | None = None) -> dict[str, Any]:
    target = _dataset(session, dataset)
    try:
        result = target.query(select=select, where=where, group_by=group_by,
                              aggregate=aggregate, sort=sort, descending=descending,
                              limit=limit)
    except DataError as exc:
        raise ToolError("bad_query", str(exc)) from exc
    out = result.as_dict()
    out["ok"] = True
    return out
