"""Font resolution, measurement, and line breaking — DESIGN §3.1.

These tests defend the claim the whole budget system rests on: that capacity is decided by
real advance width, not by counting characters. The CJK ratio test is the one that would
catch a regression to character counting, because that is exactly where the two disagree.
"""

from __future__ import annotations

import pytest

from ppt_harness.render import fonts, measure

LATIN = "The quick brown fox jumps over the lazy dog"
CJK = "敏捷的棕色狐狸跳过了那只懒狗，然后继续向前奔跑。"
STACK = "Helvetica, Arial, sans-serif"


# --------------------------------------------------------------------- resolution


def test_the_index_prefers_regular_over_bold() -> None:
    """`Arial Bold.ttf` reports the typographic family "Arial"; picking it would overstate
    every width by a few percent, silently."""
    path = fonts.find("Arial")
    assert path is not None
    assert "bold" not in path.name.lower()
    assert "italic" not in path.name.lower()


def test_a_stack_resolves_per_script_not_per_string() -> None:
    latin = fonts.resolve(STACK, "latin")
    han = fonts.resolve(STACK, "han")
    assert latin != han, "Han must not resolve to a Latin-only face"
    assert 0x4E00 in fonts.load(han).getBestCmap()


def test_named_but_uncovering_families_are_skipped() -> None:
    """A family can be installed and still have no glyphs for the script in hand."""
    stack = "Arial, sans-serif"
    assert 0x4E00 in fonts.load(fonts.resolve(stack, "han")).getBestCmap()


def test_generics_are_not_treated_as_families() -> None:
    assert fonts.parse_stack("'Aptos', 等线, sans-serif") == ["Aptos", "等线"]


def test_script_runs_split_mixed_text() -> None:
    assert [s for s, _ in fonts.runs("Hello 世界 test")] == ["latin", "han", "latin"]


def test_punctuation_does_not_fragment_latin_prose() -> None:
    assert len(fonts.runs("Hello, world. It's fine!")) == 1


# -------------------------------------------------------------------- measurement


def test_width_scales_linearly_with_size() -> None:
    a = measure.measure(LATIN, STACK, 20).width
    b = measure.measure(LATIN, STACK, 40).width
    assert b == pytest.approx(a * 2, rel=1e-6)


def test_width_carries_the_unit_of_the_size_given() -> None:
    """`width` is em times the size passed in. Handing in canvas px and comparing against
    points measures every box a quarter too narrow — phantom line breaks, no error."""
    m = measure.measure(LATIN, STACK, 21)
    assert m.width == pytest.approx(m.width_em * 21, rel=1e-9)


def test_em_width_is_independent_of_point_size() -> None:
    """Budgets are stored in em so a change to the type scale cannot invalidate them."""
    a = measure.measure(LATIN, STACK, 12).width_em
    b = measure.measure(LATIN, STACK, 96).width_em
    assert a == pytest.approx(b, rel=1e-9)


def test_cjk_runs_about_twice_the_advance_of_latin() -> None:
    """The claim in DESIGN §3.1, asserted rather than assumed.

    This is what makes character counting wrong: the same "90 characters" is roughly twice
    the width in Chinese as in English.
    """
    latin_per_char = measure.measure(LATIN, STACK, 21).width_em / len(LATIN)
    cjk_per_char = measure.measure(CJK, STACK, 21).width_em / len(CJK)
    assert 1.8 <= cjk_per_char / latin_per_char <= 2.6


def test_empty_text_measures_zero() -> None:
    assert measure.measure("", STACK, 21).width_em == 0.0


def test_tracking_widens_the_measurement() -> None:
    plain = measure.measure(LATIN, STACK, 21, tracking=0.0).width_em
    tracked = measure.measure(LATIN, STACK, 21, tracking=0.05).width_em
    assert tracked == pytest.approx(plain + 0.05 * len(LATIN), rel=1e-6)


# ------------------------------------------------------------------------ wrapping


def test_wrapping_respects_the_measured_width() -> None:
    lines = measure.wrap(LATIN, STACK, 21, 200)
    assert len(lines) > 1
    for line in lines:
        assert measure.measure(line, STACK, 21).width <= 200 * 1.05


def test_narrower_boxes_produce_more_lines() -> None:
    wide = measure.wrap(LATIN, STACK, 21, 400)
    narrow = measure.wrap(LATIN, STACK, 21, 120)
    assert len(narrow) > len(wide)


def test_cjk_breaks_between_characters_not_at_spaces() -> None:
    lines = measure.wrap(CJK, STACK, 21, 200)
    assert len(lines) > 1, "a spaceless CJK string must still wrap"


def test_kinsoku_keeps_closing_punctuation_off_a_line_start() -> None:
    """A line may not begin with a comma or full stop. Getting this wrong changes the line
    count, which changes whether a slot overflows."""
    for width in (60, 90, 120, 150, 200, 260):
        for line in measure.wrap(CJK, STACK, 21, width):
            assert not line.startswith(("，", "。", "、", "）")), f"at width {width}: {line!r}"


def test_explicit_newlines_are_preserved() -> None:
    assert len(measure.wrap("one\ntwo\nthree", STACK, 21, 500)) == 3


def test_a_zero_width_box_does_not_loop_forever() -> None:
    assert measure.wrap(LATIN, STACK, 21, 0) == [LATIN]
