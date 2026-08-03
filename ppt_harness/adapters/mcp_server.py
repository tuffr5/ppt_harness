"""MCP server — DESIGN §10.

Exposes the harness to any MCP host. The tool table, the mode gate, and the verification
that travels with every write are all the router's; this module only speaks JSON-RPC over
stdio.

Why verification-in-the-result matters most here: under MCP you cannot force a host's model
to call `render` after a write. If a write returned nothing but "ok", a host could cheerfully
report success on a slide that overflows. Every mutating tool therefore carries its own
measurement back.

The stdio loop is implemented directly rather than through the `mcp` SDK so the server runs
with no dependency beyond the standard library. `python -m ppt_harness.adapters.mcp_server
deck.pptx` is the whole invocation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from ..core import skills
from ..core.session import Session
from ..tools import router

PROTOCOL_VERSION = "2025-06-18"
SERVER = {"name": "ppt-harness", "version": "0.1.0"}


class Server:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- JSON-RPC ---------------------------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}

        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                # Prompts are how a playbook reaches a user: something they invoke by name,
                # rather than a tool the model calls on its own initiative.
                "capabilities": {"tools": {"listChanged": False},
                                 "prompts": {"listChanged": False}},
                "serverInfo": SERVER,
                "instructions": self._instructions(),
            }
        elif method in ("notifications/initialized", "notifications/cancelled"):
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": router.schemas()}
        elif method == "tools/call":
            result = self._call(params.get("name", ""), params.get("arguments") or {})
        elif method == "prompts/list":
            result = {"prompts": [s.mcp() for s in skills.playbooks()]}
        elif method == "prompts/get":
            try:
                result = self._prompt(params.get("name", ""), params.get("arguments") or {})
            except ValueError as exc:
                # Asking for a prompt that does not exist is the caller's mistake, not the
                # server's; -32602 says so, where the generic handler would report -32603
                # and imply the server broke.
                return self._error(request_id, -32602, str(exc))
        else:
            return self._error(request_id, -32601, f"unknown method {method!r}")

        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = router.dispatch(self.session, name, arguments)
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False,
                                                            indent=1)}],
            # A refused tool is a normal outcome the model should read and act on, so the
            # message body carries the reason. `isError` marks it without hiding it.
            "isError": not payload.get("ok", True),
        }

    def _prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One playbook, filled in and addressed to the deck actually open.

        The deck context is prepended rather than left to the host: a playbook that opened
        with "look at the deck" would spend its first turn on `get_outline`, and the outline
        is already known here.
        """
        try:
            skill = skills.get(name)
        except skills.SkillError as exc:
            raise ValueError(str(exc)) from exc
        if skill.kind != "playbook":
            raise ValueError(f"{name!r} is an invariant, not a playbook; it is injected "
                             "rather than invoked")

        body = skill.prompt(**{k: str(v) for k, v in arguments.items()})
        return {
            "description": skill.description,
            "messages": [{
                "role": "user",
                "content": {"type": "text",
                            "text": f"{self._instructions()}\n\n---\n\n{body}"},
            }],
        }

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _instructions(self) -> str:
        outline = self.session.outline()
        modes = {s["mode"] for s in outline["slides"]}
        lines = [
            f"Deck '{outline['deck']}' — {len(outline['slides'])} slides, theme "
            f"{outline['theme']}.",
            "",
            "Slides carry a mode. Freeform slides came from an imported file and hold the "
            "original author's shapes; edit their text with set_text. Managed slides are "
            "built from components; name a component and a variant and let the harness "
            "place it. Never ask for a coordinate — no tool takes one.",
            "",
            "Every write is budget-checked before it lands and returns its own measurement. "
            "A budget_exceeded error carries the capacity, the overage, and the ways out; "
            "work the ways out in order and never shrink the font.",
        ]
        if "freeform" in modes and self.session.deck.source_path:
            lines.append("")
            lines.append(
                f"This deck was imported from {Path(self.session.deck.source_path).name}. "
                "Export mutates that original package, so SmartArt, media, animations, and "
                "comments survive."
            )
        return "\n".join(lines)

    # -- transport --------------------------------------------------------------

    def serve(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                self._write(stdout, self._error(None, -32700, "parse error"))
                continue
            try:
                response = self.handle(request)
            except Exception as exc:  # a crashed tool must not take the server with it
                response = self._error(request.get("id"), -32603, f"{type(exc).__name__}: {exc}")
            if response is not None:
                self._write(stdout, response)

    @staticmethod
    def _write(stdout: TextIO, payload: dict[str, Any]) -> None:
        stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stdout.flush()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    session = Session.open(argv[0]) if argv else Session.blank()
    Server(session).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
