"""Worst-case slot payloads — DESIGN §3, PLAN B1.

Two payloads per component, because "worst case" means two different things and conflating
them tests neither.

`payload` is the **most a component claims to hold**: its declared `max_items`, content
sized to the room each item actually gets, every optional slot filled. This must fit. A
failure here means the component's declaration is a promise it cannot keep.

`oversized` is content **past** that point. This must be *refused*, with the capacity, the
overage, and the ways out — the budget gate is the cheapest failure in the system and it
has to fire.

Generated from each component's own declared bounds rather than stored as JSON: a fixture
that drifts from the spec it is testing proves nothing.

Two scripts, because a budget in characters is not a budget. CJK runs about twice the
advance width of Latin at the same point size, so a component that fits English at its
limit can overflow the same sentence in Chinese, and only measuring catches it.
"""

from __future__ import annotations

from typing import Any

from . import registry

#: Long enough to exercise wrapping, short enough to be a plausible slide. Real decks fail
#: on sentences, not on lorem ipsum.
LATIN = ("Latency fell thirty-eight percent after the cache rewrite, though two regions "
         "still run the old scheduler")
CJK = "缓存重写之后延迟下降了百分之三十八，但仍有两个区域运行着旧版调度器，需要在下个季度完成迁移"

SHORT_LATIN = "Cost per request held flat"
SHORT_CJK = "每次请求的成本保持不变"


def _text(script: str, short: bool = False) -> str:
    if script == "cjk":
        return SHORT_CJK if short else CJK
    return SHORT_LATIN if short else LATIN


#: Roles whose list items are a figure and a caption rather than a sentence. Feeding a
#: paragraph to a 44pt `stat` slot measures a component nobody would build.
FIGURE_ROLES = {"stat"}

STAT_VALUES = ("$4.2M", "-3.1%", "38%", "2.4x", "9,412", "1.8s")
STAT_LABELS = ("ARR", "Churn", "Latency", "Growth", "Requests", "p99")


def _value(spec: registry.SlotSpec, script: str) -> Any:
    """The worst case this slot admits, at its own declared bounds."""
    if spec.shape == "list":
        count = spec.max_items or 6
        if spec.role in FIGURE_ROLES:
            return [{"value": STAT_VALUES[i % len(STAT_VALUES)],
                     "label": STAT_LABELS[i % len(STAT_LABELS)]}
                    for i in range(count)]
        # Sized to the room each item actually gets. A slot's `max_lines` is a per-item
        # cap, but the region is shared: eight items in a body region get under two lines
        # each, so eight paragraphs is not this component's worst case — it is a different
        # component's job, and `oversized` is where that belongs.
        short = spec.max_lines <= 1 or count > 4
        return [_text(script, short=short) for _ in range(count)]
    if spec.shape == "tabular":
        return {
            "headers": ["Region", "Q1", "Q2", "Owner"],
            "rows": [[_text(script, short=True), "18.2", "22.7", "platform"]
                     for _ in range(6)],
        }
    if spec.shape == "chart":
        return {
            "kind": "bar",
            "labels": ["us-east", "us-west", "eu-central", "ap-south"],
            "series": [{"name": "Q1", "values": [18.2, 11.9, 9.4, 6.1]}],
        }
    if spec.shape == "media":
        return {"asset_id": "fixture", "alt": _text(script, short=True)}
    return _text(script, short=spec.max_lines <= 3)


def payload(key: str, script: str = "latin", *, optional: bool = True) -> dict[str, Any]:
    """The most this component claims to hold. Must fit.

    `optional=False` drops every non-required slot — the other case worth checking, because
    a component whose geometry only works when every slot is filled breaks the first time
    someone leaves the subtitle out.
    """
    comp = registry.get(key)
    return {
        name: _value(spec, script)
        for name, spec in comp.slots.items()
        if spec.required or optional
    }


def oversized(key: str, script: str = "latin") -> dict[str, Any]:
    """Past what the component claims. Must be refused.

    Every text slot gets a paragraph. This is the ordinary way a real deck fails — someone
    pastes a sentence where a label goes — and the gate exists to catch exactly this.
    """
    comp = registry.get(key)
    out: dict[str, Any] = {}
    for name, spec in comp.slots.items():
        value = _value(spec, script)
        if spec.shape == "list" and spec.role in FIGURE_ROLES:
            # A figure slot overflows on its caption, not on its number: nobody pastes a
            # paragraph where "$4.2M" goes, but "Annual recurring revenue, net of churn"
            # is exactly what turns up under it.
            out[name] = [{"value": item["value"], "label": _text(script)}
                         for item in value]
        elif spec.shape == "list":
            out[name] = [_text(script) * 2 for _ in value]
        elif spec.shape == "tabular":
            out[name] = {"headers": value["headers"],
                         "rows": [list(value["rows"][0]) for _ in range(24)]}
        elif spec.shape in ("title", "prose"):
            out[name] = _text(script) * 3
        else:
            out[name] = value
    return out


def region_for(key: str) -> tuple[str, str]:
    """A (layout, region) this component can legally occupy."""
    comp = registry.get(key)
    for region in comp.regions:
        for layout_key, frame in registry.LAYOUTS.items():
            if region in frame.regions:
                return layout_key, region
    raise KeyError(f"{key} declares no region any layout provides")


def every_case() -> list[tuple[str, str, str, bool]]:
    """(component, variant, script, optional) for the whole catalog.

    The cross product is the point: a variant that fits at three items and overflows at
    four is a bug in that variant's budget, and only enumerating finds it.
    """
    cases = []
    for key, comp in registry.COMPONENTS.items():
        for variant in comp.variants:
            for script in ("latin", "cjk"):
                cases.append((key, variant, script, True))
        # One pass with optionals dropped is enough to catch geometry that assumed them.
        cases.append((key, comp.default_variant, "latin", False))
    return cases
