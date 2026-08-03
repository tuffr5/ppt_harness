"""Tool registry — DESIGN §4.

Transport-agnostic by construction. A tool is a name, a JSON schema, a mode gate, and a
handler; MCP, the CLI, and the agent loop are three renderings of the same table rather
than three implementations.

Two rules the registry enforces rather than documents:

- **Mode gating.** Managed tools refuse freeform slides and vice versa, with an error that
  explains the mode rather than silently coercing.
- **Verification travels with the write.** Every mutating tool returns the resulting
  measurement in its own result, so verification is not a follow-up call the model might
  skip — which matters most under MCP, where you cannot force a host's model to call
  `render`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

Gate = Literal["managed", "freeform", "shared"]


class ToolError(RuntimeError):
    """A tool refused. The message is written for the model, not a log file."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_result(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": str(self), **self.detail}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[..., Any]
    gate: Gate = "shared"
    mutating: bool = False

    def mcp(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.schema}

    def anthropic(self) -> dict[str, Any]:
        """The same tool in Anthropic Messages shape."""
        return {"name": self.name, "description": self.description,
                "input_schema": self.schema}

    def openai(self) -> dict[str, Any]:
        """The same tool in OpenAI chat function-calling shape.

        One table, three renderings. The schema itself is plain JSON Schema and is shared
        verbatim — only the envelope differs, which is the point of keeping the registry
        transport-agnostic.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }


REGISTRY: dict[str, Tool] = {}


def tool(
    name: str,
    description: str,
    schema: dict[str, Any],
    gate: Gate = "shared",
    mutating: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in REGISTRY:
            raise RuntimeError(f"tool {name!r} registered twice")
        REGISTRY[name] = Tool(name, description, schema, fn, gate, mutating)
        return fn

    return register


# ------------------------------------------------------------------- schema sugar


def obj(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def string(desc: str, enum: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "string", "description": desc}
    if enum:
        out["enum"] = enum
    return out


def integer(desc: str) -> dict[str, Any]:
    return {"type": "integer", "description": desc}


@dataclass
class Diff:
    """What a mutating tool changed, plus the verification that it holds.

    `render` is the measurement, always. The image is added only when the session declares
    vision — correctness never depends on a model looking at a picture (DESIGN §10).
    """

    summary: str
    target: str
    before: Any = None
    after: Any = None
    render: dict[str, Any] = field(default_factory=dict)

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": True,
            "summary": self.summary,
            "target": self.target,
            "before": self.before,
            "after": self.after,
            "render": self.render,
        }
