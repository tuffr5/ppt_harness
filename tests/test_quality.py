"""The judged half of the bench — `bench/quality.py`, `bench/vision.py`, `bench/rubrics.py`.

Same rule as `test_bench.py`: **never a real API call**. Every model here is a scripted fake,
which keeps the suite deterministic and free — and matters more than usual, because the thing
under test is a metric whose failure mode is publishing a number rather than crashing.

Four properties are worth more than the rest, and each has a test named after it:

- the scorer never sees the image (the split *is* the method);
- an unchanged slide is described once, ever (the description is the expensive half);
- a reply nobody can parse yields no score, not a middling one;
- with no vision model configured, nothing is scored and the error says what to set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ppt_harness.bench import quality, rubrics, vision
from ppt_harness.core.session import Session
from ppt_harness.render.preview import Page, PreviewUnavailable
from ppt_harness.tools import router

PNG = b"\x89PNG\r\n\x1a\n-pretend-this-is-a-slide"


# ------------------------------------------------------------------ a scripted vision model


@dataclass
class _Msg:
    content: str | None = None


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Response:
    choices: list[_Choice]
    usage: Any = None


class FakeOpenAI:
    """An OpenAI-compatible endpoint that answers from a script and remembers the payload.

    The payload is the point: assertions here are about what went *on the wire*, not about
    what the code meant to send.
    """

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.sent: list[dict[str, Any]] = []
        self.chat = self
        self.completions = self

    def create(self, **kw: Any) -> _Response:
        self.sent.append(kw)
        reply = self.replies.pop(0) if self.replies else '{"score": 4, "reason": "fine"}'
        return _Response(choices=[_Choice(message=_Msg(content=reply))])

    @property
    def images_sent(self) -> list[int]:
        """How many image parts each request carried, in order."""
        return [sum(1 for part in call["messages"][-1]["content"]
                    if part.get("type") == "image_url")
                for call in self.sent]


class FakeAnthropic:
    """Claude's shape — content blocks in, content blocks out."""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.sent: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kw: Any) -> Any:
        self.sent.append(kw)
        reply = self.replies.pop(0) if self.replies else "a description"

        @dataclass
        class _Block:
            type: str
            text: str

        @dataclass
        class _Message:
            content: list[_Block]
            usage: Any = None

        return _Message(content=[_Block(type="text", text=reply)])


def _judge(replies: list[str]) -> tuple[vision.Judge, FakeOpenAI]:
    client = FakeOpenAI(replies)
    endpoint = vision.Endpoint(model="fake-vision", wire=vision.OPENAI, client=client)
    return vision.Judge(endpoint), client


class FakePreview:
    """`PreviewCache`'s one method, without LibreOffice.

    The seam `measure_quality` takes on purpose: the metric has to be testable on a machine
    with no renderer without ever *pretending* to have rendered something.
    """

    def __init__(self, png: bytes = PNG) -> None:
        self.png = png
        self.calls: list[str] = []

    def page(self, slide_id: str, width: int = 1280) -> Page:
        self.calls.append(slide_id)
        return Page(slide_id=slide_id, index=len(self.calls) - 1, png=self.png, width=width)


class NoRenderer:
    def page(self, slide_id: str, width: int = 1280) -> Page:
        raise PreviewUnavailable("no reference renderer: install LibreOffice")


# ------------------------------------------------------------------------- the split


def test_the_scorer_is_never_sent_the_image(populated: Session, tmp_path: Path) -> None:
    """The whole method in one assertion.

    PPTEval's human correlation comes from describing and scoring in two separate calls. If
    the image reached the scorer they would be one call wearing two hats, and nothing further
    down could tell — the number would still look like a number.
    """
    judge, client = _judge(["a plain white slide with black text",
                            '{"score": 3, "reason": "no visual elements"}'])

    result = quality.measure_quality(populated, judge, axes=("design",),
                                     cache=tmp_path / "cache", preview=FakePreview())

    describe, score = judge.exchanges
    assert describe.kind == "describe" and describe.carries_image
    assert score.kind == "score" and not score.carries_image
    assert client.images_sent == [1, 0], "one image on the describe call, none on the score"
    # And the description, not the slide, is what the scorer was given to read.
    scored_text = client.sent[1]["messages"][-1]["content"][0]["text"]
    assert "a plain white slide with black text" in scored_text
    assert result.slides[0].axes["design"].score == 3


def test_the_describe_prompt_asks_for_the_things_design_is_scored_on() -> None:
    """A rubric level that mentions icons and backgrounds is unscoreable if the describer was
    never asked whether there were any — the scorer cannot see the slide to check."""
    describe = rubrics.DESIGN.describe.lower()
    for topic in ("overlap", "contrast", "monochrome", "background", "texture", "pattern",
                  "geometric shapes", "icons"):
        assert topic in describe, f"the design describe prompt never mentions {topic}"
    assert "score" not in describe and "rate" not in describe

    prompt = rubrics.DESIGN.score_prompt("a description")
    # The levels legitimately name images as a *category of visual element*; what the prompt
    # must never do is imply the scorer can see one. Whether an image is attached is enforced
    # on the wire, in `Judge.score`.
    assert "look at" not in prompt.lower() and "this slide shown" not in prompt.lower()
    assert "Judge only what the description states." in prompt
    assert "3." in prompt and "supplementary visual elements" in prompt


def test_coherence_is_absent_on_purpose_and_says_why() -> None:
    """Leaving an axis out is a decision, and a decision has to be readable in six months."""
    assert "coherence" not in rubrics.RUBRICS
    why = rubrics.WHY_NO_COHERENCE
    assert "0.55" in why, "the paper's own correlation is the first objection"
    assert "acknowledgements" in why, "and its top levels describe a conference talk"


# ------------------------------------------------------------------------- the cache


def test_a_slide_is_described_once_and_scored_again(populated: Session,
                                                    tmp_path: Path) -> None:
    """The description is the expensive half and is a pure function of image, prompt, model.

    So a second run re-scores off disk. This is what makes editing a rubric cheap and what
    makes a before/after comparison affordable at all.
    """
    cache = tmp_path / "cache"
    first, _ = _judge(["a plain slide", '{"score": 2, "reason": "monochrome"}'])
    quality.measure_quality(populated, first, axes=("design",), cache=cache,
                            preview=FakePreview())
    assert first.described == 1

    second, _ = _judge(['{"score": 2, "reason": "monochrome"}'])
    result = quality.measure_quality(populated, second, axes=("design",), cache=cache,
                                     preview=FakePreview())

    assert second.described == 0, "the description came off disk"
    assert [e.kind for e in second.exchanges] == ["score"]
    assert result.slides[0].axes["design"].cached
    assert result.slides[0].axes["design"].score == 2


def test_a_different_image_is_a_different_question(populated: Session,
                                                   tmp_path: Path) -> None:
    """Keyed on the image bytes, not the slide id — so an edited slide invalidates itself and
    nothing has to remember to say so."""
    cache = tmp_path / "cache"
    judge, _ = _judge(["first look", '{"score": 2}', "second look", '{"score": 4}'])
    quality.measure_quality(populated, judge, axes=("design",), cache=cache,
                            preview=FakePreview(PNG))
    quality.measure_quality(populated, judge, axes=("design",), cache=cache,
                            preview=FakePreview(PNG + b"edited"))

    assert judge.described == 2


def test_a_truncated_cache_entry_is_replaced_not_fatal(populated: Session,
                                                       tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    judge, _ = _judge(["a plain slide", '{"score": 3}'])
    path = quality._cache_path(cache, PNG, rubrics.DESIGN, "fake-vision")
    path.write_text("{ this is not json", encoding="utf-8")

    result = quality.measure_quality(populated, judge, axes=("design",), cache=cache,
                                     preview=FakePreview())

    assert judge.described == 1
    assert result.slides[0].axes["design"].score == 3
    assert json.loads(path.read_text())["description"] == "a plain slide"


# --------------------------------------------------------------------- reading a reply


@pytest.mark.parametrize(("reply", "expected"), [
    ('{"score": 4, "reason": "harmonious"}', 4),
    ('```json\n{"score": 5, "reason": "x"}\n```', 5),
    ('Here is my answer: {"score": 1, "reason": "text overlaps"} — hope that helps.', 1),
    ('{"score": "3", "reason": "plain"}', 3),
    ("Score: 4", 4),
    ("**Score** = 2 because it is monochrome.", 2),
])
def test_a_score_is_read_out_of_whatever_shape_the_model_replied_in(reply: str,
                                                                    expected: int) -> None:
    score, _, error = quality.parse_score(reply)
    assert score == expected and not error


@pytest.mark.parametrize("reply", [
    "This slide is quite nice, honestly.",
    "",
    '{"score": 9, "reason": "amazing"}',
    '{"score": null}',
    '{"reason": "I could not decide"}',
    "There are 3 bullet points and 2 images.",
])
def test_an_unreadable_reply_yields_no_score_rather_than_a_middling_one(reply: str) -> None:
    """The rule the whole file is arranged around: a missing measurement must not be able to
    masquerade as a measurement. A silent 3 here would sit in a mean forever."""
    score, _, error = quality.parse_score(reply)
    assert score is None
    assert error, "and it has to say what it could not read"


def test_a_malformed_reply_leaves_the_slide_unmeasured_and_named(populated: Session,
                                                                 tmp_path: Path) -> None:
    judge, _ = _judge(["a plain slide", "I would rather not put a number on it."])
    result = quality.measure_quality(populated, judge, axes=("design",),
                                     cache=tmp_path / "c", preview=FakePreview())

    axis = result.slides[0].axes["design"]
    assert axis.score is None and not axis.measured
    assert result.mean("design") is None, "never 0.0 — nobody scored it"
    assert result.as_dict()["means"]["design"] is None
    assert result.as_dict()["scored_slides"]["design"] == [0, 1]
    assert result.unmeasured and result.slides[0].slide_id in result.unmeasured[0]


def test_a_mean_is_over_the_slides_that_were_scored_and_says_how_many(tmp_path: Path) -> None:
    """`report.py`'s first rule, applied to a judged figure: every number carries its
    denominator, and a slide that failed to score is counted rather than dropped."""
    deck = quality.DeckQuality(slides=[
        quality.SlideQuality(slide_id="sl_1", index=0,
                             axes={"design": quality.AxisScore("design", score=4)}),
        quality.SlideQuality(slide_id="sl_2", index=1,
                             axes={"design": quality.AxisScore("design", score=2)}),
        quality.SlideQuality(slide_id="sl_3", index=2,
                             axes={"design": quality.AxisScore("design", error="timed out")}),
    ])
    payload = deck.as_dict()

    assert deck.mean("design") == 3.0, "the mean is over the two that scored"
    assert payload["scored_slides"]["design"] == [2, 3]
    assert payload["human_correlation"]["design"] == 0.90
    assert payload["coherence"]["scored"] is False


# ------------------------------------------------------------- nothing to measure with


def test_with_no_vision_model_configured_nothing_is_scored(monkeypatch) -> None:
    """The state this repository is actually in: a text-only key and no vision model.

    The requirement is that this is *loud*. A benchmark that quietly returns zeros here would
    make "we never measured it" indistinguishable from "the design is terrible".
    """
    for name in (vision.MODEL_VAR, vision.BASE_URL_VAR, vision.SCORE_MODEL_VAR):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(vision.VisionUnavailable) as caught:
        vision.build()

    message = str(caught.value)
    assert vision.MODEL_VAR in message, "the error names the variable to set"
    assert "ANTHROPIC_API_KEY" in message and "OPENAI_API_KEY" in message
    assert "no default and no zero is substituted" in message


def test_a_text_only_endpoint_is_refused_before_it_is_asked(monkeypatch) -> None:
    """DeepSeek's hosted API is the one key this repo ships, and it cannot see an image.

    Refused by name rather than by an opaque 400 from a host that has never supported it —
    and the message points at the one place a DeepSeek model *is* legitimate here, which is
    the scorer, because the scorer reads prose.
    """
    monkeypatch.setenv(vision.MODEL_VAR, "deepseek-v4-flash")
    monkeypatch.delenv(vision.BASE_URL_VAR, raising=False)

    with pytest.raises(vision.VisionUnavailable, match="text-only"):
        vision.build()

    # The same name behind a base URL is a local server that may well be a VLM, and the base
    # URL is the only reason anyone sets one — same precedence rule as `providers.build`.
    monkeypatch.setenv(vision.BASE_URL_VAR, "http://127.0.0.1:8000/v1")
    assert vision.build().describer.wire == vision.OPENAI


def test_a_missing_key_names_the_key(monkeypatch) -> None:
    monkeypatch.setenv(vision.MODEL_VAR, "claude-opus-4-8")
    monkeypatch.delenv(vision.BASE_URL_VAR, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(vision.VisionUnavailable, match="ANTHROPIC_API_KEY"):
        vision.build()


def test_the_scorer_may_be_a_cheaper_text_model(monkeypatch) -> None:
    """It reads a description, never an image, so it does not have to be the vision model —
    and separating them is what lets a text-only key do half the job."""
    monkeypatch.setenv(vision.MODEL_VAR, "gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv(vision.SCORE_MODEL_VAR, "deepseek-v4-flash")
    monkeypatch.delenv(vision.BASE_URL_VAR, raising=False)
    monkeypatch.delenv(vision.SCORE_BASE_URL_VAR, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    judge = vision.build()
    assert judge.describer.model == "gpt-4o"
    assert judge.scorer.model == "deepseek-v4-flash", "text-only is fine for the scorer"


def test_no_renderer_stops_the_run_instead_of_scoring_twelve_zeroes(populated: Session,
                                                                    tmp_path: Path) -> None:
    """A missing renderer is missing for every slide. Twelve identical per-slide failures
    dressed as results would render as a deck that scored badly."""
    judge, _ = _judge([])

    with pytest.raises(PreviewUnavailable, match="LibreOffice"):
        quality.measure_quality(populated, judge, axes=("design",), cache=tmp_path / "c",
                                preview=NoRenderer())
    assert judge.exchanges == [], "and no model was billed for a slide nobody rendered"


def test_an_endpoint_that_falls_over_costs_a_slide_not_the_run(populated: Session,
                                                               tmp_path: Path) -> None:
    class Broken(FakeOpenAI):
        def create(self, **kw: Any) -> _Response:
            raise RuntimeError("connection reset")

    endpoint = vision.Endpoint(model="fake", wire=vision.OPENAI, client=Broken())
    result = quality.measure_quality(populated, vision.Judge(endpoint), axes=("design",),
                                     cache=tmp_path / "c", preview=FakePreview())

    assert result.slides[0].axes["design"].score is None
    assert "connection reset" in result.slides[0].axes["design"].error
    assert result.unmeasured


# ------------------------------------------------------------------- the wire formats


def test_the_anthropic_wire_sends_a_base64_source_block() -> None:
    """Two formats, one neutral part list. A judge built against the wrong one sends an image
    the endpoint reads as text and describes nothing."""
    client = FakeAnthropic(["a description"])
    judge = vision.Judge(vision.Endpoint(model="claude-opus-4-8", wire=vision.ANTHROPIC,
                                         client=client))
    judge.describe(PNG, "describe it", "you are a describer")

    block = client.sent[0]["messages"][0]["content"][0]
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"
    assert block["source"]["type"] == "base64" and block["source"]["data"]
    assert client.sent[0]["system"] == "you are a describer"


def test_the_openai_wire_sends_a_data_url() -> None:
    judge, client = _judge(["a description"])
    judge.describe(PNG, "describe it", "you are a describer")

    part = client.sent[0]["messages"][-1]["content"][0]
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_reasoners_scratchpad_never_reaches_the_score_parser() -> None:
    """`<think>I could give this a 5...</think>{"score": 2}` must score 2, not 5."""
    judge, _ = _judge(['<think>maybe a 5? no</think>{"score": 2, "reason": "plain"}'])
    score, _, error = quality.parse_score(judge.score("prompt", "system"))

    assert score == 2 and not error


# ------------------------------------------------------------------ the free half


def _exported(session: Session, path: Path) -> Path:
    result = router.dispatch(session, "export", {"path": str(path)})
    assert result["ok"], result
    return path


def test_the_programmatic_checks_read_the_exported_file(populated: Session,
                                                        tmp_path: Path) -> None:
    """Deterministic, no model — and run against the `.pptx` a recipient would open rather
    than the session that wrote it, which could only confirm the harness agrees with itself."""
    deck = _exported(populated, tmp_path / "deck.pptx")
    checks = quality.verify(deck, expected_pages=(1, 3), expected_script="latin")

    assert checks.pages == 1
    assert checks.aspect == pytest.approx(16 / 9, abs=0.001)
    assert checks.pages_ok and checks.aspect_ok
    assert checks.script == "latin" and checks.script_ok
    assert checks.problems == []
    assert checks.passed is True and checks.asserted == 3


def test_a_check_nobody_asked_for_did_not_pass(populated: Session, tmp_path: Path) -> None:
    """Tri-state on purpose: `None` is "not asserted", and a caller who gave no expected page
    count must not be told the page count was fine."""
    deck = _exported(populated, tmp_path / "deck.pptx")
    checks = quality.verify(deck, expected_aspect=None)

    assert checks.pages_ok is None and checks.aspect_ok is None and checks.script_ok is None
    assert checks.passed is None, "nothing was asserted, so nothing passed"
    assert checks.pages == 1, "measured all the same, and reported"


def test_the_wrong_shape_of_deck_is_caught_without_a_model(populated: Session,
                                                           tmp_path: Path) -> None:
    """The embarrassments a judge scores 4/5 without noticing: a one-slide deck that should
    have been five, and a 4:3 canvas nobody asked for."""
    deck = _exported(populated, tmp_path / "deck.pptx")
    checks = quality.verify(deck, expected_pages=(5, 12), expected_aspect=4 / 3)

    assert checks.pages_ok is False and checks.aspect_ok is False
    assert checks.passed is False
    assert any("expected 5-12" in p for p in checks.problems)
    assert any("aspect ratio" in p for p in checks.problems)


def test_the_output_script_is_detected_and_a_short_deck_says_undetermined() -> None:
    assert quality.script_of("The quick brown fox jumped over the lazy dog again") == "latin"
    han = "我们的季度业绩显示欧洲市场的流失率翻了一倍并且需要立刻处理"
    assert quality.script_of(han * 2) == "han"
    # Numbers and acronyms are not a writing system, and guessing one would be a fabricated
    # measurement in a function that exists to catch fabrications.
    assert quality.script_of("Q3 2025 — 41% / 12%") is None


def test_the_deck_level_review_rules_stand_in_for_coherence(populated: Session) -> None:
    """Not a judged score: `weak_close` is decided by counting and cannot be wrong about
    itself, which is the trade `WHY_NO_COHERENCE` makes explicit."""
    found = quality.structure(populated)
    assert isinstance(found, dict)
    assert set(found) <= {"weak_close", "duplicate_title", "title_case_drift",
                          "bullet_stop_drift"}


# ------------------------------------------------------------------------------ the CLI


def test_the_cli_reports_the_free_half_and_refuses_to_invent_the_rest(populated: Session,
                                                                      tmp_path: Path,
                                                                      monkeypatch) -> None:
    """What a user on this machine actually gets today: the deterministic checks, and a
    refusal that names the variable to set."""
    from click.testing import CliRunner

    from ppt_harness.adapters.cli import cli

    for name in (vision.MODEL_VAR, vision.BASE_URL_VAR, vision.SCORE_MODEL_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("ppt_harness.core.config.load_env", lambda *a, **k: [])
    deck = _exported(populated, tmp_path / "deck.pptx")

    result = CliRunner().invoke(cli, ["bench", "score", str(deck), "--expect-slides", "1-3"])

    assert result.exit_code != 0, "an unmeasured axis is not a successful run"
    assert "verification" in result.output and "expected 1-3" in result.output
    assert vision.MODEL_VAR in result.output
    assert "NOT measured" in result.output


def test_the_cli_verify_only_needs_nothing_at_all(populated: Session, tmp_path: Path) -> None:
    from click.testing import CliRunner

    from ppt_harness.adapters.cli import cli

    deck = _exported(populated, tmp_path / "deck.pptx")
    result = CliRunner().invoke(cli, ["bench", "score", str(deck), "--verify-only",
                                      "--expect-aspect", "16:9"])

    assert result.exit_code == 0, result.output
    assert "1 check(s) passed" in result.output


def test_the_cli_prints_a_score_per_slide_and_a_mean(populated: Session, tmp_path: Path,
                                                     monkeypatch) -> None:
    from click.testing import CliRunner

    from ppt_harness.adapters.cli import cli

    judge, _ = _judge(["a plain white slide", '{"score": 3, "reason": "plain"}'])
    monkeypatch.setattr(vision, "build", lambda *a, **k: judge)
    monkeypatch.setattr(quality, "PreviewCache", lambda session: FakePreview())
    monkeypatch.setenv("PPT_HARNESS_CACHE", str(tmp_path / "cache"))
    deck = _exported(populated, tmp_path / "deck.pptx")

    result = CliRunner().invoke(cli, ["bench", "score", str(deck), "--axis", "design",
                                      "--out", str(tmp_path / "quality.json")])

    assert result.exit_code == 0, result.output
    assert "mean design" in result.output and "3.0" in result.output
    assert "over 1/1 slide(s)" in result.output
    payload = json.loads((tmp_path / "quality.json").read_text())
    assert payload["means"]["design"] == 3.0
    assert payload["coherence"]["scored"] is False
