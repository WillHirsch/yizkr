"""Bounded detection of native slide text whose arrangement needs vision.

This detects a few explicit spatial relationships, not visual sufficiency.
Rendering admission remains the source/ownership authority. Unhandled shapes
and layouts keep native readings with inspectable gaps; unsafe inputs refuse.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.claims import ArtifactRef
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.container_sources import ContainerSources
from yizkr.pipeline.pptx_inventory import A_NS, P_NS, PptxPackage, text_runs
from yizkr.pipeline.slide_admission import (
    RENDERING_LIMITATIONS,
    UnsupportedSlideRendering,
    admit_slides,
)

ROUTE_REASONS = frozenset(
    {
        "no-layout-trigger",
        "spatial-text-layout",
        "xml-order",
        "unsupported-layout",
        "mixed-source",
        "unsupported-rendering",
    }
)
ROUTE_LIMITATIONS = RENDERING_LIMITATIONS | {
    "drawings",
    "grouped-shapes",
    "unsupported-geometry",
    "overlapping-text",
    "unsupported-text-flow",
    "hidden-shapes",
    "mixed-source-attribution",
}


class SlideRoutingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position: int = Field(gt=0)
    part_id: str = Field(min_length=1)
    route: Literal["slide-xml", "vision"]
    reason: str
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def known_codes(self):
        if self.reason not in ROUTE_REASONS or any(
            value not in ROUTE_LIMITATIONS for value in self.limitations
        ):
            raise ValueError("unknown slide routing code")
        if self.route == "vision" and self.limitations:
            raise ValueError("automatic vision cannot have unresolved gaps")
        return self


class SlideRoutingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: Literal["layout-v1"] = "layout-v1"
    positions: tuple[int, ...]
    slides: tuple[SlideRoutingDecision, ...]

    @model_validator(mode="after")
    def consistent(self):
        if [slide.position for slide in self.slides] != list(
            range(1, len(self.slides) + 1)
        ) or self.positions != tuple(
            slide.position for slide in self.slides if slide.route == "vision"
        ):
            raise ValueError(
                "slide routing needs every physical slide in order"
            )
        return self

    def configuration(self) -> dict:
        return self.model_dump(mode="json")


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


def _layout(root: ET.Element) -> tuple[str, tuple[str, ...]]:
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


def plan_slide_layout(
    original: bytes,
    sources: ContainerSources,
    *,
    excluded: Callable[[str, Sequence[str]], bool],
    source_names: Sequence[str] = (),
) -> SlideRoutingPlan:
    """Choose bounded native/vision routes without rendering or model calls.

    Only explicitly typed rendering limitations become diagnostic gaps.
    Source, exclusion, malformed-package and ownership failures propagate.
    """
    check_cancelled()
    source = ArtifactRef.from_bytes(original)
    if source != sources.inventory.source:
        raise ValueError("slide routing inventory identifies another source")
    if excluded(source.sha1, source_names):
        raise ValueError("presentation is excluded")
    unavailable = None
    try:
        admission = admit_slides(
            original, sources, excluded=excluded, source_names=source_names
        )
    except UnsupportedSlideRendering as error:
        unavailable = error.limitation
        admission = ()
    if excluded(source.sha1, source_names):
        raise ValueError("presentation is excluded")
    proofs = {proof.position: proof for proof in admission}
    bodies = {part.locator.position: part for part in sources.bodies}
    if len(bodies) != len(sources.bodies):
        raise ValueError("slide routing has repeated body positions")
    decisions = []
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        package = PptxPackage(archive, source)
        order = package.slide_parts()
        if not package.inventory.complete or len(order) != len(bodies):
            raise ValueError("slide routing has no complete physical order")
        for position, target in order:
            package.add_slide(target, position)
        expected_native = {
            part.id: part
            for part in package.inventory.parts
            if part.kind in {"slide_body", "speaker_notes"}
        }
        actual_native = {
            part.id: part for part in (*sources.bodies, *sources.notes)
        }
        if actual_native.keys() != expected_native.keys():
            raise ValueError(
                "native slide parts have no current source binding"
            )
        for part_id, expected_part in expected_native.items():
            current = actual_native[part_id]
            if (
                current.byte_object != expected_part.byte_object
                or current.locator != expected_part.locator
                or current.text != expected_part.text
                or current.eligibility.state != "allowed"
                or current.reading.state != "read"
                or current.byte_object is None
            ):
                raise ValueError("native slide part is unavailable or changed")
            if excluded(
                current.byte_object.sha1,
                (PurePosixPath(current.locator.package_part).name,),
            ):
                raise ValueError("native slide part is excluded")
        for position, target in order:
            check_cancelled()
            body = bodies.get(position)
            reason, gaps = _layout(package.root(target))
            proof = proofs.get(position)
            if unavailable is not None:
                reason = "unsupported-rendering"
                gaps = (*gaps, unavailable)
            elif proof.image_part_ids:
                reason = "mixed-source"
                gaps = (*gaps, "mixed-source-attribution")
            route = (
                "vision"
                if reason in {"spatial-text-layout", "xml-order"}
                else "slide-xml"
            )
            decisions.append(
                SlideRoutingDecision(
                    position=position,
                    part_id=body.id,
                    route=route,
                    reason=reason,
                    limitations=gaps,
                )
            )
    check_cancelled()
    return SlideRoutingPlan(
        positions=tuple(
            item.position for item in decisions if item.route == "vision"
        ),
        slides=tuple(decisions),
    )
