"""Datasets and the data tools — `state/dataset.py`, `tools/data.py`.

These exist to close one hole: `set_chart_data` takes literal values, so before this a model
asked to chart numbers it had not been given could only invent them, and every layer
downstream would pass. The budget check does not care whether a figure is true.

So the properties worth defending are about *provenance and restraint*, not arithmetic: the
table never reaches the context whole, a result says which query produced it, and a filter
cannot run code.
"""

from __future__ import annotations

import json

import pytest

from ppt_harness.core.session import Session
from ppt_harness.state.dataset import MAX_RESULT_ROWS, DataError, Dataset
from ppt_harness.tools import router

CSV = """region,quarter,revenue,customers
EMEA,Q1,"1,200,000",340
EMEA,Q2,980000,310
AMER,Q1,2100000,520
AMER,Q2,2450000,545
APAC,Q1,640000,180
"""


@pytest.fixture
def sales(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text(CSV, encoding="utf-8")
    return path


@pytest.fixture
def session(sales) -> Session:
    s = Session.blank("Data")
    assert router.dispatch(s, "load_data", {"path": str(sales)})["ok"]
    return s


# ---------------------------------------------------------------------- reading


def test_numerals_become_numbers(sales) -> None:
    """A chart needs numbers, and `"1,234"` sorts before `"9"`."""
    d = Dataset.load(sales)
    assert d.rows[0]["revenue"] == 1200000
    assert d.dtype("revenue") == "number" and d.dtype("region") == "text"


def test_an_unreadable_format_says_what_to_do(tmp_path) -> None:
    bad = tmp_path / "deck.xlsx"
    bad.write_bytes(b"not really a spreadsheet")
    with pytest.raises(DataError, match="paste the numbers"):
        Dataset.load(bad)


def test_json_accepts_the_shapes_an_export_actually_takes(tmp_path) -> None:
    wrapped = tmp_path / "w.json"
    wrapped.write_text(json.dumps({"data": [{"a": 1}, {"a": 2}]}))
    assert len(Dataset.load(wrapped).rows) == 2


# ------------------------------------------------------------------- restraint


def test_loading_returns_a_description_not_the_table(session: Session, sales) -> None:
    """The whole file never enters the context. A 50k-row table in a prompt is a table the
    model will paraphrase from memory."""
    result = router.dispatch(session, "load_data", {"path": str(sales), "name": "again"})
    assert len(result["preview"]) <= 5
    assert result["rows"] == 5, "the count is reported even though the rows are not"


def test_the_preview_warns_against_being_read(session: Session, sales) -> None:
    result = router.dispatch(session, "load_data", {"path": str(sales), "name": "x"})
    assert "do not read them off the preview" in result["next"]


def test_a_result_is_capped(tmp_path) -> None:
    big = tmp_path / "big.csv"
    big.write_text("n\n" + "\n".join(str(i) for i in range(500)), encoding="utf-8")
    d = Dataset.load(big)
    result = d.query()
    assert len(result.rows) == MAX_RESULT_ROWS
    assert result.truncated == 500 - MAX_RESULT_ROWS
    assert "narrow the query" in result.as_dict()["note"]


# ------------------------------------------------------------------ provenance


def test_a_result_carries_the_query_that_made_it(session: Session) -> None:
    """What lets a chart record where its numbers came from — and what makes a chart with no
    source recognisably hand-typed."""
    out = router.dispatch(session, "query_data",
                          {"group_by": "region", "aggregate": {"revenue": "sum"}})
    assert out["source"]["dataset"] == "sales"
    assert out["source"]["query"]["group_by"] == "region"


def test_a_grouped_result_is_ready_to_chart(session: Session) -> None:
    """Retyping values into set_chart_data is where a digit changes."""
    out = router.dispatch(session, "query_data",
                          {"group_by": "region", "aggregate": {"revenue": "sum"},
                           "sort": "sum_revenue", "descending": True})
    chartable = out["chartable"]
    assert chartable["categories"] == ["AMER", "EMEA", "APAC"]
    assert chartable["series"][0]["values"][0] == 4550000


# --------------------------------------------------------------------- querying


def test_grouping_and_aggregating(session: Session) -> None:
    out = router.dispatch(session, "query_data",
                          {"group_by": "region", "aggregate": {"customers": "sum"}})
    assert dict(out["rows"])["EMEA"] == 650


def test_filtering(session: Session) -> None:
    out = router.dispatch(session, "query_data",
                          {"where": "revenue > 1000000", "select": ["region", "revenue"]})
    assert len(out["rows"]) == 3


def test_a_text_filter_is_case_insensitive(session: Session) -> None:
    out = router.dispatch(session, "query_data", {"where": "region = emea"})
    assert len(out["rows"]) == 2


def test_a_filter_cannot_run_code(session: Session) -> None:
    """No eval, ever. A filter that can run code can read the filesystem, and the model
    composing it is not the one to trust with that."""
    out = router.dispatch(session, "query_data",
                          {"where": "__import__('os').system('echo pwned')"})
    assert out["ok"] is False
    assert out["error"] == "bad_query"


def test_an_unknown_column_lists_the_real_ones(session: Session) -> None:
    out = router.dispatch(session, "query_data", {"group_by": "nope"})
    assert out["error"] == "bad_query"
    assert "region" in out["message"]


def test_an_unknown_aggregate_is_refused(session: Session) -> None:
    out = router.dispatch(session, "query_data",
                          {"group_by": "region", "aggregate": {"revenue": "median"}})
    assert out["error"] == "bad_query"
    assert "sum" in out["message"]


# ------------------------------------------------------------------ the refusals


def test_querying_with_nothing_loaded_says_what_to_do() -> None:
    """The refusal that matters: it must not leave inventing numbers as the obvious move."""
    out = router.dispatch(Session.blank("Empty"), "query_data", {})
    assert out["error"] == "no_data_loaded"
    assert "load_data" in out["message"]
    assert "rather than implying a source you do not have" in out["message"]


def test_two_datasets_must_be_told_apart(session: Session, sales) -> None:
    router.dispatch(session, "load_data", {"path": str(sales), "name": "second"})
    out = router.dispatch(session, "query_data", {})
    assert out["error"] == "ambiguous_dataset"


def test_datasets_never_reach_the_deck(session: Session) -> None:
    """The numbers are what the deck is *about*, not part of it. A dataset that travelled
    into the exported package would leak whatever else was in the file."""
    assert session.datasets
    assert "datasets" not in session.deck.model_dump()


def test_the_data_tools_take_no_coordinate() -> None:
    from ppt_harness.tools.base import REGISTRY

    for name in ("load_data", "query_data", "list_datasets"):
        blob = json.dumps(REGISTRY[name].schema)
        assert "font" not in blob.lower()
        for prop in (REGISTRY[name].schema.get("properties") or {}).values():
            assert prop.get("type") != "number"
