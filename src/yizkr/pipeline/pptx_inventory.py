"""Structural PPTX inventory, before filtering or choosing a reading route.

The XML/package paths identify occurrences. Geometry is retained in its native
coordinate frames; neither visual reading order nor composed page coordinates
are inferred. Original XML and media bytes remain available beside the record.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_inventory import (
    ContainerInventory,
    Eligibility,
    Placement,
    ReadingOutcome,
    SourcePart,
    StructuralLocator,
    structural_id,
)

A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".svg": "image/svg+xml",
    ".emf": "image/emf",
    ".wmf": "image/wmf",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".xml": "application/xml",
}
_RESOURCE_PREFIXES = ("ppt/media/", "ppt/embeddings/", "ppt/charts/")
_SHAPES = {
    P_NS + name for name in ("sp", "pic", "grpSp", "graphicFrame", "cxnSp")
}
_NON_IMAGE_FILLS = {
    A_NS + name for name in ("noFill", "solidFill", "gradFill", "pattFill")
}


def _name(tag: str) -> str:
    for namespace, prefix in ((P_NS, "p:"), (A_NS, "a:"), (R_NS, "r:")):
        if tag.startswith(namespace):
            return prefix + tag[len(namespace) :]
    return tag


def _walk(root: ET.Element, path: str = "", ancestors=()):
    path = path or f"/{_name(root.tag)}[1]"
    yield root, path, ancestors
    counts: dict[str, int] = {}
    for child in root:
        counts[child.tag] = counts.get(child.tag, 0) + 1
        yield from _walk(
            child,
            f"{path}/{_name(child.tag)}[{counts[child.tag]}]",
            (*ancestors, (root, path)),
        )


def _raw(element: ET.Element) -> dict:
    value = {"element": _name(element.tag), "attributes": dict(element.attrib)}
    if len(element):
        value["children"] = [_raw(child) for child in element]
    return value


def _layout(element: ET.Element, path: str) -> dict:
    """Uncomposed native transforms/fill plus their shape or background."""
    own = []
    for child in element:
        if child.tag in {P_NS + "spPr", P_NS + "grpSpPr", P_NS + "blipFill"}:
            own.extend(
                _raw(found)
                for found in child
                if found.tag
                in {A_NS + "xfrm", A_NS + "srcRect", A_NS + "blipFill"}
            )
        elif child.tag in {P_NS + "xfrm", A_NS + "xfrm"}:
            own.append(_raw(child))
        elif element.tag == P_NS + "bg" and child.tag == P_NS + "bgPr":
            own.extend(
                _raw(fill) for fill in child if fill.tag == A_NS + "blipFill"
            )
    native = next((e for e in element.iter(P_NS + "cNvPr")), None)
    result = {
        "path": path,
        "element": _name(element.tag),
        "coordinate_frame": "parent",
        "transforms": own,
    }
    if native is not None:
        result["nonvisual"] = dict(native.attrib)
    return result


def text_runs(root: ET.Element) -> str:
    """Native paragraphs in XML order, preserving the existing text view."""
    return "\n".join(
        line.strip()
        for paragraph in root.iter(A_NS + "p")
        if (
            line := "".join(t.text or "" for t in paragraph.iter(A_NS + "t"))
        ).strip()
    )


@dataclass(frozen=True)
class Relationship:
    id: str
    target: str
    kind: str
    external: bool = False


class PptxPackage:
    """One cached parse supplies both inventory and compatibility views."""

    def __init__(self, archive: zipfile.ZipFile, source: ArtifactRef):
        self.archive = archive
        self.source = source
        self.names = set(archive.namelist())
        self._roots: dict[str, ET.Element] = {}
        self._rels: dict[str, list[Relationship]] = {}
        self._bytes: dict[str, bytes] = {}
        self.data: dict[str, bytes] = {}
        self.inventory = ContainerInventory(
            source=source, container="presentation"
        )
        self.resources: dict[str, SourcePart] = {}

    def bytes(self, part: str) -> bytes:
        if part not in self._bytes:
            self._bytes[part] = self.archive.read(part)
        return self._bytes[part]

    def root(self, part: str) -> ET.Element:
        if part not in self._roots:
            self._roots[part] = ET.fromstring(self.bytes(part))
        return self._roots[part]

    def relationships(self, part: str) -> list[Relationship]:
        if part in self._rels:
            return self._rels[part]
        location = PurePosixPath(part)
        rels = str(location.parent / "_rels" / (location.name + ".rels"))
        result = []
        if rels in self.names:
            for element in self.root(rels).iter(REL_NS + "Relationship"):
                target = element.get("Target", "")
                external = element.get("TargetMode") == "External"
                if not external:
                    segments = []
                    combined = (
                        target.lstrip("/")
                        if target.startswith("/")
                        else str(location.parent / target)
                    )
                    for segment in combined.split("/"):
                        if segment == "..":
                            if segments:
                                segments.pop()
                        elif segment not in (".", ""):
                            segments.append(segment)
                    target = "/".join(segments)
                result.append(
                    Relationship(
                        element.get("Id", ""),
                        target,
                        element.get("Type", ""),
                        external,
                    )
                )
        self._rels[part] = result
        return result

    def slide_parts(self) -> list[tuple[int, str]]:
        part = "ppt/presentation.xml"
        relationships = {r.id: r for r in self.relationships(part)}
        slides = []
        entries = list(self.root(part).iter(P_NS + "sldId"))
        for position, entry in enumerate(entries, start=1):
            relation = relationships.get(entry.get(R_NS + "id"))
            if relation is not None and not relation.external:
                slides.append((position, relation.target))
            else:
                self.inventory.complete = False
                self.inventory.limitations.append(
                    f"slide relationship at position {position} "
                    "is missing or external"
                )
        if not entries:
            slides = list(
                enumerate(
                    sorted(
                        n
                        for n in self.names
                        if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
                    ),
                    start=1,
                )
            )
            self.inventory.complete = False
            self.inventory.limitations.append(
                "presentation order absent; "
                "slide filenames supply compatibility order"
            )
        return slides

    def _part(
        self,
        kind: str,
        package_part: str,
        *,
        position: int | None = None,
        text: str | None = None,
    ) -> SourcePart:
        locator = StructuralLocator(
            kind=kind, package_part=package_part, position=position
        )
        present = package_part in self.names
        data = self.bytes(package_part) if present else None
        blob = ArtifactRef.from_bytes(data) if data is not None else None
        if blob is not None:
            self.data[blob.sha1] = data
        part = SourcePart(
            id=structural_id(self.source, locator),
            kind=kind,
            locator=locator,
            byte_object=blob,
            file=(
                f"container-parts/{blob.sha1}{PurePosixPath(package_part).suffix.lower()}"
                if blob
                else None
            ),
            media_type=MEDIA_TYPES.get(
                PurePosixPath(package_part).suffix.lower(),
                "application/octet-stream",
            ),
            text=text,
            eligibility=Eligibility(
                state="allowed" if text is not None else "undetermined"
            ),
            reading=(
                ReadingOutcome(
                    state="read", method="slide-xml", empty=not bool(text)
                )
                if text is not None
                else ReadingOutcome(state="unattempted")
            ),
        )
        if not present:
            part.reading = ReadingOutcome(
                state="unsupported", reason="missing package target"
            )
            part.limitations.append("missing package target")
            self.inventory.complete = False
        self.inventory.parts.append(part)
        return part

    def resource(self, target: str) -> SourcePart:
        if target not in self.resources:
            self.resources[target] = self._part("embedded_resource", target)
        return self.resources[target]

    def add_native_part(
        self, target: str, position: int, kind: str
    ) -> SourcePart:
        if target not in self.names:
            return self._part(kind, target, position=position)
        root = self.root(target)
        part = self._part(
            kind, target, position=position, text=text_runs(root)
        )
        part.layout = [
            _layout(element, path)
            for element, path, _ in _walk(root)
            if element.tag in _SHAPES
        ]
        part.limitations.append(
            "native text is linearized in XML order; visual layout "
            "and inherited master content are not rendered"
        )
        if root.get("show") in {"0", "false"}:
            part.limitations.append("hidden slide content")
        for element, path, _ in _walk(root):
            if element.tag == P_NS + "cNvPr" and element.get("hidden") in {
                "1",
                "true",
            }:
                part.limitations.append(f"hidden shape content at {path}")
        return part

    def add_slide(self, target: str, position: int) -> SourcePart:
        parent = self.add_native_part(target, position, "slide_body")
        if target not in self.names:
            return parent
        self._background(parent)
        self._occurrences(parent)
        notes_seen = set()
        for relation in self.relationships(target):
            if (
                not relation.external
                and "notesSlides/" in relation.target
                and relation.target not in notes_seen
            ):
                notes_seen.add(relation.target)
                notes = self.add_native_part(
                    relation.target, position, "speaker_notes"
                )
                if relation.target in self.names:
                    self._occurrences(notes)
        return parent

    def _related(self, part: str, kind: str) -> str | None:
        """Resolve a declared inheritance edge, never guess another master."""
        found = [
            r for r in self.relationships(part) if r.kind.endswith("/" + kind)
        ]
        if not found:
            return None
        if len(found) != 1:
            raise ValueError(f"ambiguous {kind} relationships in {part}")
        relation = found[0]
        if relation.external or relation.target not in self.names:
            raise ValueError(
                f"unavailable {kind} target {relation.target} in {part}"
            )
        return relation.target

    def _background_chain(self, slide: str):
        """Nearest styles first, without inspecting overridden ancestors."""
        yield slide
        layout = self._related(slide, "slideLayout")
        if layout is None:
            return
        if self.root(layout).tag != P_NS + "sldLayout":
            raise ValueError(f"unsupported slideLayout part {layout}")
        yield layout
        master = self._related(layout, "slideMaster")
        if master is not None:
            if self.root(master).tag != P_NS + "sldMaster":
                raise ValueError(f"unsupported slideMaster part {master}")
            yield master

    def _theme_fill(self, slide: str, index: int):
        if index in {0, 1000}:
            return None
        if index < 0:
            raise ValueError(f"unsupported background style index {index}")
        for part in self._background_chain(slide):
            for kind in ("themeOverride", "theme"):
                theme = self._related(part, kind)
                if theme is None:
                    continue
                root = self.root(theme)
                if root.tag != A_NS + kind:
                    raise ValueError(f"unsupported {kind} part {theme}")
                scheme = (
                    root.find(A_NS + "fmtScheme")
                    if kind == "themeOverride"
                    else root.find(
                        A_NS + "themeElements/" + A_NS + "fmtScheme"
                    )
                )
                if scheme is None:
                    if kind == "themeOverride":
                        # A colour/font-only override changes no fill.
                        continue
                    raise ValueError(f"theme has no format scheme: {theme}")
                tag = "fillStyleLst" if index < 1000 else "bgFillStyleLst"
                offset = index - 1 if index < 1000 else index - 1001
                styles = scheme.find(A_NS + tag)
                if styles is None or offset >= len(styles):
                    raise ValueError(
                        f"background style {index} unavailable in {theme}"
                    )
                fill = styles[offset]
                path = next(
                    path for element, path, _ in _walk(root) if element is fill
                )
                return theme, fill, path
        raise ValueError("background style has no resolvable theme")

    def _background(self, parent: SourcePart) -> None:
        """Place only the effective background; retain its native origin."""
        slide = parent.locator.package_part
        chain = []
        try:
            for origin in self._background_chain(slide):
                chain.append(origin)
                root = self.root(origin)
                backgrounds = root.findall(P_NS + "cSld/" + P_NS + "bg")
                if not backgrounds:
                    continue
                if len(backgrounds) != 1:
                    raise ValueError(f"ambiguous backgrounds in {origin}")
                background = backgrounds[0]
                path = next(
                    path
                    for element, path, _ in _walk(root)
                    if element is background
                )
                definitions = [
                    child
                    for child in background
                    if child.tag in {P_NS + "bgPr", P_NS + "bgRef"}
                ]
                if len(definitions) != 1:
                    raise ValueError(
                        "ambiguous or unsupported background at "
                        f"{origin}:{path}"
                    )
                definition = definitions[0]
                properties = (
                    definition if definition.tag == P_NS + "bgPr" else None
                )
                reference = (
                    definition if definition.tag == P_NS + "bgRef" else None
                )
                layout = _layout(background, path)
                if origin != slide:
                    layout.update(
                        origin_part=origin,
                        slide_part=slide,
                        inheritance=list(chain),
                    )
                if reference is not None:
                    resolved = self._theme_fill(
                        slide, int(reference.get("idx", ""))
                    )
                    if resolved is None:
                        return
                    fill_origin, fill, fill_path = resolved
                    layout.update(
                        origin_part=origin,
                        slide_part=slide,
                        inheritance=list(chain),
                        background_reference=_raw(reference),
                        fill_origin=fill_origin,
                        resolved_fill=_raw(fill),
                    )
                else:
                    fills = [
                        child
                        for child in properties
                        if child.tag in _NON_IMAGE_FILLS
                        or child.tag in {A_NS + "blipFill", A_NS + "grpFill"}
                    ]
                    if len(fills) != 1:
                        raise ValueError(
                            "ambiguous or unsupported background fill in "
                            f"{origin}"
                        )
                    fill = fills[0]
                    fill_origin = origin
                    fill_path = next(
                        path
                        for element, path, _ in _walk(root)
                        if element is fill
                    )
                if fill.tag in _NON_IMAGE_FILLS:
                    return  # An explicit non-picture fill overrides ancestors.
                if fill.tag != A_NS + "blipFill":
                    raise ValueError(
                        f"unsupported background fill {_name(fill.tag)} "
                        f"in {fill_origin}"
                    )
                blips = list(fill.findall(A_NS + "blip"))
                if len(blips) != 1:
                    raise ValueError(
                        "background picture has no unique image in "
                        f"{fill_origin}"
                    )
                blip = blips[0]
                attribute = "embed" if blip.get(R_NS + "embed") else "link"
                rid = blip.get(R_NS + attribute)
                relations = [
                    r for r in self.relationships(fill_origin) if r.id == rid
                ]
                if len(relations) != 1:
                    raise ValueError(
                        f"unresolved background image relationship {rid} "
                        f"in {fill_origin}"
                    )
                relation = relations[0]
                if relation.external or relation.target not in self.names:
                    raise ValueError(
                        f"unavailable background image {relation.target} "
                        f"in {fill_origin}"
                    )
                if not relation.kind.endswith("/image"):
                    raise ValueError(
                        f"unsupported background image relationship {rid} "
                        f"in {fill_origin}"
                    )
                child = self.resource(relation.target)
                locator = StructuralLocator(
                    kind="placement",
                    package_part=fill_origin,
                    position=parent.locator.position,
                    path=f"{fill_path}/a:blip[1]/@r:{attribute}",
                    relationship_id=rid,
                )
                self.inventory.placements.append(
                    Placement(
                        id=structural_id(self.source, locator),
                        parent=parent.id,
                        child=child.id,
                        locator=locator,
                        order=1,
                        occurrence="paint",
                        layout=[layout],
                        limitations=(
                            [
                                "parent contains hidden content; "
                                "visibility is not inferred"
                            ]
                            if any(
                                "hidden" in note for note in parent.limitations
                            )
                            else []
                        ),
                    )
                )
                return
        except (ValueError, ET.ParseError) as error:
            reason = f"effective slide background unavailable: {error}"
            parent.reading = ReadingOutcome(
                state="failed", method="slide-xml", reason=reason
            )
            parent.limitations.append(reason)
            self.inventory.complete = False

    def _occurrences(self, parent: SourcePart) -> None:
        target = parent.locator.package_part
        relations = {r.id: r for r in self.relationships(target)}
        seen = set()
        order = sum(p.parent == parent.id for p in self.inventory.placements)
        recorded_paths = {
            p.locator.path
            for p in self.inventory.placements
            if p.parent == parent.id and p.locator.package_part == target
        }
        for element, path, ancestors in _walk(self.root(target)):
            for attribute in ("embed", "link", "id"):
                rid = element.get(R_NS + attribute)
                if rid is None:
                    continue
                relation = relations.get(rid)
                if relation is None:
                    parent.limitations.append(
                        f"unresolved relationship {rid} at {path}"
                    )
                    self.inventory.complete = False
                    continue
                seen.add(rid)
                if f"{path}/@r:{attribute}" in recorded_paths:
                    # Only this effective occurrence is already recorded;
                    # effects/extensions retain their structural references.
                    continue
                if relation.external:
                    parent.limitations.append(
                        f"external relationship {rid} at {path}; "
                        "target not fetched"
                    )
                    continue
                if not relation.target.startswith(_RESOURCE_PREFIXES):
                    continue
                child = self.resource(relation.target)
                locator = StructuralLocator(
                    kind="placement",
                    package_part=target,
                    position=parent.locator.position,
                    path=f"{path}/@r:{attribute}",
                    relationship_id=rid,
                )
                paint_owners = [
                    (shape, location)
                    for shape, location in (*ancestors, (element, path))
                    if shape.tag in _SHAPES
                ]
                order += 1
                placement = Placement(
                    id=structural_id(self.source, locator),
                    parent=parent.id,
                    child=child.id,
                    locator=locator,
                    order=order,
                    occurrence="paint"
                    if paint_owners
                    and element.tag == A_NS + "blip"
                    and attribute in {"embed", "link"}
                    else "reference",
                    layout=[
                        _layout(owner, location)
                        for owner, location in paint_owners
                    ],
                )
                if (
                    not paint_owners
                    or element.tag != A_NS + "blip"
                    or attribute not in {"embed", "link"}
                ):
                    placement.limitations.append(
                        "structural reference without confirmed image paint; "
                        "visible placement unknown"
                    )
                if any(
                    "hidden" in limitation for limitation in parent.limitations
                ):
                    placement.limitations.append(
                        "parent contains hidden content; "
                        "visibility is not inferred"
                    )
                self.inventory.placements.append(placement)
        for relation in relations.values():
            if relation.external:
                if relation.id not in seen:
                    parent.limitations.append(
                        f"external relationship {relation.id}; "
                        "target not fetched"
                    )
            elif relation.target.startswith(_RESOURCE_PREFIXES):
                child = self.resource(relation.target)
                if relation.id not in seen:
                    child.limitations.append(
                        f"relationship-only reference from {target} "
                        f"({relation.id}); visible placement unknown"
                    )
            elif relation.target not in self.names:
                parent.limitations.append(
                    f"missing relationship target {relation.target} "
                    f"({relation.id})"
                )
                self.inventory.complete = False
            elif not (
                "notesSlides/" in relation.target
                or relation.kind.endswith("/slide")
            ):
                parent.limitations.append(
                    f"related package part is not expanded: {relation.target}"
                )
                self.inventory.complete = False

    def finish(self) -> None:
        """Retain unreferenced media too, without inventing placement."""
        for target in sorted(self.names):
            if (
                target.startswith(_RESOURCE_PREFIXES)
                and not target.endswith("/")
                and target not in self.resources
            ):
                self.resource(target).limitations.append(
                    "unreferenced package media; visible placement unknown"
                )
        for part in self.inventory.parts:
            if (
                part.kind == "embedded_resource"
                and part.byte_object is not None
            ):
                suffix = PurePosixPath(
                    part.locator.package_part
                ).suffix.lower()
                if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                    part.reading = ReadingOutcome(
                        state="unsupported",
                        reason="media format has no independent reader",
                    )
