"""Built-in templates — a theme to start a deck from when there is nothing to borrow.

`serve --from company.pptx` is the honest way to start a deck, and it stays the primary one:
a real organisation has a real file, and matching it is worth more than any palette shipped
here. This is for the other case — somebody with nothing, who would otherwise get the
harness default on every deck they ever make.

**A template here is a theme, not a `.pptx`.** That is the whole design, and the numbers make
the argument: an authored theme is ~1.5 kB of readable JSON that states every value, where a
`.pptx` carrying the same look is ~27 kB of binary from which extraction can read the palette
and the faces and must *infer* nine other fields — the type scale, the spacing ramp, the grid
columns, half the semantic palette. Shipping decks as templates would mean shipping values
the harness had to guess, and then telling the user through `theme.inferred` to go and
correct our own guesses.

So each file states everything, `source` is `authored`, and `inferred` is empty. What a
template does *not* carry is slide content: a starter outline is a playbook's job
(`narrative-arc`), not a palette's.

Font stacks name real fallbacks on purpose. A theme that asks for one face installed on one
operating system is a theme that silently becomes something else elsewhere — the harness
reports the substitution, but a shipped template should not need it to.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .document import Theme


class TemplateError(RuntimeError):
    """A template that does not exist, or a file that will not load as one."""


@dataclass(frozen=True)
class Template:
    name: str
    description: str
    theme: Theme

    def summary(self) -> str:
        brand = self.theme.palette.get("brand", "")
        display = self.theme.type.families.get("display", "").split(",")[0]
        return f"{brand}  {display}"


def root() -> Path:
    """Where the templates live — beside the package, versioned with it.

    Same argument as `skills/`: a template resolves against the component catalog and the
    type roles that shipped with it, and one describing a theme two releases old would be
    worse than none.
    """
    return Path(__file__).resolve().parent.parent / "templates"


def _parse(path: Path) -> Template:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateError(f"{path.name} is not readable JSON: {exc}") from exc

    body = blob.get("theme")
    if not isinstance(body, dict):
        raise TemplateError(f"{path.name} has no `theme` object")
    try:
        theme = Theme.model_validate(body)
    except Exception as exc:  # pydantic's own message names the field
        raise TemplateError(f"{path.name} is not a valid theme: {exc}") from exc

    return Template(name=path.stem, description=str(blob.get("description", "")),
                    theme=theme)


def load(name: str, where: Path | None = None) -> Theme:
    """One template's theme, by name."""
    path = (where or root()) / f"{name}.json"
    if not path.is_file():
        raise TemplateError(
            f"no template {name!r}; have {', '.join(n.name for n in catalog(where)) or 'none'}"
        )
    return _parse(path).theme


def catalog(where: Path | None = None) -> list[Template]:
    """Every readable template, sorted by name.

    A malformed file is skipped rather than fatal, for the same reason a malformed skill is:
    the harness's job is presentations, and this is an enhancement to it. `check` is the
    strict view, and the test suite uses it so a broken template cannot ship quietly.
    """
    found = []
    for path in sorted((where or root()).glob("*.json")):
        try:
            found.append(_parse(path))
        except TemplateError:
            continue
    return found


def check(where: Path | None = None) -> dict[str, list[str]]:
    """Every template, with whatever is wrong with it. Empty lists mean it is sound.

    Contrast is the one that matters. The theme is validated once at load so managed slides
    *cannot* fail contrast later (DESIGN §2.6); a shipped template that failed validation
    would retire that guarantee for everyone who started from it.
    """
    from .theme_default import validate_theme

    out: dict[str, list[str]] = {}
    for path in sorted((where or root()).glob("*.json")):
        try:
            template = _parse(path)
        except TemplateError as exc:
            out[path.stem] = [str(exc)]
            continue
        problems = list(validate_theme(template.theme))
        if template.theme.inferred:
            problems.append(
                "an authored template states its values; inferred should be empty, not "
                + ", ".join(template.theme.inferred)
            )
        if template.theme.source != "authored":
            problems.append(f"source is {template.theme.source!r}, not 'authored'")
        if not template.description.strip():
            problems.append("no description; the catalog line would be blank")
        out[template.name] = problems
    return out
