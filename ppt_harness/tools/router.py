"""Router — DESIGN §4, §3 of the turn.

One entry point for every transport. It resolves the tool, gates it on the target slide's
mode, dispatches, and normalizes both success and refusal into the same shape so a caller
never has to distinguish "the tool failed" from "the tool threw".

Refusals are explanatory, never coercive. Asking for a managed operation on an imported
slide is a reasonable thing for a model to try; the answer says why the slide is freeform
and what to use instead, so the next attempt is better rather than merely different.
"""

from __future__ import annotations

from typing import Any

from ..core.session import Session
from ..io.export_mutate import ExportError
from ..state.document import Mode
from ..state.store import Locked, StoreError
from . import (  # noqa: F401  -- import registers the tools
    assets_,
    constraints,
    data,
    deck,
    freeform_,
    managed,
    objects,
    preferences_,
    shared,
    text_props,
)
from .base import REGISTRY, Tool, ToolError


def tools(mode: Mode | None = None) -> list[Tool]:
    """The tool set, optionally filtered to what is legal in `mode`.

    Filtering at the transport boundary is worth more than rejecting at dispatch: a model
    that is never shown `set_variant` on an imported deck cannot waste a turn calling it.
    """
    if mode is None:
        return list(REGISTRY.values())
    return [t for t in REGISTRY.values() if t.gate in ("shared", mode.value)]


def schemas(mode: Mode | None = None) -> list[dict[str, Any]]:
    """MCP shape."""
    return [t.mcp() for t in tools(mode)]


def openai_schemas(mode: Mode | None = None) -> list[dict[str, Any]]:
    """OpenAI chat function-calling shape."""
    return [t.openai() for t in tools(mode)]


def anthropic_schemas(mode: Mode | None = None) -> list[dict[str, Any]]:
    """Anthropic Messages shape."""
    return [t.anthropic() for t in tools(mode)]


def _target_slide(session: Session, args: dict[str, Any]) -> str | None:
    if "slide_id" in args:
        return args["slide_id"]
    target = args.get("target")
    if isinstance(target, str) and "/" in target:
        return target.split("/")[0]
    return None


def dispatch(session: Session, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = dict(args or {})
    tool = REGISTRY.get(name)
    if tool is None:
        return ToolError("unknown_tool",
                         f"no tool {name!r}; have {sorted(REGISTRY)}").as_result()

    if tool.gate != "shared":
        slide_id = _target_slide(session, args)
        if slide_id is not None:
            try:
                slide = session.slide(slide_id)
            except StoreError as exc:
                return ToolError("no_slide", str(exc)).as_result()
            if slide.mode.value != tool.gate:
                return ToolError(
                    "wrong_mode",
                    f"{name} is a {tool.gate} tool but {slide_id} is {slide.mode.value}. "
                    + _advice(slide.mode),
                    mode=slide.mode.value,
                ).as_result()

    try:
        result = tool.handler(session, **args)
    except ToolError as exc:
        return exc.as_result()
    except Locked as exc:
        return ToolError("locked", str(exc)).as_result()
    except StoreError as exc:
        return ToolError("bad_target", str(exc)).as_result()
    except ExportError as exc:
        # A refusal, not an escaped exception. The writer raises when it cannot represent a
        # deck, and that answer belongs to whoever asked — a model that can read it and try
        # something else, or a caller that can report it. Escaping the router instead took
        # down a 32-example benchmark run mid-flight, and under MCP it would have been a
        # transport error rather than a tool result.
        return ToolError("export_failed", str(exc)).as_result()
    except TypeError as exc:
        return ToolError("bad_arguments", f"{name}: {exc}").as_result()

    if isinstance(result, dict):
        result.setdefault("ok", True)
        return result
    return {"ok": True, "result": result}


def _advice(mode: Mode) -> str:
    if mode is Mode.FREEFORM:
        return ("Freeform slides come from an imported file; the harness holds their "
                "original shapes rather than components. Use set_text to edit text.")
    return ("Managed slides are built from components; they have no free-floating shapes "
            "to manipulate.")
