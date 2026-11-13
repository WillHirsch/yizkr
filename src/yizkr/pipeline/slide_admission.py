"""Conservative proof of the source inputs to a composed PPTX slide.

This is admission, not a second layout engine. The inventory remains the
authority for original images and occurrences. Constructs whose rendering
could add unrepresented content refuse before a renderer or provider runs.
"""

from __future__ import annotations

import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict

from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_sources import ContainerSources
from yizkr.pipeline.pptx_inventory import (
    A_NS,
    P_NS,
    R_NS,
    REL_NS,
    PptxPackage,
    Relationship,
    _walk,
)


class SlideAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    part_id: str
    position: int
    image_part_ids: list[str]
    placement_ids: list[str]
    package_parts: dict[str, str]


RENDERING_LIMITATIONS = frozenset(
    {
        "render-drawing",
        "render-inheritance",
        "render-background",
        "render-image",
        "render-relationship",
    }
)


class UnsupportedSlideRendering(ValueError):
    """A known rendering limit, separate from unsafe or inconsistent inputs."""

    def __init__(self, reason: str, limitation: str):
        self.limitation = limitation
        super().__init__(f"slide render admission refused: {reason}")


# Deliberately enumerate the understood native drawing vocabulary. Unknown
# extensions, alternate representations, fields, charts, media, and effects
# capable of carrying other images are not silently assumed invisible.
def _tags(namespace: str, names: str) -> set[str]:
    return {namespace + name for name in names.split()}


_P_TAGS = _tags(
    P_NS,
    "sld sldLayout sldMaster presentation cSld spTree sp pic grpSp cxnSp "
    "nvSpPr nvPicPr nvGrpSpPr nvCxnSpPr cNvPr cNvSpPr cNvPicPr "
    "cNvGrpSpPr cNvCxnSpPr nvPr ph spPr grpSpPr blipFill txBody style "
    "bg bgPr bgRef clrMap clrMapOvr hf txStyles titleStyle bodyStyle "
    "otherStyle sldIdLst sldId sldMasterIdLst sldMasterId sldLayoutIdLst "
    "sldLayoutId notesMasterIdLst notesMasterId sldSz notesSz "
    "defaultTextStyle",
)
_A_TAGS = _tags(
    A_NS,
    "theme themeOverride themeElements clrScheme fontScheme fmtScheme "
    "majorFont minorFont latin ea cs font dk1 lt1 dk2 lt2 accent1 accent2 "
    "accent3 accent4 accent5 accent6 hlink folHlink fillStyleLst "
    "lnStyleLst effectStyleLst bgFillStyleLst effectStyle effectLst "
    "outerShdw innerShdw glow softEdge reflection blur alphaModFix "
    "objectDefaults extraClrSchemeLst custClrLst masterClrMapping "
    "overrideClrMapping xfrm off ext chOff chExt srcRect stretch "
    "fillRect tile blip blipFill noFill solidFill gradFill pattFill "
    "grpFill gsLst gs lin path tileRect fgClr bgClr ln headEnd tailEnd "
    "prstDash custDash ds round bevel miter prstGeom avLst gdLst gd "
    "ahLst ahXY ahPolar cxnLst cxn pos rect custGeom pathLst moveTo "
    "lnTo arcTo quadBezTo cubicBezTo close pt spLocks picLocks "
    "grpSpLocks cxnSpLocks bodyPr lstStyle p pPr r rPr t br endParaRPr "
    "defRPr lvl1pPr lvl2pPr lvl3pPr lvl4pPr lvl5pPr lvl6pPr lvl7pPr "
    "lvl8pPr lvl9pPr defPPr tabLst tab lnSpc spcPct spcPts spcBef "
    "spcAft buNone buChar buFont buFontTx buClr buClrTx buSzPct buSzPts "
    "buSzTx buAutoNum normAutofit noAutofit spAutoFit prstTxWarp "
    "lnRef fillRef effectRef fontRef srgbClr scrgbClr hslClr sysClr "
    "schemeClr prstClr tint shade comp inv gray alpha alphaOff "
    "alphaMod hue hueOff hueMod sat satOff satMod lum lumOff lumMod "
    "red redOff redMod green greenOff greenMod blue blueOff blueMod "
    "gamma invGamma",
)
_KINDS = {
    "slide",
    "slideLayout",
    "slideMaster",
    "theme",
    "themeOverride",
    "image",
    "notesSlide",
    "notesMaster",
    "officeDocument",
    "presProps",
    "viewProps",
    "tableStyles",
    "extended-properties",
    "custom-properties",
}
_REL_BASE = R_NS[1:-1] + "/"
_CORE_PROPERTIES = (
    "http://schemas.openxmlformats.org/package/2006/relationships/"
    "metadata/core-properties"
)
_THUMBNAIL = (
    "http://schemas.openxmlformats.org/package/2006/relationships/"
    "metadata/thumbnail"
)
_ALLOWED_RELATIONS = {
    P_NS + "presentation": {
        "slide",
        "slideMaster",
        "notesMaster",
        "theme",
        "presProps",
        "viewProps",
        "tableStyles",
    },
    P_NS + "sld": {
        "slideLayout",
        "image",
        "notesSlide",
        "theme",
        "themeOverride",
    },
    P_NS + "sldLayout": {
        "slideMaster",
        "image",
        "theme",
        "themeOverride",
    },
    P_NS + "sldMaster": {
        "slideLayout",
        "image",
        "theme",
        "themeOverride",
    },
    A_NS + "theme": {"image"},
    A_NS + "themeOverride": {"image"},
}
_ID_KINDS = {
    P_NS + "sldId": "slide",
    P_NS + "sldMasterId": "slideMaster",
    P_NS + "sldLayoutId": "slideLayout",
    P_NS + "notesMasterId": "notesMaster",
}


def _fail(reason: str) -> None:
    raise ValueError(f"slide render admission refused: {reason}")


class _Package(PptxPackage):
    def __init__(self, archive, source):
        names = archive.namelist()
        if len(names) != len({name.rstrip("/").casefold() for name in names}):
            _fail("duplicate package member")
        for name in names:
            path = PurePosixPath(name)
            if (
                not name
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or ":" in name
                or "%" in name
                or "\x00" in name
                or path.as_posix() != name.rstrip("/")
            ):
                _fail("noncanonical package member")
        super().__init__(archive, source)
        self.consulted: set[str] = set()
        self.aliases: dict[str, list[str]] | None = None
        self.limitations: list[UnsupportedSlideRendering] = []

    def unsupported(self, reason, limitation="render-drawing"):
        # Keep checking: a later malformed or excluded input must still stop
        # processing, even when a known rendering limitation was seen first.
        self.limitations.append(UnsupportedSlideRendering(reason, limitation))

    def image_names(self, reference):
        # Exclusion aliases are byte aliases, including media outside the
        # inventory's conventional directories and non-rendered thumbnails.
        if self.aliases is None:
            self.aliases = {}
            for name in sorted(self.names):
                if not name.endswith("/"):
                    digest = hashlib.sha1(self.bytes(name)).hexdigest()
                    self.aliases.setdefault(digest, []).append(
                        PurePosixPath(name).name
                    )
        return self.aliases.get(reference.sha1, [])

    def root(self, part):
        if part not in self.names:
            _fail(f"missing XML part {part}")
        self.consulted.add(part)
        self.check_declarations(part)
        return super().root(part)

    def check_declarations(self, part):
        declarations = self.bytes(part).replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
            _fail(f"XML declarations outside the supported subset: {part}")

    def relationships(self, part):
        location = PurePosixPath(part)
        rels = str(location.parent / "_rels" / (location.name + ".rels"))
        if rels not in self.names:
            return []
        root = self.root(rels)
        if root.tag != REL_NS + "Relationships":
            _fail(f"unsupported relationships part {rels}")
        result = []
        ids = set()
        for element in root:
            rid = element.get("Id")
            kind = element.get("Type", "")
            if (
                element.tag != REL_NS + "Relationship"
                or not rid
                or rid in ids
                or set(element.attrib) - {"Id", "Type", "Target", "TargetMode"}
                or len(element)
            ):
                _fail(f"ambiguous relationships in {rels}")
            ids.add(rid)
            if element.get("TargetMode", "Internal") != "Internal":
                _fail(f"external relationship in {rels}")
            if kind not in {
                *(_REL_BASE + value for value in _KINDS),
                _CORE_PROPERTIES,
                _THUMBNAIL,
            }:
                self.unsupported(
                    f"unsupported relationship type in {rels}",
                    "render-relationship",
                )
            target = element.get("Target", "")
            if (
                not target
                or any(
                    value in target
                    for value in ("\\", ":", "%", "?", "#", "\x00")
                )
                or "//" in target
            ):
                _fail(f"noncanonical relationship target in {rels}")
            parts = (
                [] if target.startswith("/") else list(location.parent.parts)
            )
            for segment in target.lstrip("/").split("/"):
                if segment == "..":
                    if not parts:
                        _fail(f"relationship escapes the package: {rels}")
                    parts.pop()
                elif segment not in {"", "."}:
                    parts.append(segment)
            target = "/".join(parts)
            if target not in self.names or target.endswith("/"):
                _fail(f"missing relationship target in {rels}")
            result.append(Relationship(rid, target, kind))
        return result

    def preflight(self):
        """The converter can import nonvisual parts from the whole package.

        Notes stay outside the slide contribution proof, but their links
        and opaque images must not provide an alternate external-input route.
        """
        from yizkr.pipeline.presentation import unread_reason

        checked_images = set()
        for name in sorted(self.names):
            if name.casefold().endswith((".xml", ".rels")):
                self.check_declarations(name)
            if not name.casefold().endswith(".rels"):
                continue
            location = PurePosixPath(name)
            if location.parent.name != "_rels" or not name.endswith(".rels"):
                _fail("unsupported package relationships location")
            part = (
                ""
                if name == "_rels/.rels"
                else str(location.parent.parent / location.name[:-5])
            )
            if part and part not in self.names:
                _fail("orphan package relationships part")
            for relation in self.relationships(part):
                if relation.kind not in {_REL_BASE + "image", _THUMBNAIL}:
                    # Every other admitted relationship type names XML;
                    # its filename extension cannot bypass this check.
                    self.check_declarations(relation.target)
                    continue
                if relation.target in checked_images:
                    continue
                checked_images.add(relation.target)
                if unread_reason(
                    PurePosixPath(relation.target).suffix,
                    self.bytes(relation.target),
                ):
                    self.unsupported(
                        "unsupported declared package image", "render-image"
                    )

    def checked(self, part, expected):
        root = self.root(part)
        if root.tag != expected:
            _fail(f"unexpected XML root in {part}")
        relations = {r.id: r for r in self.relationships(part)}
        allowed = {_REL_BASE + name for name in _ALLOWED_RELATIONS[expected]}
        if any(
            relation.kind not in allowed for relation in relations.values()
        ):
            self.unsupported(
                f"unsupported relationship context in {part}",
                "render-relationship",
            )
        for element in root.iter():
            if element.tag not in _P_TAGS | _A_TAGS:
                self.unsupported(
                    f"unsupported drawing element in {part}: {element.tag}"
                )
            for attribute, value in element.attrib.items():
                if attribute.startswith(R_NS):
                    relation = relations.get(value)
                    if relation is None:
                        _fail(f"unresolved relationship in {part}")
                    if element.tag == A_NS + "blip":
                        if (
                            attribute != R_NS + "embed"
                            or relation.kind != _REL_BASE + "image"
                        ):
                            _fail(f"unsupported image link in {part}")
                    elif not (
                        attribute == R_NS + "id"
                        and element.tag in _ID_KINDS
                        and relation.kind == _REL_BASE + _ID_KINDS[element.tag]
                    ):
                        if element.tag not in _P_TAGS | _A_TAGS:
                            # An opaque extension still prevents rendering.
                            # Its resolved internal edge is a capability
                            # limit, not a missing source binding. Continue
                            # checking later inputs before reporting it.
                            self.unsupported(
                                f"unsupported relationship use in {part}",
                                "render-relationship",
                            )
                        else:
                            _fail(f"unsupported relationship use in {part}")
                elif (
                    attribute == "{http://www.w3.org/XML/1998/namespace}space"
                ):
                    if value not in {"default", "preserve"}:
                        _fail(
                            f"unsupported XML whitespace directive in {part}"
                        )
                elif attribute.startswith("{"):
                    self.unsupported(
                        f"unsupported namespaced attribute in {part}"
                    )
            if element.tag == A_NS + "blip" and not element.get(
                R_NS + "embed"
            ):
                _fail(f"image without an embedded relationship in {part}")
        if expected in {P_NS + "sld", P_NS + "sldLayout", P_NS + "sldMaster"}:
            common = root.findall(P_NS + "cSld")
            if (
                len(common) != 1
                or len(common[0].findall(P_NS + "spTree")) != 1
            ):
                _fail(f"ambiguous slide shape tree in {part}")
            for tree in root.iter():
                if tree.tag in {P_NS + "spTree", P_NS + "grpSp"} and any(
                    child.tag
                    not in _tags(P_NS, "nvGrpSpPr grpSpPr sp pic grpSp cxnSp")
                    for child in tree
                ):
                    self.unsupported(
                        f"unsupported shape tree content in {part}"
                    )
        if expected in {A_NS + "theme", A_NS + "themeOverride"}:
            for element, _, ancestors in _walk(root):
                if element.tag == A_NS + "blip" and not any(
                    parent.tag
                    in {A_NS + "fillStyleLst", A_NS + "bgFillStyleLst"}
                    for parent, _ in ancestors
                ):
                    self.unsupported(
                        f"unsupported image-bearing theme style in {part}",
                        "render-image",
                    )
        return root

    def document(self):
        if "_rels/.rels" in self.names:
            roots = self.relationships("")
            documents = [
                r for r in roots if r.kind == _REL_BASE + "officeDocument"
            ]
            metadata = {
                _CORE_PROPERTIES,
                _REL_BASE + "extended-properties",
                _REL_BASE + "custom-properties",
                _THUMBNAIL,
            }
            if (
                len(documents) != 1
                or documents[0].target != "ppt/presentation.xml"
                or any(
                    r not in documents and r.kind not in metadata
                    for r in roots
                )
            ):
                _fail("unsupported package document relationship")
        self.checked("ppt/presentation.xml", P_NS + "presentation")
        theme = self._related("ppt/presentation.xml", "theme")
        if theme is not None:
            self.checked(theme, A_NS + "theme")
        # These parts configure views/printing or table styling. This subset
        # contains no tables, and the renderer selects slide export explicitly.
        # They cannot provide additional linked content.
        nonvisual = {
            "presProps": P_NS + "presentationPr",
            "viewProps": P_NS + "viewPr",
            "tableStyles": A_NS + "tblStyleLst",
        }
        for relation in self.relationships("ppt/presentation.xml"):
            expected = nonvisual.get(relation.kind.removeprefix(_REL_BASE))
            if expected is None:
                continue
            root = self.root(relation.target)
            if (
                root.tag != expected
                or self.relationships(relation.target)
                or any(
                    attribute.startswith(R_NS)
                    for element in root.iter()
                    for attribute in element.attrib
                )
            ):
                _fail("unsupported linked presentation settings")


def _inherited_shapes(package, root, part):
    """Empty placeholder layout/formatting is safe; inherited paint is not."""
    for shape in root.findall(P_NS + "cSld/" + P_NS + "spTree/*"):
        if any(True for _ in shape.iter(A_NS + "blip")):
            package.unsupported(
                f"unrepresented inherited shape content in {part}",
                "render-inheritance",
            )
        if shape.tag in {P_NS + "nvGrpSpPr", P_NS + "grpSpPr"}:
            continue
        if (
            shape.tag != P_NS + "sp"
            or shape.find(P_NS + "nvSpPr/" + P_NS + "nvPr/" + P_NS + "ph")
            is None
            or any(
                (text.text or "").strip() for text in shape.iter(A_NS + "t")
            )
        ):
            package.unsupported(
                f"unrepresented inherited shape content in {part}",
                "render-inheritance",
            )
        properties = shape.find(P_NS + "spPr")
        if properties is not None and any(
            child.tag not in {A_NS + "xfrm", A_NS + "noFill"}
            for child in properties
        ):
            package.unsupported(
                f"unrepresented inherited shape paint in {part}",
                "render-inheritance",
            )
        if shape.find(P_NS + "style") is not None:
            package.unsupported(
                f"unrepresented inherited shape style in {part}",
                "render-inheritance",
            )


def _chain(package, slide):
    chain = [slide]
    layout = package._related(slide, "slideLayout")
    if layout is not None:
        _inherited_shapes(
            package, package.checked(layout, P_NS + "sldLayout"), layout
        )
        chain.append(layout)
        master = package._related(layout, "slideMaster")
        if master is not None:
            _inherited_shapes(
                package, package.checked(master, P_NS + "sldMaster"), master
            )
            chain.append(master)
    for part in chain:
        for kind in ("theme", "themeOverride"):
            theme = package._related(part, kind)
            if theme is not None:
                package.checked(theme, A_NS + kind)
    return chain


def _backgrounds(package, slide, chain):
    """Do not infer that a transparent/empty fill hides an ancestor source.

    The inventory records the nearest background, whereas LibreOffice can
    retain an ancestor picture for noFill/empty definitions. Without new
    composition placements, only a plain opaque solid can mask a lower
    picture. Native shape noFill is unrelated to this background check.
    """
    backgrounds = []
    for part in chain:
        found = package.root(part).findall(P_NS + "cSld/" + P_NS + "bg")
        if not found:
            continue
        if len(found) != 1 or len(found[0]) != 1:
            _fail("ambiguous background definition")
        definition = found[0][0]
        if definition.tag == P_NS + "bgPr":
            fills = [
                child
                for child in definition
                if child.tag
                in _tags(
                    A_NS, "noFill solidFill gradFill pattFill grpFill blipFill"
                )
            ]
            if len(fills) != 1:
                _fail("background has no unique supported fill")
            fill = fills[0]
            plain = len(definition) == 1
        elif definition.tag == P_NS + "bgRef":
            resolved = package._theme_fill(
                slide, int(definition.get("idx", ""))
            )
            if resolved is None:
                _fail("background has no supported fill")
            fill = resolved[1]
            plain = True
        else:
            package.unsupported(
                "unsupported background definition", "render-background"
            )
            continue
        if fill.tag not in {A_NS + "solidFill", A_NS + "blipFill"}:
            package.unsupported(
                "unsupported background fill for composed rendering",
                "render-background",
            )
        if any(
            element.tag.startswith(A_NS + "alpha")
            for root in (definition, fill)
            for element in root.iter()
        ):
            package.unsupported(
                "transparent background modifiers are unsupported",
                "render-background",
            )
        color = fill[0] if len(fill) == 1 else None
        opaque = (
            plain
            and fill.tag == A_NS + "solidFill"
            and not fill.attrib
            and color is not None
            and color.tag == A_NS + "srgbClr"
            and not len(color)
            and set(color.attrib) == {"val"}
            and re.fullmatch(r"[0-9a-fA-F]{6}", color.get("val", ""))
        )
        backgrounds.append((fill.tag == A_NS + "blipFill", bool(opaque)))
    if (
        len(backgrounds) > 1
        and any(image for image, _ in backgrounds[1:])
        and not backgrounds[0][1]
    ):
        package.unsupported(
            "background override cannot prove ancestor images absent",
            "render-background",
        )


def _image_input(package, part, sources, excluded):
    actual = next(
        (p for p in sources.inventory.parts if p.id == part.id), None
    )
    if (
        actual is None
        or actual.byte_object != part.byte_object
        or actual.eligibility.state != "allowed"
        or actual.owner_binding is None
        or actual.ownership_issue is not None
    ):
        _fail("contributing image has no eligible source binding")
    source = sources.sources.get(actual.owner_binding.artifact.sha1)
    if (
        source is None
        or actual.id not in source.part_ids
        or not source.readable
        or not any(
            item.part_id == actual.id
            and item.representation == part.byte_object
            for item in source.inputs
        )
    ):
        _fail("contributing image bytes are unavailable or unsupported")
    names = sorted(
        set(source.names)
        | set(package.image_names(part.byte_object))
        | {
            PurePosixPath(p.locator.package_part).name
            for p in sources.inventory.parts
            if p.byte_object == part.byte_object and p.locator.package_part
        }
    )
    references = [part.byte_object, source.owner]
    references.extend(item.representation for item in source.inputs)
    if any(excluded(reference.sha1, names) for reference in references):
        _fail("contributing image is excluded")
    from yizkr.pipeline.presentation import unread_reason

    target = part.locator.package_part
    if unread_reason(PurePosixPath(target).suffix, package.bytes(target)):
        _fail("contributing image format is unsupported")


def admit_slides(
    original: bytes,
    sources: ContainerSources,
    *,
    excluded: Callable[[str, Sequence[str]], bool],
    source_names: Sequence[str] = (),
) -> tuple[SlideAdmission, ...]:
    """Verify every slide before producing any composed reading input.

    ``sources`` must be the fresh, byte-verified projection of this package.
    A failed or unsupported proof raises ValueError; there is no permissive
    fallback. Notes and unused images do not become visual contributors.
    """
    source = ArtifactRef.from_bytes(original)
    if (
        source != sources.inventory.source
        or sources.inventory.container != "presentation"
    ):
        _fail("source inventory does not identify these presentation bytes")
    if excluded(source.sha1, source_names):
        _fail("presentation is excluded")
    try:
        with zipfile.ZipFile(io.BytesIO(original)) as archive:
            package = _Package(archive, source)
            package.preflight()
            package.document()
            slides = package.slide_parts()
            if not slides or not package.inventory.complete:
                _fail("presentation has no unambiguous slide order")
            if len({target for _, target in slides}) != len(slides):
                _fail("presentation repeats a slide part")
            common = set(package.consulted)
            result = []
            for position, target in slides:
                package.consulted = set(common)
                root = package.checked(target, P_NS + "sld")
                if root.get("show", "1") not in {"1", "true", "0", "false"}:
                    _fail("invalid slide visibility flag")
                chain = _chain(package, target)
                _backgrounds(package, target, chain)
                for reference in root.iter(A_NS + "fillRef"):
                    fill = package._theme_fill(
                        target, int(reference.get("idx", ""))
                    )
                    if fill is not None and any(
                        True for _ in fill[1].iter(A_NS + "blip")
                    ):
                        package.unsupported(
                            "theme image shape fills lack source placements",
                            "render-image",
                        )
                # Notes do not contribute to the composed slide.
                parent = package.add_native_part(
                    target, position, "slide_body"
                )
                package._background(parent)
                package._occurrences(parent)
                if parent.reading.state != "read":
                    _fail("effective slide background is unavailable")
                body = next(
                    (p for p in sources.bodies if p.id == parent.id), None
                )
                if body is None or body.byte_object != parent.byte_object:
                    _fail("slide body has no current source-part binding")
                placements = [
                    p
                    for p in package.inventory.placements
                    if p.parent == parent.id
                ]
                if any(p.occurrence != "paint" for p in placements):
                    _fail("slide image has no verified paint occurrence")
                located = {
                    (p.locator.package_part, p.locator.path)
                    for p in placements
                }
                if any(
                    (target, path + "/@r:embed") not in located
                    for element, path, _ in _walk(root)
                    if element.tag == A_NS + "blip"
                ):
                    _fail("slide image has no verified paint occurrence")
                known = {p.id: p for p in sources.inventory.placements}
                if any(known.get(p.id) != p for p in placements):
                    _fail("slide placements differ from the source inventory")
                parts = {p.id: p for p in package.inventory.parts}
                images = sorted({p.child for p in placements})
                for image in images:
                    _image_input(package, parts[image], sources, excluded)
                result.append(
                    SlideAdmission(
                        part_id=parent.id,
                        position=position,
                        image_part_ids=images,
                        placement_ids=[p.id for p in placements],
                        package_parts={
                            name: hashlib.sha256(
                                package.bytes(name)
                            ).hexdigest()
                            for name in sorted(package.consulted)
                        },
                    )
                )
            if package.limitations:
                raise package.limitations[0]
            return tuple(result)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError) as error:
        raise ValueError(
            "slide render admission refused: invalid package"
        ) from error
