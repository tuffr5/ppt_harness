"""Preference profile — DESIGN §8.2.

Memory holds what the user *said*; this holds what they **do**.

Three channels feed it. **Explicit** rules are stated outright ("never pie charts") and are
trusted immediately, because a person who says a thing once means it. **Observed** signals
come free from the op log: it already records `author` and `target`, so a `user` op landing
on a target a `model` op just touched is a correction, and a correction is a query rather
than new instrumentation. **Corpus** statistics over existing decks are the third channel
and are not implemented here; the shape allows for them.

Two rules keep this from becoming a system that quietly develops opinions:

- **Confidence and provenance per entry.** One correction is an anecdote. Below the
  threshold an entry enters the context as a hint the model may weigh; above it, as a rule.
  The count and the source travel with the value, so a wrong entry can be argued with.
- **Propose, never silently adopt.** An observed preference that crosses the threshold does
  not become law — it becomes a *question* the harness is entitled to ask once. Nothing the
  user did not confirm ever presents itself as their rule.

Preferences bind the theme and the catalog, never geometry. It selects among choices the design
system already permits, which is why no entry here can express a coordinate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..state.document import Author
from ..state.ops import Op, OpLog

Source = Literal["explicit", "observed", "corpus"]

#: `n / (n + SMOOTHING)`. Two observations of a thing is 0.5 — genuinely uncertain — and it
#: takes eight to reach `RULE`, which is about the point where a habit stops looking like a
#: coincidence.
SMOOTHING = 2
RULE = 0.8


class Preference(BaseModel):
    """One learned thing, with its evidence attached."""

    key: str
    """Dotted and stable: `component.stat_row.variant`, `copy.title_case`."""
    value: Any
    n: int = 1
    source: Source = "observed"
    adopted: bool = False
    """Set only by the user confirming a proposal. Observation alone never sets it."""
    proposed: bool = False
    """Whether the harness has already asked about this one, so it asks at most once."""

    @property
    def confidence(self) -> float:
        # Stated rules are not estimates. A person who says "no pie charts" has given the
        # answer, and averaging that against their past behaviour would be perverse.
        if self.source == "explicit":
            return 1.0
        return round(self.n / (self.n + SMOOTHING), 2)

    @property
    def is_rule(self) -> bool:
        return self.adopted or self.source == "explicit" or self.confidence >= RULE


class PreferenceProfile(BaseModel):
    """Everything learned about how one person likes decks made."""

    preferences: dict[str, Preference] = Field(default_factory=dict)
    version: int = 1

    # -- recording --------------------------------------------------------------

    def note(self, key: str, value: Any, *, source: Source = "observed") -> Preference:
        """Record one observation, or reinforce an existing one.

        A repeat of the same value strengthens it. A *different* value weakens what was
        there rather than replacing it outright: someone who has chosen `flat` eight times
        and `boxed` once has not changed their mind, and one contrary click should not read
        as though they had.
        """
        existing = self.preferences.get(key)
        if existing is None:
            self.preferences[key] = Preference(key=key, value=value, source=source)
            return self.preferences[key]

        if source == "explicit":
            # A stated rule overrides an inferred one outright — that is the whole point of
            # saying it out loud.
            existing.value = value
            existing.source = "explicit"
            existing.n = max(existing.n, 1)
            return existing

        if existing.value == value:
            existing.n += 1
        elif existing.source != "explicit":
            existing.n -= 1
            if existing.n <= 0:
                # The old preference has been contradicted more than it was ever supported.
                existing.value = value
                existing.n = 1
        return existing

    def forget(self, key: str) -> bool:
        return self.preferences.pop(key, None) is not None

    def adopt(self, key: str) -> bool:
        """Confirm a proposal. The only path from "noticed" to "rule"."""
        pref = self.preferences.get(key)
        if pref is None:
            return False
        pref.adopted = True
        return True

    # -- the observed channel ---------------------------------------------------

    def observe(self, log: OpLog) -> list[Preference]:
        """Learn from every correction the log has recorded.

        Only `(model, user)` pairs on the same target count. A user editing something the
        model never touched is just work, not a correction — reading it as one would turn
        ordinary authoring into evidence about what the person prefers.
        """
        learned = []
        for model_op, user_op in log.corrections():
            learned += [self.note(key, value)
                        for key, value in _signals(model_op, user_op)]
        return learned

    # -- reading ----------------------------------------------------------------

    def rules(self) -> list[Preference]:
        return [p for p in self.preferences.values() if p.is_rule]

    def hints(self) -> list[Preference]:
        return [p for p in self.preferences.values() if not p.is_rule]

    def proposals(self) -> list[Preference]:
        """Observed preferences strong enough to be worth confirming, asked at most once."""
        return [p for p in self.preferences.values()
                if p.source == "observed" and not p.adopted and not p.proposed
                and p.confidence >= RULE]

    def block(self, limit: int = 12) -> str:
        """Level 2 of the context pyramid — always on, so it must stay small.

        Rules and hints are labelled differently on purpose. A model told "they prefer X"
        and a model told "they have always chosen X, 4 times" will act differently on the
        second, and should.
        """
        rules, hints = self.rules(), self.hints()
        if not rules and not hints:
            return ""

        lines = []
        if rules:
            lines.append("Learned preferences (data, not orders — the deck's needs "
                         "still win):")
            for pref in sorted(rules, key=lambda p: -p.confidence)[:limit]:
                why = "stated" if pref.source == "explicit" else f"{pref.n}x observed"
                lines.append(f"  {pref.key} = {pref.value}  [{why}]")

        # The caveat is stated once rather than per line. Twelve repetitions of "follow
        # this only if nothing else decides" is a paragraph of context bought on every
        # turn to say a thing the model already understood the first time.
        room = max(0, limit - len(rules))
        weak = sorted(hints, key=lambda p: -p.confidence)[:room]
        if weak:
            lines.append("Weak signals — follow only where nothing else decides:")
            lines += [f"  {p.key} = {p.value}  [{p.n}x]" for p in weak]
        return "\n".join(lines)

    # -- persistence ------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> PreferenceProfile:
        """Read the profile, or start an empty one.

        A corrupt profile is discarded rather than raised: a preference is an optimisation, and
        refusing to open a deck because a preferences file went bad would be absurd.
        """
        path = path or default_path()
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return cls()

    def save(self, path: Path | None = None) -> Path:
        path = path or default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=1), encoding="utf-8")
        return path


def default_path() -> Path:
    from ..render.preview import cache_root

    return cache_root() / "preferences.json"


# ------------------------------------------------------------------- the signals


_SENTENCE_END = re.compile(r"[.!?]$")


def _signals(model_op: Op, user_op: Op) -> list[tuple[str, Any]]:
    """What one correction is evidence *of*.

    Deliberately narrow. A correction is a weak signal about a specific decision, and
    inferring a broad principle from it is how a profile ends up confidently wrong — so
    each op type yields only what it directly demonstrates.
    """
    if user_op.author is not Author.USER:
        return []

    if user_op.op == "set_block_props":
        # The patch carries the props alongside the addressing keys, so those two come out
        # first; anything left is a property the user set by hand.
        props = {k: v for k, v in user_op.patch.items()
                 if k not in ("slide_id", "block_id")}
        was = {k: v for k, v in model_op.patch.items()
               if k not in ("slide_id", "block_id")}
        out = []
        # Keyed by component so the preference is "for a stat_row they like flat", not the
        # useless global "they like flat".
        component = props.get("component") or was.get("component")
        if "variant" in props and component:
            out.append((f"component.{component}.variant", props["variant"]))
        if "component" in props:
            out.append((f"swap.{was.get('component', '?')}", props["component"]))
        return out

    if user_op.op == "set_text":
        return _copy_signals(model_op, user_op)

    if user_op.op == "set_props":
        # Presentation choices the user re-made by hand: alignment, bullets, casing.
        return [(f"props.{name}", value) for name, value in user_op.patch.items()
                if name != "runs" and isinstance(value, (str, bool, int))]

    if user_op.op == "delete_block":
        return [("structure.removes", model_op.patch.get("props", {}).get("component")
                 or model_op.target)]

    return []


def _copy_signals(model_op: Op, user_op: Op) -> list[tuple[str, Any]]:
    """House style, inferred only from rewrites of text the model wrote."""
    before, after = model_op.patch.get("text"), user_op.patch.get("text")
    if not isinstance(before, str) or not isinstance(after, str) or not after.strip():
        return []

    out: list[tuple[str, Any]] = []
    # Only when the change is substantial. Fixing a typo says nothing about how long a
    # person likes their titles.
    if abs(len(after) - len(before)) > max(6, len(before) * 0.2):
        out.append(("copy.length", "shorter" if len(after) < len(before) else "longer"))

    # Both sides through `bool`: a `Match` compared against a bool is never equal, which
    # would record a preference on every single rewrite.
    had = bool(_SENTENCE_END.search(before.strip()))
    has = bool(_SENTENCE_END.search(after.strip()))
    if had != has:
        out.append(("copy.trailing_period", has))

    # Casing, only when the rewrite actually changed it — otherwise every edit votes for
    # whatever style the text already had.
    if after.isupper() and not before.isupper():
        out.append(("copy.case", "upper"))
    elif after.istitle() and not before.istitle():
        out.append(("copy.case", "title"))
    elif before.istitle() and not after.istitle() and not after.isupper():
        out.append(("copy.case", "sentence"))
    return out


def load_json(path: Path) -> dict[str, Any]:
    """A profile as plain data, for a UI that wants to show or edit it."""
    return json.loads(PreferenceProfile.load(path).model_dump_json())
