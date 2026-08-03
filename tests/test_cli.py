"""CLI — the inspection surface.

Its job is to make the measurement layer debuggable without a model in the loop. These
tests check that each command actually reaches the machinery rather than printing a
plausible-looking summary of nothing.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ppt_harness.adapters.cli import cli
from ppt_harness.core.session import Session


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.output


def test_tools_lists_every_tool_with_its_gate() -> None:
    code, out = _run("tools")
    assert code == 0
    assert "managed" in out and "shared" in out
    assert "set_text" in out and "add_slide" in out


def test_outline_reports_every_slide(fixture_deck: Path) -> None:
    code, out = _run("outline", str(fixture_deck))
    assert code == 0
    assert "freeform" in out
    assert out.count("freeform") >= 1


def test_theme_reports_what_was_inferred(fixture_deck: Path) -> None:
    code, out = _run("theme", str(fixture_deck))
    assert code == 0
    assert "inferred" in out
    assert "type.scale" in out
    assert "validate" in out
    assert "resolves to" in out, "the font stack must show what it actually resolves to"


def test_lint_runs_over_the_whole_deck(fixture_deck: Path) -> None:
    code, out = _run("lint", str(fixture_deck))
    assert code == 0
    assert "clean" in out or "overflow" in out


def test_review_reports_editorial_findings(fixture_deck: Path) -> None:
    """`lint`'s sibling on the axis nothing measures. It must exit 0 either way — a finding
    is an opinion, and an opinion that fails a build is a style guide holding a gun."""
    code, out = _run("review", str(fixture_deck))
    assert code == 0
    assert "nothing to say" in out or "→" in out


def test_fits_reports_capacity_without_writing(fixture_deck: Path) -> None:
    session = Session.open(fixture_deck)
    slide = session.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    code, out = _run("fits", str(fixture_deck), f"{slide.id}/{shape.id}", "A short line")
    assert code == 0
    assert "capacity" in out
    assert "hint" in out


def test_fits_says_no_when_it_does_not(fixture_deck: Path) -> None:
    session = Session.open(fixture_deck)
    slide = session.deck.slides[0]
    shape = next(s for s in slide.shapes if s.text and not s.opaque)
    code, out = _run("fits", str(fixture_deck), f"{slide.id}/{shape.id}", "word " * 500)
    assert code == 0
    assert "does not fit" in out


def test_call_reaches_the_same_router_the_model_uses(fixture_deck: Path) -> None:
    code, out = _run("call", str(fixture_deck), "get_outline")
    assert code == 0
    assert '"ok": true' in out.lower()


def test_export_round_trips_and_reports_fidelity(fixture_deck: Path, tmp_path: Path) -> None:
    out_path = tmp_path / "cli.pptx"
    code, out = _run("export", str(fixture_deck), str(out_path))
    assert code == 0
    assert out_path.exists()
    assert "fidelity clean" in out


# ------------------------------------------------------------------------- new


def test_new_creates_a_deck_that_opens(tmp_path: Path) -> None:
    out_path = tmp_path / "fresh.pptx"
    code, _ = _run("new", str(out_path), "--title", "Q3 review")
    assert code == 0
    assert out_path.exists()

    session = Session.open(out_path)
    assert len(session.deck.slides) == 1
    assert "Q3 review" in "\n".join(s.text or "" for s in session.deck.slides[0].shapes)


def test_new_says_where_slides_come_from(tmp_path: Path) -> None:
    """The command creates a deck; it does not write content. Saying so is the point —
    every other CLI command is offline, and this one being offline too is why there is
    no `--prompt` flag on it."""
    code, out = _run("new", str(tmp_path / "quiet.pptx"))
    assert code == 0
    assert "serve" in out


def test_new_refuses_to_clobber(tmp_path: Path) -> None:
    out_path = tmp_path / "twice.pptx"
    assert _run("new", str(out_path))[0] == 0
    code, out = _run("new", str(out_path))
    assert code != 0
    assert "--force" in out
    assert _run("new", str(out_path), "--force")[0] == 0


def test_empty_means_empty(tmp_path: Path) -> None:
    out_path = tmp_path / "bare.pptx"
    assert _run("new", str(out_path), "--empty")[0] == 0
    assert Session.open(out_path).deck.slides == []


def test_new_honours_a_canvas_size(tmp_path: Path) -> None:
    out_path = tmp_path / "fourthree.pptx"
    assert _run("new", str(out_path), "--size", "4:3")[0] == 0
    assert Session.open(out_path).deck.theme.grid.canvas == (960, 720)


def test_borrowing_a_theme_survives_the_round_trip(fixture_deck: Path,
                                                   tmp_path: Path) -> None:
    """The whole point of `--from`. Before the exporter wrote `ppt/theme/theme1.xml`, the
    first generation was themed and every *later* edit reverted to python-pptx's default
    palette — one deck, two colour schemes."""
    from ppt_harness.io.theme_extract import extract_theme

    out_path = tmp_path / "borrowed.pptx"
    assert _run("new", str(out_path), "--from", str(fixture_deck))[0] == 0

    source = extract_theme(fixture_deck)
    landed = Session.open(out_path).deck.theme
    for role in ("bg", "ink", "surface", "brand", "accents"):
        assert landed.palette[role] == source.palette[role], role
    assert landed.type.families["display"] == source.type.families["display"]


def test_serve_refuses_a_deck_and_a_template_together(fixture_deck: Path) -> None:
    """Two ways to say where the theme comes from, and they disagree.

    Opening a deck brings its own theme; `--from` says to take someone else's. Silently
    preferring one would produce a session themed from a file the user did not expect, which
    is the kind of thing nobody notices until the deck is in front of a customer.
    """
    code, out = _run("serve", str(fixture_deck), "--from", str(fixture_deck))
    assert code != 0
    assert "not" in out and "--from" in out


def test_new_from_a_template_records_where_the_theme_came_from(fixture_deck: Path) -> None:
    """A borrowed look has to be attributable.

    `source_path` cannot carry it — nothing was imported, and claiming it was would tell the
    exporter to patch a package this deck never came from.
    """
    session = Session.from_template(fixture_deck, "Q3 board review")
    assert session.deck.slides == [], "a template lends a theme, never slides"
    assert session.deck.theme_from == fixture_deck.name
    assert session.deck.source_path is None
    assert session.outline()["template"] == fixture_deck.name

    borrowed = session.theme
    from ppt_harness.io.theme_extract import extract_theme

    assert borrowed.palette["brand"] == extract_theme(fixture_deck).palette["brand"]


def test_templates_lists_what_ships(tmp_path: Path) -> None:
    code, out = _run("templates")
    assert code == 0
    assert "slate" in out and "serve --template" in out


def test_templates_names_one_in_full() -> None:
    code, out = _run("templates", "slate")
    assert code == 0
    assert "brand" in out and "deck_title" in out


def test_new_can_start_on_a_built_in_theme(tmp_path: Path) -> None:
    """The point of shipping them: a deck with nothing to borrow from still has a look."""
    from ppt_harness.state import templates

    out_path = tmp_path / "built-in.pptx"
    code, out = _run("new", str(out_path), "--template", "signal")
    assert code == 0, out
    assert "signal" in out

    landed = Session.open(out_path).deck.theme
    assert landed.palette["brand"] == templates.load("signal").palette["brand"]


def test_an_unknown_template_is_refused_by_name(tmp_path: Path) -> None:
    code, out = _run("new", str(tmp_path / "x.pptx"), "--template", "chartreuse")
    assert code != 0
    assert "chartreuse" in out and "slate" in out, "the refusal should list what exists"
