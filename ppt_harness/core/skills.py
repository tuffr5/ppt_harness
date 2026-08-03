"""Skills — DESIGN §9.

A skill is a markdown file with frontmatter. Two kinds, and the distinction is about *who
decides they are needed*:

- A **playbook** is something a user could ask for by name — "write me a deck from this
  brief". It is offered, listed, and invoked deliberately, which is why MCP exposes it as a
  prompt rather than a tool.
- An **invariant** is something the user should never have to ask for. It is injected when
  its trigger matches, and its content is a rule rather than a recipe.

The bar for writing one at all is deliberately high. **A rule that can be enforced in code
does not belong in a skill.** The export-fidelity rules live in `io/writer_assertions.py`,
the repair ladder in `core/repair.py`, and the writing rules in the agent's system prompt —
each enforced rather than described, which is strictly better than a document a model may
or may not read. What is left for a skill is genuine *procedure*: multi-step work, needed
occasionally, too long to keep in an always-on prompt.

The parser is deliberately small — `key: value` frontmatter between `---` fences, no YAML
dependency, no nesting. A skill that needed a richer format would be a skill doing too much.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Kind = Literal["playbook", "invariant"]

FENCE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
LIST = re.compile(r"^\[(.*)\]$")


class SkillError(RuntimeError):
    """A skill file the loader will not pretend to understand."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    """Trigger text. Written around the *symptom* as well as the task, because the model
    matching on it has the user's words, not the author's intent."""
    kind: Kind
    body: str
    path: Path | None = None
    arguments: tuple[str, ...] = ()
    """Names a caller may fill in. Surfaced to MCP so a host can prompt for them."""
    tools: tuple[str, ...] = ()
    """Tools this playbook depends on, declared rather than inferred.

    Scanning the prose for tool names cannot work: a skill discussing `opaque` shapes or a
    `freeform` slide is naming *concepts*, and a check that cannot tell those from tools
    either misses real staleness or cries wolf forever. Declared, it is exact — a renamed
    tool fails the skill that needed it, by name.
    """

    def prompt(self, **values: str) -> str:
        """The body with `{{name}}` placeholders filled.

        An unknown placeholder is left as it stands rather than blanked: a visible
        `{{brief}}` in the output tells the user what they failed to supply, where an empty
        string would silently produce a playbook missing its subject.
        """
        out = self.body
        for key, value in values.items():
            if value:
                out = out.replace("{{" + key + "}}", value)
        return out

    def mcp(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "arguments": [{"name": a, "description": f"{a} for this playbook",
                           "required": False} for a in self.arguments],
        }


def parse(text: str, path: Path | None = None) -> Skill:
    match = FENCE.match(text)
    if match is None:
        raise SkillError(f"{path or 'skill'} has no frontmatter; expected a --- fenced block")

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip("'\"")

    missing = [k for k in ("name", "description", "kind") if not meta.get(k)]
    if missing:
        raise SkillError(f"{path or 'skill'} is missing frontmatter: {', '.join(missing)}")
    if meta["kind"] not in ("playbook", "invariant"):
        raise SkillError(f"{path or 'skill'}: kind must be playbook or invariant, "
                         f"not {meta['kind']!r}")

    return Skill(
        name=meta["name"],
        description=meta["description"],
        kind=meta["kind"],  # type: ignore[arg-type]
        body=text[match.end():].strip(),
        path=path,
        arguments=_list(meta.get("arguments", "")),
        tools=_list(meta.get("tools", "")),
    )


def _list(value: str) -> tuple[str, ...]:
    """`[a, b, c]` as a tuple. Anything else is empty rather than an error."""
    listed = LIST.match(value.strip())
    if not listed:
        return ()
    return tuple(item.strip().strip("'\"") for item in listed.group(1).split(",")
                 if item.strip())


def skills_root() -> Path:
    """Where the shipped skills live — beside the package, versioned with it.

    DESIGN §9: skills version alongside the tool surface they describe. A skill that
    described a tool set two releases old would be worse than none.
    """
    return Path(__file__).resolve().parent.parent / "skills"


def load(root: Path | None = None) -> list[Skill]:
    """Every readable skill, sorted by name.

    A malformed file is skipped rather than fatal. One bad skill must not stop a deck from
    opening — the harness's job is presentations, and this is an enhancement to it.
    """
    root = root or skills_root()
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.rglob("*.md")):
        try:
            found.append(parse(path.read_text(encoding="utf-8"), path))
        except (SkillError, OSError):
            continue
    return sorted(found, key=lambda s: s.name)


def playbooks(root: Path | None = None) -> list[Skill]:
    return [s for s in load(root) if s.kind == "playbook"]


def invariants(root: Path | None = None) -> list[Skill]:
    return [s for s in load(root) if s.kind == "invariant"]


def get(name: str, root: Path | None = None) -> Skill:
    for skill in load(root):
        if skill.name == name:
            return skill
    raise SkillError(f"no skill {name!r}; have {[s.name for s in load(root)]}")


def catalog(root: Path | None = None) -> str:
    """One line per playbook, for the always-on context.

    Names and triggers only. The body is level 5 of the context pyramid — a playbook can run
    to a thousand words, and paying that on every turn to have it available on one would be
    the same mistake as inlining the full slot schemas.
    """
    available = playbooks(root)
    if not available:
        return ""
    lines = ["Playbooks you can follow when the request matches (ask for one by name):"]
    lines += [f"  {s.name}: {_gist(s.description)}" for s in available]
    return "\n".join(lines)


def _gist(description: str, limit: int = 120) -> str:
    """The first sentence of a trigger, for the always-on line.

    A `description` is written long on purpose — it carries the symptoms a user might
    describe, and matching on symptoms is what makes a skill fire when it should. But the
    catalog is level 2, paid on every turn, so the line takes the first sentence and the
    host fetches the rest when it lists prompts.
    """
    head = description.split(". ")[0].strip().rstrip(".")
    return head if len(head) <= limit else head[:limit - 1].rstrip() + "…"
