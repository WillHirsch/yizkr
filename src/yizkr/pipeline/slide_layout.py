"""Native slide text remains authoritative while a model arranges blocks.

The model supplies only row/cell IDs. This module reassembles exact original
text and verifies the complete permutation; image pixels supply no words.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.claims import ArtifactRef
from yizkr.pipeline.pptx_inventory import A_NS, P_NS, _walk, text_runs
from yizkr.pipeline.stages import canonical_json


class NativeTextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^b[1-9][0-9]*$")
    path: str = Field(min_length=1)
    text: str = Field(min_length=1)


class NativeSlideBlocks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    part_id: str = Field(min_length=1)
    byte_object: ArtifactRef
    blocks: tuple[NativeTextBlock, ...]

    @model_validator(mode="after")
    def canonical_blocks(self):
        if [block.id for block in self.blocks] != [
            f"b{index}" for index in range(1, len(self.blocks) + 1)
        ] or len({block.path for block in self.blocks}) != len(self.blocks):
            raise ValueError("native blocks need ordered IDs and unique paths")
        return self

    @property
    def basis_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode()
        ).hexdigest()


class NativeSlideLayout(BaseModel):
    """An arrangement of current native text, never image OCR attribution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    part_id: str = Field(min_length=1)
    byte_object: ArtifactRef
    basis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows: tuple[tuple[str, ...], ...]


def native_slide_blocks(xml: bytes, part_id: str) -> NativeSlideBlocks:
    """Read exact ordinary native shape text or refuse an unsupported layout.

    Each block retains its paragraph breaks. The XML-order concatenation is
    checked against the existing native text view, so this route cannot drop
    text-bearing constructs outside its supported shape subset.
    """
    declarations = xml.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
        raise ValueError("native slide XML declarations are unsupported")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise ValueError("invalid native slide XML") from error
    if root.tag != P_NS + "sld":
        raise ValueError("native layout needs a slide body")
    _, gaps = layout_analysis(root)
    if gaps:
        raise ValueError("unsupported native slide layout: " + ", ".join(gaps))
    blocks = []
    for element, path, _ in _walk(root):
        if element.tag == P_NS + "sp" and (text := text_runs(element)):
            blocks.append(
                NativeTextBlock(id=f"b{len(blocks) + 1}", path=path, text=text)
            )
    if "\n".join(block.text for block in blocks) != text_runs(root):
        raise ValueError("native blocks do not cover the slide's exact text")
    return NativeSlideBlocks(
        part_id=part_id,
        byte_object=ArtifactRef.from_bytes(xml),
        blocks=tuple(blocks),
    )


def assemble_native_layout(
    blocks: NativeSlideBlocks, layout: NativeSlideLayout
) -> str:
    """Validate a complete arrangement and assemble its original text."""
    if (
        layout.part_id != blocks.part_id
        or layout.byte_object != blocks.byte_object
        or layout.basis_sha256 != blocks.basis_sha256
    ):
        raise ValueError("native slide layout identifies another input")
    known = {block.id: block.text for block in blocks.blocks}
    actual = [identity for row in layout.rows for identity in row]
    if (
        any(not row for row in layout.rows)
        or len(actual) != len(known)
        or len(set(actual)) != len(actual)
        or set(actual) != known.keys()
    ):
        raise ValueError(
            "native layout must use every known block exactly once"
        )
    if any(
        len(row) > 1 and any("\n" in known[identity] for identity in row)
        for row in layout.rows
    ):
        raise ValueError("multi-paragraph native blocks need singleton rows")
    return "\n".join(
        " | ".join(known[identity] for identity in row) for row in layout.rows
    )


def parse_native_layout(
    answer: str, blocks: NativeSlideBlocks
) -> NativeSlideLayout:
    """Only a row/cell arrangement is accepted from the model response."""

    def distinct(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("native layout response repeats a key")
            result[key] = value
        return result

    value = json.loads(answer, object_pairs_hook=distinct)
    if (
        not isinstance(value, dict)
        or set(value) != {"rows"}
        or not isinstance(value["rows"], list)
        or any(
            not isinstance(row, list)
            or any(type(item) is not str for item in row)
            for row in value["rows"]
        )
    ):
        raise ValueError("native layout response needs only rows of block IDs")
    layout = NativeSlideLayout(
        part_id=blocks.part_id,
        byte_object=blocks.byte_object,
        basis_sha256=blocks.basis_sha256,
        rows=tuple(tuple(row) for row in value["rows"]),
    )
    assemble_native_layout(blocks, layout)
    return layout


@dataclass(frozen=True)
class _Box:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height


def _box(shape: ET.Element) -> _Box | None:
    transforms = shape.findall(P_NS + "spPr/" + A_NS + "xfrm")
    if len(transforms) != 1:
        return None
    transform = transforms[0]
    if set(transform.attrib) - {"rot", "flipH", "flipV"} or (
        transform.get("rot", "0") != "0"
        or any(
            transform.get(name, "0") not in {"0", "false"}
            for name in ("flipH", "flipV")
        )
    ):
        return None
    offset = transform.find(A_NS + "off")
    extent = transform.find(A_NS + "ext")
    if (
        offset is None
        or extent is None
        or len(transform) != 2
        or set(offset.attrib) != {"x", "y"}
        or set(extent.attrib) != {"cx", "cy"}
    ):
        return None
    try:
        result = _Box(
            int(offset.attrib["x"]),
            int(offset.attrib["y"]),
            int(extent.attrib["cx"]),
            int(extent.attrib["cy"]),
        )
    except (KeyError, ValueError):
        return None
    if min(result.x, result.y) < 0 or min(result.width, result.height) <= 0:
        return None
    return result


def layout_analysis(root: ET.Element) -> tuple[str, tuple[str, ...]]:
    trees = root.findall(P_NS + "cSld/" + P_NS + "spTree")
    if len(trees) != 1:
        raise ValueError("slide has no unambiguous shape tree")
    boxes = []
    gaps = set()
    for shape in trees[0]:
        if shape.tag == P_NS + "grpSpPr" and any(
            value not in {"0", "false"}
            for element in shape.iter()
            for value in element.attrib.values()
        ):
            gaps.add("unsupported-geometry")
        if shape.tag in {P_NS + "nvGrpSpPr", P_NS + "grpSpPr", P_NS + "pic"}:
            continue
        if shape.tag == P_NS + "grpSp":
            gaps.add("grouped-shapes")
            continue
        if shape.tag != P_NS + "sp" or not text_runs(shape):
            gaps.add("drawings")
            continue
        if any(
            node.get("hidden") in {"1", "true"}
            for node in shape.iter(P_NS + "cNvPr")
        ):
            gaps.add("hidden-shapes")
        geometry = shape.find(P_NS + "spPr/" + A_NS + "prstGeom")
        if shape.find(P_NS + "spPr/" + A_NS + "custGeom") is not None or (
            geometry is not None and geometry.get("prst") != "rect"
        ):
            gaps.add("drawings")
        body = shape.find(P_NS + "txBody/" + A_NS + "bodyPr")
        if (
            body is None
            or any(
                body.get(name, default) != default
                for name, default in (
                    ("vert", "horz"),
                    ("rot", "0"),
                    ("numCol", "1"),
                )
            )
            or any(
                node.tag in {A_NS + "fld", A_NS + "br", A_NS + "prstTxWarp"}
                or node.tag.startswith(A_NS + "alpha")
                for node in shape.iter()
            )
        ):
            gaps.add("unsupported-text-flow")
        box = _box(shape)
        if box is None:
            gaps.add("unsupported-geometry")
        else:
            boxes.append(box)
    spatial = False
    reversed_order = False
    for index, first in enumerate(boxes):
        for second in boxes[index + 1 :]:
            horizontal_overlap = min(first.right, second.right) > max(
                first.x, second.x
            )
            vertical_overlap = min(first.bottom, second.bottom) > max(
                first.y, second.y
            )
            if horizontal_overlap and vertical_overlap:
                gaps.add("overlapping-text")
            elif vertical_overlap:
                spatial = True
            elif second.bottom <= first.y:
                reversed_order = True
    if gaps:
        return "unsupported-layout", tuple(sorted(gaps))
    if spatial:
        return "spatial-text-layout", ()
    if reversed_order:
        return "xml-order", ()
    return "no-layout-trigger", ()
