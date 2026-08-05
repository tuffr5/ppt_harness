"""Rebuild `ppt_harness/components/iconset/paths.json` from a Tabler checkout.

Run against a `git clone --depth 1 https://github.com/tabler/tabler-icons`:

    python scripts/vendor_icons.py /path/to/tabler-icons

Only the curated subset below is vendored. The whole library is 5,130 icons and a deck
needs about a hundred and thirty concepts; carrying the rest would be a megabyte of path
data nothing in the catalog can name.

What the script does that matters: it **normalises every path to `M`/`L`/`C`/`Z`**. Tabler
draws circles and rounded corners with SVG elliptic arcs (`A`), which OOXML's `a:arcTo`
expresses with a different parametrisation — centre, radii, start and sweep angle, against
SVG's endpoint form. Converting arcs at vendor time rather than at write time means the
exporter's path reader is thirty lines and has no trigonometry in it, and — more
importantly — the preview and the exporter consume the *same* normalised string, so they
cannot disagree about where a curve goes.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path

#: Harness name -> Tabler icon file. The name is what a model writes on a slide, so it is
#: the *concept* ("growth") rather than the drawing ("trending-up"): a deck says what it
#: means and the icon set is an implementation detail we can re-source.
CURATED: dict[str, str] = {
    # -- trend and performance
    "growth": "trending-up",
    "decline": "trending-down",
    "steady": "arrows-horizontal",
    "increase": "arrow-up-right",
    "decrease": "arrow-down-right",
    "speed": "gauge",
    "activity": "activity",
    "momentum": "bolt",
    # -- data
    "chart_line": "chart-line",
    "chart_bar": "chart-bar",
    "chart_pie": "chart-pie",
    "histogram": "chart-histogram",
    "scatter": "chart-dots",
    "database": "database",
    "server": "server",
    "table": "table",
    "cloud": "cloud",
    "upload": "cloud-upload",
    "download": "cloud-download",
    # -- people
    "user": "user",
    "users": "users",
    "team": "users-group",
    "customer": "user-check",
    "hire": "user-plus",
    "partner": "friends",
    # -- organisation
    "company": "building",
    "bank": "building-bank",
    "store": "building-store",
    "community": "building-community",
    "briefcase": "briefcase",
    # -- money
    "revenue": "currency-dollar",
    "coin": "coin",
    "cash": "cash",
    "card": "credit-card",
    "wallet": "wallet",
    "invoice": "receipt",
    "savings": "pig-money",
    "price": "tag",
    "discount": "rosette-discount-check",
    # -- time
    "clock": "clock",
    "calendar": "calendar",
    "schedule": "calendar-event",
    "deadline": "hourglass",
    "history": "history",
    "alarm": "alarm",
    # -- risk
    "warning": "alert-triangle",
    "risk": "alert-circle",
    "issue": "exclamation-circle",
    "vulnerability": "shield-exclamation",
    "bug": "bug",
    "incident": "flame",
    # -- goals
    "target": "target",
    "precision": "crosshair",
    "milestone": "flag",
    "goal": "flag-3",
    "launch": "rocket",
    "win": "trophy",
    "award": "award",
    "medal": "medal",
    "star": "star",
    "idea": "bulb",
    # -- process
    "route": "route",
    "branch": "git-branch",
    "refresh": "refresh",
    "repeat": "repeat",
    "shuffle": "arrows-shuffle",
    "stack": "stack",
    "progress": "progress",
    "checklist": "checklist",
    "tasks": "list-check",
    # -- documents
    "document": "file-text",
    "file": "file",
    "report": "file-analytics",
    "folder": "folder",
    "folder_open": "folder-open",
    "library": "books",
    "notes": "notes",
    "clipboard": "clipboard",
    "plan": "clipboard-list",
    # -- place
    "location": "map-pin",
    "map": "map",
    "global": "world",
    "compass": "compass",
    # -- communication
    "email": "mail",
    "message": "message",
    "chat": "message-circle",
    "phone": "phone",
    "call": "phone-call",
    "send": "send",
    "announce": "speakerphone",
    "microphone": "microphone",
    "video": "video",
    "presentation": "presentation",
    # -- security
    "lock": "lock",
    "unlock": "lock-open",
    "shield": "shield",
    "secure": "shield-check",
    "key": "key",
    "identity": "fingerprint",
    "visible": "eye",
    "hidden": "eye-off",
    "certificate": "certificate",
    # -- quality
    "check": "circle-check",
    "tick": "check",
    "approve": "thumb-up",
    "reject": "thumb-down",
    # -- finding things
    "search": "search",
    "zoom": "zoom-in",
    "filter": "filter",
    # -- configuration
    "settings": "settings",
    "adjust": "adjustments",
    "tool": "tool",
    "tools": "tools",
    "integration": "plug",
    "module": "puzzle",
    "link": "link",
    # -- technology
    "laptop": "device-laptop",
    "mobile": "device-mobile",
    "cpu": "cpu",
    "code": "code",
    "terminal": "terminal-2",
    "ai": "brain",
    # -- operations
    "battery": "battery",
    "sustainability": "leaf",
    "recycle": "recycle",
    "health": "heartbeat",
    "heart": "heart",
    "balance": "scale",
    "measure": "ruler",
    "package": "package",
    "shipping": "truck",
    "cart": "shopping-cart",
    "ticket": "ticket",
    # -- signals
    "bell": "bell",
    "bookmark": "bookmark",
    "pin": "pin",
    "support": "lifebuoy",
    "help": "help-circle",
    "info": "info-circle",
    "question": "question-mark",
    "add": "plus",
    "remove": "minus",
    "close": "x",
    "cancel": "circle-x",
    "highlight": "sparkles",
}

NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
COMMAND = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])")
PATH_D = re.compile(r'<path[^>]*\bd="([^"]+)"')


def _tokens(d: str) -> list[str]:
    return [t for t in COMMAND.split(d) if t.strip(", \n\t")]


def _numbers(chunk: str) -> list[float]:
    return [float(n) for n in NUMBER.findall(chunk)]


def _arc_to_cubics(x0, y0, rx, ry, phi, large, sweep, x, y):
    """SVG endpoint arc -> a list of cubic segments. F.6 of the SVG 1.1 spec."""
    if rx == 0 or ry == 0 or (x0 == x and y0 == y):
        return [(x, y, x, y, x, y)]
    rx, ry = abs(rx), abs(ry)
    rad = math.radians(phi)
    cos_p, sin_p = math.cos(rad), math.sin(rad)
    dx2, dy2 = (x0 - x) / 2.0, (y0 - y) / 2.0
    x1 = cos_p * dx2 + sin_p * dy2
    y1 = -sin_p * dx2 + cos_p * dy2
    # Scale the radii up when they are too small to span the chord (F.6.6).
    lam = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry)
    if lam > 1:
        rx *= math.sqrt(lam)
        ry *= math.sqrt(lam)
    denom = (rx * rx * y1 * y1) + (ry * ry * x1 * x1)
    num = (rx * rx * ry * ry) - denom
    factor = math.sqrt(max(0.0, num / denom)) if denom else 0.0
    if large == sweep:
        factor = -factor
    cx1 = factor * rx * y1 / ry
    cy1 = -factor * ry * x1 / rx
    cx = cos_p * cx1 - sin_p * cy1 + (x0 + x) / 2.0
    cy = sin_p * cx1 + cos_p * cy1 + (y0 + y) / 2.0

    def angle(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        norm = math.hypot(ux, uy) * math.hypot(vx, vy)
        value = math.acos(max(-1.0, min(1.0, dot / norm))) if norm else 0.0
        return -value if (ux * vy - uy * vx) < 0 else value

    theta = angle(1, 0, (x1 - cx1) / rx, (y1 - cy1) / ry)
    delta = angle((x1 - cx1) / rx, (y1 - cy1) / ry, (-x1 - cx1) / rx, (-y1 - cy1) / ry)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi

    def at(t: float) -> tuple[float, float]:
        c, s = math.cos(t), math.sin(t)
        return (cx + rx * c * cos_p - ry * s * sin_p,
                cy + rx * c * sin_p + ry * s * cos_p)

    def slope(t: float) -> tuple[float, float]:
        c, s = math.cos(t), math.sin(t)
        return (-rx * s * cos_p - ry * c * sin_p,
                -rx * s * sin_p + ry * c * cos_p)

    # A cubic approximates at most a quarter turn to well within a device pixel.
    count = max(1, math.ceil(abs(delta) / (math.pi / 2)))
    step = delta / count
    alpha = 4.0 / 3.0 * math.tan(step / 4.0)
    out = []
    for i in range(count):
        a0 = theta + i * step
        a1 = a0 + step
        p0x, p0y = at(a0)
        p3x, p3y = at(a1)
        d0x, d0y = slope(a0)
        d1x, d1y = slope(a1)
        out.append((p0x + alpha * d0x, p0y + alpha * d0y,
                    p3x - alpha * d1x, p3y - alpha * d1y, p3x, p3y))
    return out


def normalise(d: str) -> str:
    """One path, as absolute `M`/`L`/`C`/`Z` only."""
    out: list[str] = []
    x = y = sx = sy = 0.0
    last_c: tuple[float, float] | None = None
    last_q: tuple[float, float] | None = None
    tokens = _tokens(d)
    i = 0

    def fmt(*values: float) -> str:
        return " ".join(f"{v:.3f}".rstrip("0").rstrip(".") or "0" for v in values)

    while i < len(tokens):
        cmd = tokens[i]
        args = _numbers(tokens[i + 1]) if i + 1 < len(tokens) and not COMMAND.fullmatch(
            tokens[i + 1]) else []
        i += 2 if args else 1
        rel = cmd.islower()
        up = cmd.upper()

        if up == "Z":
            out.append("Z")
            x, y = sx, sy
            last_c = last_q = None
            continue

        step = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}[up]
        for k in range(0, len(args), step):
            a = args[k:k + step]
            if len(a) < step:
                break
            if up == "M":
                x, y = (x + a[0], y + a[1]) if rel else (a[0], a[1])
                sx, sy = x, y
                out.append("M " + fmt(x, y))
                up = "L"  # subsequent pairs of a moveto are implicit linetos
                last_c = last_q = None
            elif up == "L":
                x, y = (x + a[0], y + a[1]) if rel else (a[0], a[1])
                out.append("L " + fmt(x, y))
                last_c = last_q = None
            elif up == "H":
                x = x + a[0] if rel else a[0]
                out.append("L " + fmt(x, y))
                last_c = last_q = None
            elif up == "V":
                y = y + a[0] if rel else a[0]
                out.append("L " + fmt(x, y))
                last_c = last_q = None
            elif up in ("C", "S"):
                if up == "C":
                    c1 = (x + a[0], y + a[1]) if rel else (a[0], a[1])
                    c2 = (x + a[2], y + a[3]) if rel else (a[2], a[3])
                    ex, ey = (x + a[4], y + a[5]) if rel else (a[4], a[5])
                else:
                    c1 = (2 * x - last_c[0], 2 * y - last_c[1]) if last_c else (x, y)
                    c2 = (x + a[0], y + a[1]) if rel else (a[0], a[1])
                    ex, ey = (x + a[2], y + a[3]) if rel else (a[2], a[3])
                out.append("C " + fmt(c1[0], c1[1], c2[0], c2[1], ex, ey))
                last_c, last_q = c2, None
                x, y = ex, ey
            elif up in ("Q", "T"):
                if up == "Q":
                    q = (x + a[0], y + a[1]) if rel else (a[0], a[1])
                    ex, ey = (x + a[2], y + a[3]) if rel else (a[2], a[3])
                else:
                    q = (2 * x - last_q[0], 2 * y - last_q[1]) if last_q else (x, y)
                    ex, ey = (x + a[0], y + a[1]) if rel else (a[0], a[1])
                c1 = (x + 2.0 / 3.0 * (q[0] - x), y + 2.0 / 3.0 * (q[1] - y))
                c2 = (ex + 2.0 / 3.0 * (q[0] - ex), ey + 2.0 / 3.0 * (q[1] - ey))
                out.append("C " + fmt(c1[0], c1[1], c2[0], c2[1], ex, ey))
                last_q, last_c = q, c2
                x, y = ex, ey
            elif up == "A":
                ex, ey = (x + a[5], y + a[6]) if rel else (a[5], a[6])
                for c1x, c1y, c2x, c2y, px, py in _arc_to_cubics(
                        x, y, a[0], a[1], a[2], int(a[3]), int(a[4]), ex, ey):
                    out.append("C " + fmt(c1x, c1y, c2x, c2y, px, py))
                x, y = ex, ey
                last_c = last_q = None
    return " ".join(out)


def main(root: Path, out: Path) -> None:
    source = root / "icons" / "outline"
    if not source.is_dir():
        raise SystemExit(f"no Tabler outline icons under {source}")
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()

    table: dict[str, str] = {}
    for name, tabler in sorted(CURATED.items()):
        svg = (source / f"{tabler}.svg").read_text()
        others = re.findall(r"<(circle|rect|line|polyline|polygon|ellipse)\b", svg)
        if others:
            raise SystemExit(f"{tabler}: non-path elements {set(others)}")
        paths = PATH_D.findall(svg)
        if not paths:
            raise SystemExit(f"{tabler}: no path data")
        table[name] = " ".join(normalise(d) for d in paths)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {
            "source": "https://github.com/tabler/tabler-icons",
            "commit": commit,
            "style": "outline",
            "licence": "MIT",
            "view_box": 24,
            "stroke": 2,
            "paths": table,
        },
        indent=1, sort_keys=False) + "\n")
    print(f"{len(table)} icons -> {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    here = Path(__file__).resolve().parent.parent
    main(Path(sys.argv[1]), here / "ppt_harness" / "components" / "iconset" / "paths.json")
