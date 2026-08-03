"""Paragraph and run properties — PLAN A1.

Everything here is a property OOXML already stores; the harness simply had no way to say so.
Alignment and list style live on `<a:pPr>`, hyperlinks on `<a:rPr>` — the same run mechanism
emphasis uses.

What is deliberately absent: size, face, and raw colour. Those belong to the theme, and a
per-call override is how decks drift. `restyle` will assign a *theme role* instead, which
keeps the palette meaning something.
"""

from __future__ import annotations

from typing import Any

from ..core.session import Session
from ..state import richtext
from ..state.document import Author, Mode, Shape
from .base import Diff, ToolError, integer, obj, string, tool

ALIGNMENTS = ("left", "center", "right", "justify")
BULLETS = ("none", "bullet", "number")
MAX_INDENT = 4

#: Emphasis a model may set on a span. Names match the markup it already writes.
MARKS = ("bold", "italic", "underline", "strike", "none")


def _shape(session: Session, target: str) -> Shape:
    slide, holder, slot = session.store.resolve_text_target(target)
    if slot is not None:
        raise ToolError(
            "managed_slot",
            f"{target} is a managed slot; its alignment and list style come from the "
            "component. Use set_variant, or eject the slide to edit it directly.",
        )
    if holder.opaque:
        raise ToolError("shape_opaque",
                        f"{target} is a {holder.type} the harness does not model")
    if slide.mode is not Mode.FREEFORM:
        raise ToolError("wrong_mode", f"{slide.id} is managed")
    return holder


@tool("set_align", "Align a shape's text.",
      obj({"target": string("slide/shape, as reported by get_slide"),
           "align": string("Alignment", list(ALIGNMENTS))},
          ["target", "align"]),
      gate="freeform", mutating=True)
def set_align(session: Session, target: str, align: str,
              author: Author = Author.MODEL) -> dict[str, Any]:
    shape = _shape(session, target)
    if align not in ALIGNMENTS:
        raise ToolError("unknown_align", f"{align!r} is not one of {list(ALIGNMENTS)}")
    before = shape.align

    with session.transaction(author) as turn:
        session.store.write(turn, "set_props", target, {"align": align}, author)

    return Diff(summary=f"aligned {target} {align}", target=target,
                before={"align": before}, after={"align": align},
                render=session.measure_slide(target.split("/")[0])).as_result()


@tool("set_list", "Turn a shape's text into a bulleted or numbered list, or back to prose.",
      obj({"target": string("slide/shape"),
           "kind": string("List style", list(BULLETS)),
           "level": integer(f"Nesting level, 0 to {MAX_INDENT}")},
          ["target", "kind"]),
      gate="freeform", mutating=True)
def set_list(session: Session, target: str, kind: str, level: int = 0,
             author: Author = Author.MODEL) -> dict[str, Any]:
    shape = _shape(session, target)
    if kind not in BULLETS:
        raise ToolError("unknown_list", f"{kind!r} is not one of {list(BULLETS)}")
    if not 0 <= level <= MAX_INDENT:
        raise ToolError("bad_level", f"level must be 0 to {MAX_INDENT}, not {level}")

    before = {"bullet": shape.bullet, "indent": shape.indent}
    with session.transaction(author) as turn:
        session.store.write(turn, "set_props", target,
                            {"bullet": kind, "indent": level}, author)

    return Diff(summary=f"set {target} to {kind}", target=target, before=before,
                after={"bullet": kind, "indent": level},
                render=session.measure_slide(target.split("/")[0])).as_result()


@tool("set_link", "Attach a hyperlink to a span of text, or remove one.",
      obj({"target": string("slide/shape"),
           "span": string("The exact words to link"),
           "url": string("Destination; omit or leave empty to remove the link")},
          ["target", "span"]),
      gate="freeform", mutating=True)
def set_link(session: Session, target: str, span: str, url: str = "",
             author: Author = Author.MODEL) -> dict[str, Any]:
    shape = _shape(session, target)
    if url and not url.startswith(("http://", "https://", "mailto:")):
        raise ToolError(
            "bad_url",
            f"{url!r} is not a link PowerPoint will follow; use http, https or mailto",
        )

    runs = shape.runs or richtext.parse(shape.text or "")
    updated, hits = richtext.apply_marks(runs, span, link=url)
    if not hits:
        raise ToolError("span_not_found",
                        f"{span!r} does not appear in {target}; the text is "
                        f"{(shape.text or '')[:120]!r}")

    with session.transaction(author) as turn:
        session.store.write(turn, "set_props", target,
                            {"runs": [r.model_dump(mode="json") for r in updated]}, author)

    verb = "linked" if url else "unlinked"
    return Diff(summary=f"{verb} {span!r} in {target}", target=target,
                after={"span": span, "url": url, "occurrences": hits},
                render=session.measure_slide(target.split("/")[0])).as_result()


@tool("set_emphasis", "Bold, italicise, underline or strike a span of text.",
      obj({"target": string("slide/shape"),
           "span": string("The exact words to change"),
           "mark": string("Emphasis to apply; 'none' clears it", list(MARKS)),
           "on": {"type": "boolean", "description": "Apply it, or turn it off"}},
          ["target", "span", "mark"]),
      gate="freeform", mutating=True)
def set_emphasis(session: Session, target: str, span: str, mark: str, on: bool = True,
                 author: Author = Author.MODEL) -> dict[str, Any]:
    """The explicit alternative to markup in `set_text`.

    Useful when the words are already right and only their emphasis is wrong — rewriting
    the whole string to change one word is how unrelated edits get lost.
    """
    shape = _shape(session, target)
    if mark not in MARKS:
        raise ToolError("unknown_mark", f"{mark!r} is not one of {list(MARKS)}")

    runs = shape.runs or richtext.parse(shape.text or "")
    marks: dict[str, Any] = ({m: False for m in MARKS if m != "none"} | {"script": ""}
                             if mark == "none" else {mark: on})
    updated, hits = richtext.apply_marks(runs, span, **marks)
    if not hits:
        raise ToolError("span_not_found",
                        f"{span!r} does not appear in {target}; the text is "
                        f"{(shape.text or '')[:120]!r}")

    with session.transaction(author) as turn:
        session.store.write(turn, "set_props", target,
                            {"runs": [r.model_dump(mode="json") for r in updated]}, author)

    return Diff(summary=f"{mark} on {span!r} in {target}", target=target,
                after={"span": span, "mark": mark, "on": on, "occurrences": hits},
                render=session.measure_slide(target.split("/")[0])).as_result()
