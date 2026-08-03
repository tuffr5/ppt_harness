"""Bounded overrides — DESIGN §3, PLAN B3.

The pressure valve. Without one, every "make it a bit tighter" becomes a reason to eject a
slide out of managed mode, and a deck that has been ejected is a deck the harness can no
longer reason about. These absorb that pressure without giving up the guarantees.

Two properties make them safe to hand a model:

- **Clamped at both ends.** An override names a step on a scale the theme already defines,
  not a number. `density` picks from a fixed set; `accent` indexes the palette's ordered
  accents and wraps. Nothing here can be set to a value that collides slots or leaves the
  palette.
- **Theme-derived.** Every value resolves through the theme, so a deck stays internally
  consistent even where a block has been nudged. An override that carried a raw colour or a
  point size would be `set_font` wearing a different name.

What is deliberately absent: size, face, and colour-as-hex. Those belong to the theme, and
the whole catalog exists so a model never has to name one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..state.document import Theme


@dataclass(frozen=True)
class Override:
    """One knob, and the steps it is allowed to take."""

    key: str
    values: tuple[Any, ...]
    default: Any
    purpose: str

    def clamp(self, value: Any) -> Any:
        """The nearest legal value. Never raises — an override is a hint, not a command.

        A numeric knob clamps to its range; a named one falls back to its default. Refusing
        instead would turn "a bit tighter" into an error the model has to reason about,
        which is the opposite of a pressure valve.
        """
        if isinstance(self.default, (int, float)) and not isinstance(self.default, bool):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return self.default
            bounded = max(min(number, max(self.values)), min(self.values))
            # An index stays an index. `accent: 2.0` reads as a scale factor to anyone
            # looking at the op log, and indexes a palette by a float.
            return round(bounded) if isinstance(self.default, int) else bounded
        return value if value in self.values else self.default


OVERRIDES: dict[str, Override] = {
    "density": Override(
        key="density",
        values=("normal", "compact"),
        default="normal",
        purpose="Tighten the spacing between a block's slots. Never its type.",
    ),
    "emphasis": Override(
        key="emphasis",
        values=("normal", "strong", "quiet"),
        default="normal",
        purpose="Push a block forward or let it recede, using the theme's ink roles.",
    ),
    "align": Override(
        key="align",
        values=("left", "center", "right"),
        default="left",
        purpose="Override the variant's alignment for this block only.",
    ),
    "accent": Override(
        key="accent",
        values=(0, 1, 2, 3, 4, 5),
        default=0,
        purpose="Which of the theme's ordered accents this block uses.",
    ),
    "media_scale": Override(
        key="media_scale",
        values=(0.6, 1.0),
        default=1.0,
        purpose="How much of its box a picture fills, between 60% and 100%.",
    ),
}

#: How much of the normal gap `density: compact` leaves. A constant rather than a number the
#: caller supplies: an override that could be set to zero would collide slots rather than
#: tighten them, which is not a tighter slide, it is a broken one.
DENSITY_COMPACT = 0.4

#: Emphasis resolves to a theme *role*, never to a colour.
EMPHASIS_INK = {"normal": "ink", "strong": "brand", "quiet": "ink_muted"}


def clamp_all(values: dict[str, Any]) -> dict[str, Any]:
    """Every override a block carries, clamped. Unknown keys are dropped.

    Dropped rather than rejected: a model that invents `spacing` should get a slide, not a
    stack trace — and `describe` tells it what the knobs actually are.
    """
    return {k: OVERRIDES[k].clamp(v) for k, v in values.items() if k in OVERRIDES}


def ink_for(theme: Theme, overrides: dict[str, Any]) -> str:
    """The ink colour a block's emphasis resolves to, through the theme."""
    role = EMPHASIS_INK.get(str(overrides.get("emphasis", "normal")), "ink")
    value = theme.palette.get(role) or theme.palette.get("ink") or "#111111"
    return value if isinstance(value, str) else "#111111"


def accent_for(theme: Theme, overrides: dict[str, Any]) -> str:
    """The accent a block uses. Indexes the theme's ordered accents and wraps."""
    return theme.accent(int(OVERRIDES["accent"].clamp(overrides.get("accent", 0))))


def gap_factor(overrides: dict[str, Any]) -> float:
    return DENSITY_COMPACT if overrides.get("density") == "compact" else 1.0


def media_factor(overrides: dict[str, Any]) -> float:
    return float(OVERRIDES["media_scale"].clamp(overrides.get("media_scale", 1.0)))


def describe() -> list[dict[str, Any]]:
    """What a model is told the knobs are — names and steps, no prose about geometry."""
    return [
        {"key": o.key, "values": list(o.values), "default": o.default,
         "purpose": o.purpose}
        for o in OVERRIDES.values()
    ]
