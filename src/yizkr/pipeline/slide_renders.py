"""Admitted PPTX representations, separate from readings and source owners.

The public operation rechecks original inputs, contributors and exclusions on
both a cache hit and publication. It never invokes a model or changes a graph.
Use a dedicated local workspace; callers retain ownership of its lifetime.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from yizkr.claims import ArtifactRef
from yizkr.pipeline import slide_renderer
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.container_inventory import (
    CONTAINER_INVENTORY,
    Eligibility,
    SourceRepresentation,
    _keep,
    _path,
    _read_regular,
    keep_inventory,
    read_inventory,
)
from yizkr.pipeline.container_sources import prepare_sources, project_sources
from yizkr.pipeline.presentation import unpack_pptx
from yizkr.pipeline.slide_admission import SlideAdmission, admit_slides
from yizkr.pipeline.slide_renderer import SlideRenderSettings
from yizkr.pipeline.stages import canonical_json

RENDER_RECORD = "slide-renders.json"
RENDER_METHOD = "libreoffice-pdfium-slide"
ExclusionCheck = Callable[[str, Sequence[str]], bool]
_DEFAULT_SETTINGS = SlideRenderSettings()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash(value) -> str:
    return _digest(canonical_json(value).encode())


class SlideRender(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    admission: SlideAdmission
    representation: SourceRepresentation
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class SlideRenderProduct(BaseModel):
    """Checksummed output metadata, not historical attribution or consent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal[1] = 1
    source: ArtifactRef
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    renderer: dict[str, JsonValue]
    basis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    slides: tuple[SlideRender, ...]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def intact(self):
        if not self.slides or [s.admission.position for s in self.slides] != (
            list(range(1, len(self.slides) + 1))
        ):
            raise ValueError("render product needs every slide in order")
        if len({s.admission.part_id for s in self.slides}) != len(self.slides):
            raise ValueError("render product repeats a slide")
        for slide in self.slides:
            rep = slide.representation
            expected = f"slide-renders/{rep.byte_object.sha1}.png"
            if (
                rep.file != expected
                or rep.media_type != "image/png"
                or rep.method != RENDER_METHOD
                or rep.eligibility.state != "allowed"
                or rep.placement_ids
            ):
                raise ValueError("invalid slide representation")
        if self.digest != _hash(
            self.model_dump(mode="json", exclude={"digest"})
        ):
            raise ValueError("render product checksum differs")
        return self


@contextmanager
def _workspace(directory: Path):
    if directory.is_symlink():
        raise ValueError("a render workspace must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        _path(directory, ".slide-renders.lock"),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("render workspace is in use") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _sources(source, original, directory, excluded):
    content = unpack_pptx(source)
    inventory = content.inventory
    if inventory is None or inventory.source != ArtifactRef.from_bytes(
        original
    ):
        raise ValueError("PPTX changed during inventory preparation")
    previous = read_inventory(directory, verify_bytes=False)
    records = (
        _path(directory, name) for name in (CONTAINER_INVENTORY, RENDER_RECORD)
    )
    if previous is None and any(
        path.exists() or path.is_symlink() for path in records
    ):
        raise ValueError("prior source bindings are missing or unreadable")
    inventory = prepare_sources(
        inventory,
        content.inventory_bytes,
        directory,
        previous=previous,
        excluded=excluded,
    )
    sources = project_sources(
        inventory, directory, data=content.inventory_bytes, excluded=excluded
    )
    admission = admit_slides(
        original, sources, excluded=excluded, source_names=(source.name,)
    )
    return inventory, content.inventory_bytes, admission


def _basis(original, renderer, admission, inventory):
    contributors = {p for slide in admission for p in slide.image_part_ids}
    return _hash(
        {
            "source_sha256": _digest(original),
            "renderer": renderer,
            "admission": [a.model_dump(mode="json") for a in admission],
            "owners": {
                part.id: part.owner_binding.model_dump(mode="json")
                for part in inventory.parts
                if part.id in contributors and part.owner_binding is not None
            },
            "processing": {
                name: _digest(Path(__file__).with_name(name).read_bytes())
                for name in (
                    "slide_renders.py",
                    "slide_renderer.py",
                    "slide_admission.py",
                    "presentation.py",
                    "pptx_inventory.py",
                    "container_inventory.py",
                    "container_sources.py",
                    "textlayer.py",
                )
            },
        }
    )


def _saved(directory, basis, admission):
    raw = _read_regular(directory, RENDER_RECORD)
    if raw is None:
        return None
    try:
        product = SlideRenderProduct.model_validate_json(raw)
    except ValueError:
        return None
    if (
        product.basis_sha256 != basis
        or tuple(s.admission for s in product.slides) != admission
    ):
        return None
    inventory = read_inventory(directory)
    if inventory is None or inventory.source != product.source:
        return None
    parts = {p.id: p for p in inventory.parts}
    for slide in product.slides:
        rep = slide.representation
        part = parts.get(slide.admission.part_id)
        if part is None or rep not in part.representations:
            return None
        payload = _read_regular(directory, rep.file)
        if payload is None or _digest(payload) != slide.sha256:
            return None
    return product


def _check_output(rep: SourceRepresentation, excluded: ExclusionCheck):
    if excluded(rep.byte_object.sha1, (rep.file, Path(rep.file).name)):
        raise ValueError("rendered slide is excluded")


def prepare_slide_renders(
    source: Path,
    directory: Path,
    *,
    excluded: ExclusionCheck,
    settings: SlideRenderSettings = _DEFAULT_SETTINGS,
) -> SlideRenderProduct:
    """Prepare all slides or refuse, without marking them transcribed.

    Exclusions are a required current policy input, including on reuse. A
    returned representation is eligible for the supported rendering route;
    attributing a composite reading to historical owners is a separate step.
    The renderer is optional and is checked even when a saved product exists.
    """
    check_cancelled()
    with _workspace(directory):
        original = source.read_bytes()
        inventory, data, admission = _sources(
            source, original, directory, excluded
        )
        renderer = slide_renderer.renderer_identity(settings)
        basis = _basis(original, renderer, admission, inventory)
        saved = _saved(directory, basis, admission)
        if saved is not None:
            if source.read_bytes() != original:
                raise ValueError("PPTX changed during render reuse")
            current, _, current_admission = _sources(
                source, original, directory, excluded
            )
            if _basis(original, renderer, current_admission, current) != basis:
                raise ValueError(
                    "source admission changed during render reuse"
                )
            for slide in saved.slides:
                _check_output(slide.representation, excluded)
            check_cancelled()
            return saved
        result = slide_renderer.render_slides(
            original, expected_slides=len(admission), settings=settings
        )
        if result.renderer != renderer:
            raise ValueError("renderer changed during preparation")
        if [p.position for p in result.pages] != [
            a.position for a in admission
        ]:
            raise ValueError("renderer did not return every slide in order")
        # Policy and original inputs may have changed while the renderer ran.
        if source.read_bytes() != original:
            raise ValueError("PPTX changed during rendering")
        current, _, current_admission = _sources(
            source, original, directory, excluded
        )
        if _basis(original, renderer, current_admission, current) != basis:
            raise ValueError("source admission changed during rendering")
        slides = []
        parts = {p.id: p for p in inventory.parts}
        for proof, page in zip(admission, result.pages, strict=True):
            blob = ArtifactRef.from_bytes(page.png)
            rep = SourceRepresentation(
                byte_object=blob,
                file=f"slide-renders/{blob.sha1}.png",
                media_type="image/png",
                method=RENDER_METHOD,
                eligibility=Eligibility(state="allowed"),
            )
            _check_output(rep, excluded)
            slides.append(
                SlideRender(
                    admission=proof,
                    representation=rep,
                    sha256=_digest(page.png),
                    width=page.width,
                    height=page.height,
                )
            )
            # Representations do not replace native readings or image owners.
            part = parts[proof.part_id]
            part.representations = [
                r for r in part.representations if r.method != RENDER_METHOD
            ] + [rep]
            data[blob.sha1] = page.png
        fields = {
            "format": 1,
            "source": inventory.source.model_dump(mode="json"),
            "source_sha256": _digest(original),
            "renderer": renderer,
            "basis_sha256": basis,
            "slides": [s.model_dump(mode="json") for s in slides],
        }
        product = SlideRenderProduct.model_validate(
            {**fields, "digest": _hash(fields)}
        )
        check_cancelled()
        keep_inventory(inventory, data, directory)
        check_cancelled()
        _keep(
            directory,
            RENDER_RECORD,
            product.model_dump_json(indent=2).encode(),
        )
        check_cancelled()
        return product
