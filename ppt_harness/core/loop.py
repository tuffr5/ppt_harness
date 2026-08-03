"""The agent loop — ARCHITECTURE stages 2-9.

Provider-agnostic. Claude natively, or any OpenAI-chat-compatible endpoint — vLLM, Ollama,
LM Studio, Together, OpenRouter — by setting `base_url`. `core/providers.py` owns the wire
format; this file owns planning and termination, so neither is written twice.

Nothing here knows about slides; it knows about tools, and the tools come from the same
registry MCP and the CLI use.

Stages 3 and 7 — policy and verify — are thin in v0 on purpose. Every mutating tool already
budget-checks before it writes and returns its own measurement afterwards, so the loop does
not need a separate verification pass to keep the deck sound. What it does own is
**termination** (stage 9), which is the part a model cannot be trusted to do for itself:

- the goal is met when a turn produces no tool calls,
- a repeated error signature means the model is stuck, not making progress,
- and a hard cap bounds the worst case regardless.

Yields events rather than returning a transcript, so a UI can stream and a script can just
drain the generator.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from ..state.document import Mode
from ..tools import router
from . import providers
from .session import Session

#: Stage 9. Two rounds of the same error is not persistence, it is a loop.
MAX_ROUNDS = 12
REPEAT_LIMIT = 2

EventKind = Literal[
    "start", "round", "text", "thinking", "tool_call", "tool_result", "done", "error",
]

#: Kept here for callers and tests; the implementation lives with the provider that needs
#: it, since only inline-scratchpad models do.
split_thinking = providers.split_thinking


@dataclass
class Event:
    kind: EventKind
    text: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    slide: str | None = None
    round: int = 0
    call_id: str = ""
    """Pairs a `tool_result` with the `tool_call` it answers — a round may hold several."""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.round:
            out["round"] = self.round
        if self.call_id:
            out["call_id"] = self.call_id
        for key in ("text", "tool", "slide"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.args:
            out["args"] = self.args
        if self.result:
            out["result"] = self.result
        return out


SYSTEM = """\
You are a presentation professional — someone who has built decks for executives, sales \
teams and conferences for years, and who has opinions about what makes a slide land. You \
are working on this deck *with* the person, not taking dictation from them. If they ask \
for something that will weaken the deck, build it and say what you would do instead; they \
decide.

You work through a harness. You never touch a file directly and you never write a \
coordinate — no tool accepts one.

Slides carry a mode.
- `freeform` slides came from an imported file. The harness holds the original author's \
shapes; edit their text with set_text. Shapes marked opaque (SmartArt, groups, embedded \
objects) are preserved exactly on export but cannot be edited.
- `managed` slides are built from components. Name a layout, a region, a component and a \
variant, and the harness places everything.

You are good at this, in the way a strong deck-builder is good at it:
- One idea per slide. If a slide needs "and", it is usually two slides.
- A title states the finding, not the topic — "Churn doubled in EMEA", not "Churn \
analysis". A slide whose title is a noun phrase is usually one nobody will remember.
- Bullets are parallel: same part of speech, same tense, same length band. Three to five \
of them. If there are eight, there is a structure hiding in there — find it.
- Prose belongs in the notes, not on the slide. Cut every word that survives its own \
removal.
- Reach for the component that matches the *shape of the argument*: a comparison wants two \
columns, a trend wants a chart, a single number wants to be large and alone.
- Respect what is already there. A deck has a voice; match it rather than replacing it \
with yours.

Rules that matter:
- Every write is budget-checked before it lands and returns its own measurement. Read the \
`render` field; do not call render again to confirm what you were just told.
- A `budget_exceeded` error carries the capacity, the overage, and the ways out. Work them \
in order: try a different variant, then a different component, then shorten the text — and \
only shorten text *you* wrote. If the user wrote those words, reshape the container or ask.
- Never reduce a font size to make something fit. The type scale belongs to the theme.
- Emphasis is content, so `**bold**`, `*italic*` and `<u>underline</u>` are yours to write \
in any text you set, on either mode. Size, face and colour are the theme's; never ask for \
them.
- Prefer few, decisive calls. Call get_outline once at the start if you need orientation.

When you are done, say what you changed in one or two sentences. Do not narrate tool calls.
"""


#: The turn that opens a conversation. Phrased as an instruction rather than as a greeting
#: because the model already holds the outline in its system prompt: everything it needs to
#: say is in context, so a tool call here is wasted latency the user waits through before
#: the UI is usable.
OPENING = """\
Open the conversation in two or three sentences, using only what you already know from the \
context above.

Introduce yourself as what you are: someone who builds presentations for a living and is \
working on this deck with them. Not an assistant awaiting instructions — a professional \
who has already looked at the deck. Show that in what you notice, not by claiming it: say \
what deck is open and what shape it is in, name the one thing that will most shape what \
they can ask for (imported slides whose layout is fixed, inferred theme values worth \
correcting, a deck with nothing in it yet), and offer a concrete next step you would take \
if it were yours.

Plain and brief. No bullet lists, no menu of features, no enthusiasm about being here. \
Do not call any tools.\
"""


def _signature(result: dict[str, Any]) -> str | None:
    """What makes two failures 'the same failure' for termination purposes."""
    if result.get("ok", True):
        return None
    return f"{result.get('error')}::{result.get('target') or result.get('message', '')[:60]}"


def context_block(session: Session) -> str:
    """Levels 1 and 2 of the context pyramid — always on, so it must stay small."""
    outline = session.outline()
    theme = session.theme
    lines = [
        f"Deck: {outline['deck']} ({len(outline['slides'])} slides), theme {theme.id}.",
    ]
    if outline.get("template"):
        # Said in the always-on block rather than left for the model to discover, because it
        # changes the first thing it should say: a deck with no slides and a borrowed theme
        # is a deck someone is about to write, not one they are about to edit.
        lines.append(
            f"Started from the template {outline['template']}: its palette, type and grid "
            "came across, no slides did. Every slide you add is managed."
        )
    lines += ["", "Slides:"]
    for slide in outline["slides"]:
        gist = (slide.get("gist") or "").replace("\n", " ")[:70]
        lines.append(f"  {slide['id']}  [{slide['mode']}]  {gist}")

    # Level 2, alongside the theme: what this person has been observed to prefer. Placed
    # before the catalog so a preference is read as a way of choosing among components
    # rather than as an extra component.
    profile = getattr(session, "preferences", None)
    if profile is not None:
        learned = profile.block()
        if learned:
            lines += ["", learned]

    # Names and triggers only. A playbook body runs to a thousand words; paying that on
    # every turn to have it available on one is the mistake the pyramid exists to avoid.
    from . import skills

    available = skills.catalog()
    if available:
        lines += ["", available]

    lines += ["", "Theme type roles: " + ", ".join(theme.type.scale)]
    if theme.inferred:
        lines.append("Inferred (guessed, correct them if wrong): " + ", ".join(theme.inferred))

    modes = {s["mode"] for s in outline["slides"]}
    if "managed" in modes or not outline["slides"]:
        from ..components import registry

        lines += ["", "Components:"]
        lines += [f"  {c['key']}: {c['purpose']} [{', '.join(c['variants'])}]"
                  for c in registry.catalog()]
        lines.append("Layouts: " + ", ".join(
            f"{k} ({', '.join(sorted(v.regions))})" for k, v in registry.LAYOUTS.items()))
    return "\n".join(lines)


class Agent:
    """One conversation against one deck."""

    def __init__(
        self,
        session: Session,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        max_rounds: int = MAX_ROUNDS,
        client: Any = None,
        provider: providers.Provider | None = None,
    ) -> None:
        self.session = session
        self.max_rounds = max_rounds
        self.provider = provider or providers.build(model=model, base_url=base_url,
                                                    api_key=api_key, client=client)
        #: The opening turn, once produced. See `greet`.
        self.greeting: str | None = None

    @property
    def model(self) -> str:
        return self.provider.model

    @property
    def messages(self) -> list[Any]:
        return self.provider.messages

    # -- tools ------------------------------------------------------------------

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Filtered to the modes actually present.

        A model never shown `set_variant` on an all-imported deck cannot waste a turn
        calling it, which is cheaper than any refusal message.
        """
        modes = {s.mode for s in self.session.deck.slides}
        gate = Mode.FREEFORM if modes == {Mode.FREEFORM} else None
        return self.provider.tools(gate)

    def system_prompt(self) -> str:
        return f"{SYSTEM}\n\n---\n\n{context_block(self.session)}"

    # -- the loop ---------------------------------------------------------------

    def greet(self) -> Iterator[Event]:
        """The opening turn, produced once per conversation.

        Offered with **no tools at all** rather than with tools the model is asked not to
        use: a greeting that calls `get_outline` stalls the UI on a round-trip for
        something already in its system prompt, and "do not call any tools" is a request,
        while an empty tool list is a fact.

        Replayed from cache on later calls, so refreshing the page is free. A greeting is
        not worth billing twice, and a second one that differed from the first would read
        as the deck having changed.
        """
        if self.greeting is not None:
            yield Event(kind="start", text=self.model)
            yield Event(kind="text", text=self.greeting)
            yield Event(kind="done")
            return

        said = []
        for event in self.run(OPENING, tools=False):
            if event.kind == "text":
                said.append(event.text)
            yield event
        # Cached only on success: a greeting that failed should be retried on the next load,
        # not remembered as the empty string and never attempted again.
        if said:
            self.greeting = "\n".join(said)

    def run(self, prompt: str, *, tools: bool = True) -> Iterator[Event]:
        self.provider.add_user(prompt)
        self.provider.tool_cache = self.tool_schemas() if tools else []
        system = self.system_prompt()
        seen: dict[str, int] = {}

        # Emitted before the first model call, which is the longest silence in a turn — a
        # slow local model can take tens of seconds to say anything, and a UI with no signal
        # is indistinguishable from one that has crashed.
        yield Event(kind="start", text=self.model)

        for round_number in range(self.max_rounds):
            yield Event(kind="round", round=round_number + 1)
            try:
                turn = self.provider.ask(system)
            except Exception as exc:
                # `done` even here. The client clears its busy state on it, so a turn that
                # ends without one leaves a spinner running forever over a dead request.
                yield Event(kind="error", text=f"{type(exc).__name__}: {exc}")
                yield Event(kind="done")
                return

            if turn.reasoning:
                yield Event(kind="thinking", text=turn.reasoning)
            if turn.text:
                yield Event(kind="text", text=turn.text)

            if not turn.calls:
                yield Event(kind="done")
                return

            results: list[tuple[providers.ToolCall, dict[str, Any]]] = []
            stop = False
            for call in turn.calls:
                yield Event(kind="tool_call", tool=call.name, args=call.args,
                            call_id=call.id)
                result = router.dispatch(self.session, call.name, call.args)
                slide = call.args.get("slide_id") or (
                    call.args.get("target", "").split("/")[0]
                    if call.args.get("target") else None
                ) or result.get("target")
                yield Event(kind="tool_result", tool=call.name, result=result,
                            slide=slide, call_id=call.id)
                results.append((call, result))

                signature = _signature(result)
                if signature:
                    seen[signature] = seen.get(signature, 0) + 1
                    if seen[signature] > REPEAT_LIMIT:
                        stop = True

            # Every call in the batch gets a result, even after deciding to stop: a
            # tool_use block without a matching tool_result is a malformed conversation,
            # and the next request would be rejected outright.
            self.provider.add_results(results)

            if stop:
                yield Event(
                    kind="error",
                    text="Stopping: the same error came back three times. The request may "
                         "not be satisfiable as stated — say so rather than retrying.",
                )
                yield Event(kind="done")
                return

        yield Event(kind="error", text=f"Stopping: hit the {self.max_rounds}-round cap.")
        yield Event(kind="done")


def run_once(session: Session, prompt: str, **kw: Any) -> list[Event]:
    """Drain a whole turn. Convenient for scripts and tests."""
    return list(Agent(session, **kw).run(prompt))
