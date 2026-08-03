"""Skills — `core/skills.py`, DESIGN §9, and their delivery path through MCP.

A skill is the one part of the harness that is *only* words. Everything else here is
enforced — budgets refuse, schemas exclude coordinates, the exporter asserts. So the things
worth defending are the ones that keep words from becoming a liability: that a malformed
one cannot stop a deck opening, that the always-on cost stays a line rather than a page, and
that a host can actually invoke what the catalog advertises.

The last one is the reason this file exists at all: the previous skill shipped as markdown
that no code read and no host could reach, and nothing failed when it was deleted.
"""

from __future__ import annotations

import json

import pytest

from ppt_harness.adapters.mcp_server import Server
from ppt_harness.core import skills
from ppt_harness.core.session import Session

GOOD = """\
---
name: example
kind: playbook
description: Does a thing. Second sentence that the catalog should not carry.
arguments: [subject]
---

# Example

Body about {{subject}}.
"""


def _write(root, name: str, text: str):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------------ parsing


def test_a_skill_parses_into_its_parts() -> None:
    skill = skills.parse(GOOD)
    assert skill.name == "example"
    assert skill.kind == "playbook"
    assert skill.arguments == ("subject",)
    assert skill.body.startswith("# Example")
    assert "---" not in skill.body, "frontmatter belongs to the loader, not the model"


def test_frontmatter_is_required() -> None:
    with pytest.raises(skills.SkillError, match="frontmatter"):
        skills.parse("# Just a heading\n")


def test_a_missing_field_names_itself() -> None:
    with pytest.raises(skills.SkillError, match="description"):
        skills.parse("---\nname: x\nkind: playbook\n---\nbody\n")


def test_an_invented_kind_is_refused() -> None:
    """Playbook and invariant differ in *who decides they are needed*, so a third kind is
    not a new option — it is an author who has not decided."""
    with pytest.raises(skills.SkillError, match="playbook or invariant"):
        skills.parse("---\nname: x\nkind: guideline\ndescription: d\n---\nbody\n")


# ------------------------------------------------------------------- placeholders


def test_arguments_are_filled_in() -> None:
    assert "Body about churn." in skills.parse(GOOD).prompt(subject="churn.")


def test_an_unsupplied_argument_stays_visible() -> None:
    """Blanking it would produce a playbook silently missing its subject; leaving the
    placeholder tells the user exactly what they failed to pass."""
    assert "{{subject}}" in skills.parse(GOOD).prompt()


# --------------------------------------------------------------------- discovery


def test_a_malformed_skill_is_skipped_not_fatal(tmp_path) -> None:
    """One bad file must not stop a deck from opening. The harness's job is presentations;
    this is an enhancement to it."""
    _write(tmp_path, "good", GOOD)
    _write(tmp_path, "bad", "no frontmatter here")
    found = skills.load(tmp_path)
    assert [s.name for s in found] == ["example"]


def test_a_missing_directory_is_simply_no_skills(tmp_path) -> None:
    assert skills.load(tmp_path / "nothing-here") == []


def test_playbooks_and_invariants_are_separable(tmp_path) -> None:
    _write(tmp_path, "p", GOOD)
    _write(tmp_path, "i", GOOD.replace("name: example", "name: rule")
                             .replace("kind: playbook", "kind: invariant"))
    assert [s.name for s in skills.playbooks(tmp_path)] == ["example"]
    assert [s.name for s in skills.invariants(tmp_path)] == ["rule"]


def test_an_unknown_name_lists_what_exists(tmp_path) -> None:
    _write(tmp_path, "p", GOOD)
    with pytest.raises(skills.SkillError, match="example"):
        skills.get("nope", tmp_path)


# ----------------------------------------------------------------- the always-on cost


def test_the_catalog_carries_one_line_per_playbook(tmp_path) -> None:
    """Level 2, paid every turn. The body is level 5 — a playbook runs to a thousand words,
    and inlining it would repeat the mistake `list_components` exists to avoid."""
    _write(tmp_path, "p", GOOD)
    catalog = skills.catalog(tmp_path)
    assert "example" in catalog
    assert "Second sentence" not in catalog, "the catalog takes the first sentence only"
    assert "Body about" not in catalog, "and never the body"


def test_a_long_trigger_is_truncated_not_dropped(tmp_path) -> None:
    """A description is written long on purpose — it carries symptoms, and matching on
    symptoms is what makes a skill fire. The catalog just cannot afford all of it."""
    long = "d" * 400
    _write(tmp_path, "p", GOOD.replace("Does a thing.", long + "."))
    line = skills.catalog(tmp_path).splitlines()[-1]
    assert len(line) < 160 and line.endswith("…")


def test_no_skills_costs_no_context(tmp_path) -> None:
    assert skills.catalog(tmp_path) == ""


def test_the_context_block_stays_small_with_skills_loaded() -> None:
    from ppt_harness.core import loop

    assert len(loop.context_block(Session.blank("X"))) < 3000


# -------------------------------------------------------------- the shipped skill


def test_the_shipped_skill_loads() -> None:
    """Guards the failure this whole feature exists to fix: markdown that ships but which
    no code reads and no host can invoke."""
    assert "narrative-arc" in [s.name for s in skills.load()]


def test_every_shipped_skill_depends_only_on_tools_that_exist() -> None:
    """The check that catches a skill going stale as the tool surface moves.

    It works because a skill *declares* what it needs. Scanning the prose cannot: a playbook
    discussing `opaque` shapes or a `freeform` slide is naming concepts, and no rule
    separates those from tool names without either missing real staleness or crying wolf on
    every skill that explains itself well.
    """
    from ppt_harness.tools import router  # noqa: F401 -- registers the tools
    from ppt_harness.tools.base import REGISTRY

    for skill in skills.load():
        missing = [t for t in skill.tools if t not in REGISTRY]
        assert not missing, f"{skill.name} depends on tools that no longer exist: {missing}"


def test_a_playbook_declares_what_it_depends_on() -> None:
    """A playbook that names no tools is either not a playbook or has not been finished."""
    for skill in skills.playbooks():
        assert skill.tools, f"{skill.name} declares no tools"


def test_no_shipped_skill_ever_asks_for_a_coordinate() -> None:
    """Principle 1 reaches prose too. A playbook telling a model to nudge something by 12px
    would be asking for a tool call the API cannot express.

    Every shipped skill, not one by name: a check that guards a single file stops guarding
    anything the moment a second file is added, and does it silently.
    """
    for skill in skills.load():
        body = skill.body.lower()
        for banned in ("pixel", "px)", "inches(", "font size", "pt(", "x=", "y="):
            assert banned not in body, f"{skill.name} mentions {banned!r}"


# ------------------------------------------------------------- delivery through MCP


@pytest.fixture
def server() -> Server:
    return Server(Session.blank("Delivery"))


def _rpc(server: Server, method: str, params: dict | None = None, rid: int = 1):
    return server.handle({"jsonrpc": "2.0", "id": rid, "method": method,
                          "params": params or {}})


def test_the_server_advertises_prompts(server: Server) -> None:
    """A host that is not told the capability exists will never call `prompts/list`."""
    caps = _rpc(server, "initialize")["result"]["capabilities"]
    assert "prompts" in caps


def test_playbooks_are_listed_as_prompts(server: Server) -> None:
    listed = _rpc(server, "prompts/list")["result"]["prompts"]
    names = [p["name"] for p in listed]
    assert "narrative-arc" in names
    entry = next(p for p in listed if p["name"] == "narrative-arc")
    assert [a["name"] for a in entry["arguments"]] == ["brief", "audience", "decision"]
    assert len(entry["description"]) > 80, "the full trigger, not the catalog gist"


def test_getting_a_prompt_returns_a_usable_message(server: Server) -> None:
    result = _rpc(server, "prompts/get", {"name": "narrative-arc",
                                          "arguments": {"brief": "Q3 churn"}})["result"]
    text = result["messages"][0]["content"]["text"]
    assert "Q3 churn" in text, "arguments are filled in"
    assert "Narrative arc" in text


def test_a_prompt_arrives_knowing_which_deck_is_open(server: Server) -> None:
    """Prepending the deck context here saves the playbook spending its first turn on
    `get_outline` for something the server already knows."""
    text = _rpc(server, "prompts/get",
                {"name": "narrative-arc"})["result"]["messages"][0]["content"]["text"]
    assert "Delivery" in text


def test_an_unknown_prompt_is_the_callers_error(server: Server) -> None:
    """-32602, not -32603: the server did not break, the request was wrong."""
    error = _rpc(server, "prompts/get", {"name": "no-such-thing"})["error"]
    assert error["code"] == -32602
    assert "narrative-arc" in error["message"], "and it says what does exist"


def test_an_invariant_cannot_be_invoked_as_a_prompt(tmp_path, monkeypatch,
                                                    server: Server) -> None:
    """An invariant is injected when its trigger matches; offering it as something to run
    would misrepresent what it is."""
    _write(tmp_path, "rule", GOOD.replace("name: example", "name: rule")
                                 .replace("kind: playbook", "kind: invariant"))
    monkeypatch.setattr(skills, "skills_root", lambda: tmp_path)
    error = _rpc(server, "prompts/get", {"name": "rule"})["error"]
    assert error["code"] == -32602
    assert "invariant" in error["message"]


def test_tools_still_work_alongside_prompts(server: Server) -> None:
    """The prompts branch must not have shadowed the method the server exists for."""
    result = _rpc(server, "tools/call", {"name": "get_outline"})["result"]
    assert json.loads(result["content"][0]["text"])["deck"] == "Delivery"
