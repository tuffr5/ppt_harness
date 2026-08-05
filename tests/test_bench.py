"""The benchmark suite — `ppt_harness/bench`.

A benchmark is code, and code that produces numbers people quote deserves more scepticism
than most, not less. The failure mode is specific and quiet: a metric that sums the wrong
field, an expectation checked against a key nobody sets, a rate whose denominator drops the
runs that failed. None of those crash; they publish.

So every test here runs the runner against a **scripted client** rather than a model. That
keeps the suite deterministic and free, and it means a bug in the benchmark is caught by the
benchmark's own tests rather than by someone noticing the number looked odd.

The corpus half needs no client at all — it is a round trip through the exporter — so it is
tested against the committed fixture directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ppt_harness.bench import adapters, corpus, metrics, report, runner
from ppt_harness.bench import tasks as task_lib
from ppt_harness.bench.tasks import Task
from ppt_harness.core.session import Session

# ------------------------------------------------------------------ a scripted model


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
class _Usage:
    """OpenAI's shape: `prompt_tokens` is a total with the cache hit already inside it."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: Any = None


@dataclass
class _Cached:
    cached_tokens: int = 0


@dataclass
class _Anthropic:
    """Claude's shape: the cache counts sit beside `input_tokens`, not inside it."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _Response:
    choices: list[_Choice]
    usage: Any = None


class FakeClient:
    """No usage on the responses — the default an endpoint that reports nothing produces."""

    def __init__(self, turns: list[_Msg], usage: list[Any] | None = None) -> None:
        self._turns = list(turns)
        self._usage = list(usage or [])
        self.chat = self
        self.completions = self

    def create(self, **kw: Any) -> _Response:
        message = self._turns.pop(0) if self._turns else _Msg(content="done")
        billed = self._usage.pop(0) if self._usage else None
        return _Response(choices=[_Choice(message=message)], usage=billed)


def _call(name: str, args: dict[str, Any], cid: str = "c1") -> _Call:
    return _Call(id=cid, function=_Fn(name=name, arguments=json.dumps(args)))


ONE_SLIDE = {
    "layout": "stack",
    "blocks": [
        {"region": "header", "component": "slide_title",
         "slots": {"title": "Churn doubled in EMEA"}},
        {"region": "body", "component": "bullets",
         "slots": {"items": ["Mid-market renewals slipped", "Expansion covered it"]}},
    ],
}


# ------------------------------------------------------------------------- the suites


def test_the_shipped_suites_parse() -> None:
    found = task_lib.suites()
    assert found, "no task suites ship with the package"
    for name in found:
        assert task_lib.load_suite(name)


@pytest.mark.parametrize("task", task_lib.load_suite("core"), ids=lambda t: t.id)
def test_every_task_is_well_formed(task: Task) -> None:
    """A task nobody can satisfy measures the benchmark, not the harness."""
    from ppt_harness.components import registry

    assert task.kind in ("generate", "edit", "review")
    assert len(task.brief.split()) >= 12, "a brief this short cannot be judged"
    for component in task.expects.get("components", []):
        assert component in registry.COMPONENTS, f"{task.id} expects an unknown component"
    known = {"min_slides", "max_slides", "clean", "components"}
    assert set(task.expects) <= known, f"{task.id} has an unknown expectation key"


def test_no_task_asks_for_something_the_api_forbids() -> None:
    """Principle 1, applied to the benchmark.

    A brief naming a hex colour, a font or a position would score the harness on refusing
    it — which is a measurement of the ban, not of the output.
    """
    import re

    banned = re.compile(r"#[0-9a-fA-F]{6}|\bpixels?\b|\bfont size\b|\bArial\b|\bpt\b")
    for task in task_lib.load_suite("core"):
        for turn in task.turns:
            assert not banned.search(turn), f"{task.id} names something no tool accepts"


# ------------------------------------------------------------------------- the metrics


def test_fit_is_measured_over_slides_not_decks(populated: Session) -> None:
    fit = metrics.measure_fit(populated)
    assert fit.slides == len(populated.deck.slides)
    assert fit.fit_rate == 1.0
    assert fit.overflow_px == 0.0


def test_friction_counts_refusals_from_the_event_stream() -> None:
    """Read off the turn's own events; nothing extra is instrumented for the benchmark."""
    from ppt_harness.core.loop import Event

    events = [
        Event(kind="round", round=1),
        Event(kind="tool_call", tool="add_slide"),
        Event(kind="tool_result", result={"ok": False, "error": "budget_exceeded"}),
        Event(kind="tool_call", tool="repair"),
        Event(kind="tool_result", result={"ok": True}),
        Event(kind="round", round=2),
    ]
    friction = metrics.measure_friction(events, seconds=12.0)
    assert friction.rounds == 2
    assert friction.tool_calls == 2
    assert friction.refusals == 1
    assert friction.repairs == 1
    assert friction.refusal_codes == {"budget_exceeded": 1}
    assert friction.refusal_rate == 0.5


def test_friction_without_a_usage_report_is_zero_not_a_crash() -> None:
    """Most of this suite runs against a scripted client that bills nothing.

    Zero tokens and `None` rates, never an exception — and never a `0.0` cache hit rate,
    which would read as "the cache never fired" when the truth is that nobody counted.
    """
    from ppt_harness.core.loop import Event

    events = [
        Event(kind="round", round=1),
        Event(kind="tool_call", tool="add_slide"),
        Event(kind="tool_result", result={"ok": True}),
    ]
    friction = metrics.measure_friction(events, seconds=1.0)
    friction.landed_slides = 3

    assert friction.tokens == 0
    assert friction.cache_hit_rate is None
    assert friction.tokens_per_landed_slide is None
    assert friction.refused_token_share is None
    assert friction.tokens_per_repair is None
    assert friction.rates()["tokens"] == 0
    assert friction.rates()["cache_hit_rate"] is None


def _usage(**kw: int) -> Any:
    from ppt_harness.core.providers import Usage

    return Usage(**kw)


def test_friction_aggregates_the_tokens_the_provider_billed() -> None:
    """One entry per round, in round order — the join the runner makes by slicing."""
    from ppt_harness.core.loop import Event

    events = [Event(kind="round", round=1), Event(kind="round", round=2)]
    friction = metrics.measure_friction(events, seconds=3.0, usage=[
        _usage(input_tokens=900, output_tokens=200, cache_write_tokens=4000),
        _usage(input_tokens=100, output_tokens=50, cache_read_tokens=4000),
    ])

    assert friction.input_tokens == 1000
    assert friction.output_tokens == 250
    assert friction.cache_write_tokens == 4000
    assert friction.cache_read_tokens == 4000
    assert friction.tokens == 9250
    # Over prompt tokens only: completions are never served from a cache, and including
    # them would let a chatty turn depress a hit rate the cache had nothing to do with.
    assert friction.cache_hit_rate == pytest.approx(4000 / 9000)

    friction.landed_slides = 5
    assert friction.tokens_per_landed_slide == pytest.approx(1850.0)


def test_a_refused_write_is_charged_its_share_of_the_round_that_made_it() -> None:
    """Principle 3 as arithmetic. Two calls in a round, one refused: half that round.

    The round that only summarises is charged to nobody — it bought no tool calls, so
    attributing it to the refusal would inflate the very number that is meant to test
    whether refusals are cheap.
    """
    from ppt_harness.core.loop import Event

    events = [
        Event(kind="round", round=1),
        Event(kind="tool_call", tool="add_slide"),
        Event(kind="tool_result", result={"ok": False, "error": "budget_exceeded"}),
        Event(kind="tool_call", tool="add_slide"),
        Event(kind="tool_result", result={"ok": True}),
        Event(kind="round", round=2),
        Event(kind="tool_call", tool="repair"),
        Event(kind="tool_result", result={"ok": True}),
        Event(kind="round", round=3),
    ]
    friction = metrics.measure_friction(events, seconds=9.0, usage=[
        _usage(input_tokens=800, output_tokens=200),      # round 1: 1000 over two calls
        _usage(input_tokens=400, output_tokens=100),      # round 2: 500 on one repair
        _usage(input_tokens=200, output_tokens=50),       # round 3: no calls, nobody's
    ])

    assert friction.refused_tokens == pytest.approx(500.0)
    assert friction.repair_tokens == pytest.approx(500.0)
    assert friction.tokens == 1750
    assert friction.refused_token_share == pytest.approx(500 / 1750)
    assert friction.tokens_per_repair == pytest.approx(500.0)


def test_a_turn_that_never_repaired_reports_no_repair_cost() -> None:
    """Zero would be a claim that repairs are free rather than that none happened."""
    from ppt_harness.core.loop import Event

    friction = metrics.measure_friction(
        [Event(kind="round", round=1)], seconds=1.0,
        usage=[_usage(input_tokens=100, output_tokens=10)])

    assert friction.repairs == 0
    assert friction.tokens_per_repair is None
    assert friction.refused_token_share == 0.0, "measured, and it really was nothing"


def test_usage_is_read_off_each_wire_format_and_normalised() -> None:
    """The two formats disagree about whether `prompt_tokens` includes the cache hit.

    Unnormalised, the same conversation would report two different cache hit rates
    depending on which address it went to, and the figure would settle nothing.
    """
    from ppt_harness.core import providers

    claude = providers.anthropic_usage(_Anthropic(input_tokens=120, output_tokens=40,
                                                  cache_read_input_tokens=6000,
                                                  cache_creation_input_tokens=0))
    assert claude == _usage(input_tokens=120, output_tokens=40, cache_read_tokens=6000)

    openai = providers.openai_usage(_Usage(prompt_tokens=6120, completion_tokens=40,
                                           prompt_tokens_details=_Cached(cached_tokens=6000)))
    assert openai == claude, "the same turn has to count the same on either wire"

    deepseek = providers.openai_usage({"prompt_tokens": 6120, "completion_tokens": 40,
                                       "prompt_cache_hit_tokens": 6000})
    assert deepseek == claude, "DeepSeek names the hit at the top level; same reading"


def test_a_partial_or_absent_usage_report_never_breaks_a_turn() -> None:
    """Telemetry is not the answer. A renamed field costs a metric, never a slide."""
    from ppt_harness.core import providers

    assert providers.anthropic_usage(None) is None
    assert providers.openai_usage(None) is None
    # A total that is somehow smaller than the cache hit inside it would book negative
    # input and drag the whole suite's figure below zero.
    assert providers.openai_usage({"prompt_tokens": 10,
                                   "prompt_cache_hit_tokens": 99}).input_tokens == 0
    assert providers.openai_usage({"prompt_tokens": None}).total == 0
    assert providers.openai_usage(object()).total == 0


def test_the_usage_log_stays_aligned_with_the_rounds_it_bills() -> None:
    """Entry `i` is round `i + 1`, and a silent call has to hold its place.

    Dropping the unreported call instead would shift every later round's cost one place
    back, so a refusal in round three would be charged whatever round four spent — a
    misattribution nothing downstream could detect.
    """
    from ppt_harness.core import providers

    client = FakeClient([_Msg(content="a"), _Msg(content="b"), _Msg(content="c")],
                        usage=[_Usage(prompt_tokens=100, completion_tokens=10),
                               None,
                               _Usage(prompt_tokens=300, completion_tokens=30)])
    provider = providers.OpenAIProvider("fake", client=client)
    turns = [provider.ask("system") for _ in range(3)]

    assert [u.total for u in provider.usage] == [110, 0, 330]
    assert turns[1].usage is None, "nothing reported is not the same as nothing spent"
    assert turns[2].usage is not None and turns[2].usage.input_tokens == 300


def test_shape_counts_what_was_built(populated: Session) -> None:
    shape = metrics.measure_shape(populated)
    assert shape.components.get("bullets", 0) >= 1
    assert shape.words > 0
    assert "stack" in shape.layouts


def test_integrity_reports_a_clean_export(populated: Session, tmp_path: Path) -> None:
    integrity = metrics.measure_integrity(populated, tmp_path / "out.pptx")
    assert integrity.exported
    assert integrity.sound
    assert integrity.parts_after > 0


# -------------------------------------------------------------------------- the runner


def test_a_task_runs_end_to_end_without_a_model(tmp_path: Path) -> None:
    task = Task(id="scripted", kind="generate", brief="Build one slide about EMEA churn.",
                expects={"min_slides": 1, "clean": True, "components": ["bullets"]})
    client = FakeClient([
        _Msg(tool_calls=[_call("add_slide", ONE_SLIDE)]),
        _Msg(content="Built one slide."),
    ])

    result = runner.run_task(task, tmp_path / "scripted", client=client)

    assert result.ok
    assert result.met, result.expectations
    assert result.expectations == {"min_slides": True, "clean": True, "has:bullets": True}
    assert Path(result.artifacts["pptx"]).exists()
    assert (tmp_path / "scripted" / "result.json").exists()
    assert result.measurements["fit"]["fit_rate"] == 1.0


def test_a_run_carries_what_it_cost_through_to_the_measurements(tmp_path: Path) -> None:
    """The whole point: tokens billed on the wire come out the other end as a rate.

    Two rounds, one refused write, and the deck that survived it — so every derived figure
    has both of its ends measured rather than assumed.
    """
    task = Task(id="billed", kind="generate", brief="Build one slide about EMEA churn now.")
    client = FakeClient(
        [_Msg(tool_calls=[_call("add_slide", ONE_SLIDE)]),
         _Msg(tool_calls=[_call("set_text", {"target": "nope/x", "text": "y"}, "c2")]),
         _Msg(content="Built one slide.")],
        usage=[_Usage(prompt_tokens=5000, completion_tokens=200,
                      prompt_tokens_details=_Cached(cached_tokens=4000)),
               _Usage(prompt_tokens=5200, completion_tokens=100,
                      prompt_tokens_details=_Cached(cached_tokens=5000)),
               _Usage(prompt_tokens=5400, completion_tokens=60,
                      prompt_tokens_details=_Cached(cached_tokens=5000))],
    )

    result = runner.run_task(task, tmp_path / "billed", client=client)
    friction = result.measurements["friction"]

    assert friction["tokens"] == 5200 + 5300 + 5460
    assert friction["cache_read_tokens"] == 14000
    assert friction["cache_hit_rate"] == pytest.approx(14000 / 15600, abs=1e-4)
    # One slide landed, so the per-slide figure is the whole bill.
    assert friction["tokens_per_landed_slide"] == pytest.approx(friction["tokens"], abs=0.5)
    # Round two spent 5300 on a single call, and that call was refused.
    assert friction["refused_tokens"] == pytest.approx(5300.0)
    assert friction["refused_token_share"] == pytest.approx(5300 / friction["tokens"], abs=1e-4)
    assert result.turns[0].tokens == friction["tokens"]

    written = json.loads((tmp_path / "billed" / "result.json").read_text())
    assert written["measurements"]["friction"]["tokens"] == friction["tokens"]
    assert written["turns"][0]["tokens"] == friction["tokens"]


def test_a_scripted_run_reports_no_tokens_rather_than_free_ones(tmp_path: Path) -> None:
    """The suite's own default. No usage on the wire must not read as a costless run."""
    task = Task(id="silent", kind="generate", brief="Build one slide about EMEA churn now.")
    client = FakeClient([_Msg(tool_calls=[_call("add_slide", ONE_SLIDE)]),
                         _Msg(content="Built one slide.")])

    result = runner.run_task(task, tmp_path / "silent", client=client)
    friction = result.measurements["friction"]

    assert result.ok
    assert friction["tokens"] == 0
    assert friction["cache_hit_rate"] is None
    assert friction["tokens_per_landed_slide"] is None
    assert friction["refused_token_share"] is None


def test_a_missed_expectation_is_reported_not_hidden(tmp_path: Path) -> None:
    """The one that matters. A benchmark that scores its failures as passes is worthless."""
    task = Task(id="wants-a-chart", kind="generate", brief="Build a slide with a chart on it.",
                expects={"min_slides": 3, "components": ["chart"]})
    client = FakeClient([
        _Msg(tool_calls=[_call("add_slide", ONE_SLIDE)]),
        _Msg(content="Built one slide."),
    ])

    result = runner.run_task(task, tmp_path / "missed", client=client)

    assert result.ok, "the run itself succeeded"
    assert not result.met, "one slide and no chart is not three slides with a chart"
    assert result.expectations["min_slides"] is False
    assert result.expectations["has:chart"] is False


def test_a_failing_turn_is_a_result_not_a_crash(tmp_path: Path) -> None:
    class Broken:
        def __init__(self) -> None:
            self.chat = self
            self.completions = self

        def create(self, **kw: Any) -> Any:
            raise RuntimeError("the provider fell over")

    task = Task(id="broken", kind="generate", brief="Build something about quarterly churn.")
    result = runner.run_task(task, tmp_path / "broken", client=Broken())

    assert not result.ok
    assert result.turns and result.turns[0].error
    assert (tmp_path / "broken" / "result.json").exists(), "a failure still leaves evidence"


# -------------------------------------------------------------------------- the report


def test_the_scorecard_keeps_its_denominators() -> None:
    results = [
        runner.Result(task="a", kind="generate", ok=True, expectations={"clean": True},
                      measurements={"fit": {"slides": 4, "clean_slides": 4},
                                    "friction": {"refusals": 1, "tool_calls": 10},
                                    "integrity": {"sound": True, "violations": []},
                                    "shape": {"review_findings": 2}}),
        runner.Result(task="b", kind="generate", ok=False, expectations={"clean": False},
                      measurements={"fit": {"slides": 1, "clean_slides": 0},
                                    "friction": {"refusals": 0, "tool_calls": 2},
                                    "integrity": {"sound": False, "violations": ["x"]},
                                    "shape": {"review_findings": 0}}),
    ]
    card = report.score(results)

    assert card.tasks == 2 and card.ran == 1 and card.met == 1
    # Over slides, not the mean of per-deck rates: a one-slide deck must not outvote a
    # four-slide one, or the benchmark quietly rewards short decks.
    assert card.fit_rate == pytest.approx(4 / 5)
    assert card.violations == 1

    body = report.markdown({"suite": "t", "environment": report.environment("m"),
                            "scorecard": card.as_dict(),
                            "tasks": [{"task": r.task, "kind": r.kind, "ok": r.ok,
                                       "met": r.met, "error": r.error,
                                       "expectations": r.expectations,
                                       **r.measurements} for r in results]})
    assert "4/5" in body and "0.800" in body


def test_the_scorecard_sums_tokens_and_keeps_their_denominators() -> None:
    """A suite figure that disagreed with the tasks it sums is the bug this file guards."""
    results = [
        runner.Result(task="a", kind="generate", ok=True,
                      measurements={"fit": {"slides": 4, "clean_slides": 4},
                                    "friction": {"refusals": 1, "tool_calls": 10,
                                                 "repairs": 2, "input_tokens": 1000,
                                                 "output_tokens": 400,
                                                 "cache_read_tokens": 8000,
                                                 "cache_write_tokens": 600,
                                                 "refused_tokens": 500.0,
                                                 "repair_tokens": 900.0}}),
        runner.Result(task="b", kind="generate", ok=True,
                      measurements={"fit": {"slides": 1, "clean_slides": 1},
                                    "friction": {"refusals": 0, "tool_calls": 2,
                                                 "repairs": 0, "input_tokens": 500,
                                                 "output_tokens": 100,
                                                 "cache_read_tokens": 2000,
                                                 "cache_write_tokens": 0,
                                                 "refused_tokens": 0.0,
                                                 "repair_tokens": 0.0}}),
    ]
    card = report.score(results).as_dict()

    assert card["tokens"] == 12600
    # Over the suite's slides, the same "not the mean of per-deck rates" choice fit_rate
    # makes — five slides cost 12,600 tokens between them.
    assert card["tokens_per_landed_slide"] == pytest.approx(2520.0)
    assert card["cache_hit_rate"] == pytest.approx(10000 / 12100, abs=1e-4)
    assert card["refused_token_share"] == pytest.approx(500 / 12600, abs=1e-4)
    assert card["tokens_per_repair"] == pytest.approx(450.0)

    body = report.markdown({"suite": "t", "environment": report.environment("m"),
                            "scorecard": card, "tasks": []})
    assert "12,600" in body, "the total is printed beside the rates derived from it"
    assert "over 5 slides" in body, "every figure carries its denominator"
    assert "2 repairs" in body


def test_a_suite_nobody_billed_says_so_rather_than_printing_zeroes() -> None:
    """A table of zeroes reads as "this run was free", not "nobody was counting"."""
    card = report.Scorecard(tasks=1, ran=1, slides=3, clean_slides=3).as_dict()
    body = report.markdown({"suite": "t", "environment": report.environment("m"),
                            "scorecard": card, "tasks": []})

    assert card["tokens"] == 0
    assert card["cache_hit_rate"] is None
    assert "not reported by this endpoint" in body
    assert "Tokens per landed slide" not in body


def test_the_environment_is_recorded() -> None:
    """A score with no machine, model or renderer attached is not evidence of anything."""
    env = report.environment("some-model")
    assert env["model"] == "some-model"
    assert env["python"] and env["platform"] and env["when"]
    assert "reference_renderer" in env


# ------------------------------------------------------------------------ the adapters


def test_ppteval_submission_is_a_flat_folder_of_decks(populated: Session,
                                                      tmp_path: Path) -> None:
    deck = tmp_path / "deck.pptx"
    metrics.measure_integrity(populated, deck)
    results = [runner.Result(task="t1", kind="generate", ok=True,
                             artifacts={"pptx": str(deck)})]

    submission = adapters.ppteval(results, tmp_path / "sub")
    assert submission.cases == 1
    assert (tmp_path / "sub" / "t1.pptx").exists()
    assert json.loads((tmp_path / "sub" / "manifest.json").read_text())["decks"] == ["t1.pptx"]


def test_the_slidesbench_mismatch_is_written_beside_the_decks(populated: Session,
                                                              tmp_path: Path) -> None:
    """Their reference-based metric scores position against a target slide, which this
    harness does not reproduce by design. Whoever runs it in six months reads the folder."""
    deck = tmp_path / "deck.pptx"
    metrics.measure_integrity(populated, deck)
    results = [runner.Result(task="t1", kind="generate", ok=True,
                             artifacts={"pptx": str(deck)})]

    adapters.slidesbench(results, tmp_path / "sub")
    note = (tmp_path / "sub" / "READ-THIS-FIRST.md").read_text()
    assert "page_eval.py" in note and "diagnostic" in note


def test_an_unrenderable_case_leaves_no_empty_submission(tmp_path: Path,
                                                         monkeypatch) -> None:
    """An empty case directory is a zero for the wrong reason."""
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"not really a deck")
    monkeypatch.setattr(adapters, "render_pdf", lambda *a, **k: None)
    results = [runner.Result(task="t1", kind="generate", ok=True,
                             artifacts={"pptx": str(deck)})]

    submission = adapters.presentbench(results, tmp_path / "sub")
    assert submission.cases == 0
    assert submission.missing == ["t1"]
    assert not list((tmp_path / "sub").rglob("*.pdf"))
    assert not (tmp_path / "sub" / "harness" / "t1").exists()


# --------------------------------------------------------------------------- the corpus


def test_a_round_trip_preserves_the_fixture(fixture_deck: Path, tmp_path: Path) -> None:
    """No model, no judge, no network — the claim the README makes loudest, measured."""
    result = corpus.round_trip(fixture_deck, tmp_path)
    assert result.ok, result.error
    assert result.preserved, result.lost
    assert result.slides > 0
    assert result.parts_before > 0


def test_an_edit_preserves_what_it_did_not_touch(fixture_deck: Path, tmp_path: Path) -> None:
    result = corpus.round_trip(fixture_deck, tmp_path, edit=True)
    assert result.preserved, result.lost
    assert result.opaque_shapes >= 0


def test_a_corpus_run_reports_rates_with_denominators(fixture_deck: Path,
                                                      tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "one.pptx").write_bytes(fixture_deck.read_bytes())
    (corpus_dir / "broken.pptx").write_bytes(b"not a deck at all")

    payload = corpus.run(corpus_dir, tmp_path / "out")

    assert payload["decks"] == 2
    assert payload["opened"] == 1, "the broken file must not be silently dropped"
    assert payload["open_rate"] == 0.5
    assert payload["preservation_rate"] == 1.0, "rates are over what opened, and say so"
    assert payload["failures"], "the file that would not open is named"
    assert (tmp_path / "out" / "corpus.json").exists()


# ---------------------------------------------------------------------- slidesbench


def _fake_slidesbench(root: Path) -> Path:
    """Their directory shape, minus the 61 MB clone."""
    from ppt_harness.core.session import Session as _S
    from ppt_harness.tools import router as _r

    examples = root / "slidesbench" / "examples" / "food"
    (examples / "slide_3").mkdir(parents=True)
    (examples / "slide_3" / "instruction_high_level.txt").write_text(
        "Create a slide titled 'Fries' with two bullet points about potatoes.",
        encoding="utf-8")
    (examples / "notes").mkdir()          # a non-example directory, to be ignored

    reference = _S.blank("ref")
    _r.dispatch(reference, "add_slide", {"layout": "stack", "blocks": [
        {"region": "header", "component": "slide_title", "slots": {"title": "Fries"}}]})
    _r.dispatch(reference, "export", {"path": str(examples / "food.pptx")})
    return root


def test_examples_are_found_and_paged(tmp_path: Path) -> None:
    from ppt_harness.bench import slidesbench

    repo = _fake_slidesbench(tmp_path / "repo")
    found = slidesbench.examples(repo, "food")

    assert [e.id for e in found] == ["slide_3"]
    assert found[0].page == 3, "the page number comes from the directory name"
    assert "Fries" in found[0].instruction
    assert found[0].reference.name == "food.pptx"


def test_a_domain_without_a_reference_deck_yields_nothing(tmp_path: Path) -> None:
    """Nine of their ten domains ship instructions and no slides — by their own policy.

    Reporting those as zero-scoring examples would invent a failure; they are simply not
    scoreable until someone re-downloads the decks.
    """
    from ppt_harness.bench import slidesbench

    root = tmp_path / "repo" / "slidesbench" / "examples" / "career"
    (root / "slide_1").mkdir(parents=True)
    (root / "slide_1" / "instruction_high_level.txt").write_text("x", encoding="utf-8")

    assert slidesbench.examples(tmp_path / "repo", "career") == []


def test_scores_are_parsed_from_their_stdout(tmp_path: Path, monkeypatch) -> None:
    """We read their printed table rather than their JSON, which crashes on its own floats."""
    import subprocess

    from ppt_harness.bench import slidesbench

    class Done:
        stdout = "# of blocks 2 generated 3 reference\nmatch     : 82.0\ntext      : 95.4\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    scores = slidesbench.score_page(tmp_path, Path("python"), tmp_path / "a.pptx", 1,
                                    tmp_path / "b.pptx", 3)
    assert scores == pytest.approx({"match": 0.82, "text": 0.954})


def test_an_evaluator_that_prints_nothing_raises_with_its_message(tmp_path: Path,
                                                                  monkeypatch) -> None:
    import subprocess

    from ppt_harness.bench import slidesbench

    class Done:
        stdout = ""
        stderr = "ZeroDivisionError: division by zero"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    with pytest.raises(RuntimeError, match="ZeroDivisionError"):
        slidesbench.score_page(tmp_path, Path("python"), tmp_path / "a.pptx", 1,
                               tmp_path / "b.pptx", 1)


def test_the_summary_reports_every_figure_beside_its_identity_baseline() -> None:
    """Their reference scores 0-37 on colour against *itself*, varying by slide.

    A colour figure without that baseline reads as catastrophic failure when it is the
    ceiling, so the summary carries both or neither.
    """
    from ppt_harness.bench import slidesbench

    scored = [slidesbench.Score(example="food/slide_1", ok=True,
                                scores={"match": 0.8, "color": 0.0},
                                baseline={"match": 1.0, "color": 0.25})]
    payload = slidesbench._summarise(scored, "food", "instruction.txt", Path("repo"))

    assert payload["ours"] == {"match": 80.0, "color": 0.0}
    assert payload["identity_baseline"] == {"match": 100.0, "color": 25.0}
    assert "identity_baseline" in payload["note"]


# ------------------------------------------------------------------- renderer fallback


def test_a_broken_powerpoint_falls_back_to_libreoffice(tmp_path: Path, monkeypatch) -> None:
    """Engine choice tested whether PowerPoint was *installed*, never whether it worked.

    A Mac whose PowerPoint has stopped answering — a modal it is holding, a licence prompt,
    an instance driven for hours — failed every render while a working LibreOffice sat
    beside it unused.
    """
    from ppt_harness.fidelity import reference

    def refuse(source, out_pdf):
        raise reference.NoReferenceRenderer("error -9074")

    def succeed(source, out_pdf):
        Path(out_pdf).write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(reference, "_powerpoint_available", lambda: True)
    monkeypatch.setattr(reference, "_powerpoint_to_pdf", refuse)
    monkeypatch.setattr(reference, "_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(reference, "_libreoffice_to_pdf", succeed)

    used = reference.to_pdf(tmp_path / "in.pptx", tmp_path / "out.pdf")
    assert used == "libreoffice", "the engine actually used has to be what is reported"


def test_naming_an_engine_means_that_engine_or_nothing(tmp_path: Path, monkeypatch) -> None:
    """Asking for PowerPoint explicitly wants PowerPoint's answer.

    The whole point of naming it is that it is the engine the recipient will use, so a
    silent substitution would make a fidelity figure quietly incomparable.
    """
    from ppt_harness.fidelity import reference

    def refuse(source, out_pdf):
        raise reference.NoReferenceRenderer("error -9074")

    monkeypatch.setattr(reference, "_powerpoint_available", lambda: True)
    monkeypatch.setattr(reference, "_powerpoint_to_pdf", refuse)
    monkeypatch.setattr(reference, "_soffice", lambda: "/usr/bin/soffice")

    with pytest.raises(reference.NoReferenceRenderer):
        reference.to_pdf(tmp_path / "in.pptx", tmp_path / "out.pdf", engine="powerpoint")


# ----------------------------------------------------------------------------- charts


def test_a_bar_is_rounded_at_the_data_end_only() -> None:
    """Both ends rounded floats the bar off its axis — that reads as a range, not a
    magnitude measured from zero."""
    from ppt_harness.bench import plot

    path = plot._bar(10, 20, 100, 14, "#2a78d6")
    assert path.startswith('<path d="M10.0,20.0 H106.0'), path
    assert "Q" in path, "the data end should curve"
    assert plot._bar(10, 20, 0.2, 14, "#2a78d6") == "", "a zero-length bar draws nothing"


def test_every_bar_carries_its_value_and_a_legend() -> None:
    """Colour never carries identity alone: greyscale, CVD and print all have to work."""
    from ppt_harness.bench import plot

    svg = plot.grouped_bars(
        "T", "sub", ["match", "color"],
        [plot.Series("high-level brief", {"match": 79.2, "color": 1.2}),
         plot.Series("detailed brief", {"match": 78.7, "color": 0.0})],
        ceiling={"match": 100.0, "color": 37.5})

    assert "79.2" in svg and "78.7" in svg, "values are direct-labelled"
    assert "high-level brief" in svg and "detailed brief" in svg, "the legend names each"
    assert 'role="img"' in svg and "aria-label" in svg


def test_the_ceiling_is_drawn_wherever_it_is_given() -> None:
    """The reason these charts exist. `color` tops out at 37.5 on their own metric, so a
    bar on a bare 0-100 axis would report a result at the ceiling as a catastrophe."""
    from ppt_harness.bench import plot

    with_ceiling = plot.grouped_bars("T", "s", ["color"],
                                     [plot.Series("ours", {"color": 1.2})],
                                     ceiling={"color": 37.5})
    without = plot.grouped_bars("T", "s", ["color"],
                                [plot.Series("ours", {"color": 1.2})])

    assert "achievable ceiling" in with_ceiling
    assert with_ceiling.count("stroke-dasharray") > without.count("stroke-dasharray")


def test_publish_writes_charts_json_and_a_report(tmp_path: Path) -> None:
    from ppt_harness.bench import plot

    runs = {
        "detailed brief": {
            "benchmark": "SlidesBench (AutoPresent)", "domain": "food",
            "variant": "instruction.txt", "examples": 2, "scored": 2,
            "ours": {"match": 78.7, "text": 69.4, "position": 68.4, "color": 0.0},
            "identity_baseline": {"match": 100.0, "text": 100.0, "position": 98.8,
                                  "color": 37.5},
            "results": [{"example": "food/slide_1", "ok": True,
                         "scores": {"match": 0.66, "text": 1.0, "position": 0.78,
                                    "color": 0.0}},
                        {"example": "food/slide_2", "ok": False, "error": "crashed",
                         "scores": {}}],
        },
        "corpus": {"corpus": "/tmp/decks", "decks": 3, "opened": 3, "preserved": 3,
                   "open_rate": 1.0, "preservation_rate": 1.0, "slides": 15,
                   "opaque_shapes": 4},
    }
    report = plot.publish(runs, tmp_path)

    body = report.read_text()
    assert "SlidesBench" in body and "Round trip" in body
    assert (tmp_path / "charts" / "slidesbench-dimensions.svg").exists()
    assert (tmp_path / "charts" / "slidesbench-spread.svg").exists()
    assert (tmp_path / "results" / "slidesbench-food-detailed brief.json").exists()
    # The caveats travel with the numbers or the numbers get quoted without them.
    assert "Nothing here is a comparison to another system" in body
    assert "diagnostic" in body


def test_a_benchmark_nobody_ran_is_omitted_not_drawn_empty(tmp_path: Path) -> None:
    """A placeholder chart is how an empty result becomes a claim."""
    from ppt_harness.bench import plot

    report = plot.publish({"corpus": {"corpus": "/tmp/d", "decks": 1, "opened": 1,
                                      "preserved": 1, "open_rate": 1.0,
                                      "preservation_rate": 1.0, "slides": 2,
                                      "opaque_shapes": 0}}, tmp_path)

    assert "SlidesBench" not in report.read_text()
    assert not (tmp_path / "charts" / "slidesbench-dimensions.svg").exists()
