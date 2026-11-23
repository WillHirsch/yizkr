"""A container's paged reading, located as a container reader locates it.

`located` gives a reading made page by page (each page or slide opened by
its markers, then its own text) the source context a container reader
attaches from its projection (yizkr.pipeline.container_sources
.source_context): one attributed span over each page's or slide's own
text, owned by the container and located at that page's or slide's body;
and each image the pages mark as one placed source, with one occurrence
on each page or slide that marks it, located at that position. Every ID
is the structural ID of its locator, so the same reading is always
located the same way, and the place a stage names an image by
(source_context.artifact_places) is "page/slide N, placement <id>".
"""

from __future__ import annotations

from yizkr.claims import ArtifactRef, PlacementSupport, SourceSupport
from yizkr.pipeline.container_inventory import structural_id
from yizkr.pipeline.source_context import (
    LocatedReading,
    PlacedSource,
    SourceContext,
)
from yizkr.pipeline.transcribe import (
    DECK_ENGINE,
    Transcription,
    reading_digest,
)
from yizkr.source_structure import StructuralLocator


def _support(
    container: ArtifactRef,
    locator: StructuralLocator,
    placements: list[PlacementSupport] | None = None,
    reading_sha256: str | None = None,
) -> SourceSupport:
    return SourceSupport(
        container=container,
        part_id=structural_id(container, locator),
        locator=locator,
        placements=placements or [],
        reading_sha256=reading_sha256,
    )


def located(reading: Transcription) -> Transcription:
    """`reading` with the source context of its pages and the images they
    mark: a deck's (DECK_ENGINE) as slide bodies and slide placements, any
    other's as PDF pages and paint operations."""
    container = reading.artifact
    deck = reading.engine == DECK_ENGINE
    digest = reading_digest(reading)
    spans: list[LocatedReading] = []
    occurrences: dict[str, list[PlacementSupport]] = {}
    for page in reading.pages or ():
        if deck:
            slide = f"ppt/slides/slide{page.page}.xml"
            body = StructuralLocator(
                kind="slide_body", position=page.page, package_part=slide
            )
        else:
            body = StructuralLocator(
                kind="page", position=page.page, path=f"/pages/{page.page}"
            )
        spans.append(
            LocatedReading(
                start=page.start,
                end=page.end,
                owner=container,
                support=_support(container, body, reading_sha256=digest),
            )
        )
        for order, sha1 in enumerate(page.images, start=1):
            if deck:
                occurrence = StructuralLocator(
                    kind="placement",
                    position=page.page,
                    package_part=slide,
                    path=f"/p:sld[1]/p:cSld[1]/p:spTree[1]/p:pic[{order}]",
                )
            else:
                occurrence = StructuralLocator(
                    kind="paint",
                    position=page.page,
                    path=f"/pages/{page.page}/content/op[{order}]",
                )
            occurrences.setdefault(sha1, []).append(
                PlacementSupport(
                    id=structural_id(container, occurrence),
                    locator=occurrence,
                )
            )
    placed = [
        PlacedSource(
            owner=ArtifactRef(sha1=sha1),
            support=_support(
                container,
                StructuralLocator(
                    kind="embedded_resource",
                    package_part=f"ppt/media/{sha1}.png",
                )
                if deck
                else StructuralLocator(kind="image", path=f"/objects/{sha1}"),
                placements,
            ),
        )
        for sha1, placements in occurrences.items()
    ]
    return reading.model_copy(
        update={
            "source_context": SourceContext(
                container=container, readings=spans, placements=placed
            )
        }
    )
