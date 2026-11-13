"""Slide vision over admitted, container-owned visual content.

The normal presentation reader remains native. Opt-in explicit or layout
policies select physical slides, preserve original image owners and refuse
selected composites whose visible image evidence cannot be attributed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    inspect_slide_renders,
    prepare_slide_renders,
    slide_render_dependencies,
)

if TYPE_CHECKING:
    from yizkr.pipeline.transcribe import Transcription

ExclusionCheck = Callable[[str, Sequence[str]], bool]


def _positions(positions: tuple[int, ...], mode: str) -> None:
    if mode not in {"explicit", "layout"}:
        raise ValueError("unknown slide vision mode")
    if mode == "layout":
        if positions != ():
            raise ValueError(
                "layout routing does not accept explicit positions"
            )
        return
    if (
        not isinstance(positions, tuple)
        or not positions
        or any(
            type(position) is not int or position < 1 for position in positions
        )
        or len(set(positions)) != len(positions)
    ):
        raise ValueError(
            "slide vision needs distinct positive slide positions"
        )


def _configuration(positions, settings, mode):
    return {
        **(
            {"mode": mode}
            if mode == "layout"
            else {"positions": sorted(positions)}
        ),
        "settings": {
            "executable": settings.executable,
            "long_edge": settings.long_edge,
            "timeout": settings.timeout,
        },
    }


@dataclass(frozen=True)
class SlideVisionSelection:
    """A durable source-specific choice, independent of workspace paths."""

    positions: tuple[int, ...] = ()
    settings: SlideRenderSettings = SlideRenderSettings()
    mode: str = "explicit"

    def __post_init__(self) -> None:
        _positions(self.positions, self.mode)

    def configuration(self) -> dict[str, Any]:
        return _configuration(self.positions, self.settings, self.mode)

    def request(self, directory: Path) -> SlideVisionRequest:
        return SlideVisionRequest(
            self.positions, directory, self.settings, self.mode
        )


@dataclass(frozen=True)
class SlideVisionRequest:
    """A slide-reading policy and a dedicated caller-owned render folder.

    The folder retains render products independently of the model cache.
    Explicit positions follow presentation order, including hidden slides;
    layout mode resolves its own positions. Neither choice changes source
    ownership or authorizes mixed-source evidence.
    """

    positions: tuple[int, ...]
    directory: Path
    settings: SlideRenderSettings = SlideRenderSettings()
    mode: str = "explicit"

    def __post_init__(self) -> None:
        _positions(self.positions, self.mode)
        if not isinstance(self.directory, Path):
            raise ValueError("slide vision needs a render workspace Path")

    def configuration(self) -> dict[str, Any]:
        return _configuration(self.positions, self.settings, self.mode)


def resolve_slide_vision(
    source: Path,
    content: PresentationContent,
    request: SlideVisionRequest,
    *,
    excluded: ExclusionCheck,
) -> tuple[SlideVisionRequest | None, dict[str, Any] | None]:
    """Resolve an optional layout policy without rendering or publication."""
    if request.mode == "explicit":
        return request, None
    from yizkr.pipeline.slide_routing import plan_slide_layout

    original = source.read_bytes()
    if (
        content.inventory is None
        or content.inventory.source != ArtifactRef.from_bytes(original)
    ):
        raise ValueError("prepared presentation belongs to another source")
    sources = project_sources(
        content.inventory,
        request.directory,
        data=content.inventory_bytes,
        excluded=excluded,
    )
    plan = plan_slide_layout(
        original, sources, excluded=excluded, source_names=(source.name,)
    )
    if source.read_bytes() != original:
        raise ValueError("presentation changed during slide routing")
    effective = (
        replace(request, positions=plan.positions, mode="explicit")
        if plan.positions
        else None
    )
    return effective, plan.configuration()


def _selection(request, admission) -> None:
    positions = set(request.positions)
    if not positions <= {proof.position for proof in admission}:
        raise ValueError("selected slide position is outside the presentation")
    mixed = [
        proof.position
        for proof in admission
        if proof.position in positions and proof.image_part_ids
    ]
    if mixed:
        raise ValueError(
            "selected slide has original image contributors; "
            f"mixed-source attribution is unsupported at positions {mixed}"
        )


def slide_vision_dependencies(
    source: Path,
    content: PresentationContent,
    request: SlideVisionRequest,
    *,
    excluded: ExclusionCheck,
) -> dict[str, Any]:
    """Current visual generation basis, independent of saved output health."""
    from yizkr.pipeline.slide_admission import SlideAdmission

    effective, report = resolve_slide_vision(
        source, content, request, excluded=excluded
    )
    configuration = request.configuration()
    if report is not None:
        configuration["slide_routing"] = report
    if effective is None:
        return configuration
    observed = slide_render_dependencies(
        source,
        request.directory,
        excluded=excluded,
        settings=request.settings,
        prepared=content,
    )
    _selection(
        effective,
        [
            SlideAdmission.model_validate(value)
            for value in observed.pop("admission")
        ],
    )
    return {**configuration, **observed}


def slide_vision_reading_failure(
    source: Path,
    transcription: Transcription,
    content: PresentationContent,
    request: SlideVisionRequest,
    *,
    excluded: ExclusionCheck,
) -> str | None:
    """Revalidate a persisted selected-slide reading without publication.

    Stale reading/provenance/representation data asks for regeneration. Unsafe
    current source admission or ownership raises instead of selecting another
    route. Provider caches are neither consulted nor needed.
    """
    from yizkr.pipeline.container_sources import source_context
    from yizkr.pipeline.transcribe import DECK_ENGINE, check_vision_page

    effective, report = resolve_slide_vision(
        source, content, request, excluded=excluded
    )
    if report is not None:
        metadata = (
            transcription.provenance.metadata
            if transcription.provenance is not None
            else {}
        )
        dependencies = metadata.get("dependencies", {})
        if not isinstance(dependencies, dict) or (
            dependencies.get("slide_routing") != report
        ):
            return "slide routing report is missing or stale"
    product = None
    if effective is not None:
        product = inspect_slide_renders(
            source,
            request.directory,
            excluded=excluded,
            settings=request.settings,
            prepared=content,
        )
        if product is None:
            return "slide render product is missing, damaged or stale"
        _selection(effective, [slide.admission for slide in product.slides])
    if content.inventory is None:
        raise ValueError("slide reading requires a current source inventory")
    if (
        transcription.artifact != content.inventory.source
        or transcription.engine != DECK_ENGINE
    ):
        return "slide reading identifies another source or reader"
    pages = transcription.pages or ()
    expected = sorted(
        part.locator.position
        for part in content.inventory.parts
        if part.kind == "slide_body"
    )
    if [page.page for page in pages] != expected:
        return "slide reading lacks complete physical-slide provenance"
    rendered = (
        {slide.admission.position: slide for slide in product.slides}
        if product is not None
        else {}
    )
    for page in pages:
        selected = effective is not None and page.page in effective.positions
        if page.provenance.method != ("vision" if selected else "slide-xml"):
            return "selected slide reading routes have changed"
        if selected:
            slide = rendered[page.page]
            if (
                page.provenance.reading_input
                != slide.representation.byte_object
                or page.provenance.render_product_sha256 != product.digest
            ):
                return "slide reading does not identify its rendered input"
            if not check_vision_page(transcription, page).passed:
                return (
                    "selected slide vision record no longer passes validation"
                )
        elif (
            page.body_end is not None
            or page.provenance.reading_input is not None
            or page.provenance.render_product_sha256 is not None
        ):
            return (
                "native slide reading carries incompatible render provenance"
            )
    sources = project_sources(
        content.inventory,
        request.directory,
        data=content.inventory_bytes,
        excluded=excluded,
    )
    context = source_context(transcription, sources)
    if any(span.state != "attributed" for span in context.readings):
        return (
            "slide body and speaker notes no longer have supported boundaries"
        )
    if {span.support.locator.position for span in context.readings} != {
        page.page for page in pages
    }:
        return "slide reading lacks current source-part support"
    return None


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


def restore_slide_render_representations(
    content: PresentationContent,
    product: SlideRenderProduct,
    directory: Path,
    *,
    excluded: ExclusionCheck,
) -> None:
    """Merge verified render bytes into current preparation, in memory only.

    Call after current product inspection. Fresh source eligibility, owners
    and readings remain authoritative; the workspace contributes no new ones.
    """
    if content.inventory is None or content.inventory.source != product.source:
        raise ValueError("render product belongs to another source inventory")
    parts = {part.id: part for part in content.inventory.parts}
    payloads = []
    for slide in product.slides:
        part = parts.get(slide.admission.part_id)
        if (
            part is None
            or part.kind != "slide_body"
            or part.locator.position != slide.admission.position
        ):
            raise ValueError("render product has no matching slide body")
        payloads.append(
            (part, slide.representation, _payload(directory, slide, excluded))
        )
    for part, representation, data in payloads:
        part.representations = [
            old
            for old in part.representations
            if old.method != representation.method
        ] + [representation.model_copy(deep=True)]
        content.inventory_bytes[representation.byte_object.sha1] = data


def slide_image_inputs(
    read: ContainerPages,
) -> dict[Path, tuple[ArtifactRef, ArtifactRef]]:
    """Snapshot every independent outbound image, including unplaced ones."""
    if read.sources is None:
        raise ValueError("slide reading requires prepared source bindings")
    inputs = {}
    for owner, image in read.images.items():
        bound = read.sources.sources.get(owner)
        current = bound.current_input if bound is not None else None
        if current is None:
            raise ValueError("independent slide image has no current input")
        inputs[image.path] = (bound.owner, current.representation)
    return inputs


def _check_image_input(
    path, sources, image_inputs, directory, excluded
) -> None:
    expected = image_inputs.get(path)
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
            excluded(reference.sha1, (*source.names, path.name))
            for reference in (owner, representation)
        )
    ):
        raise ValueError("independent slide image is excluded or changed")
    payload = _read_regular(
        directory,
        path.relative_to(directory).as_posix(),
    )
    if payload is None or ArtifactRef.from_bytes(payload) != representation:
        raise ValueError("independent slide image input is missing or damaged")


def verify_slide_image_inputs(
    content: PresentationContent,
    directory: Path,
    inputs: dict[Path, tuple[ArtifactRef, ArtifactRef]],
    *,
    excluded: ExclusionCheck,
    path: Path | None = None,
) -> None:
    """Readmit actual outbound bytes independently of painted contributors."""
    sources = project_sources(
        content.inventory,
        directory,
        data=content.inventory_bytes,
        excluded=excluded,
    )
    for target in (path,) if path is not None else inputs:
        _check_image_input(target, sources, inputs, directory, excluded)


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
        _check_image_input(
            path,
            sources,
            self.image_inputs,
            self.reading_directory,
            self.excluded,
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
    _selection(request, admission)
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
    restore_slide_render_representations(
        content, product, request.directory, excluded=excluded
    )
    for slide in product.slides:
        if slide.admission.position in positions:
            path = directory / slide.representation.file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                content.inventory_bytes[slide.representation.byte_object.sha1]
            )
    read.inventory = content.inventory
    read.inventory_bytes = content.inventory_bytes
    read.sources = project_sources(
        content.inventory,
        directory,
        data=content.inventory_bytes,
        excluded=excluded,
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
    inputs = slide_image_inputs(read)
    return SlideVisionProof(
        source, content, request, product, directory, excluded, inputs
    )
