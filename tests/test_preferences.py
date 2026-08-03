"""Preference profile — `core/preferences.py`, DESIGN §8.2.

A system that learns preferences is a system that can be confidently wrong about someone,
so most of what is worth defending here is restraint: what it refuses to conclude, how
slowly it concludes it, and whether a person can tell why it thinks what it thinks.
"""

from __future__ import annotations

from ppt_harness.core.preferences import RULE, PreferenceProfile
from ppt_harness.core.session import Session
from ppt_harness.state.document import Author
from ppt_harness.state.ops import Op, OpLog, Turn
from ppt_harness.tools import router


def _pair(log: OpLog, target: str, model_patch: dict, user_patch: dict,
          op: str = "set_text") -> None:
    """A model op followed by a user op on the same target — one correction."""
    turn = Turn(id=len(log.turns))
    for author, patch in ((Author.MODEL, model_patch), (Author.USER, user_patch)):
        log.append(turn, Op(seq=-1, op=op, target=target, patch=patch, inverse={},
                            author=author, turn=turn.id))


# ------------------------------------------------------------------ confidence


def test_one_correction_is_an_anecdote_not_a_rule() -> None:
    """The whole failure mode of a preference-learning system in one test: acting on n=1."""
    profile = PreferenceProfile()
    pref = profile.note("component.stat_row.variant", "flat")
    assert pref.is_rule is False
    assert pref.confidence < RULE


def test_a_habit_becomes_a_rule_only_once_it_looks_like_one() -> None:
    profile = PreferenceProfile()
    for _ in range(8):
        profile.note("component.stat_row.variant", "flat")
    pref = profile.preferences["component.stat_row.variant"]
    assert pref.confidence >= RULE and pref.is_rule


def test_a_stated_rule_is_trusted_at_once() -> None:
    """Observation is inference; a person saying it is not. Averaging what someone told you
    against what they did last month would be perverse."""
    profile = PreferenceProfile()
    pref = profile.note("avoid.pie_charts", "never", source="explicit")
    assert pref.confidence == 1.0 and pref.is_rule


def test_one_contrary_choice_does_not_overturn_a_habit() -> None:
    """Someone who chose `flat` eight times and `boxed` once has not changed their mind."""
    profile = PreferenceProfile()
    for _ in range(8):
        profile.note("component.stat_row.variant", "flat")
    profile.note("component.stat_row.variant", "boxed")
    assert profile.preferences["component.stat_row.variant"].value == "flat"


def test_a_sustained_change_of_mind_does_win_eventually() -> None:
    """The other half of the same rule — a preference that can never be revised is a bug."""
    profile = PreferenceProfile()
    for _ in range(3):
        profile.note("copy.length", "longer")
    for _ in range(4):
        profile.note("copy.length", "shorter")
    assert profile.preferences["copy.length"].value == "shorter"


def test_a_stated_rule_outranks_an_observed_one() -> None:
    profile = PreferenceProfile()
    for _ in range(10):
        profile.note("copy.length", "longer")
    profile.note("copy.length", "shorter", source="explicit")
    assert profile.preferences["copy.length"].value == "shorter"


# -------------------------------------------------------------- propose, not adopt


def test_a_strong_observation_is_proposed_rather_than_adopted() -> None:
    """DESIGN §8.2: nothing the user did not confirm ever presents itself as their rule."""
    profile = PreferenceProfile()
    for _ in range(9):
        profile.note("component.stat_row.variant", "flat")
    proposals = profile.proposals()
    assert [p.key for p in proposals] == ["component.stat_row.variant"]
    assert profile.preferences["component.stat_row.variant"].adopted is False


def test_a_proposal_is_asked_once() -> None:
    profile = PreferenceProfile()
    for _ in range(9):
        profile.note("structure.closes_with", "takeaway")
    profile.preferences["structure.closes_with"].proposed = True
    assert profile.proposals() == []


def test_adopting_is_the_only_path_from_noticed_to_rule() -> None:
    profile = PreferenceProfile()
    profile.note("copy.case", "sentence")
    assert profile.preferences["copy.case"].is_rule is False
    assert profile.adopt("copy.case") is True
    assert profile.preferences["copy.case"].is_rule is True


# ------------------------------------------------------------ the observed channel


def test_only_a_correction_counts_as_a_signal() -> None:
    """A user editing something the model never touched is authoring, not correcting.
    Counting it would turn ordinary work into evidence."""
    log = OpLog()
    turn = Turn(id=0)
    log.append(turn, Op(seq=-1, op="set_text", target="s1/b1/title",
                        patch={"text": "Mine alone"}, inverse={}, author=Author.USER,
                        turn=0))
    profile = PreferenceProfile()
    assert profile.observe(log) == []
    assert profile.preferences == {}


def test_a_shortened_rewrite_is_read_as_a_preference_for_shorter() -> None:
    log = OpLog()
    _pair(log, "s1/b1/title",
          {"text": "A rather long and thoroughly over-explained title"},
          {"text": "Churn doubled"})
    profile = PreferenceProfile()
    profile.observe(log)
    assert profile.preferences["copy.length"].value == "shorter"


def test_a_typo_fix_teaches_nothing() -> None:
    """A one-character change says nothing about how long a person likes their titles, and
    a profile that concludes otherwise will be wrong loudly and often."""
    log = OpLog()
    _pair(log, "s1/b1/title", {"text": "Revenue grew"}, {"text": "Revenue grew!"})
    profile = PreferenceProfile()
    profile.observe(log)
    assert "copy.length" not in profile.preferences


def test_a_trailing_period_signal_fires_only_when_it_changed() -> None:
    """Guards a real bug: comparing a `Match` object against a bool made every rewrite look
    like a punctuation preference."""
    log = OpLog()
    _pair(log, "s1/b1/title", {"text": "Revenue grew."}, {"text": "Costs fell."})
    profile = PreferenceProfile()
    profile.observe(log)
    assert "copy.trailing_period" not in profile.preferences


def test_a_variant_correction_is_keyed_to_its_component() -> None:
    """"For a stat_row they like flat" is usable; "they like flat" is not."""
    log = OpLog()
    _pair(log, "bk_1",
          {"slide_id": "s1", "block_id": "bk_1", "component": "stat_row"},
          {"slide_id": "s1", "block_id": "bk_1", "variant": "flat",
           "component": "stat_row"},
          op="set_block_props")
    profile = PreferenceProfile()
    profile.observe(log)
    assert profile.preferences["component.stat_row.variant"].value == "flat"


# ------------------------------------------------------------------- in context


def test_an_empty_profile_costs_no_context() -> None:
    """Level 2 is on every turn, so a feature nobody has used yet must be free."""
    assert PreferenceProfile().block() == ""


def test_rules_and_hints_are_labelled_differently() -> None:
    """A model told "they prefer X" acts differently from one told "guessed once", and
    should. Provenance travels with the value."""
    profile = PreferenceProfile()
    profile.note("avoid.pie_charts", "never", source="explicit")
    profile.note("copy.case", "sentence")
    block = profile.block()
    assert "avoid.pie_charts = never  [stated]" in block
    assert "copy.case = sentence  [1x]" in block
    assert "Weak signals" in block, "and the caveat is stated once, not per line"


def test_the_block_stays_small_under_a_long_history() -> None:
    profile = PreferenceProfile()
    for i in range(200):
        profile.note(f"component.c{i}.variant", "flat")
    assert len(profile.block()) < 900


def test_the_profile_enters_the_system_prompt(populated: Session) -> None:
    from ppt_harness.core import loop

    populated.preferences.note("avoid.pie_charts", "never", source="explicit")
    assert "avoid.pie_charts" in loop.context_block(populated)


# ------------------------------------------------------------------ persistence


def test_a_profile_round_trips(tmp_path) -> None:
    profile = PreferenceProfile()
    profile.note("copy.length", "shorter")
    profile.note("copy.length", "shorter")
    profile.save(tmp_path / "preferences.json")

    loaded = PreferenceProfile.load(tmp_path / "preferences.json")
    assert loaded.preferences["copy.length"].n == 2


def test_a_corrupt_profile_starts_fresh_rather_than_raising(tmp_path) -> None:
    """Preferences are an optimisation. Refusing to open a deck because a preferences file went
    bad would be absurd."""
    path = tmp_path / "preferences.json"
    path.write_text("{ not json at all")
    assert PreferenceProfile.load(path).preferences == {}


# ---------------------------------------------------------------------- the tools


def test_a_stated_preference_can_be_remembered(populated: Session) -> None:
    result = router.dispatch(populated, "remember_preference",
                             {"key": "avoid.pie_charts", "value": "never"})
    assert result["ok"]
    assert populated.preferences.preferences["avoid.pie_charts"].source == "explicit"


def test_a_preference_outside_the_vocabulary_is_refused_with_a_reason(
        populated: Session) -> None:
    """Left open, this becomes a second system prompt the model writes for itself, and
    every later turn pays for it."""
    result = router.dispatch(populated, "remember_preference",
                             {"key": "the user seemed happy", "value": "yes"})
    assert result["ok"] is False
    assert result["error"] == "unknown_namespace"
    assert "component" in result["message"]


def test_a_preference_may_not_be_a_paragraph(populated: Session) -> None:
    result = router.dispatch(populated, "remember_preference",
                             {"key": "copy.voice", "value": "word " * 40})
    assert result["error"] == "value_too_long"


def test_get_preferences_separates_what_is_known_from_what_is_guessed(
        populated: Session) -> None:
    populated.preferences.note("avoid.pie_charts", "never", source="explicit")
    populated.preferences.note("copy.case", "sentence")
    body = router.dispatch(populated, "get_preferences")
    assert [r["key"] for r in body["rules"]] == ["avoid.pie_charts"]
    assert [h["key"] for h in body["hints"]] == ["copy.case"]


def test_get_preferences_surfaces_what_is_worth_confirming(populated: Session) -> None:
    for _ in range(9):
        populated.preferences.note("component.stat_row.variant", "flat")
    body = router.dispatch(populated, "get_preferences")
    assert body["worth_confirming"]
    assert "should that be the default?" in body["worth_confirming"][0]["ask"]


def test_preferences_cannot_express_a_coordinate() -> None:
    """Principle 1 reaches here too: learned preferences bind the theme and the catalog,
    never geometry."""
    from ppt_harness.tools.base import REGISTRY

    for name in ("remember_preference", "get_preferences"):
        schema = REGISTRY[name].schema
        for prop in (schema.get("properties") or {}).values():
            assert prop.get("type") != "number"
