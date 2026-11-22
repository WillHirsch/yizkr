"""Admit one document's complete saved views without running a stage.

A run publishes each stage's view (`stage_planning.VIEWS`) beside the
document's state and dependency records (`document_state.DocumentState`,
`Derivations`). Admission checks those products' own consistency: the current
source bytes and exclusions, every stage recorded complete, and each view made
from the saved views it depends on. It does not compare processing
fingerprints with current code: whether a view is fresh is the run's decision
when it makes or reuses the view. Nothing is written and no model is called.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.corpus import ExclusionList, is_excluded, read_exclusions
from yizkr.pipeline import stage_planning
from yizkr.pipeline.critique import Finding, Invalid, entries
from yizkr.pipeline.document_state import (
    BATCH_VOLATILE,
    BATCHED,
    EXCERPT,
    EXCERPTS_DIR,
    PDF,
    SUPPORTED_SUFFIXES,
    TRANSCRIPTION_VOLATILE,
    DocumentRun,
    document_kind,
)
from yizkr.pipeline.stages import content_hash
from yizkr.pipeline.textlayer import is_pdf_input
from yizkr.pipeline.transcribe import Transcription

# Single-owner kinds: the source is the only owner of its reading and claims.
SUPPORTED_KINDS = ("text", "media")
UNSUPPORTED_KINDS = {
    PDF: "a PDF is a container whose placed images are separate owners",
    "presentation": (
        "a deck is a container whose slides and placed images need "
        "registration of their own"
    ),
    EXCERPT: "an excerpt is registered through the container that places it",
    "gedcom": "a GEDCOM file has no reading to locate its claims in",
}


class ProductsRefused(ValueError):
    """Saved products or current inputs cannot enter reviewed ingestion."""


class UnsupportedKind(ProductsRefused):
    """The document's kind has no admissible single-owner products."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(
            f"{kind} sources are not admitted: {UNSUPPORTED_KINDS[kind]}"
        )


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SavedCritique(_Record):
    """The critic's findings and the reasons its stage noted for review."""

    findings: tuple[Finding | Invalid, ...]
    attention: tuple[str, ...]


class DocumentProducts(_Record):
    """One document's complete saved views, as reviewed ingestion uses them.

    `digest` binds every field, so registration can pin these exact products
    and a later admission of different ones refuses.
    """

    format: Literal[1] = 1
    kind: Literal["text", "media"]
    artifact: ArtifactRef
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reading: Transcription
    batch: ClaimBatch
    extraction_attention: tuple[str, ...]
    critique: SavedCritique

    @property
    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


def _kind(source: Path, directory: Path) -> str:
    """The document's kind: an excerpt by its place, as a run keeps it (in
    its state directory's EXCERPTS_DIR), and otherwise as its source's type
    and bytes give it, PDF bytes under another suffix included."""
    if directory.parent.name == EXCERPTS_DIR:
        return EXCERPT
    if source.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ProductsRefused("the source's file type is not one a run reads")
    kind = document_kind(source)
    try:
        if kind == "media" and is_pdf_input(source):
            return PDF
    except OSError as exc:
        raise ProductsRefused("the source is unavailable") from exc
    return kind


def _reasons(attention: dict[str, Any], stage: str) -> tuple[str, ...]:
    reasons = attention.get(stage, [])
    if not isinstance(reasons, list) or not all(
        isinstance(reason, str) for reason in reasons
    ):
        raise ProductsRefused(f"the saved {stage} attention cannot be read")
    return tuple(reasons)


def load_document_products(
    source: Path, directory: Path, *, exclusions: ExclusionList
) -> DocumentProducts:
    """Admit the complete saved products of `source` from its document
    `directory`, such as a scratch run's directory for that source.

    The never-ingest list is read again from its file and asked first, by
    the source's current and recorded names and SHA-1, and again once the
    bytes are read. Admission refuses (ProductsRefused) unless the bytes hash
    to the artifact every product names; the document kind is text or a
    standalone image (UnsupportedKind otherwise); the saved state records
    every stage of that kind complete; and the dependency record says each
    view was made from the saved views and inputs beside it as they are now.
    """
    if not isinstance(exclusions, ExclusionList):
        raise ProductsRefused("saved products require explicit exclusions")
    current = (
        read_exclusions(exclusions.source).with_embedded(exclusions.embedded)
        if exclusions.source is not None
        else exclusions
    )
    source = Path(source)
    directory = Path(directory).resolve()
    run = DocumentRun(source, directory.parent, dirname=directory.name)
    if run.unreadable is not None or not run.state_path.is_file():
        raise ProductsRefused("the document has no readable saved state")
    state = run.state
    try:
        excluded = is_excluded(
            current,
            source,
            name=Path(state.file).name,
            sha1=state.content_sha1,
        )
    except OSError as exc:
        raise ProductsRefused("the source is unavailable") from exc
    if excluded:
        raise ProductsRefused("the source is on the never-ingest list")
    kind = _kind(source, directory)
    if kind not in SUPPORTED_KINDS:
        raise UnsupportedKind(kind)
    if document_kind(Path(state.file)) != kind:
        raise ProductsRefused(
            "the saved products were made for another kind of source"
        )
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise ProductsRefused("the source is unavailable") from exc
    artifact = ArtifactRef(sha1=hashlib.sha1(data).hexdigest())
    if is_excluded(current, name=source.name, sha1=artifact.sha1):
        raise ProductsRefused("the source is on the never-ingest list")
    if state.content_sha1 != artifact.sha1:
        raise ProductsRefused(
            "the source bytes differ from the saved products"
        )

    detail = state.detail or {}
    outcomes = detail.get("stage_results")
    attention = detail.get("attention") or {}
    if (
        state.status != BATCHED
        or not isinstance(outcomes, dict)
        or not isinstance(attention, dict)
    ):
        raise ProductsRefused("the document's saved products are not complete")
    for stage in stage_planning.stages_for(kind):
        outcome = outcomes.get(stage.name)
        if (
            not isinstance(outcome, dict)
            or outcome.get("status") != "complete"
        ):
            raise ProductsRefused(
                f"the saved {stage.name} is not recorded complete"
            )
        dependencies = outcome.get("dependencies")
        if (
            isinstance(dependencies, dict)
            and dependencies.get("source_sha1", artifact.sha1) != artifact.sha1
        ):
            raise ProductsRefused("a saved product names another source")

    try:
        reading = Transcription.model_validate_json(
            run.transcription.read_bytes()
        )
        batch = ClaimBatch.model_validate_json(run.batch.read_bytes())
        findings = tuple(entries(json.loads(run.critique.read_bytes())))
        context = (
            content_hash(json.loads(run.interpretation_context.read_bytes()))
            if run.interpretation_context.is_file()
            else None
        )
    except (OSError, ValueError, RecursionError) as exc:
        raise ProductsRefused("a saved view cannot be read") from exc
    if reading.artifact != artifact or any(
        cited.artifact != artifact
        for claim in batch.claims
        for cited in (claim.source, claim.circumstance_source)
        if cited is not None
    ):
        raise ProductsRefused("a saved product names another source")
    if (
        reading.source_context is not None
        and reading.source_context.container != artifact
    ):
        raise UnsupportedKind(EXCERPT)

    derivations = run.derivations
    contents = {
        run.transcription.name: content_hash(
            reading.model_dump(mode="json"), volatile=TRANSCRIPTION_VOLATILE
        ),
        run.batch.name: content_hash(
            batch.model_dump(mode="json"), volatile=BATCH_VOLATILE
        ),
    }
    if context is not None:
        contents[run.interpretation_context.name] = context
    if any(
        derivations.content(name) != digest
        for name, digest in contents.items()
    ) or not all(derivations.current(view) for view in run.views):
        raise ProductsRefused(
            "a saved view was not made from the products its dependency "
            "record names"
        )
    return DocumentProducts(
        kind=kind,
        artifact=artifact,
        source_sha256=hashlib.sha256(data).hexdigest(),
        reading=reading,
        batch=batch,
        extraction_attention=_reasons(
            attention, stage_planning.batch_stage(kind).name
        ),
        critique=SavedCritique(
            findings=findings, attention=_reasons(attention, "critique")
        ),
    )
