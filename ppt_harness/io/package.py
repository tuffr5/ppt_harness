"""Package hygiene and audit — OOXML at the zip level.

Everything else in `io/` works through python-pptx's object model, which is the right tool
for *shapes*. This module works on the package: parts, content types, and the relationship
graph that ties them together. Those are what the object model cannot see, and they are
exactly where an edited deck rots.

The failure this exists for is real and reproducible: detaching a `<p:pic>` element removes
the picture from the slide, and leaves behind the relationship pointing at its image and the
image part itself. PowerPoint tolerates that — it is not a schema violation — so nothing
complains and the debris simply accumulates. Delete twenty images across a few sessions and
the file still carries every one of them.

Two operations, deliberately separate:

- `audit` **reports** and changes nothing, so it can be run against a file the harness did
  not produce.
- `sweep` **fixes** the subset that is safe to fix automatically — unreferenced
  relationships, and the parts that become unreachable once those are gone.

`audit` takes an `original` to baseline against, because a deck that arrived broken should
not report its inherited faults as though this edit caused them. Without that, the first
audit of any real-world deck is a wall of noise nobody reads, and an audit nobody reads is
worse than none.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

#: Relationship types whose target is *cited from the part's own XML* — `r:embed` on a
#: picture, `r:id` on a chart or a hyperlink. Only these can be dangling, because only these
#: have a citation that can go missing.
#:
#: An allowlist, not a blocklist, and the distinction is the whole correctness argument.
#: Most relationships are bound by the relationship alone: a slide's layout, a notesSlide's
#: backlink to its slide, printer settings, the theme. Listing *those* as exceptions means
#: every type forgotten becomes a false positive — which is exactly what the first draft did
#: to a pristine file.
EXPLICITLY_REFERENCED = (
    "/image", "/video", "/audio", "/chart", "/diagramData", "/oleObject",
    "/hyperlink", "/package",
)

#: Parts that exist for the package rather than for any one slide. Never orphans.
ALWAYS_REACHABLE = ("[Content_Types].xml", "_rels/.rels", "docProps/")

#: Extensions whose content type is not a matter of opinion. `.bin` is deliberately absent —
#: it is printer settings in one place and an OLE object in another, so a mismatch cannot be
#: judged from the name.
CONTENT_TYPE_BY_EXTENSION = {
    "png": "image/png", "gif": "image/gif", "bmp": "image/bmp",
    "jpeg": "image/jpeg", "jpg": "image/jpeg", "jpe": "image/jpeg",
    "tiff": "image/tiff", "tif": "image/tiff", "svg": "image/svg+xml",
    "webp": "image/webp", "mp4": "video/mp4", "mp3": "audio/mpeg",
    "wav": "audio/wav", "m4a": "audio/mp4",
}


@dataclass
class Finding:
    kind: str
    part: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.part} — {self.detail}"


@dataclass
class Audit:
    findings: list[Finding] = field(default_factory=list)
    inherited: list[Finding] = field(default_factory=list)
    """Present in the original too. Reported separately, never as this edit's fault."""

    @property
    def clean(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "clean": self.clean,
            "problems": [{"kind": f.kind, "part": f.part, "detail": f.detail}
                         for f in self.findings],
        }
        if self.inherited:
            out["inherited"] = len(self.inherited)
            out["note"] = (f"{len(self.inherited)} further problem(s) were already in the "
                           "source file and are not attributed to this edit")
        return out


def _rels_path(part: str) -> str:
    head, tail = posixpath.split(part)
    return posixpath.join(head, "_rels", tail + ".rels")


def _resolve(source_part: str, target: str) -> str:
    """A relationship target, as a package path.

    Targets are relative to the *directory* of the part that declares them, which is why
    `../media/image1.png` from `ppt/slides/slide6.xml` lands in `ppt/media/`.
    """
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def _relationships(zf: zipfile.ZipFile, part: str) -> list[tuple[str, str, str, bool]]:
    """`(rId, reltype, target, external)` for one part."""
    path = _rels_path(part)
    if path not in zf.namelist():
        return []
    body = zf.read(path).decode("utf-8", "replace")
    out = []
    for match in re.finditer(r"<Relationship\b([^>]*)/?>", body):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', match.group(1)))
        if "Id" not in attrs or "Target" not in attrs:
            continue
        out.append((attrs["Id"], attrs.get("Type", ""), attrs["Target"],
                    attrs.get("TargetMode") == "External"))
    return out


def _xml_parts(zf: zipfile.ZipFile) -> list[str]:
    return [n for n in zf.namelist()
            if n.endswith(".xml") and not n.endswith(".rels")
            and not n.startswith("docProps/")]


def _check_content_types(audit: Audit, names: set[str], defaults: dict[str, str],
                         overrides: dict[str, str]) -> None:
    """That a part's declared type is *right*, not merely present.

    A `.png` declared `image/jpeg` is not a schema violation and no relationship is broken —
    PowerPoint simply renders garbage where the picture was. Nothing else in the package
    catches it, because every structural check it fails is one it passes.
    """
    for part_name in overrides:
        if part_name.lstrip("/") not in names:
            audit.findings.append(Finding(
                "override_for_missing_part", part_name,
                "[Content_Types].xml declares a type for a part that is not here"))

    for part in sorted(names):
        if part.endswith(".rels") or part == "[Content_Types].xml":
            continue
        extension = part.rsplit(".", 1)[-1].lower() if "." in part else ""
        expected = CONTENT_TYPE_BY_EXTENSION.get(extension)
        if expected is None:
            continue
        declared = overrides.get(f"/{part}") or defaults.get(extension)
        if declared and declared.lower() != expected:
            audit.findings.append(Finding(
                "content_type_mismatch", part,
                f"declared {declared!r} but the extension says {expected!r}; a picture "
                "declared as the wrong type renders as nothing"))


def _check_relationship_graph(audit: Audit, zf: zipfile.ZipFile, names: set[str]) -> None:
    """Invariants of the graph itself, rather than of any one relationship.

    These are the checks that catch a deck that is *broken* rather than merely untidy: a
    slide the presentation no longer lists is invisible to a reader while still counting
    toward the file, and a duplicate `rId` makes a reference ambiguous — PowerPoint resolves
    it one way, python-pptx may resolve it the other.
    """
    for part in sorted(names):
        rels = _relationships(zf, part)
        seen: set[str] = set()
        for rid, _reltype, target, external in rels:
            if rid in seen:
                audit.findings.append(Finding(
                    "duplicate_relationship_id", _rels_path(part),
                    f"{rid} is declared twice; a reference to it is ambiguous"))
            seen.add(rid)
            if external:
                continue
            # `..` that climbs past the package root. A reader resolving it reaches outside
            # the file, which is either corruption or something worse.
            if _resolve(part, target).startswith(".."):
                audit.findings.append(Finding(
                    "target_escapes_package", part,
                    f"{rid} -> {target} resolves outside the package"))

    if "ppt/presentation.xml" not in names:
        return
    body = zf.read("ppt/presentation.xml").decode("utf-8", "replace")
    listed = set(re.findall(r'<p:sldId\b[^>]*r:id="([^"]+)"', body))
    by_id = {rid: _resolve("ppt/presentation.xml", target)
             for rid, reltype, target, external in
             _relationships(zf, "ppt/presentation.xml")
             if not external and reltype.endswith("/slide")}

    for rid in sorted(listed - set(by_id)):
        audit.findings.append(Finding(
            "slide_id_unresolved", "ppt/presentation.xml",
            f"the slide list cites {rid}, which is not a slide relationship"))

    for rid, target in sorted(by_id.items()):
        if rid not in listed:
            audit.findings.append(Finding(
                "slide_not_in_presentation", target,
                "the part exists and is related, but the presentation's slide list does not "
                "include it; it will not be shown"))


def _collect(path: Path | str) -> Audit:
    audit = Audit()
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())

        if "[Content_Types].xml" not in names:
            audit.findings.append(Finding("no_content_types", "[Content_Types].xml",
                                          "the package declares no content types"))
            return audit
        types = zf.read("[Content_Types].xml").decode("utf-8", "replace")
        defaults = {ext.lower(): ct for ext, ct in
                    re.findall(r'<Default\b[^>]*Extension="([^"]+)"[^>]*'
                               r'ContentType="([^"]+)"', types)}
        override_types = dict(re.findall(r'<Override\b[^>]*PartName="([^"]+)"[^>]*'
                                         r'ContentType="([^"]+)"', types))
        overrides = set(override_types)

        _check_content_types(audit, names, defaults, override_types)
        _check_relationship_graph(audit, zf, names)

        reachable: set[str] = set()
        for part in list(names):
            for rid, reltype, target, external in _relationships(zf, part):
                if external:
                    continue
                resolved = _resolve(part, target)
                reachable.add(resolved)

                if resolved not in names:
                    audit.findings.append(Finding(
                        "missing_target", part,
                        f"{rid} points at {target!r}, which is not in the package"))
                    continue

                # Only a relationship the XML was supposed to cite can be dangling. This is
                # where a deleted picture leaves its mark: the element goes, the citation
                # goes with it, and the relationship and its image stay behind.
                if part.endswith(".xml") and reltype.endswith(EXPLICITLY_REFERENCED):
                    body = zf.read(part).decode("utf-8", "replace")
                    if f'"{rid}"' not in body:
                        audit.findings.append(Finding(
                            "dangling_relationship", part,
                            f"{rid} -> {target} is declared but nothing cites it; the shape "
                            "that used it was probably deleted"))

        for part in sorted(names):
            if part.endswith(".rels") or part.startswith(ALWAYS_REACHABLE):
                continue
            if part not in reachable and not part.startswith("ppt/presentation.xml"):
                audit.findings.append(Finding(
                    "orphaned_part", part,
                    "no relationship reaches this part; it is dead weight in the file"))
            extension = part.rsplit(".", 1)[-1].lower()
            if extension not in defaults and f"/{part}" not in overrides:
                audit.findings.append(Finding(
                    "undeclared_content_type", part,
                    f"neither a Default for .{extension} nor an Override declares its type"))

        for part in _xml_parts(zf) + [n for n in names if n.endswith(".rels")]:
            body = zf.read(part)
            if b"<" not in body:
                audit.findings.append(Finding("empty_xml", part, "part holds no markup"))
                continue
            # An actual parse, not a substring check. This is deliberately *not* schema
            # validation: our writes go through python-pptx's object model, which cannot
            # emit misordered elements, so the realistic failure is a part that does not
            # parse at all — a truncated write, a bad encoding, a stray byte. Catching that
            # costs a parse; catching schema violations costs several megabytes of vendored
            # XSDs to guard against a class of defect this writer cannot produce.
            try:
                etree.fromstring(body)
            except etree.XMLSyntaxError as exc:
                audit.findings.append(Finding(
                    "malformed_xml", part, f"does not parse: {str(exc).split(',')[0]}"))
    return audit


def audit(path: Path | str, original: Path | str | None = None) -> Audit:
    """Structural problems in a package, optionally baselined against the source.

    Baselining matters more than it sounds. Real decks arrive with debris from whatever
    produced them, and an audit that blames this edit for all of it trains the reader to
    ignore the output — which is the failure mode of every linter nobody runs.
    """
    result = _collect(path)
    if original is None:
        return result

    inherited = {(f.kind, f.part) for f in _collect(original).findings}
    fresh, old = [], []
    for finding in result.findings:
        (old if (finding.kind, finding.part) in inherited else fresh).append(finding)
    return Audit(findings=fresh, inherited=old)


def sweep(path: Path | str) -> list[str]:
    """Remove what is safely removable, in place. Returns what went.

    Only two things: relationships nothing references, and the parts that become unreachable
    once those are gone. Never a missing target — a part that *should* exist and does not is
    a problem to report, and deleting the relationship would hide it.

    Rewrites the zip rather than editing in place, because a zip has no in-place delete. The
    replacement is moved over the original only after it is complete, so an interrupted
    sweep leaves the deck it started with.
    """
    path = Path(path)
    removed: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        drop_rels: dict[str, set[str]] = {}

        # Only media, and only from slides. The first draft sweep took every relationship
        # its own audit called dangling, and promptly severed `notesSlide -> slide` and
        # dropped `printerSettings` — both unreferenced in XML by design, both load-bearing.
        # "Unreferenced" is a weak signal; "an image a slide no longer shows" is a strong
        # one, and it is the only case this was built for.
        for part in sorted(names):
            if not part.startswith("ppt/slides/slide"):
                continue
            body = zf.read(part).decode("utf-8", "replace")
            for rid, reltype, target, external in _relationships(zf, part):
                if external or not reltype.endswith(("image", "video", "audio")):
                    continue
                if f'"{rid}"' in body:
                    continue
                drop_rels.setdefault(_rels_path(part), set()).add(rid)
                removed.append(f"{part}: {rid} -> {target}")

        # A media part is dropped only when *every* relationship reaching it is going. A
        # picture used on two slides and deleted from one must survive.
        doomed_targets: set[str] = set()
        surviving: set[str] = set()
        for part in names:
            rels_path = _rels_path(part)
            for rid, _reltype, target, external in _relationships(zf, part):
                if external:
                    continue
                resolved = _resolve(part, target)
                if rid in drop_rels.get(rels_path, ()):
                    doomed_targets.add(resolved)
                else:
                    surviving.add(resolved)
        orphans = {t for t in doomed_targets - surviving if t in names}
        removed += sorted(orphans)

        if not drop_rels and not orphans:
            return []

        staging = path.with_suffix(path.suffix + ".sweeping")
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as out:
            for item in zf.infolist():
                if item.filename in orphans:
                    continue
                body = zf.read(item.filename)
                if item.filename in drop_rels:
                    body = _strip_relationships(body, drop_rels[item.filename])
                out.writestr(item, body)

    shutil.move(str(staging), str(path))
    return removed


def _strip_relationships(body: bytes, rids: set[str]) -> bytes:
    text = body.decode("utf-8", "replace")
    for rid in rids:
        text = re.sub(rf'<Relationship\b[^>]*Id="{re.escape(rid)}"[^>]*/>\s*', "", text)
    return text.encode("utf-8")
