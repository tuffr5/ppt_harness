"""Preference tools — DESIGN §8.2.

The explicit channel. Observation is free and happens without anyone asking; these two
tools exist for the other half — a rule the user *stated*, and the proposal a strong
observation is allowed to become.

`remember_preference` is deliberately narrow. It takes a key from a fixed vocabulary and a
short value, so the profile stays a readable table rather than a second system prompt the
model writes for itself. A model that could store arbitrary prose here would eventually
store the whole conversation, and every later turn would pay for it.
"""

from __future__ import annotations

from typing import Any

from ..core.preferences import RULE
from ..core.session import Session
from .base import ToolError, obj, string, tool

#: What a stated preference is allowed to be about. Anything outside this is either a fact
#: about one deck — which belongs in the deck — or an instruction for this turn, which
#: belongs in the conversation.
NAMESPACES = ("component", "copy", "structure", "avoid", "props", "swap")


@tool("remember_preference",
      "Record a preference the user stated, so later decks follow it without being asked. "
      "Only for standing rules ('never pie charts', 'titles are findings'), never for a "
      "one-off instruction about the deck in front of you.",
      obj({"key": string("Dotted key: component.<key>.variant, copy.length, "
                         "structure.opens_with, avoid.<thing>"),
           "value": string("Short value — a word or a short phrase")},
          ["key", "value"]),
      # Marked mutating because it is: the profile outlives the conversation, and a host
      # with an approval gate should see a tool that writes to it.
      mutating=True)
def remember_preference(session: Session, key: str, value: str) -> dict[str, Any]:
    head = key.split(".")[0]
    if head not in NAMESPACES:
        raise ToolError(
            "unknown_namespace",
            f"{key!r} is not a preference the profile holds. Keys start with one of "
            f"{', '.join(NAMESPACES)}. If this is about the deck in front of you rather "
            "than a standing rule, just do it instead of remembering it.",
        )
    if len(value) > 80:
        raise ToolError(
            "value_too_long",
            "a preference is a setting, not a paragraph; say it in a few words "
            f"(got {len(value)} characters)",
        )

    profile = session.preferences
    pref = profile.note(key, value, source="explicit")
    session.remember_preferences()
    return {"ok": True, "remembered": {"key": pref.key, "value": pref.value,
                                       "source": pref.source},
            "summary": f"noted: {pref.key} = {pref.value}"}


@tool("get_preferences",
      "What has been learned about how this user likes decks made, and what is only a "
      "guess so far.",
      obj({}))
def get_preferences(session: Session) -> dict[str, Any]:
    profile = session.preferences
    return {
        "rules": [{"key": p.key, "value": p.value, "source": p.source,
                   "n": p.n, "confidence": p.confidence} for p in profile.rules()],
        "hints": [{"key": p.key, "value": p.value, "n": p.n,
                   "confidence": p.confidence} for p in profile.hints()],
        # Surfaced so the model can ask once — "you have switched stat_row to flat five
        # times, make it the default?" — rather than quietly acting on a pattern the user
        # never agreed to (DESIGN §8.2, propose never adopt).
        "worth_confirming": [
            {"key": p.key, "value": p.value, "n": p.n,
             "ask": f"you have chosen {p.value} for {p.key} {p.n} times — "
                    "should that be the default?"}
            for p in profile.proposals()
        ],
        "threshold": RULE,
    }
