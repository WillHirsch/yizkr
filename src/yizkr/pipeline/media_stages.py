"""Shared media reading operations without workflow or publication ownership.

Format adapters keep their reading algorithms and model inputs. Operations bind
them to source snapshots and return temporary products separately from the
transcription. Callers own reuse, persisted products and client lifetime.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from yizkr.claims import ArtifactRef, SourceSupport
from yizkr.pipeline.container_inventory import (
    ContainerInventory,
    read_inventory,
)
from yizkr.pipeline.container_sources import (
    BoundSource,
    ContainerSources,
    image_source_context,
    prepare_sources,
    reading_file,
)
from yizkr.pipeline.language import (
    LanguageResolution,
    record_language,
    resolve_language,
)
from yizkr.pipeline.llm import LlmClient
from yizkr.pipeline.pdf_inventory import PdfInventory, inventory_pdf
from yizkr.pipeline.presentation import (
    PresentationContent,
    read_derived_media,
    unpack_pptx,
)
from yizkr.pipeline.stages import canonical_json, sha256_text
from yizkr.pipeline.textlayer import check_pdf_vision, is_pdf_input
from yizkr.pipeline.transcribe import (
    DeckTranscriber,
    LlmVisionTranscriber,
    PagedReading,
    PdfPageTranscriber,
    Transcription,
    reading_digest,
    refuse_archive,
)
from yizkr.pipeline.transcription_check import (
    TranscriptionCheckFailed,
    check_transcription,
)

ExclusionPredicate = Callable[[str, Sequence[str]], bool]


def _never(_sha1: str, _names: Sequence[str] = ()) -> bool:
    return False


def _source(
    path: Path,
    expected: ArtifactRef | None,
    excluded: ExclusionPredicate | None,
) -> ArtifactRef:
    artifact = ArtifactRef.from_path(path)
    if expected is not None and artifact != expected:
        raise ValueError("the media source no longer matches its artifact")
    if excluded is not None and excluded(artifact.sha1, (path.name,)):
        raise ValueError("the media source is excluded")
    return artifact


@dataclass(frozen=True)
class PreparationRequest:
    source: Path
    expected_artifact: ArtifactRef | None = None
    existing_directory: Path | None = None
    excluded: ExclusionPredicate | None = None


@dataclass(frozen=True)
class PreparedContainer:
    """A format plan exposing the adapter's current prepared inventory."""

    source: ArtifactRef
    pdf: PdfInventory | None = None
    presentation: PresentationContent | None = None

    @property
    def inventory(self) -> ContainerInventory:
        if self.pdf is not None:
            return self.pdf.inventory
        if (
            self.presentation is not None
            and self.presentation.inventory is not None
        ):
            return self.presentation.inventory
        raise ValueError("a prepared container needs a format inventory")

    @property
    def data(self) -> dict[str, bytes]:
        if self.pdf is not None:
            return self.pdf.data
        if self.presentation is not None:
            return self.presentation.inventory_bytes
        raise ValueError("a prepared container needs a format inventory")


def prepare_container(request: PreparationRequest) -> PreparedContainer | None:
    """Inspect source structure and verified prior bindings, without writes.

    With no existing product directory, readers prepare fresh bindings in
    their temporary workspace. An explicit directory can be absent; inspecting
    it does not create it. Source bytes and existing products remain unchanged.
    """
    from yizkr.core.corpus import DECK_SUFFIXES

    artifact = _source(
        request.source, request.expected_artifact, request.excluded
    )
    excluded = request.excluded or _never
    if request.source.suffix.lower() in DECK_SUFFIXES:
        content = unpack_pptx(request.source)
        prepared = PreparedContainer(artifact, presentation=content)
    elif is_pdf_input(request.source):
        prepared = PreparedContainer(
            artifact,
            pdf=inventory_pdf(
                request.source.read_bytes(), lambda sha: excluded(sha, ())
            ),
        )
    else:
        return None
    if prepared.inventory.source != artifact:
        raise ValueError("the media source changed during preparation")
    if request.existing_directory is not None:
        directory = request.existing_directory
        inventory = prepare_sources(
            prepared.inventory,
            prepared.data,
            directory,
            previous=read_inventory(directory, verify_bytes=False),
            legacy=read_derived_media(directory),
            excluded=excluded,
        )
        if prepared.pdf is not None:
            prepared.pdf.replace_inventory(inventory)
            prepared.pdf.apply_exclusions(lambda sha: excluded(sha, ()))
        else:
            assert prepared.presentation is not None
            prepared.presentation.inventory = inventory
    return prepared


def preparation_dependencies(
    prepared: PreparedContainer | None,
) -> dict[str, Any]:
    """Stable structural inputs, excluding reading outcomes and products.

    Page/container render representations are outputs. Independent image
    representations are inputs and remain in this basis, as do eligibility and
    explicit owner bindings. Paths never identify a source or representation.
    """
    if prepared is None:
        return {}
    value = prepared.inventory.model_dump(mode="json")
    for part in value["parts"]:
        part.pop("reading")
        part.pop("file")
        if part["owner_binding"] is not None:
            part["owner_binding"].pop("file")
            # Recovering the same owner from a surviving reading changes the
            # explanation of its binding, not the input or its attribution.
            part["owner_binding"].pop("basis")
        part["representations"] = [
            {
                key: item
                for key, item in representation.items()
                if key != "file"
            }
            for representation in part["representations"]
            if part["kind"] == "embedded_resource"
        ]
    return {
        "source_sha1": prepared.source.sha1,
        "inventory_sha256": sha256_text(canonical_json(value)),
    }


@dataclass(frozen=True)
class MediaReadRequest:
    source: Path
    route: Literal["paged", "vision"]
    expected_artifact: ArtifactRef | None = None
    language: str = "auto"
    fallback_language: str = "en"
    prepared: PreparedContainer | None = None
    excluded: ExclusionPredicate | None = None


@dataclass
class MediaReadResult:
    transcription: Transcription
    resolution: LanguageResolution
    dependencies: dict[str, Any] = field(default_factory=dict)
    products: PagedReading | None = None
    prepared: PreparedContainer | None = None
    status: Literal["complete"] = field(default="complete", init=False)
    artifacts: tuple[Path, ...] = field(default=(), init=False)

    @property
    def attention(self) -> list[str]:
        attention = (
            [self.transcription.flag] if self.transcription.flag else []
        )
        if self.products is not None:
            attention.extend(self.products.attention())
        return attention

    def require_complete(self) -> Transcription:
        return self.transcription

    def close(self) -> None:
        """Release temporary products; the borrowed client stays open."""
        if self.products is not None:
            self.products.close()

    def __enter__(self) -> MediaReadResult:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def read_media(
    request: MediaReadRequest, client: Callable[[], LlmClient]
) -> MediaReadResult:
    """Read eligible media, borrowing a client only when vision is required."""
    from yizkr.core.corpus import DECK_SUFFIXES
    from yizkr.pipeline.run_provenance import Capture

    artifact = _source(
        request.source, request.expected_artifact, request.excluded
    )
    capture = Capture(
        "transcription",
        "mixed",
        document_kind=(
            "presentation"
            if request.source.suffix.lower() in DECK_SUFFIXES
            else "pdf"
            if is_pdf_input(request.source)
            else "media"
        ),
    )
    prepared = request.prepared
    if prepared is not None and (
        prepared.source != artifact or prepared.inventory.source != artifact
    ):
        raise ValueError("prepared container belongs to another source")
    if request.route not in {"paged", "vision"}:
        raise ValueError("unknown media reading route")
    if prepared is None and (
        request.route == "paged" or request.excluded is not None
    ):
        prepared = prepare_container(
            PreparationRequest(
                request.source, artifact, excluded=request.excluded
            )
        )
    dependencies = {
        "source_sha1": artifact.sha1,
        "configuration": {
            "route": request.route,
            "language": request.language,
            "fallback_language": request.fallback_language,
        },
        "preparation": preparation_dependencies(prepared),
    }
    borrowed: LlmClient | None = None

    def get_client() -> LlmClient:
        nonlocal borrowed
        if borrowed is None:
            borrowed = client()
        return borrowed

    transcription = None
    resolution = None
    if request.route == "paged":
        common = {
            "language": request.language,
            "fallback_language": request.fallback_language,
            "excluded": (lambda sha, names=(): request.excluded(sha, names))
            if request.excluded is not None
            else None,
        }
        if request.source.suffix.lower() in DECK_SUFFIXES:
            reader = DeckTranscriber(
                get_client,
                **common,
                content=prepared.presentation if prepared else None,
            )
        else:
            reader = PdfPageTranscriber(
                get_client,
                **common,
                inventory=prepared.pdf if prepared else None,
            )
        transcription = reader.transcribe(request.source)
        resolution = reader.last_resolution
    if transcription is None:
        refuse_archive(request.source)
        excluded = (
            (lambda sha: request.excluded(sha, ()))
            if request.excluded is not None
            else None
        )
        plan = prepared.pdf if prepared is not None else None
        # Check the complete outbound PDF before even constructing a provider.
        if excluded is not None and is_pdf_input(request.source):
            check_pdf_vision(request.source, excluded, inventory=plan)
        vision = LlmVisionTranscriber(
            get_client(),
            language=request.language,
            fallback_language=request.fallback_language,
            excluded=excluded,
            pdf_inventory=plan,
        )
        transcription = vision.transcribe(request.source)
        resolution = vision.last_resolution
    products = transcription.reading
    transcription.forget_reading()
    try:
        if resolution is None:
            raise ValueError(
                "the media reader returned no language resolution"
            )
        if borrowed is not None:
            dependencies["model"] = {
                key: value
                for key in ("model_identifier", "config_hash")
                if isinstance(value := getattr(borrowed, key, None), str)
            }
        if transcription.provenance is None:
            if borrowed is None:
                capture.process = "deterministic"
            transcription.provenance = capture.finish(
                dependencies, client=borrowed
            )
        return MediaReadResult(
            transcription, resolution, dependencies, products, prepared
        )
    except BaseException:
        if products is not None:
            products.close()
        raise


@dataclass(frozen=True)
class VerifiedImageReading:
    transcription: Transcription
    source: BoundSource
    supports: tuple[SourceSupport, ...]
    product_sha256: str


@dataclass(frozen=True)
class ImageReadingCheck:
    reading: VerifiedImageReading | None = None
    failure: str | None = None
    missing: bool = False


def verify_image_reading(
    sources: ContainerSources | None,
    owner: str,
    directory: Path,
    *,
    verify_product: bool = True,
) -> ImageReadingCheck:
    """Admit a saved reading against the current verified source projection.

    Missing products are separate from corrupt, stale or ineligible ones. The
    caller supplies a projection made under the current exclusion policy; this
    operation never allocates an owner or repairs a saved product.
    """
    name = reading_file(owner)
    path = directory / name
    image = f"image {owner[:12]}"
    try:
        payload = path.read_bytes()
        kept = Transcription.model_validate_json(payload)
    except FileNotFoundError:
        return ImageReadingCheck(missing=True)
    except (OSError, ValueError, RecursionError) as exc:
        return ImageReadingCheck(
            failure=(
                f"the transcription of {image} cannot be read "
                f"({type(exc).__name__}: {exc})"
            )
        )
    if kept.artifact.sha1 != owner:
        return ImageReadingCheck(
            failure=f"the transcription of {image} names another image"
        )
    source = sources.sources.get(owner) if sources else None
    if source is None or not source.readable or source.current_input is None:
        return ImageReadingCheck(
            failure=f"{image} has no eligible, unambiguous reading input"
        )
    assert sources is not None
    digest = hashlib.sha256(payload).hexdigest()
    if verify_product:
        for part in sources.inventory.parts:
            outcome = part.reading
            if (
                part.id in source.part_ids
                and outcome.state == "read"
                and outcome.product == name
                and outcome.product_sha256 != digest
            ):
                return ImageReadingCheck(
                    failure=(
                        f"the recorded transcription of {image} has changed"
                    )
                )
    failure = _image_input_failure(kept, source)
    if failure is not None:
        return ImageReadingCheck(failure=failure)
    return ImageReadingCheck(
        reading=VerifiedImageReading(
            kept,
            source,
            tuple(sources.support(part_id) for part_id in source.part_ids),
            digest,
        )
    )


def _image_input_failure(
    transcription: Transcription, source: BoundSource
) -> str | None:
    """Check a current reading/input pair, including held in-memory values."""
    image = f"image {source.owner.sha1[:12]}"
    if transcription.artifact != source.owner:
        return f"the transcription of {image} names another image"
    if not source.readable or source.current_input is None:
        return f"{image} has no eligible, unambiguous reading input"
    if transcription.source_context is not None:
        semantic = reading_digest(transcription)
        if any(
            located.support.reading_sha256 not in {None, semantic}
            for located in transcription.source_context.readings
        ):
            return f"the source support of {image} names another reading"
    observed = transcription.reading_input or transcription.artifact
    if observed != source.current_input.representation:
        return f"the prepared representation of {image} has changed"
    check = check_transcription(
        transcription.text,
        pages=1,
        record=transcription.record(),
        expect_record=True,
    )
    if not check.passed:
        return f"{TranscriptionCheckFailed(check)} ({image})"
    return None


def excerpt_dependencies(verified: VerifiedImageReading) -> dict[str, Any]:
    current = verified.source.current_input
    return {
        "source_sha1": verified.source.owner.sha1,
        "representation_sha1": current.representation.sha1
        if current
        else None,
        "reading_sha256": reading_digest(verified.transcription),
        "support_sha256": sha256_text(
            canonical_json(
                [
                    support.model_dump(mode="json")
                    for support in verified.supports
                ]
            )
        ),
    }


@dataclass(frozen=True)
class ExcerptReadRequest:
    owner: ArtifactRef
    candidates: Sequence[VerifiedImageReading]
    language: str = "auto"
    fallback_language: str = "en"


def read_excerpt(request: ExcerptReadRequest) -> MediaReadResult:
    """Select the first verified parent reading, preserving its text."""
    selected = next(
        (
            candidate
            for candidate in request.candidates
            if candidate.source.owner == request.owner
            and _image_input_failure(candidate.transcription, candidate.source)
            is None
        ),
        None,
    )
    if selected is None:
        raise RuntimeError(
            f"no container that places image {request.owner.sha1[:12]} "
            "keeps a reading "
            "of it that passes the transcription check"
        )
    kept = selected.transcription
    record = kept.record()
    resolution = (
        record_language(
            record.pages, request.language, fallback=request.fallback_language
        )
        if record is not None
        else resolve_language(
            kept.text, request.language, fallback=request.fallback_language
        )
    )
    check = check_transcription(
        kept.text, pages=1, record=record, expect_record=True
    )
    transcription = kept.model_copy(
        update={
            "language": resolution.code,
            "flag": check.signature if check.flagged else None,
        }
    )
    transcription.source_context = image_source_context(
        transcription, selected.supports
    )
    return MediaReadResult(
        transcription,
        resolution,
        {
            **excerpt_dependencies(selected),
            "configuration": {
                "language": request.language,
                "fallback_language": request.fallback_language,
            },
        },
    )
