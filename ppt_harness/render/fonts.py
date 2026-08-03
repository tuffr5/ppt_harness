"""Font resolution — the foundation under every budget.

Budgets are enforced in advance width against *real* font metrics, so the first question is
always "which file will actually render this string". Getting that wrong makes every
downstream number confidently incorrect, which is worse than having no number.

A CSS stack resolves per script, not per string: `'Aptos', '等线', sans-serif` means Latin
runs come from Aptos and Han runs from 等线. Measuring Han text with Aptos's metrics
misprices it by roughly 2x — exactly the error DESIGN §3.1 warns about.
"""

from __future__ import annotations

import functools
import logging
import sys
import unicodedata
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont

# Scanning a whole system font directory turns up faces with minor table defects. They are
# still perfectly measurable, and the warnings are noise on every single import.
logging.getLogger("fontTools").setLevel(logging.ERROR)

FONT_DIRS = {
    "darwin": ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
               "/Library/Fonts", "~/Library/Fonts"],
    "linux": ["/usr/share/fonts", "/usr/local/share/fonts", "~/.local/share/fonts",
              "~/.fonts"],
    "win32": ["C:/Windows/Fonts"],
}

GENERIC = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui"}

#: Last-resort faces per script, tried in order when the stack names nothing installed.
FALLBACK = {
    "latin": ["Helvetica", "Arial", "Helvetica Neue", "DejaVu Sans", "Liberation Sans"],
    "han": ["PingFang SC", "Hiragino Sans GB", "Songti SC", "STHeiti", "Arial Unicode MS",
            "Noto Sans CJK SC", "SimSun"],
    "kana": ["Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic",
             "Noto Sans CJK JP", "Arial Unicode MS"],
    "hangul": ["Apple SD Gothic Neo", "AppleGothic", "Malgun Gothic",
               "Noto Sans CJK KR", "Arial Unicode MS"],
}


class FontNotFound(RuntimeError):
    pass


# ------------------------------------------------------------------------ scanning


@functools.lru_cache(maxsize=1)
def _index() -> dict[str, Path]:
    """family name (lowercased) -> font file. Built once per process.

    Ranked, not first-wins. `Arial Bold.ttf` legitimately reports the typographic family
    "Arial" (name ID 16), so a naive scan resolves body text to a bold face and overstates
    every width. The regular upright face wins.
    """
    best: dict[str, tuple[int, Path]] = {}
    for raw in FONT_DIRS.get(sys.platform, FONT_DIRS["linux"]):
        root = Path(raw).expanduser()
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in (".ttf", ".otf", ".ttc", ".otc"):
                continue
            for name, rank in _families(path):
                key = name.lower()
                if key not in best or rank < best[key][0]:
                    best[key] = (rank, path)
    return {name: path for name, (_, path) in best.items()}


#: Lower is better. A face is only as good as its distance from regular upright.
def _rank(subfamily: str) -> int:
    s = subfamily.lower()
    if s in ("regular", "book", "roman", "normal", ""):
        return 0
    penalty = 1
    for token, cost in (("italic", 4), ("oblique", 4), ("bold", 3), ("black", 5),
                        ("heavy", 5), ("light", 2), ("thin", 5), ("medium", 2),
                        ("semibold", 3), ("condensed", 6), ("expanded", 6)):
        if token in s:
            penalty += cost
    return penalty


def _families(path: Path) -> list[tuple[str, int]]:
    """(family, rank) pairs a file provides, without fully parsing it.

    `.ttc` collections hold several faces; a missing or malformed name table is a bad font,
    not a crash — skip it and keep indexing.
    """
    try:
        if path.suffix.lower() in (".ttc", ".otc"):
            fonts = list(TTCollection(str(path), lazy=True).fonts)
        else:
            fonts = [TTFont(str(path), lazy=True, fontNumber=0)]
    except Exception:
        return []

    out: list[tuple[str, int]] = []
    for font in fonts:
        try:
            records = {r.nameID: r.toUnicode() for r in font["name"].names
                       if r.nameID in (1, 2, 16, 17)}
        except Exception:
            continue
        subfamily = records.get(17) or records.get(2) or ""
        rank = _rank(subfamily)
        for name_id in (16, 1):
            value = records.get(name_id)
            if value:
                out.append((value, rank))
    return out


def parse_stack(stack: str) -> list[str]:
    """Split a CSS font stack into family names, dropping generics."""
    out = []
    for part in stack.split(","):
        name = part.strip().strip("'\"")
        if name and name.lower() not in GENERIC:
            out.append(name)
    return out


def find(family: str) -> Path | None:
    """Exact family match, then the shortest name that extends it.

    System faces are frequently installed under a weight-qualified name — asking for
    "Hiragino Sans GB" must find "Hiragino Sans GB W3", or CJK silently falls back to a
    pan-Unicode face whose metrics are not the ones PowerPoint will use.
    """
    index = _index()
    key = family.lower()
    if key in index:
        return index[key]
    prefixed = [n for n in index if n.startswith(key + " ")]
    return index[min(prefixed, key=len)] if prefixed else None


# -------------------------------------------------------------------------- scripts


def script_of(char: str) -> str:
    """Coarse script bucket. Only needs to be fine enough to pick a face and a width class."""
    code = ord(char)
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF:
        return "han"
    if 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF:
        return "kana"
    if 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF:
        return "hangul"
    if 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFF60:
        return "han"  # CJK punctuation and fullwidth forms travel with Han
    return "latin"


def runs(text: str) -> list[tuple[str, str]]:
    """Split into (script, substring) runs so each is measured with the right face."""
    if not text:
        return []
    out: list[tuple[str, str]] = []
    current = script_of(text[0])
    start = 0
    for i, ch in enumerate(text[1:], 1):
        # Whitespace and neutrals stay with the run they interrupt; splitting on them
        # would fragment Latin prose into one run per word for no benefit.
        if ch.isspace() or (unicodedata.category(ch).startswith("P") and script_of(ch) == "latin"):
            continue
        kind = script_of(ch)
        if kind != current:
            out.append((current, text[start:i]))
            current, start = kind, i
    out.append((current, text[start:]))
    return out


@functools.lru_cache(maxsize=256)
def resolve(stack: str, script: str) -> Path:
    """Pick the file that will actually render `script` text given this CSS stack.

    Order: families named in the stack that both exist *and* cover the script, then the
    per-script fallbacks. Coverage matters — Aptos is installed-and-named but has no Han
    glyphs, and measuring Han with it would silently produce Latin-width numbers.
    """
    probe = {"han": "\u4e2d", "kana": "\u3042", "hangul": "\uac00", "latin": "A"}[script]

    for family in parse_stack(stack):
        path = find(family)
        if path and _covers(path, family, probe):
            return path
    for family in FALLBACK[script]:
        path = find(family)
        if path and _covers(path, family, probe):
            return path
    for family in FALLBACK["latin"]:
        path = find(family)
        if path:
            return path
    raise FontNotFound(f"no installed font renders {script!r} for stack {stack!r}")


@functools.lru_cache(maxsize=1024)
def _covers(path: Path, family: str, char: str) -> bool:
    try:
        font = load(path, family)
        return ord(char) in font.getBestCmap()
    except Exception:
        return False


@functools.lru_cache(maxsize=128)
def load(path: Path, family: str | None = None) -> TTFont:
    """Open a face, picking the right member of a `.ttc` by family name."""
    if path.suffix.lower() in (".ttc", ".otc"):
        coll = TTCollection(str(path), lazy=True)
        if family:
            for font in coll.fonts:
                names = {r.toUnicode() for r in font["name"].names if r.nameID in (1, 16)}
                if any(n.lower() == family.lower() for n in names):
                    return font
        return coll.fonts[0]
    return TTFont(str(path), lazy=True)


def embeddable(family: str) -> bool:
    """OS/2 fsType licensing bits. 2 = restricted, 0x0200 = bitmap-embedding only.

    Reported rather than enforced: a non-embeddable theme font is a wider fidelity margin,
    not a hard failure.
    """
    path = find(family)
    if path is None:
        return False
    try:
        fs_type = load(path, family)["OS/2"].fsType
    except Exception:
        return False
    return not (fs_type & 0x0002 or fs_type & 0x0200)


def clear_cache() -> None:
    for fn in (_index, resolve, load, _covers):
        fn.cache_clear()
