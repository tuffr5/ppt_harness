"""A one-shot image-and-text client, for the judged half of the bench and nothing else.

**Why this is not in `core/providers.py`.** That module serves the agent loop: it owns a
message history, rebuilds a tool schema cache per turn, streams so a long thinking turn does
not trip a timeout, and marks a system block ephemeral so the context pyramid is bought once.
None of that applies here. A PPTEval call is stateless, toolless, single-shot, and sends one
image — retrofitting image blocks into a stateful tool-calling provider would put a second,
unrelated wire format inside the class every conversation in the harness runs through, to be
exercised only by a benchmark. So: same two wire formats, thirty lines, separate file.

**What it will not do.** If no vision model is configured it raises `VisionUnavailable`
naming the variable to set. It never falls back to the agent's model, never substitutes a
text-only endpoint, and never returns a placeholder score. A missing measurement and a bad
measurement are different findings and the difference is the only thing keeping this metric
honest — the machine this was written on has a DeepSeek key and nothing else, so the
unavailable path is the *default* experience here, not an edge case.

**The describe/score split is enforced here, not just intended.** `score` asserts its own
payload carries no image, and every call is recorded in `exchanges` so a test — or a person
who does not believe it — can check what actually went on the wire.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any

from ..core.providers import Usage, anthropic_usage, openai_usage, split_thinking

#: Long enough for the six sentences the describe prompt asks for, with room for a model that
#: ignores the limit; short enough that a runaway description is not billed to the end.
DESCRIBE_TOKENS = 800
#: One JSON object with a one-sentence reason. A scorer that needs more is not answering.
SCORE_TOKENS = 300

ANTHROPIC = "anthropic"
OPENAI = "openai"

#: Env vars, in the order a reader of `.env.example` would expect to find them.
MODEL_VAR = "PPT_HARNESS_VISION_MODEL"
BASE_URL_VAR = "PPT_HARNESS_VISION_BASE_URL"
SCORE_MODEL_VAR = "PPT_HARNESS_SCORE_MODEL"
SCORE_BASE_URL_VAR = "PPT_HARNESS_SCORE_BASE_URL"


class VisionUnavailable(RuntimeError):
    """No model that can look at a slide is configured, so nothing can be measured.

    Raised in preference to any score at all. Callers are expected to report it verbatim:
    every message this module raises names the variable, the key, or the package that is
    missing, because "could not score the deck" sends nobody anywhere.
    """


@dataclass(frozen=True)
class Part:
    """One piece of a message, before either wire format has had its say."""

    kind: str
    """`text` or `image`."""
    text: str = ""
    png: bytes = b""


@dataclass
class Exchange:
    """One request as it went out, kept so the describe/score split can be *checked*."""

    kind: str
    """`describe` or `score`."""
    model: str
    wire: str
    parts: tuple[Part, ...]
    reply: str = ""
    usage: Usage | None = None

    @property
    def carries_image(self) -> bool:
        return any(p.kind == "image" for p in self.parts)


def _anthropic_content(parts: tuple[Part, ...]) -> list[dict[str, Any]]:
    """Claude's block format. Base64 source, media type declared."""
    out: list[dict[str, Any]] = []
    for part in parts:
        if part.kind == "image":
            out.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.b64encode(part.png).decode("ascii")}})
        else:
            out.append({"type": "text", "text": part.text})
    return out


def _openai_content(parts: tuple[Part, ...]) -> list[dict[str, Any]]:
    """The OpenAI chat format — a `data:` URL rather than a source object.

    A list of parts even when every part is text: some compatible servers accept a bare
    string and some do not, and the list form is the one both understand.
    """
    out: list[dict[str, Any]] = []
    for part in parts:
        if part.kind == "image":
            data = base64.b64encode(part.png).decode("ascii")
            out.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{data}"}})
        else:
            out.append({"type": "text", "text": part.text})
    return out


@dataclass
class Endpoint:
    """One model at one address. Owns nothing between calls — that is the point."""

    model: str
    wire: str
    base_url: str | None = None
    api_key: str | None = None
    client: Any = None

    def _built(self) -> Any:
        """The vendor SDK client, made on first use so building a `Judge` costs nothing."""
        if self.client is not None:
            return self.client
        if self.wire == ANTHROPIC:
            try:
                import anthropic
            except ImportError as exc:
                raise VisionUnavailable(
                    "scoring with Claude needs the anthropic package: uv sync --extra claude"
                ) from exc
            self.client = anthropic.Anthropic(api_key=self.api_key)
        else:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise VisionUnavailable(
                    "this endpoint needs the openai package: uv sync --extra agent"
                ) from exc
            self.client = OpenAI(api_key=self.api_key or "not-needed", base_url=self.base_url)
        return self.client

    def send(self, parts: tuple[Part, ...], system: str,
             max_tokens: int) -> tuple[str, Usage | None]:
        """One request, one reply. Exceptions are the caller's to report, not to swallow."""
        client = self._built()
        if self.wire == ANTHROPIC:
            message = client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": _anthropic_content(parts)}],
            )
            text = "".join(getattr(b, "text", "") for b in message.content
                           if getattr(b, "type", "") == "text")
            return text.strip(), anthropic_usage(getattr(message, "usage", None))

        response = client.chat.completions.create(
            model=self.model, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": _openai_content(parts)}],
        )
        choice = response.choices[0].message
        # Reasoners inline a scratchpad in `content`; left in, it reaches a score parser that
        # would happily read a 1-5 out of the model's own deliberation about a 1-5.
        _, text = split_thinking(choice.content or "")
        return text.strip(), openai_usage(getattr(response, "usage", None))


class Judge:
    """A vision model that describes, and a text model that scores what it wrote.

    Two endpoints rather than one because the second job needs no eyes: the scorer reads
    prose, so a cheap text model does it, and separating them makes that a configuration
    choice instead of a rewrite. They are the same endpoint unless someone says otherwise.
    """

    def __init__(self, describer: Endpoint, scorer: Endpoint | None = None) -> None:
        self.describer = describer
        self.scorer = scorer or describer
        self.exchanges: list[Exchange] = []
        self.usage = Usage()

    def _log(self, exchange: Exchange) -> str:
        self.exchanges.append(exchange)
        if exchange.usage is not None:
            self.usage = Usage(
                input_tokens=self.usage.input_tokens + exchange.usage.input_tokens,
                output_tokens=self.usage.output_tokens + exchange.usage.output_tokens,
                cache_read_tokens=self.usage.cache_read_tokens
                + exchange.usage.cache_read_tokens,
                cache_write_tokens=self.usage.cache_write_tokens
                + exchange.usage.cache_write_tokens,
            )
        return exchange.reply

    def describe(self, png: bytes, prompt: str, system: str) -> str:
        """Look at the slide and write it down. The expensive half, hence the cache upstream."""
        parts = (Part(kind="image", png=png), Part(kind="text", text=prompt))
        exchange = Exchange(kind="describe", model=self.describer.model,
                            wire=self.describer.wire, parts=parts)
        exchange.reply, exchange.usage = self.describer.send(
            parts, system, DESCRIBE_TOKENS)
        return self._log(exchange)

    def score(self, prompt: str, system: str) -> str:
        """Score a description. **Never** sees the image, and asserts as much.

        The assertion is the metric's central claim in executable form: if an image ever
        reached this call the two halves would have collapsed into one look-and-judge
        request, the scores would drift the way single-call judges drift, and nothing
        downstream could tell.
        """
        parts = (Part(kind="text", text=prompt),)
        exchange = Exchange(kind="score", model=self.scorer.model, wire=self.scorer.wire,
                            parts=parts)
        if exchange.carries_image:   # pragma: no cover - guards a future edit, not a branch
            raise AssertionError("the scorer must never be sent an image")
        exchange.reply, exchange.usage = self.scorer.send(parts, system, SCORE_TOKENS)
        return self._log(exchange)

    @property
    def described(self) -> int:
        return sum(1 for e in self.exchanges if e.kind == "describe")

    def as_dict(self) -> dict[str, Any]:
        return {
            "describe_model": self.describer.model,
            "score_model": self.scorer.model,
            "describe_calls": self.described,
            "score_calls": sum(1 for e in self.exchanges if e.kind == "score"),
            "tokens": self.usage.total or None,
        }


# ------------------------------------------------------------------------ resolution


#: Hosted addresses whose models cannot take an image, whatever the model name suggests.
#: Only the one this repository actually ships a key for: guessing at the rest would go stale,
#: and an endpoint we have not enumerated still fails loudly, just with its own 400.
TEXT_ONLY_HOSTS = ("deepseek",)


def _wire_of(model: str, base_url: str | None) -> str:
    """Same rule as `providers.build`: a base URL means OpenAI-compatible, before anything
    else does. It is the only reason to set one."""
    if base_url:
        return OPENAI
    return ANTHROPIC if model.startswith("claude") else OPENAI


def _missing() -> VisionUnavailable:
    """The error a machine with no vision model gets — which is most machines, including this
    one. It names the variable, the two keys, and an example, because a person reading it is
    trying to find out what to type next."""
    return VisionUnavailable(
        f"no vision model configured: set {MODEL_VAR} to a model that can read an image "
        f"(for example {MODEL_VAR}=claude-opus-4-8 with ANTHROPIC_API_KEY set, or "
        f"{MODEL_VAR}=gpt-4o with OPENAI_API_KEY set, or a local server with "
        f"{BASE_URL_VAR}). PPTEval's design score is not computed without one — no default "
        "and no zero is substituted, because an unmeasured deck and a badly-designed deck "
        "must not read alike."
    )


def _endpoint(model: str, base_url: str | None, api_key: str | None,
              client: Any, *, vision: bool) -> Endpoint:
    wire = _wire_of(model, base_url)
    if client is None and vision and not base_url:
        # Checked before any request, so the failure names the model rather than arriving as
        # an opaque 400 from a host that was asked for something it has never supported.
        for host in TEXT_ONLY_HOSTS:
            if model.startswith(host):
                raise VisionUnavailable(
                    f"{model} is served by a text-only API: it cannot see a slide. Set "
                    f"{MODEL_VAR} to a vision model, or point {BASE_URL_VAR} at a server "
                    f"running one. ({SCORE_MODEL_VAR} may still be {model} — the scorer "
                    "reads a description, never the image.)"
                )
    if client is None and wire == ANTHROPIC:
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise VisionUnavailable(f"set ANTHROPIC_API_KEY to score with {model}")
    elif client is None and not base_url:
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise VisionUnavailable(f"set OPENAI_API_KEY to score with {model}")
    return Endpoint(model=model, wire=wire, base_url=base_url, api_key=api_key, client=client)


def build(model: str | None = None, *, base_url: str | None = None,
          api_key: str | None = None, client: Any = None,
          score_model: str | None = None, score_base_url: str | None = None,
          score_client: Any = None) -> Judge:
    """Resolve a judge from arguments, then the environment. Raises rather than degrading.

    Deliberately *not* wired to `PPT_HARNESS_MODEL`. That variable names the model driving the
    agent, which on this repository's own `.env` is text-only; inheriting it would turn "you
    have not configured a vision model" into a run that fails one slide at a time somewhere
    inside a benchmark, or worse, one that appears to work against a model politely
    hallucinating a description of an image it never received.
    """
    model = model or os.environ.get(MODEL_VAR) or ""
    base_url = base_url or os.environ.get(BASE_URL_VAR) or None
    if not model:
        raise _missing()

    describer = _endpoint(model, base_url, api_key, client, vision=True)

    scorer_model = score_model or os.environ.get(SCORE_MODEL_VAR) or ""
    if not scorer_model and score_client is None:
        return Judge(describer)
    scorer = _endpoint(scorer_model or model,
                       score_base_url or os.environ.get(SCORE_BASE_URL_VAR) or None,
                       api_key, score_client, vision=False)
    return Judge(describer, scorer)
