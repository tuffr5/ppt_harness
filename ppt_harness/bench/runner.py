"""Run a task through the harness and write down what happened.

One task is: open or create a deck, take each turn in order against the real agent loop, then
measure. Nothing is mocked — the point of the exercise is what the model and the harness do
together, and a benchmark that stubbed either would be measuring the stub.

Two properties this file exists to hold on to:

**A failed task is a result, not a crash.** A model that times out, an export that refuses, a
suite item that turns out to be impossible — each is recorded and scored as what it is. A
runner that raises on the first bad task produces a benchmark nobody finishes running.

**Every artifact is kept.** The `.pptx`, the PDF, the event log, and the measurements land
under one directory per task, because the number is only worth something if you can open the
deck it came from. This is also what feeds `bench/adapters`, which submit those same
artifacts to the public benchmarks.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.loop import Agent
from ..core.session import Session
from . import metrics
from .tasks import Task


@dataclass
class TurnRecord:
    prompt: str
    rounds: int = 0
    tool_calls: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    reply: str = ""
    error: str = ""
    seconds: float = 0.0


@dataclass
class Result:
    task: str
    kind: str
    ok: bool = False
    error: str = ""
    turns: list[TurnRecord] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    expectations: dict[str, bool] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    model: str = ""

    @property
    def met(self) -> bool:
        """Did the deck contain what the brief plainly asked for?

        Deliberately weak. These are presence checks — a chart exists, there are four slides
        — not judgements. Anything stronger belongs to a judge, and pretending otherwise is
        how a benchmark starts scoring its own opinions as facts.
        """
        return all(self.expectations.values()) if self.expectations else self.ok


def _check(task: Task, session: Session, fit: metrics.Fit,
           shape: metrics.Shape) -> dict[str, bool]:
    out: dict[str, bool] = {}
    expects = task.expects or {}
    if "min_slides" in expects:
        out["min_slides"] = len(session.deck.slides) >= expects["min_slides"]
    if "max_slides" in expects:
        out["max_slides"] = len(session.deck.slides) <= expects["max_slides"]
    if expects.get("clean"):
        out["clean"] = fit.fit_rate == 1.0
    for component in expects.get("components", []):
        # `any`, not `all`: a brief asking for "a chart" is met by one chart, and a deck that
        # used a card grid where a chart was requested has missed it. Named per component so
        # the report says which one was skipped rather than that "components" failed.
        out[f"has:{component}"] = component in shape.components
    return out


def run_task(task: Task, out_dir: Path, *, model: str | None = None,
             base_url: str | None = None, template: str | None = None,
             suite_dir: Path | None = None, client: Any = None) -> Result:
    """One task, end to end. Never raises for a task-level failure."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result = Result(task=task.id, kind=task.kind)

    try:
        if task.source:
            source = (suite_dir or Path.cwd()) / task.source
            session = Session.open(source)
            original: Path | None = Path(source)
        elif template:
            session = (Session.from_template(template, task.id)
                       if Path(template).exists()
                       else Session.from_builtin(template, task.id))
            original = None
        else:
            session = Session.blank(task.id)
            original = None
    except Exception as exc:
        result.error = f"could not open the deck: {type(exc).__name__}: {exc}"
        return result

    # `client` is how the suite tests itself. A benchmark runner is code like any other and
    # has its own bugs — an expectation checked against the wrong field, a metric summed
    # twice — and finding those should not cost a model call or an API key.
    agent = Agent(session, **({"model": model} if model else {}),
                  **({"base_url": base_url} if base_url else {}),
                  **({"client": client} if client is not None else {}))
    result.model = agent.model

    friction = metrics.Friction()
    for prompt in task.turns:
        record = TurnRecord(prompt=prompt)
        started = time.monotonic()
        events: list[Any] = []
        try:
            for event in agent.run(prompt):
                events.append(event)
                if event.kind == "tool_call":
                    record.tool_calls.append(event.tool)
                elif event.kind == "tool_result" and not (event.result or {}).get("ok", True):
                    record.refusals.append(str((event.result or {}).get("error")))
                elif event.kind == "text":
                    record.reply = event.text
                elif event.kind == "error":
                    record.error = event.text
        except Exception as exc:   # a provider that dies mid-stream is a task failure
            record.error = f"{type(exc).__name__}: {exc}"

        record.seconds = round(time.monotonic() - started, 1)
        turn_friction = metrics.measure_friction(events, record.seconds)
        record.rounds = turn_friction.rounds
        friction.rounds += turn_friction.rounds
        friction.tool_calls += turn_friction.tool_calls
        friction.refusals += turn_friction.refusals
        friction.repairs += turn_friction.repairs
        friction.seconds = round(friction.seconds + record.seconds, 1)
        for code, count in turn_friction.refusal_codes.items():
            friction.refusal_codes[code] = friction.refusal_codes.get(code, 0) + count
        result.turns.append(record)

    # Measurement is guarded too. A benchmark that dies while *scoring* loses every task
    # that already ran — which is exactly what happened: an export the writer could not
    # represent raised through the runner and took a 32-example run with it.
    try:
        fit = metrics.measure_fit(session)
        shape = metrics.measure_shape(session)
        integrity = metrics.measure_integrity(session, out_dir / "deck.pptx",
                                              original=original)
        result.measurements = metrics.as_dict(fit, friction, integrity, shape)
        result.expectations = _check(task, session, fit, shape)
        result.ok = integrity.exported and not any(t.error for t in result.turns)
    except Exception as exc:
        result.error = f"could not measure the result: {type(exc).__name__}: {exc}"
        integrity = metrics.Integrity()
    if integrity.exported:
        result.artifacts["pptx"] = str(out_dir / "deck.pptx")

    (out_dir / "turns.json").write_text(
        json.dumps([vars(t) for t in result.turns], indent=1), encoding="utf-8")
    (out_dir / "result.json").write_text(
        json.dumps(_result_json(result), indent=1), encoding="utf-8")
    return result


def _result_json(result: Result) -> dict[str, Any]:
    return {
        "task": result.task, "kind": result.kind, "ok": result.ok, "met": result.met,
        "error": result.error, "model": result.model,
        "expectations": result.expectations,
        "measurements": result.measurements,
        "artifacts": result.artifacts,
        "turns": [{"prompt": t.prompt, "rounds": t.rounds, "seconds": t.seconds,
                   "tools": t.tool_calls, "refusals": t.refusals, "error": t.error}
                  for t in result.turns],
    }


def render_pdf(pptx: Path, out_pdf: Path) -> str | None:
    """The artifact the judged benchmarks want. Returns the engine, or None if there is none.

    Optional on purpose: the deterministic half of this suite must run on a machine with no
    Office and no browser, and only the adapters that submit to a judge need a picture.
    """
    from ..fidelity import reference

    if reference.available() is None:
        return None
    try:
        return reference.to_pdf(pptx, out_pdf)
    except Exception:
        return None
