"""MCP adapter — DESIGN §10.

The adapter must be a *rendering* of the tool table, not a second implementation. These
tests hold it to that, and check the one thing the transport is uniquely responsible for:
that a refused tool comes back as readable content rather than a protocol error, so the
host's model can act on the reason instead of just seeing a failure.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from ppt_harness.adapters.mcp_server import PROTOCOL_VERSION, Server
from ppt_harness.core.session import Session
from ppt_harness.tools import router


def _exchange(session: Session, requests: list[dict]) -> list[dict]:
    stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    stdout = io.StringIO()
    Server(session).serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def _payload(response: dict) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


def test_initialize_announces_the_protocol_and_the_deck(imported: Session) -> None:
    (response,) = _exchange(imported, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
    result = response["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "ppt-harness"
    assert "Freeform" in result["instructions"]


def test_instructions_teach_the_mode_model(imported: Session) -> None:
    """A host's model sees these once. They have to carry the two rules that keep the loop
    short: modes exist, and no tool takes a coordinate."""
    (response,) = _exchange(imported, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
    text = response["result"]["instructions"]
    assert "coordinate" in text
    assert "budget" in text
    assert "set_text" in text


def test_imported_decks_say_so_in_the_instructions(imported: Session,
                                                   fixture_deck: Path) -> None:
    (response,) = _exchange(imported, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
    assert fixture_deck.name in response["result"]["instructions"]


def test_tools_list_matches_the_router(imported: Session) -> None:
    """One table, three transports."""
    (response,) = _exchange(imported, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    served = {t["name"] for t in response["result"]["tools"]}
    assert served == {t.name for t in router.tools()}
    for tool in response["result"]["tools"]:
        assert "inputSchema" in tool and tool["description"]


def test_a_tool_call_returns_the_payload_as_text(imported: Session) -> None:
    (response,) = _exchange(imported, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "get_outline", "arguments": {}}}])
    assert response["result"]["isError"] is False
    payload = _payload(response)
    assert payload["ok"] is True
    assert len(payload["slides"]) == len(imported.deck.slides)


def test_a_refusal_is_readable_content_not_a_protocol_error(imported: Session) -> None:
    """A budget_exceeded is a normal outcome the model should read and act on. Hiding it in
    a JSON-RPC error would strip the capacity, the overage, and the ways out."""
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    (response,) = _exchange(imported, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "set_text",
                    "arguments": {"target": f"{slide.id}/{shape.id}",
                                  "text": "word " * 500}}}])
    assert "error" not in response
    assert response["result"]["isError"] is True
    payload = _payload(response)
    assert payload["error"] == "budget_exceeded"
    assert "options:" in payload["message"]


def test_notifications_get_no_response(imported: Session) -> None:
    assert _exchange(imported, [
        {"jsonrpc": "2.0", "method": "notifications/initialized"}]) == []


def test_an_unknown_method_is_a_protocol_error(imported: Session) -> None:
    (response,) = _exchange(imported, [
        {"jsonrpc": "2.0", "id": 7, "method": "resources/list"}])
    assert response["error"]["code"] == -32601


def test_malformed_json_does_not_kill_the_server(imported: Session) -> None:
    stdin = io.StringIO(
        "{not json\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n")
    stdout = io.StringIO()
    Server(imported).serve(stdin, stdout)
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["result"] == {}


def test_state_persists_across_calls(imported: Session) -> None:
    """One session, one deck. A write in one call must be visible in the next."""
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    responses = _exchange(imported, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "set_text",
                    "arguments": {"target": f"{slide.id}/{shape.id}",
                                  "text": "Persisted"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "get_slide", "arguments": {"slide_id": slide.id}}},
    ])
    texts = [s["text"] for s in _payload(responses[1])["shapes"]]
    assert "Persisted" in texts
