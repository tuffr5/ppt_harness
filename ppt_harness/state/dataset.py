"""Datasets — the numbers a deck is about.

Without this, a model asked to "chart our Q3 numbers" has exactly one way to comply: type
plausible figures into `set_chart_data` and let the budget check pass. Nothing in the
harness could tell an invented chart from a true one, because there was no true one to
compare against.

Three decisions make that harder to do by accident:

- **The model asks questions; it never sees the table.** `load_data` returns a shape and a
  handful of preview rows. Everything else goes through `query`, which returns a result
  small enough to put on a slide. A 50k-row file never enters the context, so there is
  nothing to paraphrase from memory.
- **A result carries its provenance.** Every `Result` knows the dataset and the query that
  produced it, so a chart built from one can record where its numbers came from — and a
  chart that records nothing was typed by hand, which is exactly the distinction that was
  previously invisible.
- **Stdlib only.** csv and json, no pandas. Slide-sized aggregates do not need a dataframe,
  and a hard dependency on one would be a large cost for arithmetic this simple.

Values are typed on read — a column of numerals becomes numbers — because a chart needs
numbers and `"1,234"` sorts before `"9"`.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: How many rows come back with `load_data`. Enough to see what the columns hold and catch a
#: header row that is really data; too few to invite reading numbers off the preview.
PREVIEW = 5

#: A query result is bound for a slide. Past this it is a spreadsheet, and the honest answer
#: is that it does not belong on one.
MAX_RESULT_ROWS = 60

AGGREGATES = ("sum", "mean", "count", "min", "max")

_NUMERIC = re.compile(r"^-?[\d,]*\.?\d+%?$")


class DataError(RuntimeError):
    """A dataset problem written for the model, not a log file."""


def _coerce(value: str) -> Any:
    """A cell as the type it means.

    Thousands separators and a trailing `%` are stripped, because a spreadsheet exports them
    and arithmetic cannot survive them. A percentage becomes its numeric part, not a
    fraction — `45%` is 45, since that is what a reader expects on an axis.
    """
    text = (value or "").strip()
    if not text:
        return None
    if _NUMERIC.match(text):
        cleaned = text.rstrip("%").replace(",", "")
        try:
            return float(cleaned) if "." in cleaned else int(cleaned)
        except ValueError:
            return text
    return text


@dataclass
class Result:
    """A small answer, and where it came from."""

    columns: list[str]
    rows: list[list[Any]]
    source: dict[str, Any] = field(default_factory=dict)
    """`{dataset, query}` — what a chart records so its numbers can be traced."""
    truncated: int = 0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": len(self.rows),
            "source": self.source,
        }
        if self.truncated:
            out["truncated"] = self.truncated
            out["note"] = (f"{self.truncated} further row(s) not returned; narrow the query "
                           "rather than putting them on a slide")
        # Shaped so it can go straight into `set_chart_data` when it has the right form: a
        # label column and one or more numeric columns is exactly a categories/series pair.
        if len(self.columns) >= 2 and self.rows and all(
                isinstance(r[0], str) for r in self.rows):
            numeric = [i for i in range(1, len(self.columns))
                       if all(isinstance(r[i], (int, float)) for r in self.rows)]
            if numeric:
                out["chartable"] = {
                    "categories": [r[0] for r in self.rows],
                    "series": [{"name": self.columns[i],
                                "values": [r[i] for r in self.rows]} for i in numeric],
                }
        return out


@dataclass
class Dataset:
    name: str
    columns: list[str]
    rows: list[dict[str, Any]]
    source_path: str | None = None

    # -- reading ----------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str, name: str | None = None) -> Dataset:
        path = Path(path)
        if not path.is_file():
            raise DataError(f"no file at {path}")
        suffix = path.suffix.lower()
        if suffix in (".csv", ".tsv", ".txt"):
            rows = cls._read_delimited(path, "\t" if suffix == ".tsv" else ",")
        elif suffix == ".json":
            rows = cls._read_json(path)
        else:
            raise DataError(
                f"{path.name}: only .csv, .tsv and .json are read. Convert it, or paste the "
                "numbers into the conversation and say where they came from."
            )
        if not rows:
            raise DataError(f"{path.name} has no rows")
        columns = list(rows[0])
        return cls(name=name or path.stem, columns=columns, rows=rows,
                   source_path=str(path))

    @staticmethod
    def _read_delimited(path: Path, delimiter: str) -> list[dict[str, Any]]:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            if not reader.fieldnames:
                raise DataError(f"{path.name} has no header row")
            return [{(k or "").strip(): _coerce(v) for k, v in row.items()
                     if k is not None} for row in reader]

    @staticmethod
    def _read_json(path: Path) -> list[dict[str, Any]]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            # A single object, or `{"data": [...]}` — the two shapes an export actually takes.
            listed = next((v for v in raw.values() if isinstance(v, list)), None)
            raw = listed if listed is not None else [raw]
        if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
            raise DataError(f"{path.name}: expected a list of objects")
        keys: list[str] = []
        for row in raw:
            keys += [k for k in row if k not in keys]
        return [{k: row.get(k) for k in keys} for row in raw]

    # -- describing --------------------------------------------------------------

    def dtype(self, column: str) -> str:
        values = [r.get(column) for r in self.rows if r.get(column) is not None]
        if values and all(isinstance(v, (int, float)) for v in values):
            return "number"
        return "text"

    def summary(self) -> dict[str, Any]:
        return {
            "dataset": self.name,
            "rows": len(self.rows),
            "columns": [{"name": c, "type": self.dtype(c)} for c in self.columns],
            "preview": [[r.get(c) for c in self.columns] for r in self.rows[:PREVIEW]],
            "source": self.source_path,
        }

    # -- querying ----------------------------------------------------------------

    def query(self, *, select: list[str] | None = None, group_by: str | None = None,
              aggregate: dict[str, str] | None = None, where: str | None = None,
              sort: str | None = None, descending: bool = False,
              limit: int | None = None) -> Result:
        """One question, answered small.

        Deliberately not an expression language. Every clause here is a thing a slide
        actually needs, and each one that cannot be expressed is a question the model must
        ask the user rather than guess at — which is the safer failure.
        """
        rows = self._filter(self.rows, where)

        if group_by:
            if group_by not in self.columns:
                raise DataError(f"no column {group_by!r}; have {self.columns}")
            rows = self._aggregate(rows, group_by, aggregate or {})
            columns = list(rows[0]) if rows else [group_by]
        else:
            columns = select or self.columns
            missing = [c for c in columns if c not in self.columns]
            if missing:
                raise DataError(f"no column(s) {missing}; have {self.columns}")
            rows = [{c: r.get(c) for c in columns} for r in rows]

        if sort:
            if sort not in columns:
                raise DataError(f"cannot sort by {sort!r}; the result has {columns}")
            rows.sort(key=lambda r: (r.get(sort) is None, r.get(sort)),
                      reverse=descending)

        cap = min(limit or MAX_RESULT_ROWS, MAX_RESULT_ROWS)
        truncated = max(0, len(rows) - cap)
        rows = rows[:cap]

        return Result(
            columns=columns,
            rows=[[r.get(c) for c in columns] for r in rows],
            source={"dataset": self.name,
                    "query": {k: v for k, v in
                              {"select": select, "group_by": group_by,
                               "aggregate": aggregate, "where": where, "sort": sort,
                               "descending": descending or None, "limit": limit}.items()
                              if v}},
            truncated=truncated,
        )

    def _filter(self, rows: list[dict[str, Any]], where: str | None) -> list[dict[str, Any]]:
        """`column op value`, and nothing cleverer.

        No eval, ever: a filter that can run code is a filter that can read the filesystem,
        and the model composing it is not the one who should be trusted with that.
        """
        if not where:
            return list(rows)
        match = re.match(r"\s*(\w[\w \-]*?)\s*(>=|<=|!=|=|>|<|contains)\s*(.+?)\s*$", where)
        if not match:
            raise DataError(
                f"cannot read the filter {where!r}; write it as `column > 100`, "
                "`region = EMEA`, or `name contains north`")
        column, op, literal = match.group(1), match.group(2), match.group(3).strip("'\"")
        if column not in self.columns:
            raise DataError(f"no column {column!r}; have {self.columns}")
        wanted = _coerce(literal)

        def keep(row: dict[str, Any]) -> bool:
            value = row.get(column)
            if op == "contains":
                return str(wanted).lower() in str(value or "").lower()
            if op in ("=", "!="):
                same = str(value).lower() == str(wanted).lower()
                return same if op == "=" else not same
            if not isinstance(value, (int, float)) or not isinstance(wanted, (int, float)):
                return False
            return {">": value > wanted, "<": value < wanted,
                    ">=": value >= wanted, "<=": value <= wanted}[op]

        return [r for r in rows if keep(r)]

    def _aggregate(self, rows: list[dict[str, Any]], group_by: str,
                   aggregate: dict[str, str]) -> list[dict[str, Any]]:
        for column, how in aggregate.items():
            if how not in AGGREGATES:
                raise DataError(f"{how!r} is not an aggregate; use {list(AGGREGATES)}")
            if column not in self.columns:
                raise DataError(f"no column {column!r}; have {self.columns}")

        buckets: dict[Any, list[dict[str, Any]]] = {}
        for row in rows:
            buckets.setdefault(row.get(group_by), []).append(row)

        out = []
        for key, group in buckets.items():
            entry: dict[str, Any] = {group_by: key}
            if not aggregate:
                entry["count"] = len(group)
            for column, how in aggregate.items():
                values = [r.get(column) for r in group
                          if isinstance(r.get(column), (int, float))]
                entry[f"{how}_{column}"] = _apply(how, values, len(group))
            out.append(entry)
        return out


def _apply(how: str, values: list[float], group_size: int) -> Any:
    if how == "count":
        return group_size
    if not values:
        return None
    if how == "sum":
        total = math.fsum(values)
    elif how == "mean":
        total = math.fsum(values) / len(values)
    elif how == "min":
        total = min(values)
    else:
        total = max(values)
    # Rounded because these land on a slide: a bar labelled 41.66666666666667 is a bar
    # nobody proofread.
    return round(total, 4)
