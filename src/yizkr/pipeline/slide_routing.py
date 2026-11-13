"""Bounded detection of native slide text whose arrangement needs vision.

This detects a few explicit spatial relationships, not visual sufficiency.
Rendering admission remains the source/ownership authority. Unhandled shapes
and layouts keep native readings with inspectable gaps; unsafe inputs refuse.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.claims import ArtifactRef
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.container_sources import ContainerSources
from yizkr.pipeline.pptx_inventory import PptxPackage
from yizkr.pipeline.slide_admission import (
    RENDERING_LIMITATIONS,
    UnsupportedSlideRendering,
    admit_slides,
)
from yizkr.pipeline.slide_layout import layout_analysis

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

    policy: Literal["layout-v2"] = "layout-v2"
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
        admit_slides(
            original, sources, excluded=excluded, source_names=source_names
        )
    except UnsupportedSlideRendering as error:
        unavailable = error.limitation
    if excluded(source.sha1, source_names):
        raise ValueError("presentation is excluded")
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
            reason, gaps = layout_analysis(package.root(target))
            if unavailable is not None:
                reason = "unsupported-rendering"
                gaps = (*gaps, unavailable)
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
