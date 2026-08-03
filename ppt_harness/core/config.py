"""Configuration from the environment, and from a `.env` beside the project.

A `.env` file exists so an API key does not have to live in a shell profile — where it
leaks into every process — or on a command line, where it lands in shell history. It is
read once at startup by the entry points, never by the library, so importing `ppt_harness`
has no side effects.

**Real environment variables always win.** A file that could silently override
`ANTHROPIC_API_KEY` would make a stale checkout charge the wrong account, and the failure
would be invisible.
"""

from __future__ import annotations

import os
from pathlib import Path

FILENAME = ".env"


def find_env(start: Path | None = None) -> Path | None:
    """The nearest `.env`, searching upward from `start` to the filesystem root."""
    here = (start or Path.cwd()).resolve()
    for directory in [here, *here.parents]:
        candidate = directory / FILENAME
        if candidate.is_file():
            return candidate
    return None


def parse(text: str) -> dict[str, str]:
    """`KEY=value` lines. Comments and blanks ignored; quotes stripped; no interpolation.

    Deliberately not a shell parser — a config file that can run commands is a liability,
    and every real use here is a flat secret.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()

        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        else:
            # An unquoted trailing comment is not part of the value. People write
            # `MODEL=x  # or y` and mean the model to be `x`; keeping the rest produces a
            # model name no endpoint has ever heard of, and a 404 that blames the endpoint.
            value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if key:
            values[key] = value
    return values


def load_env(start: Path | None = None) -> list[str]:
    """Populate the environment from the nearest `.env`. Returns the names it set."""
    path = find_env(start)
    if path is None:
        return []
    applied = []
    for key, value in parse(path.read_text(encoding="utf-8", errors="replace")).items():
        if key not in os.environ:  # a real variable is never overridden
            os.environ[key] = value
            applied.append(key)
    return applied
