"""Shared fixtures.

Most tests run on a synthetic deck so they are fast and hermetic. The ones that genuinely
need a real package — import, theme extraction, mutating export — take `fixture_deck` and
skip when none is present, so the suite stays green on a machine without one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ppt_harness.core.session import Session
from ppt_harness.state.document import Block, Mode, Slide

ROOT = Path(__file__).resolve().parents[1]


#: Committed, small, and built by `scripts/make_demo_deck.py`. It deliberately carries a
#: native chart, a table, a group, a picture, filled autoshapes, a connector, master art
#: and a `normAutofit` confession — the parts most likely to break an exporter.
DEMO = ROOT / "tests" / "fixtures" / "demo.pptx"


@pytest.fixture(scope="session")
def fixture_deck() -> Path:
    """The deck the suite runs against.

    Fixed rather than discovered. Picking up whatever `.pptx` happened to sit in the repo
    root meant the suite tested a different deck on every machine — passing on one, skipping
    on another, and silently depending on a private 174 MB file that is not in the
    repository. `PPT_HARNESS_FIXTURE` still points it elsewhere on purpose.
    """
    env = os.environ.get("PPT_HARNESS_FIXTURE")
    if env:
        return Path(env)
    if not DEMO.exists():
        pytest.skip(f"{DEMO.relative_to(ROOT)} is missing; "
                    "run `python scripts/make_demo_deck.py`")
    return DEMO


@pytest.fixture
def imported(fixture_deck: Path) -> Session:
    return Session.open(fixture_deck)


@pytest.fixture
def blank() -> Session:
    return Session.blank("Test deck")


@pytest.fixture
def managed_slide() -> Slide:
    return Slide(
        id="sl_test",
        index=0,
        mode=Mode.MANAGED,
        layout="stack",
        blocks=[
            Block(id="bk_title", region="header", component="slide_title", variant="plain",
                  slots={"title": "A reasonable title"}),
            Block(id="bk_body", region="body", component="bullets", variant="plain",
                  slots={"items": ["First point", "Second point", "Third point"]}),
        ],
    )


@pytest.fixture
def populated(blank: Session, managed_slide: Slide) -> Session:
    blank.deck.slides.append(managed_slide)
    return blank
