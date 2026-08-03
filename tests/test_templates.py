"""Built-in templates — a theme to start from when there is nothing to borrow.

Every template here is a promise made to whoever starts a deck on it, and the promises are
checkable: it validates, it states its values rather than inferring them, and a deck built on
it renders. The suite is the only thing standing between a mistyped hex code and somebody's
customer deck, because nothing downstream re-checks a theme — it is validated once at load
precisely so managed slides cannot fail contrast later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ppt_harness.core.session import Session
from ppt_harness.state import templates
from ppt_harness.state.theme_default import validate_theme
from ppt_harness.tools import router

NAMES = [t.name for t in templates.catalog()]


def test_some_templates_ship() -> None:
    """The folder is the feature; an empty one is a silent regression in packaging."""
    assert NAMES, "no templates found beside the package"


@pytest.mark.parametrize("name", NAMES)
def test_every_template_is_sound(name: str) -> None:
    """Contrast, provenance, and a description, in one place.

    `check` is the strict view — `catalog` skips a bad file so one typo cannot stop the
    harness from opening a deck, and that leniency is exactly why the suite has to be strict
    here instead.
    """
    assert templates.check()[name] == []


@pytest.mark.parametrize("name", NAMES)
def test_a_template_states_its_values_rather_than_guessing(name: str) -> None:
    """The reason these are JSON and not `.pptx`.

    Extraction from a real deck reads the palette and the faces and infers about nine other
    fields — the type scale, the spacing ramp, the grid. A shipped template that arrived with
    `inferred` populated would be asking the user to correct our own guesses.
    """
    theme = templates.load(name)
    assert theme.inferred == []
    assert theme.source == "authored"
    assert validate_theme(theme) == []


@pytest.mark.parametrize("name", NAMES)
def test_a_deck_can_be_built_on_every_template(name: str) -> None:
    """The theme has to survive contact with the component catalog, not just the validator.

    A type scale that validates can still be too large for the boxes the expander derives, so
    this writes a real slide through the ordinary tool path and checks it measures clean.
    """
    session = Session.from_builtin(name, "Q3 board review")
    result = router.dispatch(session, "add_slide", {
        "layout": "stack",
        "blocks": [
            {"region": "header", "component": "slide_title",
             "slots": {"title": "Churn doubled in EMEA"}},
            {"region": "body", "component": "bullets", "slots": {"items": [
                "Mid-market renewals slipped two quarters running",
                "Expansion revenue covered the gap",
                "Two hires close the coverage hole"]}},
        ],
    })
    assert result["ok"], result.get("message")
    assert result["render"]["clean"], f"{name} cannot hold an ordinary slide"


@pytest.mark.parametrize("name", NAMES)
def test_font_stacks_name_a_fallback(name: str) -> None:
    """A single face is a theme that silently becomes another one on someone else's machine.

    The harness reports a substitution, but a *shipped* template should not need it to: it
    has no idea what is installed where it lands.
    """
    for family, stack in templates.load(name).type.families.items():
        assert "," in stack, f"{name}.{family} names one face: {stack!r}"


def test_an_unknown_template_says_what_there_is() -> None:
    with pytest.raises(templates.TemplateError) as caught:
        templates.load("no-such-template")
    assert "no-such-template" in str(caught.value)
    for name in NAMES:
        assert name in str(caught.value)


def test_a_malformed_template_is_skipped_not_fatal(tmp_path: Path) -> None:
    """One bad file must not stop the harness listing the good ones."""
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "empty.json").write_text('{"description": "x"}', encoding="utf-8")
    assert templates.catalog(tmp_path) == []
    assert set(templates.check(tmp_path)) == {"broken", "empty"}
    assert all(problems for problems in templates.check(tmp_path).values())


def test_a_built_in_start_records_where_the_theme_came_from() -> None:
    """Same contract as `--from`: a borrowed look is attributable, and no slides travel."""
    session = Session.from_builtin(NAMES[0], "Q3 board review")
    assert session.deck.slides == []
    assert session.deck.source_path is None
    assert NAMES[0] in (session.deck.theme_from or "")
    assert NAMES[0] in session.outline()["template"]


def test_the_wheel_would_carry_the_templates() -> None:
    """Data inside a package ships only if it is declared.

    Nothing in the test suite would notice otherwise — the source tree has the files either
    way — and the failure lands on an installed user as an empty catalog, which reads as "the
    feature does not exist" rather than as a packaging bug. The same declaration already
    exists for `skills/**/*.md`; this pins that they stay together.
    """
    import tomllib

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = config["tool"]["setuptools"]["package-data"]["ppt_harness"]
    assert any("templates/" in pattern and pattern.endswith(".json")
               for pattern in declared), declared
    assert any("bench/suites" in pattern for pattern in declared), \
        "the benchmark's task suites are package data too"
