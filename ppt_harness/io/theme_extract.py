"""Theme extraction — DESIGN §7 step 2.

The highest-value part of import and the easiest: it makes "add three slides to this deck"
produce slides that match, which is the most common request against an imported file and
needs no adoption at all.

What is *read* and what is *inferred* are tracked separately. `theme1.xml` genuinely
contains the palette and font families; it contains nothing about type scale, spacing, or
shape language, so those are derived and listed in `theme.inferred` for the user to correct.
Presenting a guess as a reading is the failure mode this avoids.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree

from ..state.document import EMU_PER_INCH, Grid, Theme, TypeSpec, Typography
from ..state.theme_default import contrast, default_theme

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS = {"a": A, "p": P}

#: OOXML color-scheme slot -> harness palette role. Roles are the contract; slots are not.
ROLE_FROM_SLOT = {"lt1": "bg", "lt2": "surface", "dk1": "ink", "dk2": "brand"}

#: Script -> the `<a:font script=…>` code OOXML uses. Kept so CJK does not silently fall
#: back to a Latin face whose metrics would misprice every budget by roughly 2x.
SCRIPT_CODES = {"Hans": "zh-Hans", "Hant": "zh-Hant", "Jpan": "ja", "Hang": "ko"}


class ThemeExtractionError(RuntimeError):
    pass


# ------------------------------------------------------------------------ colors


def _color(node: etree._Element | None) -> str | None:
    """Resolve one color node to `#RRGGBB`.

    `sysClr` carries the last-computed value in `lastClr`; without it the color depends on
    the viewer's OS theme and is not a color we can promise anything about.
    """
    if node is None:
        return None
    srgb = node.find(f"{{{A}}}srgbClr")
    if srgb is not None and srgb.get("val"):
        return "#" + srgb.get("val", "").upper()
    sys = node.find(f"{{{A}}}sysClr")
    if sys is not None and sys.get("lastClr"):
        return "#" + sys.get("lastClr", "").upper()
    return None


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ra, rb, strict=True))


def _hue(hex_color: str) -> float:
    r, g, b = (c / 255 for c in _rgb(hex_color))
    hi, lo = max(r, g, b), min(r, g, b)
    if hi == lo:
        return 0.0
    d = hi - lo
    if hi == r:
        h = ((g - b) / d) % 6
    elif hi == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60


def _readable_on(bg: str, dark: str, light: str) -> str:
    return dark if contrast(dark, bg) >= contrast(light, bg) else light


def _muted(ink: str, bg: str, minimum: float = 4.5) -> str:
    """Step a muted ink toward the background only as far as contrast allows.

    A theme that passes validation means managed slides *cannot* fail contrast, so this
    must not hand back a value that merely looks nice.
    """
    best = ink
    for step in range(1, 21):
        candidate = _mix(ink, bg, step * 0.025)
        if contrast(candidate, bg) < minimum:
            break
        best = candidate
    return best


def _pick_by_hue(accents: list[str], low: float, high: float, fallback: str) -> str:
    for color in accents:
        if low <= _hue(color) <= high:
            return color
    return fallback


# ------------------------------------------------------------------------- fonts


def _fonts(scheme: etree._Element, which: str) -> tuple[str, dict[str, str]]:
    node = scheme.find(f"{{{A}}}{which}")
    if node is None:
        raise ThemeExtractionError(f"theme has no {which}")
    latin = node.find(f"{{{A}}}latin")
    family = (latin.get("typeface") if latin is not None else None) or "Calibri"
    scripts = {}
    for font in node.findall(f"{{{A}}}font"):
        code = SCRIPT_CODES.get(font.get("script", ""))
        face = font.get("typeface")
        if code and face:
            scripts[code] = face
    return family, scripts


def _stack(primary: str, scripts: dict[str, str], generic: str) -> str:
    """A CSS font stack the HTML renderer and the measurer both consume, so preview and
    budget agree on which face a given script resolves to."""
    seen, ordered = {primary}, [primary]
    for face in scripts.values():
        if face not in seen:
            seen.add(face)
            ordered.append(face)
    return ", ".join(f"'{f}'" if " " in f else f for f in ordered) + f", {generic}"


# ------------------------------------------------------------------------ extract


def extract_theme(path: Path | str, theme_part: str = "ppt/theme/theme1.xml") -> Theme:
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if theme_part not in names:
            raise ThemeExtractionError(f"{path.name} has no {theme_part}")
        theme_xml = etree.fromstring(z.read(theme_part))
        pres_xml = etree.fromstring(z.read("ppt/presentation.xml"))
        master = next((n for n in sorted(names) if n.startswith("ppt/slideMasters/slideMaster")
                       and n.endswith(".xml")), None)
        master_xml = etree.fromstring(z.read(master)) if master else None
        layouts = sorted(n for n in names
                         if n.startswith("ppt/slideLayouts/slideLayout") and n.endswith(".xml"))
        layout_types = []
        for name in layouts:
            root = etree.fromstring(z.read(name))
            layout_types.append(root.get("type") or "custom")

    scheme = theme_xml.find(f".//{{{A}}}clrScheme")
    fonts = theme_xml.find(f".//{{{A}}}fontScheme")
    if scheme is None or fonts is None:
        raise ThemeExtractionError("theme is missing clrScheme or fontScheme")

    # -- palette: read what the file states, derive the rest --------------------
    slots = {slot: _color(scheme.find(f"{{{A}}}{slot}")) for slot in
             ("dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3",
              "accent4", "accent5", "accent6")}
    bg = slots["lt1"] or "#FFFFFF"
    ink = slots["dk1"] or "#000000"
    surface = slots["lt2"] or _mix(bg, ink, 0.04)
    accents = [slots[f"accent{i}"] for i in range(1, 7)]
    accents = [c for c in accents if c] or ["#004098"]
    brand = accents[0]

    inferred: list[str] = []
    palette = {
        "bg": bg,
        "surface": surface,
        # Derived but not yet consumed — see the note in `theme_default`. Still declared
        # inferred below, because the day something draws a divider this guess is what it
        # will draw with, and a guess nobody flagged is the kind that ships wrong.
        "rule": _mix(surface, ink, 0.18),
        "ink": ink,
        "ink_muted": _muted(ink, bg),
        "brand": brand,
        "brand_ink": _readable_on(brand, ink, bg),
        "accents": accents,
        "positive": _pick_by_hue(accents, 80, 165, accents[0]),
        "negative": _pick_by_hue(accents, 0, 45, accents[-1]),
    }
    inferred += ["palette.rule", "palette.ink_muted", "palette.positive", "palette.negative"]

    # -- typography: families are read, the scale is not ------------------------
    display, display_scripts = _fonts(fonts, "majorFont")
    body, body_scripts = _fonts(fonts, "minorFont")

    # -- grid: canvas is read, everything else comes off the master -------------
    size = pres_xml.find(f"{{{P}}}sldSz")
    cx = int(size.get("cx", 12192000)) if size is not None else 12192000
    cy = int(size.get("cy", 6858000)) if size is not None else 6858000
    width_px = round(cx / EMU_PER_INCH * 96)
    height_px = round(cy / EMU_PER_INCH * 96)

    margin_emu = _placeholder_margin(master_xml)
    if margin_emu is None:
        margin_px = 64
        inferred.append("grid.margin")
    else:
        margin_px = round(margin_emu / cx * width_px)

    base = default_theme()
    scale = _scale_for(base.type.scale, height_px)

    theme = Theme(
        id=f"{path.stem}-extracted",
        source="extracted",
        palette=palette,
        type=Typography(
            families={
                "display": _stack(display, display_scripts, "serif"),
                "body": _stack(body, body_scripts, "sans-serif"),
            },
            scale=scale,
            floor=base.type.floor,
        ),
        grid=Grid(
            canvas=(width_px, height_px),
            margin=margin_px,
            columns=12,
            gutter=round(margin_px / 4) or 16,
            baseline=4,
        ),
        spacing=base.spacing,
        shape=base.shape,
        layouts=sorted(set(layout_types)),
        inferred=sorted({*inferred, "type.scale", "spacing", "shape", "grid.columns",
                         "grid.gutter"}),
    )
    return theme


def _placeholder_margin(master: etree._Element | None) -> int | None:
    """The master's own content margin, in EMU.

    Read from the left edge of title and body *placeholders* only. Every `<a:off>` in the
    master is the wrong set — decorative sub-shapes inside a group sit near x=0 and would
    report a margin of a few pixels.
    """
    if master is None:
        return None
    tree = master.find(f".//{{{P}}}cSld/{{{P}}}spTree")
    if tree is None:
        return None
    lefts = []
    for sp in tree.findall(f"{{{P}}}sp"):
        ph = sp.find(f".//{{{P}}}nvSpPr/{{{P}}}nvPr/{{{P}}}ph")
        if ph is None or ph.get("type") not in (None, "title", "ctrTitle", "body", "subTitle"):
            continue
        off = sp.find(f"{{{P}}}spPr/{{{A}}}xfrm/{{{A}}}off")
        if off is not None and off.get("x") is not None:
            lefts.append(int(off.get("x", 0)))
    return min(lefts) if lefts else None


def _scale_for(base: dict[str, TypeSpec], height_px: int) -> dict[str, TypeSpec]:
    """Scale the default type ramp to the deck's canvas.

    A 4:3 deck at 960x720 needs the same *optical* size as 16:9 at 1280x720; both are 720
    tall, so height is the right dimension to key on.
    """
    factor = height_px / 720
    return {
        role: TypeSpec(
            family=spec.family,
            size=round(spec.size * factor, 1),
            weight=spec.weight,
            line=round(spec.line * factor, 1),
            track=spec.track,
        )
        for role, spec in base.items()
    }
