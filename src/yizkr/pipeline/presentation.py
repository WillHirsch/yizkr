"""PPTX adapter: a slide deck read slide by slide as a paged container
(ADR-0008).

A .pptx is a documented ZIP of XML parts, so unpacking is fully
deterministic — structure never rides the generative path (ADR-0006
context). `unpack_pptx` parses the package once into a format-neutral
source inventory (yizkr.pipeline.pptx_inventory), before reading
filters: native slide and speaker-notes parts in presentation order
(from ``ppt/presentation.xml`` and its relationships — slide FILE
numbers are not authoritative after reordering), original media and
embedded-object bytes, structural occurrences and known limitations.
Source parts and occurrences retain their identity when eligibility or
reading outcomes change. XML text order and raw transforms are preserved
without inferring visual reading order or composed page geometry. It
also counts the deck's slides, the page count its registration takes.

**A deck is a paged container** (yizkr.pipeline.containers):
`slide_pages` gives an unpacked deck's slides as pages, each read from
its XML, with its text (`slide_text`) and the images it paints that are
read on their own, in the order the slide paints them. Its transcription
marks each such image below the slide's marker by its owner's SHA-1
(``[Slide 2, image 1: sha1:…]``), reads it by the vision engine on its
own and keeps that reading under that SHA-1 (`derived_transcription`),
so the image is an excerpt, a document of its own, as an image a PDF
places is (yizkr.pipeline.transcribe.DeckTranscriber; ADR-0008).

Readers and registration use `container_sources` to project that inventory's
verified owner bindings, current reading inputs and native paint occurrences.
Canonical owner bytes are separate from a newly prepared reading input.
Speaker notes and slide-body text have separate source-part support even when
they cite the same container. Original images without a visible placement
are independent sources too; their support does not invent a slide position.

The pipeline publishes `container-inventory.json`, the deck's source parts,
placements and owner bindings. Each image read on its own keeps its
transcription under its owner's SHA-1 in the document's `DERIVED_MEDIA_DIR`
(`derived_transcription`); its claims cite that owner and carry the original
source part and placement support.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_inventory import ContainerInventory
from yizkr.pipeline.containers import (
    SLIDE_XML,
    VISION_SUFFIXES,
    ContainerPage,
    ContainerPages,
    image_size,
)
from yizkr.pipeline.pptx_inventory import PptxPackage

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".webp"}
# the leading phrase of the note a container's reading makes of an image
# it does not read on its own (container_sources.materialize_images); a
# short identifier of its owner or source part follows its first colon,
# so that a reason's label never shows a file's name
UNREAD_IMAGE = "image not read on its own"


@dataclass
class Slide:
    number: int  # 1-based position in the presentation order
    text: str
    notes: str = ""


@dataclass
class PresentationContent:
    """A deck's package parsed once (`unpack_pptx`): its source inventory,
    the original bytes of the parts it records, by SHA-1, and its slide
    count."""

    # every slide part the presentation order names, whether or not the
    # archive holds it, or, when it names none, every slide file of the
    # archive (a compatibility order the inventory notes as a limitation):
    # the deck's page count. A slide part the archive lacks is an
    # unsupported part of the inventory, which `slide_pages` refuses to
    # read.
    slide_count: int
    inventory: ContainerInventory | None = None
    inventory_bytes: dict[str, bytes] = field(default_factory=dict)


def unpack_pptx(path: Path) -> PresentationContent:
    """Parse the deck's original source parts once into its inventory, and
    count the slide parts its presentation order names, or, when it names
    none, the archive's slide files, which the inventory then notes as a
    compatibility order.

    The inventory distinguishes structural placements from relationship-only
    references.
    """
    original = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        package = PptxPackage(archive, ArtifactRef.from_bytes(original))
        if "ppt/presentation.xml" not in package.names:
            raise ValueError(
                f"{path.name} is not a PPTX (no ppt/presentation.xml)"
            )
        slides = package.slide_parts()
        for position, part in slides:
            package.add_slide(part, position)
        package.finish()
    return PresentationContent(len(slides), package.inventory, package.data)


def slide_text(slide: Slide) -> str:
    """A slide's own text, as its page of the deck's transcription holds
    it below the slide's `[Slide N]` marker: its text runs, then its
    speaker notes behind a `[Slide N notes]` marker. The markers give the
    extraction the slide scope, so every claim's `page_number` locator
    points into the right slide (R2)."""
    lines = [slide.text] if slide.text else []
    if slide.notes:
        lines += [f"[Slide {slide.number} notes]", slide.notes]
    return "\n".join(lines)


def _require_slide_bodies(content: PresentationContent) -> None:
    """Missing mandatory slide bytes cannot become a successful blank read."""
    if content.inventory is None:
        return
    for part in content.inventory.parts:
        if part.kind == "slide_body" and part.reading.state != "read":
            raise ValueError(
                f"cannot read slide {part.locator.position}: "
                f"{part.reading.reason or 'native slide content unavailable'} "
                f"({part.locator.package_part})"
            )


def unread_reason(suffix: str, data: bytes) -> str | None:
    """Why a container's image is not read on its own, or None when it is
    (yizkr.pipeline.containers): a note for an image in a format the
    vision engine does not read, or whose size its header does not give.
    `suffix` is that of the file the image is read from."""
    if suffix.lower() not in VISION_SUFFIXES:
        return (
            f"a {suffix.lower() or 'suffix-less'} image, a format the "
            "vision engine does not read"
        )
    size = image_size(data)
    if size is None:
        return "an image whose size its header does not give"
    if min(size) <= 0:
        return "an image whose header gives nonpositive dimensions"
    return None


def _never(sha1: str, names: Sequence[str] = ()) -> bool:
    return False


def slide_pages(
    content: PresentationContent,
    directory: Path,
    *,
    excluded: Callable[[str, Sequence[str]], bool] = _never,
) -> ContainerPages:
    """Read the deck `content` holds (`unpack_pptx`) slide by slide, as a
    paged container (yizkr.pipeline.containers): each slide a page read
    from its XML (`SLIDE_XML`), its text `slide_text`, with the images a
    slide paints that are read on their own, in the order it paints them,
    each written into `directory` once, as ``<sha1><suffix>``. Its images
    come from the projection of its source inventory
    (yizkr.pipeline.container_sources): all eligible original images, with
    separate actual paint occurrences. Each image is known by its bound
    owner's SHA-1 and takes the suffix of its current reading input's file
    in the deck's inventory (container_sources.materialize_images). Images
    without paint occurrences are also read, without inventing a slide
    placement. An image the never-ingest list names by its SHA-1 or by any
    of its names in the archive (`excluded`, given both), or that
    `unread_reason` gives a reason for, is not read; the reader notes each
    unsupported input once, behind the phrase `UNREAD_IMAGE`. A slide whose
    native content is unavailable is refused before anything is
    written."""
    from yizkr.pipeline.container_sources import (
        materialize_images,
        prepare_sources,
        project_sources,
    )

    _require_slide_bodies(content)
    if content.inventory is None:
        raise ValueError(
            "presentation source inventory is required; "
            "unpack the original package"
        )
    content.inventory = prepare_sources(
        content.inventory,
        content.inventory_bytes,
        directory,
        excluded=excluded,
    )
    sources = project_sources(
        content.inventory,
        directory,
        data=content.inventory_bytes,
        excluded=excluded,
    )
    notes = {part.locator.position: part.text or "" for part in sources.notes}
    read = ContainerPages(
        [
            ContainerPage(
                part.locator.position,
                SLIDE_XML,
                slide_text(
                    Slide(
                        part.locator.position,
                        part.text or "",
                        notes.get(part.locator.position, ""),
                    )
                ),
            )
            for part in sources.bodies
        ],
        inventory=content.inventory,
        inventory_bytes=content.inventory_bytes,
        sources=sources,
    )
    materialize_images(sources, content.inventory_bytes, directory, read)
    return read


# The folder of a container's document directory that keeps, for the
# images the container reads on their own, their transcriptions by owner
# SHA-1. An existing run's canonical owner bytes may be kept there too
# (container_sources), beside those readings.
DERIVED_MEDIA_DIR = "derived-media"
DERIVED_TRANSCRIPTION = ".transcription.json"


def derived_transcription(directory: Path, sha1: str) -> Path:
    """Where the transcription of an image a container reads on its own
    is kept, by its owner's SHA-1:
    ``derived-media/<sha1>.transcription.json``. The image's bytes are
    kept with the container inventory."""
    return directory / DERIVED_MEDIA_DIR / f"{sha1}{DERIVED_TRANSCRIPTION}"
