"""ppt-harness — create, edit, and inspect presentations by talking to an LLM.

Two entry points cover almost everything:

    from ppt_harness import Session, dispatch

    session = Session.open("deck.pptx")       # imported: every slide lands freeform
    session = Session.blank("New deck")       # generated: managed slides only

    dispatch(session, "get_outline")
    dispatch(session, "set_text", {"target": "s1/s1_sh2", "text": "..."})

`dispatch` is the same path the model, the MCP server, and the CLI all take, so anything
reachable from a conversation is reachable from a script.

The subpackages carry no `__init__.py`; they are namespace packages, and an init file that
only restates the directory name is a file to keep in sync for nothing.
"""

from __future__ import annotations

__version__ = "0.0.1"

__all__ = ["Session", "__version__", "dispatch", "tools"]


def __getattr__(name: str):
    """Resolve the public surface lazily.

    Importing the tool layer pulls in python-pptx and builds the font index, which is real
    work. A caller that only wants `__version__` should not pay for it.
    """
    if name == "Session":
        from .core.session import Session

        return Session
    if name in ("dispatch", "tools"):
        from .tools import router

        return getattr(router, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
