"""A run's prepared containers, and the publication of their inventories.

`ContainerPreparations` keeps the prepared container of each document a
driver runs (media_stages.prepare_container) and answers for a document:
its preparation, its deck or PDF inventory, its current source projection
(container_sources.project_sources) and whether the never-ingest list
names an image. It hands every consumer the same prepared object, and
readers add representations and outcomes to it.

The functions publish a container's common source inventory
(`container-inventory.json`, with its parts and representations:
container_inventory.keep_inventory), restore a PDF's missing
representations without another model call, keep the readings of the
images a container reads on its own, and record each reading's outcome
beside the structural inventory. Each takes the document, which gives its
directory, source and SHA-1, and the component that holds its prepared
container; none takes a driver. The driver decides when they run: before
a reading is chosen or reused, after a reading, and at registration.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from yizkr.claims import ArtifactRef
from yizkr.core.corpus import ExclusionList, is_excluded
from yizkr.pipeline.document_state import (
    PDF,
    DocumentRun,
    _supersede,
    _write_view,
)

if TYPE_CHECKING:
    from yizkr.pipeline.container_inventory import ContainerInventory
    from yizkr.pipeline.container_sources import ContainerSources
    from yizkr.pipeline.media_stages import PreparedContainer
    from yizkr.pipeline.pdf_inventory import PdfInventory
    from yizkr.pipeline.presentation import PresentationContent
    from yizkr.pipeline.transcribe import PagedReading, Transcription


class ContainerPreparations:
    """The prepared container of each document, made once from its source
    and its directory as they stand and kept for the life of the driver
    that holds this, across the invocations it makes. `exclusions` gives
    the never-ingest list as it stands when it is called: the predicate
    asks it on every call, so it observes the list as discovery expands
    it with the images a deck's globs name."""

    def __init__(self, exclusions: Callable[[], ExclusionList | None]) -> None:
        self._exclusions = exclusions
        # (document directory, source SHA-1) -> its prepared container, or
        # None for a source that is not a container
        self._prepared: dict[tuple[Path, str], PreparedContainer | None] = {}

    def excluded(self, sha1: str, names: Sequence[str] = ()) -> bool:
        """Does the never-ingest list name an image a paged container
        places on a page, by the SHA-1 of its bytes or by any of its
        `names` (a deck's image's names in the archive, which its globs
        match, as registration matches them)? Its SHA-1 is named too when
        a glob names those bytes in any deck of the folder (the driver's
        `_gather_embedded`), so a PDF's image is asked of a deck's names
        for its bytes. Such an image is never read on its own, kept as the
        container's reading of it, or extracted."""
        exclusions = self._exclusions()
        return exclusions is not None and any(
            is_excluded(exclusions, name=name, sha1=sha1)
            for name in names or [None]
        )

    def preparation(self, doc: DocumentRun) -> PreparedContainer | None:
        """Current deterministic preparation, shared by readers and reuse."""
        from yizkr.pipeline.media_stages import (
            PreparationRequest,
            prepare_container,
        )

        key = (doc.dir, doc.sha1)
        if key not in self._prepared:
            self._prepared[key] = prepare_container(
                PreparationRequest(
                    source=doc.source,
                    expected_artifact=ArtifactRef(sha1=doc.sha1),
                    existing_directory=doc.dir,
                    excluded=self.excluded,
                )
            )
        return self._prepared[key]

    def presentation(self, doc: DocumentRun) -> PresentationContent:
        prepared = self.preparation(doc)
        if prepared is None or prepared.presentation is None:
            raise ValueError("source has no prepared presentation")
        return prepared.presentation

    def pdf_inventory(self, doc: DocumentRun) -> PdfInventory:
        prepared = self.preparation(doc)
        if prepared is None or prepared.pdf is None:
            raise ValueError("source has no prepared PDF")
        return prepared.pdf

    def container_preparation(
        self, doc: DocumentRun
    ) -> tuple[ContainerInventory, dict[str, bytes]] | None:
        """Expose current preparation without publishing persisted views."""
        prepared = self.preparation(doc)
        if prepared is None:
            return None
        # Readers can add representations and outcomes to this preparation.
        return prepared.inventory, prepared.data

    def container_sources(self, doc: DocumentRun) -> ContainerSources | None:
        """Current eligible sources, occurrences and readings; no publication.

        All consumers observe the same original-source preparation and current
        policy.
        """
        from yizkr.pipeline.container_sources import project_sources

        prepared = self.container_preparation(doc)
        if prepared is None:
            return None
        inventory, data = prepared
        return project_sources(
            inventory, doc.dir, data=data, excluded=self.excluded
        )


def _image_failure(
    doc: DocumentRun,
    containers: ContainerPreparations,
    sha1: str,
    *,
    verify_product: bool = True,
) -> str | None:
    """Why the saved reading of image `sha1` does not stand against the
    document's current source projection, or None when it does."""
    from yizkr.pipeline.media_stages import verify_image_reading

    return verify_image_reading(
        containers.container_sources(doc),
        sha1,
        doc.dir,
        verify_product=verify_product,
    ).failure


def publish_inventory(
    doc: DocumentRun, containers: ContainerPreparations
) -> None:
    """Publish source correspondence while retaining human products."""
    from yizkr.pipeline.container_inventory import keep_inventory

    prepared = containers.container_preparation(doc)
    if prepared is not None:
        inventory, data = prepared
        keep_inventory(inventory, data, doc.dir)


def publish_pdf_inventory(
    doc: DocumentRun, containers: ContainerPreparations
) -> None:
    """Publish PDF source parts before choosing or reusing a reading.

    Missing metadata and damaged representations are rebuilt without
    invalidating an unchanged transcription, proposal or review. A source
    that is not a PDF is not prepared here.
    """
    from yizkr.pipeline.container_inventory import (
        keep_inventory,
        read_inventory,
    )
    from yizkr.pipeline.textlayer import PdfExclusionError, is_pdf_input

    if is_pdf_input(doc.source):
        prepared = containers.pdf_inventory(doc)
        previous = read_inventory(doc.dir)
        if (
            previous is not None
            and previous.source == prepared.inventory.source
        ):
            parts = {part.id: part for part in prepared.inventory.parts}
            for old in previous.parts:
                part = parts.get(old.id)
                if part is None or part.kind not in {
                    "container_source",
                    "page_body",
                }:
                    continue
                try:
                    prepared.guard(
                        {part.locator.position}
                        if part.kind == "page_body"
                        else None
                    )
                except PdfExclusionError:
                    continue
                for representation in old.representations:
                    if (
                        representation.file is not None
                        and representation.method
                        in {
                            "rendered-pdf-v1",
                            "pdfium-page-render",
                            "pdf-page-image",
                        }
                        and representation.eligibility.state == "allowed"
                        and not any(
                            current.method == representation.method
                            for current in part.representations
                        )
                    ):
                        part.representations.append(representation)
                        prepared.data[representation.byte_object.sha1] = (
                            doc.dir / representation.file
                        ).read_bytes()
        keep_inventory(prepared.inventory, prepared.data, doc.dir)


def restore_pdf_representations(
    doc: DocumentRun, containers: ContainerPreparations, transcription
) -> None:
    """Recreate missing prepared bytes without making another model call.

    The caller has validated the saved reading. These are current source
    representations, not proof of an unrecorded historical model payload.
    """
    from tempfile import TemporaryDirectory

    from yizkr.pipeline.textlayer import prepare_pdf_vision, read_pages

    prepared = containers.pdf_inventory(doc)
    if transcription.input_representation == "rendered-pdf-v1":
        if any(
            rep.method == "rendered-pdf-v1"
            for part in prepared.inventory.parts
            for rep in part.representations
        ):
            return
        with TemporaryDirectory(prefix="yizkr-pdf-inventory-") as directory:
            prepare_pdf_vision(
                doc.source,
                Path(directory),
                containers.excluded,
                inventory=prepared,
            )
    else:
        needed = {
            page.page
            for page in transcription.pages or ()
            if page.provenance.method == "vision"
        }
        if not any(
            not prepared.page_parts[number].representations
            for number in needed
        ):
            return
        with TemporaryDirectory(prefix="yizkr-pdf-inventory-") as directory:
            read_pages(
                doc.source,
                Path(directory),
                excluded=containers.excluded,
                inventory=prepared,
            )
    publish_pdf_inventory(doc, containers)


def record_reading_outcomes(
    doc: DocumentRun,
    containers: ContainerPreparations,
    reading: PagedReading | None = None,
    transcription: Transcription | None = None,
    *,
    restore_slides: Callable[[], None] | None = None,
) -> None:
    """Record actual image outcomes beside the structural inventory.

    Native text belongs to the producer. Image success requires a
    validated reading of those bytes, including an explicitly empty
    reading. Reconstructing missing inventory can adopt an existing
    valid reading without making another model call. With a
    `transcription`, a PDF's missing representations are restored first,
    then the step the caller supplies as `restore_slides` carries a deck's
    verified slide renders into its inventory, and then the outcomes are
    recorded.
    """
    from yizkr.pipeline.container_inventory import (
        ReadingOutcome,
        keep_inventory,
    )
    from yizkr.pipeline.presentation import derived_transcription
    from yizkr.pipeline.transcribe import Transcription

    if transcription is not None and doc.kind == PDF:
        restore_pdf_representations(doc, containers, transcription)
    if transcription is not None and restore_slides is not None:
        restore_slides()
    prepared = containers.container_preparation(doc)
    if prepared is None:
        return
    inventory, data = prepared
    if inventory.source.sha1 != doc.sha1:
        raise ValueError("container inventory is missing or invalid")
    observed = (
        {image.sha1: image for image in reading.images}
        if reading is not None
        else {}
    )
    for part in inventory.parts:
        binding = part.owner_binding
        if (
            part.kind != "embedded_resource"
            or binding is None
            or part.ownership_issue is not None
            or part.eligibility.state != "allowed"
            or part.reading.state == "unsupported"
        ):
            continue
        sha1 = binding.artifact.sha1
        image = observed.get(sha1)
        if image is not None and image.failure is not None:
            part.reading = ReadingOutcome(state="failed", reason=image.failure)
            continue
        path = derived_transcription(doc.dir, sha1)
        if not path.is_file() or path.is_symlink():
            if part.reading.state != "failed":
                part.reading = ReadingOutcome(state="unattempted")
            continue
        failure = _image_failure(
            doc, containers, sha1, verify_product=image is None
        )
        if failure is not None:
            part.reading = ReadingOutcome(state="failed", reason=failure)
            continue
        reading_data = path.read_bytes()
        image_transcription = Transcription.model_validate_json(reading_data)
        record = image_transcription.record()
        empty = not any(page.text.strip() for page in record.pages)
        part.reading = ReadingOutcome(
            state="read",
            method=image_transcription.engine,
            empty=empty,
            product=path.relative_to(doc.dir).as_posix(),
            product_sha256=hashlib.sha256(reading_data).hexdigest(),
        )
    if transcription is not None:
        pages = {page.page: page for page in transcription.pages or ()}
        digest = hashlib.sha256(doc.transcription.read_bytes()).hexdigest()
        for part in inventory.parts:
            if (
                part.kind == "container_source"
                and transcription.input_representation == "rendered-pdf-v1"
            ):
                part.reading = ReadingOutcome(
                    state="read",
                    method=transcription.engine,
                    empty=not any(
                        page.text.strip()
                        for page in transcription.record().pages
                    ),
                    product=doc.transcription.name,
                    product_sha256=digest,
                )
            if part.kind not in {"page_body", "slide_body"}:
                continue
            page = pages.get(part.locator.position)
            if part.kind == "slide_body" and (
                page is None or page.provenance.method != "vision"
            ):
                continue
            if page is not None:
                part.reading = ReadingOutcome(
                    state="read",
                    method=page.provenance.engine,
                    empty=not transcription.text[
                        page.start : page.body_end
                        if page.body_end is not None
                        else page.end
                    ].strip(),
                    product=doc.transcription.name,
                    product_sha256=digest,
                )
    keep_inventory(inventory, data, doc.dir)


def keep_image_readings(
    doc: DocumentRun, containers: ContainerPreparations, reading: PagedReading
) -> None:
    """Publish image readings beside the source inventory.

    Reading paths retain their supported compatibility names. Source and
    placement authority is the common inventory; no image-only record is
    reconstructed from transcription markers. A kept reading that no
    longer stands is set aside before the new one is written.
    """
    from yizkr.pipeline.presentation import derived_transcription

    publish_inventory(doc, containers)
    for image in reading.images:
        path = derived_transcription(doc.dir, image.sha1)
        if path.exists() and _image_failure(doc, containers, image.sha1):
            _supersede(path)
        if image.transcription is not None:
            _write_view(path, image.transcription.model_dump_json(indent=2))
    record_reading_outcomes(doc, containers, reading)
