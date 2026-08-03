"""OOXML colour resolution.

A fill is rarely a literal colour. `<a:schemeClr val="accent6"><a:lumMod val="75000"/>` means
"accent 6 of the theme, at 75% luminance", and resolving it needs the theme's colour scheme
plus the transform stack. Rendering the raw slot instead — or giving up and drawing grey —
makes a preview that does not look like the file.

Only the transforms that actually appear in decks are implemented: `lumMod`, `lumOff`,
`shade`, `tint`, `satMod`, and `alpha`. Anything else is passed through unchanged rather
than approximated, because a wrong colour is worse than an unmodified one.
"""

from __future__ import annotations

import colorsys

from lxml import etree

A = "http://schemas.openxmlformats.org/drawingml/2006/main"

#: DrawingML slot -> colour-scheme element. `tx`/`bg` are the presentation-facing names for
#: the same four slots the scheme calls `dk`/`lt`, and they are swapped in the scheme file.
SLOT_ALIAS = {"tx1": "dk1", "bg1": "lt1", "tx2": "dk2", "bg2": "lt2"}


def scheme_map(theme_xml: etree._Element) -> dict[str, str]:
    """`accent1` … `dk2` -> `#RRGGBB`, read from a theme part."""
    scheme = theme_xml.find(f".//{{{A}}}clrScheme")
    out: dict[str, str] = {}
    if scheme is None:
        return out
    for node in scheme:
        name = etree.QName(node).localname
        srgb = node.find(f"{{{A}}}srgbClr")
        sys_clr = node.find(f"{{{A}}}sysClr")
        if srgb is not None and srgb.get("val"):
            out[name] = "#" + srgb.get("val", "").upper()
        elif sys_clr is not None and sys_clr.get("lastClr"):
            out[name] = "#" + sys_clr.get("lastClr", "").upper()
    return out


# ------------------------------------------------------------------- transforms


def _rgb(value: str) -> tuple[float, float, float]:
    return tuple(int(value[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in rgb)


def _apply(hex_color: str, transform: str, amount: float) -> str:
    r, g, b = _rgb(hex_color)
    if transform in ("lumMod", "lumOff"):
        hue, lum, sat = colorsys.rgb_to_hls(r, g, b)
        lum = lum * amount if transform == "lumMod" else min(1.0, lum + amount)
        return _hex(colorsys.hls_to_rgb(hue, max(0.0, min(1.0, lum)), sat))
    if transform == "shade":
        return _hex((r * amount, g * amount, b * amount))
    if transform == "tint":
        return _hex(tuple(c + (1 - c) * (1 - amount) for c in (r, g, b)))  # type: ignore[arg-type]
    if transform == "satMod":
        hue, lum, sat = colorsys.rgb_to_hls(r, g, b)
        return _hex(colorsys.hls_to_rgb(hue, lum, max(0.0, min(1.0, sat * amount))))
    return hex_color


def resolve(node: etree._Element | None, scheme: dict[str, str]) -> tuple[str, float] | None:
    """One colour element to (`#RRGGBB`, alpha), or `None` if it states no colour."""
    if node is None:
        return None

    base: str | None = None
    holder: etree._Element | None = None

    srgb = node.find(f"{{{A}}}srgbClr")
    scheme_clr = node.find(f"{{{A}}}schemeClr")
    sys_clr = node.find(f"{{{A}}}sysClr")
    prst = node.find(f"{{{A}}}prstClr")

    if srgb is not None:
        base, holder = "#" + (srgb.get("val") or "000000").upper(), srgb
    elif scheme_clr is not None:
        slot = scheme_clr.get("val") or ""
        base = scheme.get(SLOT_ALIAS.get(slot, slot))
        holder = scheme_clr
    elif sys_clr is not None and sys_clr.get("lastClr"):
        base, holder = "#" + sys_clr.get("lastClr", "").upper(), sys_clr
    elif prst is not None:
        base, holder = PRESET.get(prst.get("val") or "", "#000000"), prst

    if base is None or holder is None:
        return None

    alpha = 1.0
    for child in holder:
        name = etree.QName(child).localname
        raw = child.get("val")
        if raw is None:
            continue
        amount = int(raw) / 100000
        if name == "alpha":
            alpha = amount
        else:
            base = _apply(base, name, amount)
    return base, alpha


def fill_of(sp_pr: etree._Element | None, scheme: dict[str, str]) -> tuple[str, float] | None:
    """The shape's own fill. `noFill` is a decision, and returns `None` like "unstated" —
    both mean "draw nothing", which is all the renderer needs to know."""
    if sp_pr is None or sp_pr.find(f"{{{A}}}noFill") is not None:
        return None
    return resolve(sp_pr.find(f"{{{A}}}solidFill"), scheme)


def line_of(sp_pr: etree._Element | None,
            scheme: dict[str, str]) -> tuple[str, float, float] | None:
    """(colour, alpha, width in points), or `None` for no outline."""
    if sp_pr is None:
        return None
    ln = sp_pr.find(f"{{{A}}}ln")
    if ln is None or ln.find(f"{{{A}}}noFill") is not None:
        return None
    colour = resolve(ln.find(f"{{{A}}}solidFill"), scheme)
    if colour is None:
        return None
    width_emu = int(ln.get("w") or 9525)  # OOXML default is 0.75pt
    return colour[0], colour[1], width_emu / 12700


PRESET = {"black": "#000000", "white": "#FFFFFF", "red": "#FF0000", "green": "#008000",
          "blue": "#0000FF", "yellow": "#FFFF00", "gray": "#808080", "grey": "#808080"}
