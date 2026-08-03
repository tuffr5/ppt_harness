"""Editorial review — `core/review.py` and the `review_deck` tool.

Every other check in this harness measures something and is therefore either right or
broken. This one has opinions, so what the tests defend is different: not that a rule fires,
but that it **stays quiet when the text cannot support it**. A review channel dies of false
positives, not of misses — one wrong finding a turn and the whole thing gets skipped, good
findings included.

So most of what follows is a negative case. The rule firing is the easy half.
"""

from __future__ import annotations

import pytest

from ppt_harness.core import review
from ppt_harness.core.session import Session
from ppt_harness.state.document import Block, Mode, Shape, Slide
from ppt_harness.tools import router


def managed(sid: str, index: int = 0, *, title: str = "", items: list[str] | None = None,
            prose: str = "") -> Slide:
    blocks = []
    if title:
        blocks.append(Block(id="bk_t", region="header", component="slide_title",
                            variant="plain", slots={"title": title}))
    if items:
        blocks.append(Block(id="bk_b", region="body", component="bullets",
                            variant="plain", slots={"items": items}))
    if prose:
        blocks.append(Block(id="bk_p", region="body", component="quote", variant="plain",
                            slots={"prose": prose}))
    return Slide(id=sid, index=index, mode=Mode.MANAGED, layout="stack", blocks=blocks)


def freeform(sid: str, index: int = 0, *, title: str = "",
             items: list[str] | None = None) -> Slide:
    frame = {"x": 0, "y": 0, "cx": 100, "cy": 100}
    shapes = []
    if title:
        shapes.append(Shape(id="sh_t", ooxml_id=1, type="textbox", frame=frame,
                            role="slide_title", text=title))
    if items:
        shapes.append(Shape(id="sh_b", ooxml_id=2, type="textbox", frame=frame,
                            role="body", text="\n".join(items), bullet="bullet"))
    return Slide(id=sid, index=index, mode=Mode.FREEFORM, shapes=shapes)


def deck_of(session: Session, *slides: Slide) -> Session:
    session.deck.slides.extend(slides)
    return session


def rules(findings) -> list[str]:
    return [f.rule for f in findings]


# ------------------------------------------------------------------------ normalisation


def test_both_modes_flatten_to_the_same_shape() -> None:
    """A rule that had to know which mode it was reading would be written twice and go
    stale on one of them. The flattening is what keeps every rule mode-blind."""
    a = review.read(managed("s1", title="Churn doubled", items=["one", "two"]))
    b = review.read(freeform("s2", title="Churn doubled", items=["one", "two"]))
    assert a.title == b.title == "Churn doubled"
    assert [items for items, _ in a.lists] == [items for items, _ in b.lists]


def test_a_freeform_title_is_not_also_read_as_body() -> None:
    slide = freeform("s1", title="Churn doubled")
    assert review.read(slide).prose == ()


def test_figures_are_not_read_as_bullets() -> None:
    """A `stat_row`'s items are numbers with labels. Asking whether they end in a full stop
    is a question about the wrong kind of thing."""
    slide = Slide(id="s1", index=0, mode=Mode.MANAGED, layout="stack", blocks=[
        Block(id="bk", region="body", component="stat_row", variant="plain",
              slots={"items": [{"value": "2.1×", "label": "growth"}]}),
    ])
    assert review.read(slide).lists == ()


# -------------------------------------------------------------------------- slide rules


def test_a_filing_label_title_is_caught() -> None:
    found = review.review(deck_of(Session.blank("D"), managed("s1", title="Revenue analysis")).deck)
    assert "topic_title" in rules(found)


def test_a_title_that_states_a_finding_is_left_alone() -> None:
    found = review.review(
        deck_of(Session.blank("D"), managed("s1", title="Churn doubled in EMEA")).deck)
    assert "topic_title" not in rules(found)


def test_a_two_word_department_is_not_two_claims() -> None:
    """The conjunction rule needs length. "Sales and Marketing" names one thing, and a rule
    that flagged it would fire on half the decks in the world."""
    found = review.review(
        deck_of(Session.blank("D"), managed("s1", title="Sales and Marketing")).deck)
    assert "title_conjunction" not in rules(found)


def test_a_long_title_joining_two_claims_is_caught() -> None:
    found = review.review(deck_of(Session.blank("D"), managed(
        "s1", title="Churn doubled in EMEA and pricing is the fix")).deck)
    assert "title_conjunction" in rules(found)


def test_a_list_of_five_is_fine_and_a_list_of_seven_is_not() -> None:
    ok = review.review(deck_of(Session.blank("D"),
                               managed("s1", items=[f"item {i}" for i in range(5)])).deck)
    crowded = review.review(deck_of(Session.blank("E"),
                                    managed("s1", items=[f"item {i}" for i in range(7)])).deck)
    assert "crowded_list" not in rules(ok)
    assert "crowded_list" in rules(crowded)


def test_prose_long_enough_to_be_read_is_caught() -> None:
    found = review.review(deck_of(Session.blank("D"),
                                  managed("s1", prose="word " * 50)).deck)
    assert "wall_of_text" in rules(found)


def test_a_short_note_is_not_a_wall() -> None:
    found = review.review(deck_of(Session.blank("D"),
                                  managed("s1", prose="Source: finance, Q3 close.")).deck)
    assert "wall_of_text" not in rules(found)


def test_bullets_that_disagree_about_full_stops_are_caught() -> None:
    found = review.review(deck_of(Session.blank("D"), managed(
        "s1", items=["First point.", "Second point", "Third point."])).deck)
    assert "bullet_stop_mixed" in rules(found)


def test_two_bullets_are_too_few_to_have_a_convention() -> None:
    """Two items differing is a coin toss, not a house style."""
    found = review.review(deck_of(Session.blank("D"),
                                  managed("s1", items=["First point.", "Second point"])).deck)
    assert "bullet_stop_mixed" not in rules(found)


def test_bullets_with_mixed_opening_case_are_caught() -> None:
    found = review.review(deck_of(Session.blank("D"), managed(
        "s1", items=["Ship the fix", "review pricing", "Tell the board"])).deck)
    assert "bullet_case_mixed" in rules(found)


# --------------------------------------------------------------------------- deck rules


def test_a_repeated_title_is_caught_on_the_second_slide() -> None:
    found = review.review(deck_of(Session.blank("D"),
                                  managed("s1", 0, title="Churn doubled in EMEA"),
                                  managed("s2", 1, title="Churn doubled in EMEA")).deck)
    dupes = [f for f in found if f.rule == "duplicate_title"]
    assert [f.slide_id for f in dupes] == ["s2"], "the first one is not the problem"


def test_a_deck_that_ends_on_questions_is_caught() -> None:
    found = review.review(deck_of(Session.blank("D"),
                                  managed("s1", 0, title="Churn doubled in EMEA"),
                                  managed("s2", 1, title="Questions?")).deck)
    assert "weak_close" in rules(found)


def test_a_deck_that_ends_on_an_ask_is_not() -> None:
    found = review.review(deck_of(Session.blank("D"),
                                  managed("s1", 0, title="Churn doubled in EMEA"),
                                  managed("s2", 1, title="Approve the pricing change")).deck)
    assert "weak_close" not in rules(found)


def test_mixed_title_case_across_the_deck_is_caught() -> None:
    found = review.review(deck_of(
        Session.blank("D"),
        managed("s1", 0, title="Churn doubled in the region"),
        managed("s2", 1, title="Pricing drove the change"),
        managed("s3", 2, title="Retention Held Through The Quarter"),
    ).deck)
    drift = next(f for f in found if f.rule == "title_case_drift")
    assert "s3" in drift.message, "and it names the odd one out"


def test_a_deck_with_one_convention_says_nothing() -> None:
    found = review.review(deck_of(
        Session.blank("D"),
        managed("s1", 0, title="Churn doubled in the region"),
        managed("s2", 1, title="Pricing drove the change"),
    ).deck)
    assert "title_case_drift" not in rules(found)


# ------------------------------------------------------------------------- the dead band


@pytest.mark.parametrize("title", [
    "Q3 results",                      # too few words to read a convention from
    "EMEA CHURN DOUBLED",              # all caps says nothing about either convention
    "Churn doubled in EMEA",           # proper nouns look like Title Case and are not
])
def test_a_title_that_cannot_say_says_nothing(title: str) -> None:
    """The single most important behaviour in the file. Each of these looks like evidence
    of a capitalisation convention and is not; a rule that guessed here would infer a house
    style from noise and then ask the user to change slides to match it."""
    assert review._case_of(title) is None


def test_a_deck_of_ambiguous_titles_produces_no_drift_finding() -> None:
    found = review.review(deck_of(
        Session.blank("D"),
        managed("s1", 0, title="Churn doubled in EMEA"),
        managed("s2", 1, title="Q3 results"),
        managed("s3", 2, title="AMER LED THE QUARTER"),
    ).deck)
    assert "title_case_drift" not in rules(found)


# ----------------------------------------------------------------------------- ordering


def test_every_rule_appears_in_the_order() -> None:
    """`review` sorts by `ORDER.index`, so a rule missing from it raises ValueError the
    first time it fires — on a user's deck, not here."""
    emitted = set()
    for slide in (managed("s1", title="Revenue analysis and pricing is the fix",
                          items=["a.", "B", "c"] + [f"x{i}" for i in range(6)],
                          prose="word " * 50),):
        r = review.read(slide)
        for rule in review.SLIDE_RULES:
            emitted |= {f.rule for f in rule(r)}
    for deck_rule in review.DECK_RULES:
        emitted |= {f.rule for f in deck_rule([review.read(managed("s1", title="Questions?"))])}
    assert emitted, "the sample should trip several rules"
    assert emitted <= set(review.ORDER), f"not in ORDER: {emitted - set(review.ORDER)}"


def test_what_changes_meaning_outranks_what_changes_consistency() -> None:
    found = review.review(deck_of(
        Session.blank("D"),
        managed("s1", 0, title="Revenue analysis", items=["First.", "second", "Third."]),
        managed("s2", 1, title="Pricing Drove The Change In Retention"),
    ).deck)
    assert rules(found)[0] == "topic_title"


# --------------------------------------------------------------------------- the tool


@pytest.fixture
def session() -> Session:
    return deck_of(Session.blank("D"),
                   managed("s1", 0, title="Revenue analysis"),
                   managed("s2", 1, title="Migration status"))


def test_a_finding_never_refuses(session: Session) -> None:
    """The whole point. A budget refusal is a fact and may refuse; a finding is an opinion,
    and an opinion that can block a write is a style guide holding a gun."""
    result = router.dispatch(session, "review_deck")
    assert result["ok"] is True
    assert result["findings"]


def test_a_clean_deck_is_silent() -> None:
    quiet = deck_of(Session.blank("D"),
                    managed("s1", 0, title="Churn doubled in the region",
                            items=["Ship the fix", "Review pricing"]))
    result = router.dispatch(quiet, "review_deck")
    assert result["findings"] == [] and result["clean"] is True


def test_a_finding_is_offered_once(session: Session) -> None:
    first = router.dispatch(session, "review_deck")
    second = router.dispatch(session, "review_deck")
    assert second["findings"] == []
    assert second["suppressed"] == len(first["findings"]), "and the loss stays visible"


def test_everything_can_be_asked_for_again(session: Session) -> None:
    router.dispatch(session, "review_deck")
    again = router.dispatch(session, "review_deck", {"include_raised": True})
    assert again["findings"], "suppression is a default, not a wall"


def test_one_slide_can_be_reviewed_alone(session: Session) -> None:
    result = router.dispatch(session, "review_deck", {"slide_id": "s2"})
    assert {f["slide"] for f in result["findings"]} == {"s2"}


def test_reviewing_a_slide_that_is_not_there_says_so(session: Session) -> None:
    """Silence would read as "slide 9 is fine" rather than "there is no slide 9"."""
    result = router.dispatch(session, "review_deck", {"slide_id": "s99"})
    assert result["ok"] is False


def test_a_hidden_slide_is_not_reviewed() -> None:
    """It is not in the deck anyone will see, so it is not worth anyone's attention."""
    session = deck_of(Session.blank("D"), managed("s1", 0, title="Revenue analysis"))
    session.deck.slides[0].hidden = True
    assert router.dispatch(session, "review_deck")["findings"] == []
