"""Default theme — ARCHITECTURE.md §2.6.

Roles, never loose colors. Validated once at load so managed slides cannot fail contrast.
"""

from __future__ import annotations

from .document import Grid, Theme, TypeSpec, Typography


def default_theme() -> Theme:
    return Theme(
        id="harness-default",
        source="authored",
        palette={
            "bg": "#FFFFFF",
            "surface": "#F5F7FA",
            #: Reserved, and honestly labelled as such: paired with `shape.rule_weight`,
            #: these describe a divider line that no component currently draws. Kept
            #: rather than deleted because a theme is a contract — a deck extracted today
            #: should still resolve against a catalog that grows a divider tomorrow — but
            #: nothing reads either token, so neither is load-bearing yet.
            "rule": "#DCE1E8",
            "ink": "#12161C",
            "ink_muted": "#5A6472",
            "brand": "#004098",
            "brand_ink": "#FFFFFF",
            "accents": ["#0E7C66", "#B4532A", "#6A4C93", "#004098"],
            "positive": "#0E7C66",
            "negative": "#B4532A",
        },
        type=Typography(
            families={
                "display": "Georgia, 'Source Han Serif', serif",
                "body": "Inter, 'Helvetica Neue', -apple-system, sans-serif",
            },
            scale={
                "deck_title": TypeSpec(family="display", size=52, weight=700, line=60,
                                       track=-0.015),
                "slide_title": TypeSpec(family="display", size=34, weight=700, line=42),
                "block_title": TypeSpec(family="body", size=24, weight=600, line=31),
                "body": TypeSpec(family="body", size=21, weight=400, line=30),
                "stat": TypeSpec(family="display", size=44, weight=700, line=50),
                "label": TypeSpec(family="body", size=16, weight=500, line=22),
                "caption": TypeSpec(family="body", size=15, weight=400, line=21),
            },
            floor=15,
        ),
        grid=Grid(canvas=(1280, 720), margin=72, columns=12, gutter=20, baseline=4),
        spacing=[4, 8, 12, 16, 24, 32, 48, 64],
        shape={"radius": 3, "rule_weight": 4, "card_fill": "none", "shadow": "none"},
        layouts=["title", "stack", "two_col", "hero_plus_row", "full_bleed"],
    )


# --------------------------------------------------------------------- validation


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))

    def chan(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


PAIRS = [("ink", "bg"), ("ink_muted", "bg"), ("ink", "surface"), ("brand_ink", "brand")]


def validate_theme(theme: Theme, minimum: float = 4.5) -> list[str]:
    """Run once at load. A theme that passes means managed slides cannot fail contrast."""
    problems: list[str] = []
    for fg, bg in PAIRS:
        fgv, bgv = theme.palette.get(fg), theme.palette.get(bg)
        if not isinstance(fgv, str) or not isinstance(bgv, str):
            problems.append(f"missing palette role: {fg} or {bg}")
            continue
        ratio = contrast(fgv, bgv)
        if ratio < minimum:
            problems.append(f"contrast {fg}/{bg} = {ratio:.2f} < {minimum}")
    for role, spec in theme.type.scale.items():
        if spec.size < theme.type.floor:
            problems.append(f"type role {role} = {spec.size}pt below floor {theme.type.floor}")
    return problems
