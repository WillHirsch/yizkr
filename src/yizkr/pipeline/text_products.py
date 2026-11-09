"""Persist one text source's completed interpretations independently of caches.

The checkpoint owns products and their dependencies, never review or graph
authority. Each completed stage replaces one checksummed document atomically;
unfinished results remain diagnostics. Callers own the borrowed model clients.
"""

from __future__ import annotations

import fcntl
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.corpus import ExclusionList, is_excluded, read_exclusions
from yizkr.core.provenance import ProcessRecord
from yizkr.pipeline import text_stages
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.chunking import DEFAULT_CHUNK_BUDGET, SIDE_OUTPUTS
from yizkr.pipeline.critique import Finding, Invalid
from yizkr.pipeline.stages import atomic_write_text, content_hash, sha256_text
from yizkr.pipeline.transcribe import Transcription

PRODUCT_FILE = "text-products.json"
LOCK_FILE = ".text-products.lock"


class TextProductsError(ValueError):
    """Saved products or current inputs cannot support this invocation."""


class TextProductsIncomplete(TextProductsError):
    """An unfinished stage has no admissible complete product bundle."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextSettings(_Record):
    language: str = Field(default="auto", min_length=1)
    fallback_language: str = Field(default="en", min_length=1)
    strategy: Literal["single", "chunked", "auto"] = "chunked"
    budget: int = Field(default=DEFAULT_CHUNK_BUDGET, gt=0)
    retries: int = Field(default=2, ge=0)
    concurrency: int = Field(default=1, gt=0)
    read_preparation: bool = True


class CritiqueSnapshot(_Record):
    findings: tuple[Finding | Invalid, ...]
    status: Literal["complete", "incomplete"]
    attention: tuple[str, ...]
    incomplete_reasons: tuple[str, ...]
    dependencies: dict[str, Any]
    provenance: ProcessRecord | None

    @classmethod
    def from_result(cls, result: text_stages.CritiqueResult):
        return cls(
            findings=tuple(result.findings),
            status=result.status,
            attention=tuple(result.attention),
            incomplete_reasons=tuple(result.incomplete_reasons),
            dependencies=result.dependencies,
            provenance=result.provenance,
        )


class _Checkpoint(_Record):
    format: Literal[1] = 1
    artifact: ArtifactRef
    source_name: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    settings: TextSettings
    reading: Transcription
    reading_dependencies: dict[str, Any]
    batch: ClaimBatch | None = None
    extraction_dependencies: dict[str, Any] = Field(default_factory=dict)
    extraction_attention: tuple[str, ...] = ()
    critique: CritiqueSnapshot | None = None
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def consistent(self):
        if self.digest != content_hash(
            self.model_dump(mode="json", exclude={"digest"})
        ):
            raise ValueError("text product checksum differs")
        if (
            self.reading.artifact != self.artifact
            or self.reading.provenance is None
            or self.reading_dependencies.get("source_sha1")
            != self.artifact.sha1
            or self.reading_dependencies.get("text_sha256")
            != sha256_text(self.reading.text)
            or self.reading_dependencies.get("configuration")
            != {
                "language": self.settings.language,
                "fallback_language": self.settings.fallback_language,
                "correction": False,
            }
        ):
            raise ValueError("text reading dependencies differ")
        reading_hash = content_hash(self.reading.model_dump(mode="json"))
        if self.batch is None:
            if (
                self.extraction_dependencies
                or self.extraction_attention
                or self.critique is not None
            ):
                raise ValueError("text checkpoint lacks its parent batch")
        elif (
            self.batch.extraction_run.provenance is None
            or self.extraction_dependencies.get("source_sha1")
            != self.artifact.sha1
            or self.extraction_dependencies.get("transcription_sha256")
            != reading_hash
        ):
            raise ValueError("text extraction dependencies differ")
        if self.batch is not None:
            strategy = text_stages.ExtractionRequest(
                self.reading, self.artifact, strategy=self.settings.strategy
            ).resolved_strategy
            configuration = {"strategy": strategy}
            if strategy == "chunked":
                configuration.update(
                    budget=self.settings.budget,
                    retries=self.settings.retries,
                    concurrency=self.settings.concurrency,
                    read_preparation=self.settings.read_preparation,
                )
            if (
                self.extraction_dependencies.get("configuration")
                != configuration
            ):
                raise ValueError("text extraction settings differ")
        if self.critique is not None and (
            self.critique.status != "complete"
            or self.critique.incomplete_reasons
            or self.critique.provenance is None
            or self.critique.dependencies.get("transcription_sha256")
            != reading_hash
            or self.critique.dependencies.get("batch_sha256")
            != content_hash(self.batch.model_dump(mode="json"))
        ):
            raise ValueError("text critique is incomplete or inconsistent")
        return self


class TextProducts(_Checkpoint):
    """A complete immutable snapshot; its digest binds every saved field."""

    batch: ClaimBatch
    critique: CritiqueSnapshot


def _checkpoint(**material) -> _Checkpoint:
    # Hash the model's JSON representation, including all default fields.
    material.pop("digest", None)
    initial = _Checkpoint.model_construct(digest="0" * 64, **material)
    data = initial.model_dump(mode="json", exclude={"digest"})
    data["digest"] = content_hash(data)
    return _Checkpoint.model_validate(data)


@contextmanager
def _workspace(directory: Path, *, create: bool) -> Iterator[None]:
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    try:
        handle = (directory / LOCK_FILE).open("a+" if create else "r")
    except OSError as exc:
        raise TextProductsError(
            "text product workspace is unavailable"
        ) from exc
    with handle:
        try:
            fcntl.flock(
                handle,
                (fcntl.LOCK_EX if create else fcntl.LOCK_SH) | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise TextProductsError(
                "text product workspace is in use"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _source(
    source: Path,
    exclusions: ExclusionList,
    saved: _Checkpoint | None = None,
) -> tuple[ArtifactRef, str]:
    if not isinstance(exclusions, ExclusionList):
        raise TextProductsError("text ingestion requires explicit exclusions")
    current = (
        read_exclusions(exclusions.source).with_embedded(exclusions.embedded)
        if exclusions.source is not None
        else exclusions
    )
    if is_excluded(
        current,
        source,
        name=saved.source_name if saved else None,
        sha1=saved.artifact.sha1 if saved else None,
    ):
        raise TextProductsError("text source is on the never-ingest list")
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise TextProductsError("text source is unavailable") from exc
    artifact = ArtifactRef(sha1=hashlib.sha1(data).hexdigest())
    digest = hashlib.sha256(data).hexdigest()
    if is_excluded(current, name=source.name, sha1=artifact.sha1):
        raise TextProductsError("text source is on the never-ingest list")
    if saved is not None and (
        artifact != saved.artifact or digest != saved.source_sha256
    ):
        raise TextProductsError("text source bytes differ from saved products")
    return artifact, digest


def _paths(source: Path, directory: Path, exclusions: ExclusionList) -> None:
    if not isinstance(exclusions, ExclusionList):
        raise TextProductsError("text ingestion requires explicit exclusions")
    text_stages.validate_paths(
        inputs=(source, exclusions.source),
        outputs=(
            directory / PRODUCT_FILE,
            directory / LOCK_FILE,
            directory / "extraction-answer.txt",
            directory / "extraction-rejects.json",
            directory / "incomplete-extraction.json",
            directory / "incomplete-critique.json",
            *(directory / "extraction" / name for name in SIDE_OUTPUTS),
        ),
        workspaces=(directory,),
    )


def _read(directory: Path) -> _Checkpoint | None:
    path = directory / PRODUCT_FILE
    if not path.exists():
        return None
    try:
        return _Checkpoint.model_validate_json(path.read_text())
    except (OSError, ValueError) as exc:
        raise TextProductsError("saved text products are invalid") from exc


def _publish(directory: Path, checkpoint: _Checkpoint) -> None:
    check_cancelled()
    atomic_write_text(
        directory / PRODUCT_FILE, checkpoint.model_dump_json(indent=2) + "\n"
    )


def _complete(checkpoint: _Checkpoint | None) -> TextProducts:
    if (
        checkpoint is None
        or checkpoint.batch is None
        or checkpoint.critique is None
    ):
        raise TextProductsIncomplete("text products are not complete")
    return TextProducts.model_validate(checkpoint.model_dump(mode="json"))


def load_text_products(
    source: Path, directory: Path, *, exclusions: ExclusionList
) -> TextProducts:
    """Read complete products after checking current bytes and exclusions."""
    _paths(source, directory, exclusions)
    with _workspace(directory, create=False):
        checkpoint = _read(directory)
        _source(source, exclusions, checkpoint)
        return _complete(checkpoint)


def prepare_text_products(
    source: Path,
    directory: Path,
    *,
    exclusions: ExclusionList,
    settings: TextSettings | None = None,
    extractor_client=None,
    critic_client=None,
) -> TextProducts:
    """Resume one source's reading, extraction and critique checkpoints.

    Changed source bytes or settings require a separate product directory.
    Already completed stages use their saved producers, without a model call.
    The caller must explicitly review the resulting proposals before writing.
    """
    _paths(source, directory, exclusions)
    with _workspace(directory, create=True):
        checkpoint = _read(directory)
        if settings is None:
            settings = checkpoint.settings if checkpoint else TextSettings()
        artifact, source_hash = _source(source, exclusions, checkpoint)
        if checkpoint is not None and checkpoint.settings != settings:
            raise TextProductsError("text settings differ from saved products")
        if checkpoint is None:
            check_cancelled()
            reading = text_stages.read_text(
                text_stages.TextReadRequest(
                    source=source,
                    text_file=source,
                    language=settings.language,
                    fallback_language=settings.fallback_language,
                )
            )
            checkpoint = _checkpoint(
                artifact=artifact,
                source_name=source.name,
                source_sha256=source_hash,
                settings=settings,
                reading=reading.transcription,
                reading_dependencies=reading.dependencies,
            )
            _source(source, exclusions, checkpoint)
            _publish(directory, checkpoint)
        if checkpoint.batch is None:
            _source(source, exclusions, checkpoint)
            check_cancelled()
            if extractor_client is None:
                raise TextProductsIncomplete("text extraction needs a client")
            result = text_stages.extract(
                text_stages.ExtractionRequest(
                    transcription=checkpoint.reading,
                    artifact=checkpoint.artifact,
                    strategy=settings.strategy,
                    state_dir=directory / "extraction",
                    raw_dump=directory / "extraction-answer.txt",
                    rejects_dump=directory / "extraction-rejects.json",
                    diagnostic_dump=directory / "incomplete-extraction.json",
                    budget=settings.budget,
                    retries=settings.retries,
                    concurrency=settings.concurrency,
                    read_preparation=settings.read_preparation,
                ),
                extractor_client,
            )
            _source(source, exclusions, checkpoint)
            if result.status != "complete" or result.batch is None:
                raise TextProductsIncomplete(
                    "; ".join(result.incomplete_reasons)
                    or "text extraction is incomplete"
                )
            checkpoint = _checkpoint(
                **(
                    dict(checkpoint)
                    | {
                        "batch": result.batch,
                        "extraction_dependencies": result.dependencies,
                        "extraction_attention": tuple(result.attention),
                    }
                )
            )
            _publish(directory, checkpoint)
        if checkpoint.critique is None:
            _source(source, exclusions, checkpoint)
            check_cancelled()
            if critic_client is None:
                raise TextProductsIncomplete("text critique needs a client")
            result = text_stages.critique(
                text_stages.CritiqueRequest(
                    batch=checkpoint.batch, transcription=checkpoint.reading
                ),
                critic_client,
            )
            _source(source, exclusions, checkpoint)
            critique = CritiqueSnapshot.from_result(result)
            if critique.status != "complete":
                atomic_write_text(
                    directory / "incomplete-critique.json",
                    critique.model_dump_json(indent=2) + "\n",
                )
                raise TextProductsIncomplete(
                    "; ".join(critique.incomplete_reasons)
                    or "text critique is incomplete"
                )
            checkpoint = _checkpoint(
                **(dict(checkpoint) | {"critique": critique})
            )
            _publish(directory, checkpoint)
            (directory / "incomplete-critique.json").unlink(missing_ok=True)
        return _complete(checkpoint)
