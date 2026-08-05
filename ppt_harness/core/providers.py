"""Model providers.

The loop owns planning and termination; a provider owns one wire format. Two wire formats
exist because they answer different questions: a local OpenAI-compatible endpoint costs
nothing and keeps working offline, and Claude is fast and good enough at tool use to be
worth the API call. DeepSeek is a third *address*, not a third format — it rides the
OpenAI one, which is why it is a subclass rather than a peer.

All are driven by a **manual** loop rather than an SDK tool runner. The harness needs
control the runners do not expose: it detects a repeated error signature and stops (stage 9),
and it yields an event per tool call so a UI can stream the turn as it happens. Keeping both
providers on the same shape also means termination is written once, not twice.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..state.document import Mode
from ..tools import compact, router

#: Claude, when nothing else is asked for. Model ids are hyphenated — `claude-opus-4-8`,
#: never `claude-opus-4.8`, which is rejected rather than resolved.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
DEFAULT_OPENAI_MODEL = "gpt-4o"

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"

#: Enough for a turn that explains itself; the harness's own replies are short.
MAX_TOKENS = 8000


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    """What one model call cost, in one vocabulary.

    Normalised onto Anthropic's split rather than OpenAI's, because the split *is* the
    measurement: `input_tokens` here always means prompt the endpoint actually processed,
    never the part it served from cache. OpenAI and DeepSeek report `prompt_tokens`
    inclusive of their cache hits, so the cached slice is subtracted on the way in —
    otherwise the same conversation would report two different cache hit rates depending on
    which address it was sent to, and the number would settle nothing.

    Cache *writes* are Anthropic-only: no OpenAI-compatible endpoint bills for populating a
    cache, so the field stays zero there rather than being guessed at.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_write_tokens)


@dataclass
class ModelTurn:
    reasoning: str = ""
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    """`None` when the endpoint reported nothing, which is not the same as reporting zero."""


class Provider(ABC):
    """One wire format. Owns its own message history."""

    name: str

    def __init__(self, model: str) -> None:
        self.model = model
        self.messages: list[Any] = []
        #: Rebuilt once per turn by the agent rather than per round.
        self.tool_cache: list[dict[str, Any]] = []
        #: One entry per completed `ask`, in order, so a caller counting rounds can line
        #: round N up with entry N-1 without the loop having to carry tokens through its
        #: event stream. A call whose usage the endpoint withheld still records a zero
        #: entry — dropping it would silently shift every later round's cost onto its
        #: neighbour.
        self.usage: list[Usage] = []

    def _bill(self, counted: Usage | None) -> Usage | None:
        """Log what a call cost and hand it back for the turn to carry."""
        self.usage.append(counted or Usage())
        return counted

    @abstractmethod
    def tools(self, mode: Mode | None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def ask(self, system: str) -> ModelTurn:
        """One model turn. Records the assistant message so the next round sees it."""

    @abstractmethod
    def add_results(self, results: list[tuple[ToolCall, dict[str, Any]]]) -> None: ...

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


# --------------------------------------------------------------------- anthropic


class AnthropicProvider(Provider):
    """Claude, natively.

    Native rather than through Anthropic's OpenAI-compatibility endpoint, for two reasons
    that matter to this loop specifically:

    - **Prompt caching.** The system prompt carries the whole context pyramid and is resent
      on every round of every turn. Marking it ephemeral makes the deck outline and
      component catalog nearly free after the first call, which is most of the input.
    - **Adaptive thinking.** Choosing a component and a variant under a budget is exactly
      the kind of decision worth thinking about, and the compatibility layer cannot ask
      for it.
    """

    name = "anthropic"

    def __init__(self, model: str = DEFAULT_ANTHROPIC_MODEL, *, api_key: str | None = None,
                 client: Any = None) -> None:
        super().__init__(model)
        self._client = client
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise RuntimeError(
                    "Claude needs the anthropic package: uv sync --extra claude"
                ) from exc
            if not self._api_key:
                raise RuntimeError("set ANTHROPIC_API_KEY to use Claude")
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def tools(self, mode: Mode | None) -> list[dict[str, Any]]:
        return router.anthropic_schemas(mode)

    def ask(self, system: str) -> ModelTurn:
        # Streamed and then collected: a turn that thinks and calls several tools can run
        # long enough to trip a request timeout, and `get_final_message` costs nothing.
        with self.client.messages.stream(
            model=self.model,
            max_tokens=MAX_TOKENS,
            # The system prompt holds the context pyramid and does not change within a
            # turn, so it is the one block worth caching.
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive", "display": "summarized"},
            tools=self.tool_cache,
            messages=self.messages,
        ) as stream:
            message = stream.get_final_message()

        self.messages.append({"role": "assistant", "content": message.content})

        turn = ModelTurn(usage=self._bill(anthropic_usage(getattr(message, "usage", None))))
        for block in message.content:
            if block.type == "thinking":
                turn.reasoning += getattr(block, "thinking", "") or ""
            elif block.type == "text":
                turn.text += block.text
            elif block.type == "tool_use":
                turn.calls.append(ToolCall(id=block.id, name=block.name,
                                           args=dict(block.input or {})))
        return turn

    def add_results(self, results: list[tuple[ToolCall, dict[str, Any]]]) -> None:
        blocks = []
        for call, result in results:
            blocks.append({
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": _payload(result),
                # Marked so the model treats a refusal as a refusal rather than as data —
                # every harness rejection carries the ways out, and they should be read.
                "is_error": not result.get("ok", True),
            })
        self.messages.append({"role": "user", "content": blocks})


# ----------------------------------------------------------------------- openai


class OpenAIProvider(Provider):
    """Any OpenAI-chat-compatible endpoint — OpenAI, vLLM, Ollama, Together, OpenRouter."""

    name = "openai"

    def __init__(self, model: str = DEFAULT_OPENAI_MODEL, *, base_url: str | None = None,
                 api_key: str | None = None, client: Any = None) -> None:
        super().__init__(model)
        self._client = client
        self._base_url = base_url
        # A local server that ignores auth still wants the header present.
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY") or "not-needed"

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "this endpoint needs the openai package: uv sync --extra agent"
                ) from exc
            self._client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        return self._client

    def tools(self, mode: Mode | None) -> list[dict[str, Any]]:
        return router.openai_schemas(mode)

    def ask(self, system: str) -> ModelTurn:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *self.messages],
            tools=self.tool_cache,
            tool_choice="auto",
        )
        choice = response.choices[0].message
        calls = [
            ToolCall(id=c.id, name=c.function.name, args=_loads(c.function.arguments))
            for c in (choice.tool_calls or [])
        ]

        # `<think>` in the text is one convention; a hosted reasoner (DeepSeek, or an
        # OpenRouter proxying one) returns the chain of thought in a sibling field instead.
        # Both land in the same place — and neither is sent back, which is not just tidiness:
        # DeepSeek rejects a request whose history carries `reasoning_content`.
        reasoning, text = split_thinking(choice.content or "")
        # DeepSeek's reasoners put the scratchpad in `reasoning_content` rather than in
        # `<think>` tags. Same intent, different envelope.
        reasoning = (getattr(choice, "reasoning_content", None) or "") or reasoning
        reasoning = getattr(choice, "reasoning_content", None) or reasoning
        entry: dict[str, Any] = {"role": "assistant", "content": text}
        if calls:
            entry["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                for c in calls
            ]
        self.messages.append(entry)
        return ModelTurn(reasoning=reasoning, text=text, calls=calls,
                         usage=self._bill(openai_usage(getattr(response, "usage", None))))

    def add_results(self, results: list[tuple[ToolCall, dict[str, Any]]]) -> None:
        for call, result in results:
            self.messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": _payload(result),
            })


# --------------------------------------------------------------------- deepseek


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek's hosted API — the OpenAI wire format at a different address.

    A class rather than a `base_url` the caller has to remember, for the same reason the
    Anthropic one exists: the endpoint is fixed, the key lives under its own name, and a
    missing key should say *which* key is missing instead of surfacing as a 401 from a host
    nobody typed. Nothing about the conversation differs, so the wire format is inherited
    whole.

    `deepseek-reasoner` puts its chain of thought in `reasoning_content`; `ask` already
    reads that field and never sends it back, which is what that model requires.
    """

    name = "deepseek"

    def __init__(self, model: str = DEFAULT_DEEPSEEK_MODEL, *, base_url: str | None = None,
                 api_key: str | None = None, client: Any = None) -> None:
        super().__init__(model, base_url=base_url or DEEPSEEK_BASE_URL,
                         api_key=api_key, client=client)
        # Set after the parent, whose fallback chain ends at OPENAI_API_KEY — a key for a
        # different vendor must never be sent here.
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or ""

    @property
    def client(self) -> Any:
        if self._client is None and not self._api_key:
            raise RuntimeError("set DEEPSEEK_API_KEY to use DeepSeek")
        return super().client


# ------------------------------------------------------------------------ usage
#
# Read off whatever the endpoint volunteered, defensively throughout. A renamed field, a
# `None` where a count was expected, a response object with no usage on it at all — each
# costs a metric and must never cost the turn, because nothing downstream of a slide depends
# on knowing what the slide cost.


def _count(source: Any, *names: str) -> int:
    """The first of `names` that holds a number, whether `source` is a mapping or an object."""
    for name in names:
        value = (source.get(name) if isinstance(source, dict)
                 else getattr(source, name, None))
        if isinstance(value, int | float) and not isinstance(value, bool):
            return int(value)
    return 0


def anthropic_usage(raw: Any) -> Usage | None:
    """Claude's counts, which already carry the split this harness wants."""
    if raw is None:
        return None
    return Usage(
        input_tokens=_count(raw, "input_tokens"),
        output_tokens=_count(raw, "output_tokens"),
        cache_read_tokens=_count(raw, "cache_read_input_tokens"),
        cache_write_tokens=_count(raw, "cache_creation_input_tokens"),
    )


def openai_usage(raw: Any) -> Usage | None:
    """The OpenAI-compatible counts, unpicked into the same split.

    `prompt_tokens` is a *total* on this wire format, cache hits included, so the hit has to
    come back out of it. Two vendors say where the hit is in two places: OpenAI nests
    `cached_tokens` under `prompt_tokens_details`, DeepSeek puts `prompt_cache_hit_tokens`
    at the top level. An endpoint that reports neither simply looks like a total cache miss,
    which is the honest reading — it is also the truth for most local servers.
    """
    if raw is None:
        return None
    prompt = _count(raw, "prompt_tokens", "input_tokens")
    details = (raw.get("prompt_tokens_details") if isinstance(raw, dict)
               else getattr(raw, "prompt_tokens_details", None))
    cached = _count(details, "cached_tokens") if details is not None else 0
    cached = cached or _count(raw, "prompt_cache_hit_tokens")
    return Usage(
        # Clamped: a partial report where the cached count exceeds the total it was meant to
        # be part of would otherwise book negative input and drag the whole suite's figure.
        input_tokens=max(prompt - cached, 0),
        output_tokens=_count(raw, "completion_tokens", "output_tokens"),
        cache_read_tokens=cached,
    )


# ---------------------------------------------------------------------- helpers


def _payload(result: dict[str, Any]) -> str:
    """What actually goes into the conversation.

    Trimmed, because a result is resent on every later round of the turn — the verbose form
    is kept for the UI, which is the audience that can use it.
    """
    return json.dumps(compact.for_model(result), ensure_ascii=False)[:6000]


def _loads(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def split_thinking(content: str) -> tuple[str, str]:
    """(reasoning, answer) for models that inline a scratchpad in `content`.

    Qwen3, DeepSeek-R1 and friends emit `<think>` blocks in the text. Claude returns
    thinking as its own block type and never needs this. Left in the answer it reads as
    though it *were* the answer.
    """
    import re

    pattern = re.compile(r"<think>(.*?)</think>\s*", re.DOTALL)
    reasoning = "\n".join(m.strip() for m in pattern.findall(content))
    answer = pattern.sub("", content).strip()
    # An unterminated block means the model was cut off mid-thought; everything after the
    # opening tag is reasoning, not an answer.
    if "<think>" in answer:
        head, _, tail = answer.partition("<think>")
        reasoning = (reasoning + "\n" + tail.strip()).strip()
        answer = head.strip()
    return reasoning, answer


def build(model: str | None = None, base_url: str | None = None,
          api_key: str | None = None, client: Any = None) -> Provider:
    """Pick a provider from what the caller asked for, then from the environment.

    An explicit `base_url` always means an OpenAI-compatible endpoint — that is the only
    reason to set one — so it decides before the model name does. A `deepseek-*` name with
    no base URL means the hosted API; with one it means whatever is serving at that address,
    which is how a locally-run R1 stays reachable.
    """
    base_url = base_url or os.environ.get("PPT_HARNESS_BASE_URL") or None
    model = model or os.environ.get("PPT_HARNESS_MODEL") or None

    if base_url:
        return OpenAIProvider(model or DEFAULT_OPENAI_MODEL, base_url=base_url,
                              api_key=api_key, client=client)
    if model and model.startswith("claude"):
        return AnthropicProvider(model, api_key=api_key, client=client)
    if model and model.startswith("deepseek"):
        return DeepSeekProvider(model, api_key=api_key, client=client)
    if model:
        return OpenAIProvider(model, api_key=api_key, client=client)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider(DEFAULT_ANTHROPIC_MODEL, api_key=api_key, client=client)
    if os.environ.get("DEEPSEEK_API_KEY"):
        return DeepSeekProvider(DEFAULT_DEEPSEEK_MODEL, api_key=api_key, client=client)
    return OpenAIProvider(DEFAULT_OPENAI_MODEL, api_key=api_key, client=client)
