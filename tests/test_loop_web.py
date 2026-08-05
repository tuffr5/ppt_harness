"""Agent loop and web client — ARCHITECTURE stages 2-9, DESIGN §10.

The loop is driven by a fake OpenAI-compatible client, so these tests are hermetic and
assert the parts that are actually ours: the tool schemas, the context we build, and
**termination** — the one thing a model cannot be trusted to decide for itself.

No test here calls a real endpoint. What a given model chooses to do is not a property of
this codebase.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import pytest
from conftest import DEMO
from fastapi.testclient import TestClient

from ppt_harness.adapters.web import create_app
from ppt_harness.core import loop
from ppt_harness.core.session import Session
from ppt_harness.state.document import Mode

# --------------------------------------------------------------- a fake endpoint


@dataclass
class _Fn:
    name: str
    arguments: str


@dataclass
class _Call:
    id: str
    function: _Fn
    type: str = "function"


@dataclass
class _Msg:
    content: str | None = None
    tool_calls: list[_Call] = field(default_factory=list)


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Response:
    choices: list[_Choice]


class FakeClient:
    """Replays a scripted sequence of assistant turns and records what it was sent."""

    def __init__(self, turns: list[_Msg]) -> None:
        self._turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.chat = self  # so `client.chat.completions.create` resolves
        self.completions = self

    def create(self, **kw: Any) -> _Response:
        self.requests.append(kw)
        message = self._turns.pop(0) if self._turns else _Msg(content="done")
        return _Response(choices=[_Choice(message=message)])


def _call(name: str, args: dict[str, Any], cid: str = "c1") -> _Call:
    return _Call(id=cid, function=_Fn(name=name, arguments=json.dumps(args)))


def _agent(session: Session, turns: list[_Msg], **kw: Any) -> loop.Agent:
    return loop.Agent(session, client=FakeClient(turns), **kw)


# -------------------------------------------------------------------- the loop


def test_a_turn_with_no_tool_calls_is_done(imported: Session) -> None:
    events = list(_agent(imported, [_Msg(content="Nothing to change.")]).run("hi"))
    assert [e.kind for e in events] == ["start", "round", "text", "done"]


def test_tool_calls_are_dispatched_and_reported(imported: Session) -> None:
    agent = _agent(imported, [_Msg(tool_calls=[_call("get_outline", {})]),
                              _Msg(content="Five slides.")])
    events = list(agent.run("what is in this deck"))
    kinds = [e.kind for e in events if e.kind not in ("start", "round")]
    assert kinds == ["tool_call", "tool_result", "text", "done"]
    assert next(e for e in events if e.kind == "tool_result").result["ok"] is True


def test_a_refusal_reaches_the_model_rather_than_raising(imported: Session) -> None:
    agent = _agent(imported, [_Msg(tool_calls=[_call("set_variant", {
        "slide_id": imported.deck.slides[0].id, "block_id": "x", "variant": "y"})]),
        _Msg(content="That slide is freeform.")])
    events = list(agent.run("change the variant"))
    result = next(e for e in events if e.kind == "tool_result").result
    assert result["ok"] is False
    tool_message = json.loads(agent.messages[-2]["content"])
    assert tool_message["error"] == "wrong_mode"


def test_the_same_error_three_times_stops_the_loop(imported: Session) -> None:
    """Stage 9. Two rounds of the same failure is not persistence, it is a loop — and the
    cost of not noticing is paid in tokens and in the user's patience."""
    bad = _Msg(tool_calls=[_call("get_slide", {"slide_id": "nope"})])
    agent = _agent(imported, [bad, bad, bad, bad, bad])
    events = list(agent.run("show me slide nope"))
    assert events[-1].kind == "done"
    error = next(e for e in events if e.kind == "error")
    assert "three times" in error.text
    assert sum(1 for e in events if e.kind == "tool_call") == 3


def test_the_round_cap_bounds_the_worst_case(imported: Session) -> None:
    """Distinct calls never repeat a signature, so only the cap can stop them."""
    turns = [_Msg(tool_calls=[_call("get_slide", {"slide_id": s.id}, cid=f"c{i}")])
             for i, s in enumerate(imported.deck.slides * 3)]
    agent = _agent(imported, turns, max_rounds=4)
    events = list(agent.run("look at everything"))
    assert sum(1 for e in events if e.kind == "tool_call") == 4
    assert "cap" in next(e for e in events if e.kind == "error").text


def test_a_client_failure_becomes_an_event_not_a_crash(imported: Session) -> None:
    class Boom(FakeClient):
        def create(self, **kw: Any):
            raise RuntimeError("endpoint down")

    agent = loop.Agent(imported, client=Boom([]))
    events = list(agent.run("hello"))
    failure = next(e for e in events if e.kind == "error")
    assert "endpoint down" in failure.text
    assert events[-1].kind == "done", "a crashed turn must still release the UI"


def test_malformed_tool_arguments_do_not_crash_the_turn(imported: Session) -> None:
    broken = _Call(id="c1", function=_Fn(name="get_outline", arguments="{not json"))
    agent = _agent(imported, [_Msg(tool_calls=[broken]), _Msg(content="ok")])
    events = list(agent.run("go"))
    assert next(e for e in events if e.kind == "tool_result").result["ok"] is True


# ------------------------------------------------------------------- the request


def test_tools_are_offered_in_openai_shape(imported: Session) -> None:
    schemas = _agent(imported, []).tool_schemas()
    for schema in schemas:
        assert schema["type"] == "function"
        assert set(schema["function"]) == {"name", "description", "parameters"}
        assert schema["function"]["parameters"]["type"] == "object"


def test_managed_tools_are_hidden_on_an_all_imported_deck(imported: Session) -> None:
    names = {s["function"]["name"] for s in _agent(imported, []).tool_schemas()}
    assert "set_text" in names
    assert "add_slide" not in names


def test_a_deck_with_managed_slides_gets_the_full_set(populated: Session) -> None:
    names = {s["function"]["name"] for s in _agent(populated, []).tool_schemas()}
    assert "add_slide" in names


def test_the_system_prompt_carries_the_context_pyramid(imported: Session) -> None:
    prompt = _agent(imported, []).system_prompt()
    assert "coordinate" in prompt
    assert imported.deck.slides[0].id in prompt
    assert imported.theme.id in prompt


def test_the_context_block_stays_small(imported: Session) -> None:
    """Levels 1 and 2 are on every turn, so their size is a per-turn cost."""
    assert len(loop.context_block(imported)) < 3000


def test_the_context_block_declares_what_was_guessed(imported: Session) -> None:
    assert "Inferred" in loop.context_block(imported)


def test_the_catalog_appears_only_when_managed_slides_can_exist(imported: Session,
                                                                populated: Session) -> None:
    assert "Components:" not in loop.context_block(imported)
    assert "Components:" in loop.context_block(populated)


def test_the_system_prompt_is_resent_each_round(imported: Session) -> None:
    client = FakeClient([_Msg(tool_calls=[_call("get_outline", {})]), _Msg(content="ok")])
    list(loop.Agent(imported, client=client).run("go"))
    for request in client.requests:
        assert request["messages"][0]["role"] == "system"


# ------------------------------------------------------- reasoning models


def test_thinking_is_separated_from_the_answer() -> None:
    """Qwen3, DeepSeek-R1 and friends put their scratchpad in `content`. Left alone it
    lands in the user's chat window as though it were the answer."""
    reasoning, answer = loop.split_thinking("<think>weighing it up</think>The answer.")
    assert reasoning == "weighing it up"
    assert answer == "The answer."


def test_content_without_thinking_is_untouched() -> None:
    assert loop.split_thinking("just an answer") == ("", "just an answer")


def test_an_unterminated_think_block_is_all_reasoning() -> None:
    """A model cut off mid-thought has produced no answer. Showing the fragment as one
    would be worse than showing nothing."""
    reasoning, answer = loop.split_thinking("<think>cut off mid")
    assert reasoning == "cut off mid"
    assert answer == ""


def test_reasoning_is_emitted_and_kept_out_of_the_history(imported: Session) -> None:
    """It costs tokens on every subsequent round and does not improve the next answer."""
    agent = _agent(imported, [_Msg(content="<think>hmm</think>Five slides.")])
    events = [e for e in agent.run("how many slides") if e.kind not in ("start", "round")]
    assert [e.kind for e in events] == ["thinking", "text", "done"]
    assert events[0].text == "hmm"
    assert events[1].text == "Five slides."
    assert "<think>" not in agent.messages[-1]["content"]


def test_serve_refuses_a_port_already_in_use() -> None:
    """8000 is also where a local vLLM or vllm-mlx typically listens."""
    import socket

    from ppt_harness.adapters.web import port_is_free, serve

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert port_is_free("127.0.0.1", port) is False
        with pytest.raises(RuntimeError, match="already in use"):
            serve(None, host="127.0.0.1", port=port)


# ------------------------------------------------------------------------- web


@pytest.fixture
def web(imported: Session) -> TestClient:
    return TestClient(create_app(imported))


def test_the_page_is_served(web: TestClient) -> None:
    response = web.get("/")
    assert response.status_code == 200
    assert "ppt-harness" in response.text


def test_outline_reports_whether_a_browser_is_present(web: TestClient) -> None:
    body = web.get("/api/outline").json()
    assert "browser" in body
    assert len(body["slides"]) > 0


def test_the_preview_is_the_export_rendered(web: TestClient, imported: Session) -> None:
    """Not an approximation of the export — the export itself, rendered.

    With a real renderer present the pane is a picture of the exported file with the
    harness's own measurements drawn over it. Without one it degrades to the HTML renderer
    rather than showing a blank pane.
    """
    slide_id = imported.deck.slides[0].id
    markup = web.get(f"/api/slide/{slide_id}").text
    if web.get("/api/outline").json()["renderer"]:
        assert "<img" in markup and "preview.png" in markup
        assert "class=\"probe" in markup, "measurements must be overlaid on the render"
    else:
        assert "class=\"slot" in markup


def test_a_missing_slide_is_404(web: TestClient) -> None:
    assert web.get("/api/slide/nope").status_code == 404


def test_measurement_is_available_per_slide(web: TestClient, imported: Session) -> None:
    body = web.get(f"/api/slide/{imported.deck.slides[0].id}/measure").json()
    assert "overflow_px" in body and "clean" in body


def test_an_inspector_edit_is_attributed_to_the_user(web: TestClient,
                                                     imported: Session) -> None:
    """Authorship is not cosmetic: a user op landing on a target the model just touched is
    the observed channel of the preference profile."""
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    response = web.post("/api/edit", json={"target": f"{slide.id}/{shape.id}",
                                           "text": "By hand"})
    assert response.status_code == 200
    ops = web.get("/api/log").json()["ops"]
    assert ops[-1]["author"] == "user"


def test_a_rejected_edit_is_422_with_the_reason(web: TestClient, imported: Session) -> None:
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    response = web.post("/api/edit", json={"target": f"{slide.id}/{shape.id}",
                                           "text": "word " * 500})
    assert response.status_code == 422
    assert response.json()["error"] == "budget_exceeded"


def test_undo_is_reachable_from_the_page(web: TestClient, imported: Session) -> None:
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    before = shape.text
    web.post("/api/edit", json={"target": f"{slide.id}/{shape.id}", "text": "Changed"})
    assert web.post("/api/undo").json()["ok"] is True
    assert shape.text == before


def test_export_reports_fidelity(web: TestClient) -> None:
    body = web.post("/api/export").json()
    assert body["ok"] is True
    assert "fidelity clean" in body["summary"]


def test_chat_streams_server_sent_events(imported: Session) -> None:
    from ppt_harness.adapters import web as web_module

    agent = loop.Agent(imported, client=FakeClient([
        _Msg(tool_calls=[_call("get_outline", {})]), _Msg(content="Five slides.")]))
    chunks = list(web_module._stream(agent, "what is in here"))
    kinds = [json.loads(c[6:])["kind"] for c in chunks]
    assert [k for k in kinds if k not in ("start", "round")] == [
        "tool_call", "tool_result", "text", "done"]
    assert kinds[0] == "start", "the client needs a signal before the first model call"
    assert all(c.startswith("data: ") and c.endswith("\n\n") for c in chunks)


def test_a_crashed_turn_still_closes_the_stream(imported: Session) -> None:
    from ppt_harness.adapters import web as web_module

    class Boom:
        def run(self, prompt: str):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover

    chunks = [json.loads(c[6:]) for c in web_module._stream(Boom(), "go")]
    assert chunks[0]["kind"] == "error" and "kaboom" in chunks[0]["text"]
    assert chunks[-1]["kind"] == "done"


def test_the_web_session_is_the_one_the_tools_write_through(web: TestClient,
                                                            imported: Session) -> None:
    """The preview cannot show a deck the model has not actually changed.

    Asserted through the render *version* rather than by looking for the text in the
    markup: the pane is now a raster of the exported file, so the words live in the image,
    not the HTML. The version is a hash of deck state, so a changed version is proof the
    picture is about to be regenerated from the edit.
    """
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    before = web.get("/api/outline").json()["version"]

    web.post("/api/edit", json={"target": f"{slide.id}/{shape.id}", "text": "Shared state"})

    assert shape.text == "Shared state"
    assert web.get("/api/outline").json()["version"] != before


def test_modes_survive_the_round_trip(web: TestClient) -> None:
    for entry in web.get("/api/outline").json()["slides"]:
        assert entry["mode"] in {m.value for m in Mode}


# ------------------------------------------------------------------------ search


def test_search_finds_text_across_slides(imported: Session) -> None:
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    needle = shape.text.split()[0]
    results = imported.search(needle)
    assert any(r["slide"] == slide.id for r in results)


def test_search_is_case_insensitive(imported: Session) -> None:
    shape = next(s for sl in imported.deck.slides for s in sl.shapes
                 if s.text and s.text.strip())
    needle = shape.text.strip().split()[0]
    assert imported.search(needle.lower()) == imported.search(needle.upper())


def test_an_empty_query_matches_nothing(imported: Session) -> None:
    """Not 'everything'. A blank box should clear the filter, not select the whole deck."""
    assert imported.search("") == []
    assert imported.search("   ") == []


def test_search_skips_inherited_layout_art(imported: Session) -> None:
    """A logo caption on the master would otherwise match on every slide and drown the
    answers the user asked for."""
    inherited = [s for sl in imported.deck.slides for s in sl.inherited if s.text]
    if not inherited:
        pytest.skip("fixture layout carries no text art")
    needle = inherited[0].text.strip()
    if not needle:
        pytest.skip("inherited art has no searchable text")
    assert len(imported.search(needle)) < len(imported.deck.slides)


def test_snippets_carry_their_target(imported: Session) -> None:
    """The caller has to be able to jump to the match, not just the slide."""
    shape = next(s for sl in imported.deck.slides for s in sl.shapes
                 if s.text and s.text.strip())
    for result in imported.search(shape.text.strip().split()[0]):
        for hit in result["hits"]:
            assert hit["target"].startswith(result["slide"] + "/")
            assert hit["snippet"]


def test_the_search_endpoint_reports_matches(web: TestClient, imported: Session) -> None:
    shape = next(s for sl in imported.deck.slides for s in sl.shapes
                 if s.text and s.text.strip())
    needle = shape.text.strip().split()[0]
    body = web.get("/api/search", params={"q": needle}).json()
    assert body["query"] == needle
    assert body["results"]


# --------------------------------------------------------------------- providers


def test_a_claude_model_selects_the_native_provider() -> None:
    from ppt_harness.core import providers

    assert providers.build(model="claude-opus-5").name == "anthropic"
    assert providers.build(model="gpt-4o").name == "openai"


def test_the_greeting_is_offered_no_tools_at_all(imported: Session) -> None:
    """"Do not call any tools" is a request; an empty tool list is a fact. A greeting that
    calls get_outline stalls the UI on a round-trip for something already in its prompt."""
    client = FakeClient([_Msg(content="Five imported slides, ready when you are.")])
    agent = loop.Agent(imported, client=client)
    list(agent.greet())
    assert client.requests[0]["tools"] == []


def test_the_greeting_says_who_is_talking(imported: Session) -> None:
    """The opening turn is where the model establishes it is a presentation professional
    rather than a chat window attached to a file."""
    client = FakeClient([_Msg(content="hello")])
    agent = loop.Agent(imported, client=client)
    list(agent.greet())
    sent = json.dumps(client.requests[0]["messages"])
    assert "presentation professional" in client.requests[0]["messages"][0]["content"]
    assert "Introduce yourself" in sent


def test_the_greeting_is_produced_once_and_then_replayed(imported: Session) -> None:
    """A reload should not re-bill, and a second greeting that differed from the first
    would read as the deck having changed."""
    client = FakeClient([_Msg(content="First and only.")])
    agent = loop.Agent(imported, client=client)

    first = [e.text for e in agent.greet() if e.kind == "text"]
    second = [e.text for e in agent.greet() if e.kind == "text"]
    assert first == second == ["First and only."]
    assert len(client.requests) == 1, "the second greeting cost nothing"


def test_a_failed_greeting_is_retried_rather_than_cached(imported: Session) -> None:
    """Caching the empty string here would mean one bad request permanently silences the
    opening turn for the life of the process."""
    class Boom(FakeClient):
        def create(self, **kw: Any):
            raise RuntimeError("endpoint down")

    agent = loop.Agent(imported, client=Boom([]))
    list(agent.greet())
    assert agent.greeting is None


def test_a_greeting_never_becomes_an_error_in_an_empty_chat(imported: Session) -> None:
    """Nobody asked for it, so it must not be the first thing they have to read and
    dismiss. The real failure surfaces on their first actual request."""
    from ppt_harness.adapters.web import _stream_greeting

    class Boom(FakeClient):
        def create(self, **kw: Any):
            raise RuntimeError("endpoint down")

    agent = loop.Agent(imported, client=Boom([]))
    assert all("error" not in chunk for chunk in _stream_greeting(agent))


def test_a_base_url_always_means_an_openai_endpoint() -> None:
    """Setting one has no other purpose, so it decides before the model name does — a local
    server may well be serving a model whose name starts with 'claude'."""
    from ppt_harness.core import providers

    chosen = providers.build(model="claude-3-whatever", base_url="http://localhost:8000/v1")
    assert chosen.name == "openai"


def test_anthropic_tools_carry_the_same_schema_in_a_different_envelope() -> None:
    """One table, three renderings — the JSON Schema itself is shared verbatim."""
    from ppt_harness.tools import router

    native = {t["name"]: t for t in router.anthropic_schemas()}
    openai = {t["function"]["name"]: t for t in router.openai_schemas()}
    assert set(native) == set(openai)
    for name, tool in native.items():
        assert set(tool) == {"name", "description", "input_schema"}
        assert tool["input_schema"] == openai[name]["function"]["parameters"]


def test_a_refused_tool_is_marked_as_an_error_for_claude(imported: Session) -> None:
    """Every harness rejection carries the ways out; `is_error` is what makes the model
    read it as a refusal rather than as data."""
    from ppt_harness.core import providers

    provider = providers.AnthropicProvider(client=object())
    call = providers.ToolCall(id="tu_1", name="set_variant")
    provider.add_results([(call, {"ok": False, "error": "wrong_mode"})])
    block = provider.messages[-1]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "tu_1"
    assert block["is_error"] is True


def test_a_successful_tool_is_not_marked_as_an_error(imported: Session) -> None:
    from ppt_harness.core import providers

    provider = providers.AnthropicProvider(client=object())
    provider.add_results([(providers.ToolCall(id="tu_2", name="get_outline"), {"ok": True})])
    assert provider.messages[-1]["content"][0]["is_error"] is False


def test_every_tool_call_gets_a_result_even_when_stopping(imported: Session) -> None:
    """A `tool_use` block with no matching `tool_result` is a malformed conversation, and
    the next request is rejected outright — so the stop has to come after the results."""
    bad = _Msg(tool_calls=[_call("get_slide", {"slide_id": "nope"})])
    agent = _agent(imported, [bad, bad, bad, bad])
    list(agent.run("show me slide nope"))
    calls = sum(len(m.get("tool_calls", [])) for m in agent.messages
                if m.get("role") == "assistant")
    replies = sum(1 for m in agent.messages if m.get("role") == "tool")
    assert calls == replies


def test_the_outline_names_the_model_in_use(web: TestClient) -> None:
    """Which model answered matters when the same UI drives a local 4B and Claude."""
    body = web.get("/api/outline").json()
    assert body["provider"] in {"openai", "anthropic", "deepseek"}
    assert body["model"]


# ------------------------------------------------------------------------- config


def test_env_file_values_are_loaded(tmp_path, monkeypatch) -> None:
    """Uses a throwaway variable name on purpose.

    `load_env` writes straight into `os.environ`, which monkeypatch cannot undo because it
    never saw the assignment. Loading a *real* key here would leak into every later test
    and silently change which provider they select — which is exactly what happened.
    """
    from ppt_harness.core import config

    name = "PPT_HARNESS_TEST_VALUE"
    (tmp_path / ".env").write_text(f"{name}=loaded\n")
    monkeypatch.delenv(name, raising=False)
    try:
        assert config.load_env(tmp_path) == [name]
        assert os.environ[name] == "loaded"
    finally:
        os.environ.pop(name, None)


def test_a_real_environment_variable_always_wins(tmp_path, monkeypatch) -> None:
    """A file that could silently override the key would make a stale checkout charge the
    wrong account, and the failure would be invisible."""
    from ppt_harness.core import config

    name = "PPT_HARNESS_TEST_VALUE"
    (tmp_path / ".env").write_text(f"{name}=from-file\n")
    monkeypatch.setenv(name, "from-shell")  # monkeypatch restores what it set
    assert config.load_env(tmp_path) == []
    assert os.environ[name] == "from-shell"


def test_env_parsing_handles_the_shapes_people_write() -> None:
    from ppt_harness.core import config

    parsed = config.parse(
        '# a comment\n\n'
        'export PLAIN=one\n'
        'QUOTED="two"\n'
        "SINGLE='three'\n"
        'WITH_EQUALS=a=b\n'
        'no_equals_line\n'
    )
    assert parsed == {"PLAIN": "one", "QUOTED": "two", "SINGLE": "three",
                      "WITH_EQUALS": "a=b"}


def test_no_env_file_is_not_an_error(tmp_path) -> None:
    from ppt_harness.core import config

    assert config.load_env(tmp_path / "nothing-here") == []


# ------------------------------------------------------------------------ progress


def test_a_turn_announces_itself_before_the_model_is_asked(imported: Session) -> None:
    """The first model call is the longest silence in a turn. A UI with no signal until the
    first token is indistinguishable from one that has crashed."""
    agent = _agent(imported, [_Msg(content="done")])
    events = list(agent.run("hello"))
    assert events[0].kind == "start"
    assert events[0].text == agent.model


def test_each_round_is_numbered(imported: Session) -> None:
    agent = _agent(imported, [_Msg(tool_calls=[_call("get_outline", {})]),
                              _Msg(content="Five slides.")])
    rounds = [e.round for e in agent.run("what is in here") if e.kind == "round"]
    assert rounds == [1, 2]


def test_results_carry_the_id_of_the_call_they_answer(imported: Session) -> None:
    """A round may hold several calls. Pairing by 'the last one I saw' mislabels every one
    of them but the first."""
    agent = _agent(imported, [
        _Msg(tool_calls=[_call("get_outline", {}, cid="a"),
                         _call("get_theme", {}, cid="b")]),
        _Msg(content="ok"),
    ])
    events = list(agent.run("look around"))
    calls = [e for e in events if e.kind == "tool_call"]
    results = [e for e in events if e.kind == "tool_result"]
    assert [c.call_id for c in calls] == ["a", "b"]
    assert [r.call_id for r in results] == ["a", "b"]


def test_progress_fields_survive_serialisation(imported: Session) -> None:
    agent = _agent(imported, [_Msg(tool_calls=[_call("get_outline", {}, cid="z")]),
                              _Msg(content="ok")])
    payloads = [e.as_dict() for e in agent.run("go")]
    assert any(p.get("round") for p in payloads)
    assert any(p.get("call_id") == "z" for p in payloads)


def test_a_turn_always_ends_with_done(imported: Session) -> None:
    """The client clears its busy state on `done`; a turn that never sends one leaves the
    UI spinning forever."""
    for turns in ([_Msg(content="fine")],
                  [_Msg(tool_calls=[_call("get_slide", {"slide_id": "nope"})])] * 5):
        assert list(_agent(imported, turns).run("x"))[-1].kind == "done"


# ------------------------------------------------------------------------- compact


def test_the_ui_still_receives_the_full_result(imported: Session) -> None:
    """Trimming is for the model's context, not for the client.

    The overlay draws from the `render` block of a write, so the event must keep the boxes
    even though the copy sent to the model has them stripped.
    """
    slide = imported.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    agent = _agent(imported, [
        _Msg(tool_calls=[_call("set_text", {"target": f"{slide.id}/{shape.id}",
                                            "text": "Edited"})]),
        _Msg(content="ok"),
    ])
    events = list(agent.run("retitle it"))
    measured = next(e for e in events if e.kind == "tool_result").result["render"]
    entries = measured.get("shapes") or measured.get("slots") or []
    assert entries and all("box" in e for e in entries), "the event must keep geometry"

    # And the model's copy must not carry those numbers.
    sent = json.loads(agent.messages[-2]["content"])
    assert '"box"' not in json.dumps(sent)


def test_the_model_is_not_sent_coordinates(imported: Session) -> None:
    """No tool accepts a coordinate, so every number saying where something sits is dead
    weight — paid again on every later round of the turn."""
    from ppt_harness.tools import compact, router

    trimmed = compact.for_model(
        router.dispatch(imported, "get_slide", {"slide_id": imported.deck.slides[0].id}))
    blob = json.dumps(trimmed)
    assert '"frame"' not in blob
    assert '"box"' not in blob


def test_slots_that_fit_collapse_to_a_count(imported: Session) -> None:
    """Thirty-eight records saying 'fits' inform no decision that 'all fit' does not."""
    from ppt_harness.tools import compact

    trimmed = compact.for_model({"ok": True, "render": {
        "clean": True, "overflow_px": 0,
        "shapes": [{"target": f"s1/x{i}", "fits": True, "box": [0, 0, 9, 9]}
                   for i in range(38)],
    }})
    assert trimmed["render"]["shapes"] == "38 measured, all fit"


def test_overflow_detail_survives_trimming(imported: Session) -> None:
    """The one thing the model must be able to act on."""
    from ppt_harness.tools import compact

    trimmed = compact.for_model({"ok": True, "render": {
        "clean": False, "overflow_px": 96,
        "shapes": [
            {"target": "s1/a", "fits": True, "box": [0, 0, 1, 1]},
            {"target": "s1/b", "fits": False, "overflow_px": 96, "lines": 4,
             "max_lines": 3, "note": "shrunk by normAutofit", "box": [0, 0, 1, 1]},
        ],
    }})
    bad = trimmed["render"]["shapes"]["overflowing"]
    assert len(bad) == 1
    assert bad[0]["target"] == "s1/b"
    assert bad[0]["overflow_px"] == 96
    assert "normAutofit" in bad[0]["note"]
    assert "box" not in bad[0]


def test_a_refusal_is_never_trimmed_away(imported: Session) -> None:
    """Rejections carry the capacity, the overage and the ways out; those are the payload."""
    from ppt_harness.tools import compact

    refusal = {"ok": False, "error": "budget_exceeded",
               "message": "capacity 46.2ew · got 61.4ew · options: shorten ~29 chars"}
    assert compact.for_model(refusal) == refusal


# ------------------------------------------------------------------------ deepseek


def test_a_deepseek_model_selects_its_own_provider() -> None:
    """Named rather than configured: an endpoint the user has to remember is one they will
    get wrong."""
    from ppt_harness.core import providers

    chosen = providers.build(model="deepseek-v4-flash", api_key="x")
    assert chosen.name == "deepseek"
    assert "deepseek.com" in chosen._base_url


def test_a_base_url_still_wins_over_a_deepseek_name() -> None:
    """A locally-served R1 is reachable by pointing at it."""
    from ppt_harness.core import providers

    chosen = providers.build(model="deepseek-r1", base_url="http://localhost:8000/v1")
    assert chosen.name == "openai"


def test_deepseek_never_falls_back_to_an_openai_key(monkeypatch) -> None:
    """A key for one vendor must never be sent to another."""
    from ppt_harness.core import providers

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-not-travel")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    chosen = providers.build(model="deepseek-v4-flash")
    assert chosen._api_key != "sk-openai-should-not-travel"
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        _ = chosen.client


def test_reasoning_content_is_surfaced_not_replayed(imported: Session) -> None:
    """DeepSeek's reasoners return the scratchpad in a field rather than in `<think>` tags.
    It belongs in the UI, and must not be fed back."""

    class Reasoned(_Msg):
        reasoning_content = "weighed the options"

    client = FakeClient([Reasoned(content="The answer.")])
    agent = loop.Agent(imported, client=client, model="gpt-4o")
    events = list(agent.run("go"))

    thinking = next(e for e in events if e.kind == "thinking")
    assert thinking.text == "weighed the options"
    assert "weighed" not in agent.messages[-1]["content"]


def test_an_inline_comment_is_not_part_of_a_model_name() -> None:
    """`MODEL=x  # or y` means the model is `x`. Keeping the rest produces a name no
    endpoint has heard of, and a 404 that blames the endpoint."""
    from ppt_harness.core import config

    assert config.parse("PPT_HARNESS_MODEL=deepseek-v4-flash  # or -pro") == {
        "PPT_HARNESS_MODEL": "deepseek-v4-flash"}
    assert config.parse("URL=https://x.com/a#frag") == {"URL": "https://x.com/a#frag"}
    assert config.parse('Q="keeps # inside quotes"') == {"Q": "keeps # inside quotes"}


def test_the_context_block_names_the_template(fixture_deck) -> None:
    """A deck with no slides and a borrowed theme is about to be *written*, not edited, and
    the opening turn is worth nothing if it cannot tell the difference."""
    session = Session.from_template(fixture_deck, "Q3 board review")
    block = loop.context_block(session)
    assert fixture_deck.name in block
    assert "no slides did" in block
    assert "Components:" in block, "an empty deck can only be built from components"


# ----------------------------------------------------------------------- the picker


def test_the_picker_stops_asking_once_it_is_answered() -> None:
    """The bug a click reported as "it does nothing".

    A deck started from a card is empty *by definition*, so a dismissal keyed on slide count
    was false again the instant it was answered: the page reloaded, the picker saw an empty
    deck and offered itself a second time. The question is whether the person has said which
    deck they are working on, not whether that deck has anything in it yet.
    """
    client = TestClient(create_app(Session.blank("Untitled")))
    assert client.get("/api/templates").json()["started"] is False

    started = client.post("/api/start", json={"template": "slate"})
    assert started.status_code == 200
    assert started.json()["theme"] == "slate"
    assert client.get("/api/templates").json()["started"] is True, \
        "the picker offered itself again after being answered"


@pytest.mark.parametrize("make,why", [
    (lambda: Session.from_builtin("slate", "T"), "--template named a theme"),
    (lambda: Session.open(DEMO), "a named deck brought its own slides"),
])
def test_a_choice_made_at_the_command_line_is_not_asked_again(make, why: str) -> None:
    """Every way in except a bare `serve` arrives already answered, and the browser has no
    business re-opening a question the command line settled."""
    client = TestClient(create_app(make()))
    assert client.get("/api/templates").json()["started"] is True, why


def test_starting_over_swaps_the_deck_the_whole_app_is_looking_at() -> None:
    """`nonlocal` reaches every route's closure at once, so nothing keeps serving the old
    deck — the outline, the theme and the preview version all have to move together."""
    client = TestClient(create_app(Session.from_builtin("editorial", "T")))
    client.post("/api/start", json={"template": "signal", "title": "Fresh"})

    outline = client.get("/api/outline").json()
    assert outline["template"] == "signal (built in)"
    assert outline["deck"] == "Fresh"
    assert client.get("/api/theme").json()["palette"]["brand"] == "#0F766E"


def _app_with(session: Session, fake: FakeClient, monkeypatch) -> TestClient:
    """The app, with its own agent talking to a fake endpoint.

    The agent is built inside `create_app` — that is what lets `/api/start` replace it — so
    a test cannot hand one in. Patching the name `create_app` resolves reaches the same
    agent the routes use, rather than a second one that proves nothing about them.
    """
    import ppt_harness.adapters.web as web

    monkeypatch.setattr(web, "Agent",
                        lambda s, **kw: loop.Agent(s, client=fake, **kw))
    return TestClient(web.create_app(session))


def test_the_greeting_prompt_is_not_replayed_as_something_the_user_said(
        imported: Session, monkeypatch) -> None:
    """`/api/history` is the human's record, not the model's.

    The greeting has to be asked for as a user message — that is the only way to ask a model
    for anything — but nobody typed it. Replaying the message list verbatim put "Open the
    conversation in two or three sentences…" in the log attributed to the person reading it,
    above a reply to an instruction they never gave.
    """
    fake = FakeClient([_Msg(content="Five imported slides, ready when you are.")])
    client = _app_with(imported, fake, monkeypatch)
    client.post("/api/greeting").read()

    turns = client.get("/api/history").json()["turns"]
    assert [t["role"] for t in turns] == ["assistant"], \
        f"the greeting instruction was replayed as a user turn: {turns}"
    assert turns[0]["text"] == "Five imported slides, ready when you are."


def test_a_real_user_turn_is_still_replayed(imported: Session, monkeypatch) -> None:
    """The subtraction is the greeting prompt exactly, not "user turns that look internal"."""
    fake = FakeClient([_Msg(content="Renamed it.")])
    client = _app_with(imported, fake, monkeypatch)
    client.post("/api/chat", json={"prompt": "retitle slide 1"}).read()

    turns = client.get("/api/history").json()["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["text"] == "retitle slide 1"
