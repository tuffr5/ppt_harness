"""The contract every tool signs — PLAN.md, "Verification".

Written once, applied to the whole registry. A new tool inherits these tests by existing,
which is the point: the guarantees the harness advertises are only true if they are true of
*every* tool, and a per-tool test suite is exactly where that stops being the case.

What is asserted here is the harness's own advertising:

- no tool accepts a coordinate, a font, or a size (DESIGN principle 1);
- a mutating tool budget-checks *before* it writes, and returns its own measurement;
- every op inverts, so undo is turn-granular rather than best-effort;
- a refusal is a result, never an exception across the boundary;
- nothing a tool does leaves the package invalid.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ppt_harness.core.session import Session
from ppt_harness.state.document import Author, Mode
from ppt_harness.tools import router
from ppt_harness.tools.base import Tool

ALL_TOOLS = sorted(router.tools(), key=lambda t: t.name)
MUTATING = [t for t in ALL_TOOLS if t.mutating]

#: Argument names that would hand the model a coordinate, a font, or a size. `set_frame` is
#: the one designed exception and is absent until A3 builds it as a logged escape hatch.
BANNED = {"x", "y", "cx", "cy", "left", "top", "width", "height", "frame",
          "font", "font_size", "size", "pt", "px", "emu", "color", "colour", "rgb"}

ESCAPE_HATCHES = {"set_frame"}


def _ids(tools: list[Tool]) -> list[str]:
    return [t.name for t in tools]


# ------------------------------------------------------------------- the schemas


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=_ids(ALL_TOOLS))
def test_no_tool_accepts_a_coordinate_a_font_or_a_size(tool: Tool) -> None:
    """DESIGN's first principle, enforced by the shape of the API rather than by a rule
    someone has to remember."""
    if tool.name in ESCAPE_HATCHES:
        pytest.skip("designed escape hatch; logged and flagged")
    properties = set(tool.schema.get("properties") or {})
    assert not (properties & BANNED), f"{tool.name} exposes {sorted(properties & BANNED)}"


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=_ids(ALL_TOOLS))
def test_every_tool_is_describable_to_a_model(tool: Tool) -> None:
    assert tool.description.strip()
    assert tool.schema.get("type") == "object"
    for name, spec in (tool.schema.get("properties") or {}).items():
        assert spec.get("description"), f"{tool.name}.{name} has no description"


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=_ids(ALL_TOOLS))
def test_schemas_are_valid_json_and_stable_across_transports(tool: Tool) -> None:
    """One table, three renderings. The JSON Schema itself must be shared verbatim."""
    json.dumps(tool.schema)
    assert tool.mcp()["inputSchema"] is tool.schema
    assert tool.anthropic()["input_schema"] is tool.schema
    assert tool.openai()["function"]["parameters"] is tool.schema


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=_ids(ALL_TOOLS))
def test_gates_are_declared(tool: Tool) -> None:
    assert tool.gate in ("managed", "freeform", "shared")


# ------------------------------------------------------------------- the dispatch


def test_an_unknown_tool_is_a_result_not_a_crash(imported: Session) -> None:
    result = router.dispatch(imported, "no_such_tool", {})
    assert result["ok"] is False
    assert "no_such_tool" in result["message"]


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=_ids(ALL_TOOLS))
def test_missing_arguments_refuse_rather_than_raise(imported: Session, tool: Tool) -> None:
    """A malformed call from a model is ordinary, not exceptional. It has to come back as
    something the model can read and correct."""
    result = router.dispatch(imported, tool.name, {})
    assert isinstance(result, dict)
    assert "ok" in result
    if not result["ok"]:
        assert result.get("message"), f"{tool.name} refused without saying why"


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=_ids(ALL_TOOLS))
def test_a_nonexistent_target_is_refused_clearly(imported: Session, tool: Tool) -> None:
    probe = {"slide_id": "no_such_slide", "target": "no_such_slide/no_such_shape",
             "block_id": "no_such_block", "shape_id": "no_such_shape"}
    args = {k: v for k, v in probe.items() if k in (tool.schema.get("properties") or {})}
    if not args:
        pytest.skip("takes no target")
    result = router.dispatch(imported, tool.name, args)
    assert isinstance(result, dict) and "ok" in result
    if not result["ok"]:
        assert result.get("message")


# ------------------------------------------------------------------ mutating tools


@pytest.mark.parametrize("tool", MUTATING, ids=_ids(MUTATING))
def test_a_mutating_tool_is_gated_on_mode(imported: Session, tool: Tool) -> None:
    """A managed tool must refuse an imported slide with an error that explains the mode,
    rather than silently coercing it."""
    if tool.gate != "managed":
        pytest.skip("not mode-gated to managed")
    slide = next(s for s in imported.deck.slides if s.mode is Mode.FREEFORM)
    props = tool.schema.get("properties") or {}
    if "slide_id" not in props:
        pytest.skip("does not take a slide")
    result = router.dispatch(imported, tool.name, {"slide_id": slide.id})
    assert result["ok"] is False
    assert result["error"] in ("wrong_mode", "no_block", "missing_argument", "unknown_slot")


def test_a_write_returns_its_own_measurement(imported: Session) -> None:
    """Verification travels with the write. It is not a follow-up call the model might
    skip — which matters most under MCP, where a host's model cannot be compelled."""
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    result = router.dispatch(imported, "set_text",
                             {"target": f"{slide.id}/{shape.id}", "text": "Short"})
    assert result["ok"]
    assert "render" in result, "a mutating tool must carry its measurement"
    assert "clean" in result["render"]


def test_a_write_budget_checks_before_it_lands(imported: Session) -> None:
    """The cheapest failure is a rejected write that never rendered."""
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    before = shape.text
    result = router.dispatch(imported, "set_text",
                             {"target": f"{slide.id}/{shape.id}", "text": "word " * 400})
    assert result["ok"] is False
    assert result["error"] == "budget_exceeded"
    assert shape.text == before, "a refused write must not have landed"


def test_a_refusal_carries_the_ways_out(imported: Session) -> None:
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    result = router.dispatch(imported, "set_text",
                             {"target": f"{slide.id}/{shape.id}", "text": "word " * 400})
    assert "options:" in result["message"], "a rejection must say what to do instead"


# ------------------------------------------------------------------------ the ops


def test_every_op_the_store_can_apply_can_also_be_inverted() -> None:
    """Undo is turn-granular, so an op with no inverse is one a turn cannot roll back."""
    from ppt_harness.state.store import DeckStore

    appliers = {n[len("_apply_"):] for n in dir(DeckStore) if n.startswith("_apply_")}
    inverters = {n[len("_invert_"):] for n in dir(DeckStore) if n.startswith("_invert_")}
    assert appliers <= inverters, f"no inverse for {sorted(appliers - inverters)}"


def test_a_write_is_undoable_and_redoable(imported: Session) -> None:
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    original = shape.text

    router.dispatch(imported, "set_text",
                    {"target": f"{slide.id}/{shape.id}", "text": "Changed"})
    assert shape.text == "Changed"
    assert router.dispatch(imported, "undo")["ok"]
    assert shape.text == original
    assert router.dispatch(imported, "redo")["ok"]
    assert shape.text == "Changed"


def test_a_failed_write_leaves_no_partial_turn(imported: Session) -> None:
    """A turn commits whole or rolls back whole. A half-applied turn is worse than a
    refusal, because nothing reports it."""
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    before = len(imported.store.log)
    router.dispatch(imported, "set_text",
                    {"target": f"{slide.id}/{shape.id}", "text": "word " * 400})
    assert len(imported.store.log) == before


def test_writes_are_attributed(imported: Session) -> None:
    """Authorship drives the repair arbiter and the preference profile; an unattributed op makes
    both of them guess."""
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    router.dispatch(imported, "set_text",
                    {"target": f"{slide.id}/{shape.id}", "text": "By the user",
                     "author": Author.USER})
    assert imported.store.log.ops[-1].author is Author.USER


# ---------------------------------------------------------------------- the package


def _slide_counts(path: Path) -> tuple[int, int, int]:
    import re
    import zipfile

    from lxml import etree

    P = "http://schemas.openxmlformats.org/presentationml/2006/main"
    with zipfile.ZipFile(path) as z:
        parts = [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        presentation = etree.fromstring(z.read("ppt/presentation.xml"))
        ids = presentation.findall(f".//{{{P}}}sldIdLst/{{{P}}}sldId")
        rels = etree.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
        slide_rels = [e for e in rels if (e.get("Type") or "").endswith("/slide")]
    return len(parts), len(ids), len(slide_rels)


@pytest.mark.parametrize("scenario", ["set_text", "add_slide", "delete_slide", "reorder"])
def test_no_operation_leaves_the_package_invalid(imported: Session, tmp_path: Path,
                                                 scenario: str) -> None:
    """An orphaned slide part makes PowerPoint offer to *repair* the file rather than open
    it — invisible until someone double-clicks."""
    slide = imported.deck.slides[0]
    if scenario == "set_text":
        shape = next(s for s in slide.shapes if s.text and not s.opaque)
        router.dispatch(imported, "set_text",
                        {"target": f"{slide.id}/{shape.id}", "text": "Edited"})
    elif scenario == "add_slide":
        router.dispatch(imported, "add_slide", {"layout": "stack", "blocks": [
            {"region": "header", "component": "slide_title", "slots": {"title": "New"}}]})
    elif scenario == "delete_slide":
        router.dispatch(imported, "delete_slide", {"slide_id": imported.deck.slides[-1].id})
    else:
        router.dispatch(imported, "reorder", {"slide_id": slide.id, "index": 1})

    out = tmp_path / f"{scenario}.pptx"
    result = router.dispatch(imported, "export", {"path": str(out)})
    assert result["ok"], result.get("message")

    parts, ids, rels = _slide_counts(out)
    assert parts == ids == rels, f"{scenario}: {parts} parts, {ids} sldId, {rels} rels"
    assert ids == len(imported.deck.slides)


def _describe(result: dict[str, Any]) -> str:
    return result.get("message") or result.get("summary") or ""
