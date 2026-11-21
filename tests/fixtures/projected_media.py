"""A container's fictional media, projected as registration receives them.

`project_media` builds a container inventory for named media bytes on
given slides or pages, prepares its owner bindings and keeps it as the
stages before the write do, and returns the common projection
(yizkr.pipeline.container_sources) that
`Registrar.register_container_sources` takes. Preparation decides what is
bound, so only what a projection can hold reaches registration: a deck's
part whose name gives no image type is no source of its own, though its
name stays one of its bytes' names.

A deck's medium is recorded as the deck reader records package media. A
PDF's image is not recorded as the PDF reader records it: it is modelled
as complete original image media, a part of an image media type that
holds the bytes itself, so it binds with basis ``original-media``, as the
reader's DCT (JPEG) image does. The reader records the stream as
``application/vnd.pdf.image-stream`` (kept as ``.pdf-stream``) with a
``pdfium-image`` representation that becomes the owner, and a decoded
(PNG) image then binds as ``prepared-part``. That basis, and the
registrar's check of a prepared image against an existing owner, are not
exercised through this helper; tests/unit/test_container_citations.py
reads real PDFs for them.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Literal

from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_inventory import (
    ContainerInventory,
    Eligibility,
    Placement,
    SourcePart,
    keep_inventory,
    structural_id,
)
from yizkr.pipeline.container_sources import (
    ContainerSources,
    prepare_sources,
    project_sources,
)
from yizkr.pipeline.pptx_inventory import MEDIA_TYPES
from yizkr.source_structure import StructuralLocator

Medium = tuple[str, bytes, Sequence[int]]


def _body(source: ArtifactRef, container: str, position: int) -> SourcePart:
    if container == "pdf":
        locator = StructuralLocator(kind="page_body", position=position)
    else:
        locator = StructuralLocator(
            kind="slide_body",
            package_part=f"ppt/slides/slide{position}.xml",
            position=position,
        )
    return SourcePart(
        id=structural_id(source, locator), kind=locator.kind, locator=locator
    )


def _paint(
    source: ArtifactRef, container: str, position: int, order: int
) -> StructuralLocator:
    if container == "pdf":
        return StructuralLocator(
            kind="paint",
            position=position,
            path=f"/pages/{position}/content/op[{order}]",
        )
    return StructuralLocator(
        kind="placement",
        package_part=f"ppt/slides/slide{position}.xml",
        position=position,
        path=(
            f"/p:sld[1]/p:cSld[1]/p:spTree[1]/p:pic[{order}]"
            "/p:blipFill[1]/a:blip[1]/@r:embed"
        ),
        relationship_id=f"rId{order}",
    )


def project_media(
    directory: Path,
    source: ArtifactRef | Path,
    media: Sequence[Medium],
    *,
    container: Literal["presentation", "pdf"] = "presentation",
) -> ContainerSources:
    """The projection of `media` in the container `source` (its bytes'
    reference, or the file holding them), its inventory and bytes kept
    under `directory`.

    Each medium is ``(name, bytes, positions)``, painted once on each
    slide or page it lists, in the order the media are given. A deck holds
    it as ``ppt/media/<name>``, its media type read from the name's suffix
    as the deck reader reads it, so the same bytes under two names are one
    source with both names. A PDF's images have no names of their own: a
    PDF's image is modelled as original image media, a part of the image
    media type the name's suffix gives, kept in a file with that suffix,
    bound as ``original-media`` and named by its places
    (``page 6, image 1``)."""
    if isinstance(source, Path):
        source = ArtifactRef.from_path(source)
    bodies: dict[int, SourcePart] = {}
    parts: list[SourcePart] = []
    placements: list[Placement] = []
    data: dict[str, bytes] = {}
    painted: dict[int, int] = {}
    for index, (name, payload, positions) in enumerate(media, start=1):
        owner = ArtifactRef.from_bytes(payload)
        suffix = PurePosixPath(name).suffix.lower()
        if container == "pdf":
            locator = StructuralLocator(kind="image", path=f"/objects/{index}")
        else:
            locator = StructuralLocator(
                kind="embedded_resource", package_part=f"ppt/media/{name}"
            )
        part = SourcePart(
            id=structural_id(source, locator),
            kind="embedded_resource",
            locator=locator,
            byte_object=owner,
            file=f"container-parts/{owner.sha1}{suffix}",
            media_type=MEDIA_TYPES.get(suffix, "application/octet-stream"),
        )
        if container == "pdf":
            # allowed and a source of its own, as the PDF's preparation
            # leaves an image stream, but holding its image bytes itself,
            # with no pdfium-image representation: original media
            part.eligibility = Eligibility(state="allowed")
            part.source_role = "independent"
        parts.append(part)
        data[owner.sha1] = payload
        for position in positions:
            if position not in bodies:
                bodies[position] = _body(source, container, position)
            painted[position] = order = painted.get(position, 0) + 1
            paint = _paint(source, container, position, order)
            placements.append(
                Placement(
                    id=structural_id(source, paint),
                    parent=bodies[position].id,
                    child=part.id,
                    locator=paint,
                    order=order,
                    occurrence="paint",
                )
            )
    inventory = ContainerInventory(
        source=source,
        container=container,
        parts=[*(bodies[n] for n in sorted(bodies)), *parts],
        placements=placements,
    )
    prepared = prepare_sources(inventory, data, directory)
    keep_inventory(prepared, data, directory)
    return project_sources(prepared, directory)
