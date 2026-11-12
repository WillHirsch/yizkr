"""Explicit slide vision over admitted, container-owned visual content.

The normal presentation reader remains native. This opt-in operation selects
physical slides, preserves separately read original image owners and refuses
selected composites whose visible image evidence cannot be attributed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yizkr.claims import ArtifactRef
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.container_inventory import _read_regular, read_inventory
from yizkr.pipeline.container_sources import ContainerSources, project_sources
from yizkr.pipeline.containers import ContainerPages
from yizkr.pipeline.presentation import PresentationContent
from yizkr.pipeline.slide_admission import admit_slides
from yizkr.pipeline.slide_renderer import SlideRenderSettings
from yizkr.pipeline.slide_renders import (
    SlideRender,
    SlideRenderProduct,
    prepare_slide_renders,
)

ExclusionCheck = Callable[[str, Sequence[str]], bool]


@dataclass(frozen=True)
class SlideVisionRequest:
    """Selected physical slides and a dedicated, caller-owned render folder.

    The folder retains render products independently of the model cache.
    Positions are presentation order, including hidden slides. Selecting a
    slide never changes its source owner or authorizes mixed-source evidence.
    """

    positions: tuple[int, ...]
    directory: Path
    settings: SlideRenderSettings = SlideRenderSettings()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.positions, tuple)
            or not self.positions
            or any(
                type(position) is not int or position < 1
                for position in self.positions
            )
            or len(set(self.positions)) != len(self.positions)
        ):
            raise ValueError(
                "slide vision needs distinct positive slide positions"
            )
        if not isinstance(self.directory, Path):
            raise ValueError("slide vision needs a render workspace Path")

    def configuration(self) -> dict[str, Any]:
        return {
            "positions": sorted(self.positions),
            "settings": {
                "executable": self.settings.executable,
                "long_edge": self.settings.long_edge,
                "timeout": self.settings.timeout,
            },
        }


def _payload(
    directory: Path, slide: SlideRender, excluded: ExclusionCheck
) -> bytes:
    representation = slide.representation
    names = (representation.file, Path(representation.file).name)
    if excluded(representation.byte_object.sha1, names):
        raise ValueError("rendered slide is excluded")
    data = _read_regular(directory, representation.file)
    if (
        data is None
        or ArtifactRef.from_bytes(data) != representation.byte_object
        or hashlib.sha256(data).hexdigest() != slide.sha256
    ):
        raise ValueError("slide reading representation is missing or damaged")
    return data


@dataclass(frozen=True)
class SlideVisionProof:
    source: Path
    content: PresentationContent
    request: SlideVisionRequest
    product: SlideRenderProduct
    reading_directory: Path
    excluded: ExclusionCheck
    image_inputs: dict[Path, tuple[ArtifactRef, ArtifactRef]]

    def dependencies(self) -> dict[str, Any]:
        return {
            **self.request.configuration(),
            "render_product_sha256": self.product.digest,
            "render_basis_sha256": self.product.basis_sha256,
            "renderer": self.product.renderer,
        }

    def _sources(self) -> ContainerSources:
        check_cancelled()
        original = self.source.read_bytes()
        if ArtifactRef.from_bytes(original) != self.product.source:
            raise ValueError("presentation changed during slide reading")
        sources = project_sources(
            self.content.inventory,
            self.reading_directory,
            data=self.content.inventory_bytes,
            excluded=self.excluded,
        )
        admission = admit_slides(
            original,
            sources,
            excluded=self.excluded,
            source_names=(self.source.name,),
        )
        if admission != tuple(
            slide.admission for slide in self.product.slides
        ):
            raise ValueError("slide render admission changed during reading")
        return sources

    def _input(self, path: Path, sources: ContainerSources) -> None:
        for slide in self.product.slides:
            if (
                slide.admission.position in self.request.positions
                and path == self.reading_directory / slide.representation.file
            ):
                _payload(self.reading_directory, slide, self.excluded)
                return
        expected = self.image_inputs.get(path)
        if expected is None:
            raise ValueError("unrecognized slide reading input")
        owner, representation = expected
        source = sources.sources.get(owner.sha1)
        current = source.current_input if source is not None else None
        if (
            source is None
            or not source.readable
            or current is None
            or current.representation != representation
            or any(
                self.excluded(reference.sha1, (*source.names, path.name))
                for reference in (owner, representation)
            )
        ):
            raise ValueError("independent slide image is excluded or changed")
        payload = _read_regular(
            self.reading_directory,
            path.relative_to(self.reading_directory).as_posix(),
        )
        if (
            payload is None
            or ArtifactRef.from_bytes(payload) != representation
        ):
            raise ValueError(
                "independent slide image input is missing or damaged"
            )

    def before_vision(self, path: Path) -> None:
        """Re-admit every actual provider input, including checked retries."""
        self._input(path, self._sources())

    def verify(self) -> None:
        """Recheck current admission after provider calls, before return."""
        check_cancelled()
        sources = self._sources()
        for slide in self.product.slides:
            if slide.admission.position in self.request.positions:
                self._input(
                    self.reading_directory / slide.representation.file, sources
                )
        for path in self.image_inputs:
            self._input(path, sources)
        current = prepare_slide_renders(
            self.source,
            self.request.directory,
            excluded=self.excluded,
            settings=self.request.settings,
            prepared=self.content,
        )
        if current.digest != self.product.digest:
            raise ValueError("slide render admission changed during reading")
        check_cancelled()


def select_slide_vision(
    source: Path,
    directory: Path,
    content: PresentationContent,
    read: ContainerPages,
    request: SlideVisionRequest,
    *,
    excluded: ExclusionCheck,
) -> SlideVisionProof:
    """Replace selected native bodies with verified PNG inputs, without calls.

    Original images remain independently read through the existing container
    projection. Notes stay native and separate from the selected visible body.
    Every selected slide is proved before rendering or constructing a client.
    """
    check_cancelled()
    positions = set(request.positions)
    if not positions <= {page.number for page in read.pages}:
        raise ValueError("selected slide position is outside the presentation")
    if excluded is None:
        raise ValueError("slide vision requires a current exclusion predicate")
    if read.sources is None:
        raise ValueError("slide vision requires prepared source bindings")
    original = source.read_bytes()
    admission = admit_slides(
        original, read.sources, excluded=excluded, source_names=(source.name,)
    )
    if any(
        proof.position in positions and proof.image_part_ids
        for proof in admission
    ):
        raise ValueError("selected slide has original image contributors")
    product = prepare_slide_renders(
        source,
        request.directory,
        excluded=excluded,
        settings=request.settings,
        prepared=content,
    )
    if (
        product.source != ArtifactRef.from_bytes(original)
        or tuple(slide.admission for slide in product.slides) != admission
    ):
        raise ValueError("slide admission changed during preparation")
    inventory = read_inventory(request.directory)
    if inventory is None or inventory.source != product.source:
        raise ValueError("render inventory is missing or damaged")
    # The render workspace and the reading share one verified owner binding.
    # Carry render bytes with the usual temporary products for callers that
    # publish the inventory, while the dedicated folder retains its record.
    content.inventory = inventory
    for slide in product.slides:
        data = _payload(request.directory, slide, excluded)
        content.inventory_bytes[slide.representation.byte_object.sha1] = data
        if slide.admission.position in positions:
            path = directory / slide.representation.file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    read.inventory = inventory
    read.inventory_bytes = content.inventory_bytes
    read.sources = project_sources(
        inventory, directory, data=content.inventory_bytes, excluded=excluded
    )
    notes = {
        part.locator.position: part.text or "" for part in read.sources.notes
    }
    rendered = {slide.admission.position: slide for slide in product.slides}
    for page in read.pages:
        if page.number not in positions:
            continue
        slide = rendered[page.number]
        page.method = "vision"
        page.text = ""
        page.vision = directory / slide.representation.file
        page.speaker_notes = notes.get(page.number, "")
        page.reading_input = slide.representation.byte_object
        page.render_product_sha256 = product.digest
    inputs = {}
    for owner, image in read.images.items():
        bound = read.sources.sources.get(owner)
        current = bound.current_input if bound is not None else None
        if current is None:
            raise ValueError("independent slide image has no current input")
        inputs[image.path] = (bound.owner, current.representation)
    return SlideVisionProof(
        source, content, request, product, directory, excluded, inputs
    )
