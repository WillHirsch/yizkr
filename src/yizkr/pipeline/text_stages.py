"""Shared text evidence operations, without workflow or graph ownership.

Callers choose eligible inputs, dispatch and reuse, publish accepted products
and apply review policy. Operations borrow clients and write only explicitly
requested diagnostic artifacts. Dependency metadata describes this invocation;
it is not a second persisted freshness or approval authority.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.provenance import ProcessRecord
from yizkr.pipeline.chunking import (
    AUTO_CHUNK_THRESHOLD,
    DEFAULT_CHUNK_BUDGET,
    SIDE_OUTPUTS,
)
from yizkr.pipeline.critique import Finding, Invalid
from yizkr.pipeline.extract import ExtractionFailed
from yizkr.pipeline.interpretation_context import InterpretationContext
from yizkr.pipeline.language import LanguageResolution, resolve_language
from yizkr.pipeline.llm import LlmClient
from yizkr.pipeline.run_provenance import Capture
from yizkr.pipeline.stages import (
    atomic_write_text,
    content_hash,
    sha256_file,
    sha256_text,
)
from yizkr.pipeline.transcribe import Transcription

Status = Literal["complete", "incomplete"]


def validate_paths(
    *,
    inputs: Iterable[Path | None] = (),
    outputs: Iterable[Path | None] = (),
    workspaces: Iterable[Path | None] = (),
) -> None:
    """Refuse overlapping inputs and output destinations before execution.

    Resolve symlinks and existing hardlinks. Inputs must also stay outside
    output workspaces; diagnostic files may be inside one. Callers supply
    their input paths because an in-memory reading does not retain them.
    This checks configuration, not concurrent filesystem mutation.
    """
    try:
        _validate_paths(inputs, outputs, workspaces)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot inspect stage paths: {exc}") from exc


def _validate_paths(
    inputs: Iterable[Path | None],
    outputs: Iterable[Path | None],
    workspaces: Iterable[Path | None],
) -> None:
    sources = [path.resolve() for path in inputs if path is not None]
    files = [path.resolve() for path in outputs if path is not None]
    directories = [path.resolve() for path in workspaces if path is not None]

    def aliases(first: Path, second: Path) -> bool:
        return first == second or (
            first.exists() and second.exists() and first.samefile(second)
        )

    for index, output in enumerate(files):
        if output.is_dir():
            raise ValueError(f"output {output} is a directory")
        for source in sources:
            if aliases(output, source) or output in source.parents:
                raise ValueError(f"output {output} overlaps input {source}")
        for other in files[index + 1 :]:
            if (
                aliases(output, other)
                or output in other.parents
                or other in output.parents
            ):
                raise ValueError(f"outputs {output} and {other} overlap")
    for directory in directories:
        if directory.exists() and not directory.is_dir():
            raise ValueError(f"workspace {directory} is not a directory")
        for source in sources:
            if aliases(directory, source) or directory in source.parents:
                raise ValueError(
                    f"workspace {directory} contains input {source}"
                )
        for output in files:
            if aliases(directory, output) or output in directory.parents:
                raise ValueError(
                    f"output {output} overlaps workspace {directory}"
                )


@dataclass(frozen=True)
class TextReadRequest:
    source: Path
    text_file: Path
    language: str = "auto"
    fallback_language: str = "en"
    correction: bool = False


@dataclass(frozen=True)
class TextReadResult:
    transcription: Transcription
    resolution: LanguageResolution
    dependencies: dict[str, Any]

    @property
    def provenance(self) -> ProcessRecord | None:
        return self.transcription.provenance


def read_text(request: TextReadRequest) -> TextReadResult:
    """Read supplied text, retaining the media's separate source identity.

    A curator correction preserves the existing unchecked correction route.
    An ordinary text representation passes TextFileTranscriber's check.
    """
    from yizkr.pipeline.transcribe import TextFileTranscriber

    capture = Capture(
        "transcription",
        "manual" if request.correction else "deterministic",
        configuration={
            "language": request.language,
            "fallback_language": request.fallback_language,
            "correction": request.correction,
        },
    )
    if request.correction:
        text = request.text_file.read_text()
        resolution = resolve_language(
            text, request.language, fallback=request.fallback_language
        )
        transcription = Transcription(
            artifact=ArtifactRef.from_path(request.source),
            text=text,
            language=resolution.code,
            engine="correction",
        )
    else:
        reader = TextFileTranscriber(
            request.text_file,
            language=request.language,
            fallback_language=request.fallback_language,
        )
        transcription = reader.transcribe(request.source)
        resolution = reader.last_resolution
        assert resolution is not None
    result = TextReadResult(
        transcription,
        resolution,
        {
            "source_sha1": transcription.artifact.sha1,
            "text_sha256": sha256_text(transcription.text),
            "configuration": {
                "language": request.language,
                "fallback_language": request.fallback_language,
                "correction": request.correction,
            },
        },
    )
    transcription.provenance = capture.finish(result.dependencies)
    return result


@dataclass(frozen=True)
class ExtractionRequest:
    transcription: Transcription
    artifact: ArtifactRef
    strategy: Literal["chunked", "single", "auto", "file"] = "chunked"
    state_dir: Path | None = None
    raw_dump: Path | None = None
    rejects_dump: Path | None = None
    claims_file: Path | None = None
    diagnostic_dump: Path | None = None
    budget: int = DEFAULT_CHUNK_BUDGET
    retries: int = 2
    concurrency: int = 1
    read_preparation: bool = True
    pointer: str | None = None
    context: InterpretationContext | None = None

    @property
    def resolved_strategy(self) -> str:
        if self.strategy != "auto":
            return self.strategy
        return (
            "chunked"
            if len(self.transcription.text) > AUTO_CHUNK_THRESHOLD
            else "single"
        )


@dataclass(frozen=True)
class ExtractionResult:
    status: Status
    batch: ClaimBatch | None
    diagnostic_batch: ClaimBatch | None
    attention: list[str]
    failed_chunks: list[str]
    failure_reasons: dict[str, str]
    gaps: list[dict]
    artifacts: tuple[Path, ...]
    dependencies: dict[str, Any]
    incomplete_reasons: list[str] = field(default_factory=list)

    @property
    def provenance(self) -> ProcessRecord | None:
        return self.batch.extraction_run.provenance if self.batch else None

    def require_complete(self) -> ClaimBatch:
        """Return an admissible proposal, never a diagnostic partial batch."""
        if self.status != "complete" or self.batch is None:
            raise ExtractionFailed(
                "; ".join(self.incomplete_reasons) or "extraction incomplete"
            )
        return self.batch


def _model(client: LlmClient) -> dict[str, Any]:
    """Configuration exposed by the borrowed client, without host lookup."""
    return {
        key: value
        for key in ("model_identifier", "config_hash")
        if isinstance(value := getattr(client, key, None), str)
    }


def _context_attention(context: InterpretationContext | None) -> list[str]:
    if context is None or not context.gaps:
        return []
    reasons = "; ".join(dict.fromkeys(gap.reason for gap in context.gaps))
    return [f"interpretation context is incomplete: {reasons}"]


def empty_container_reading(transcription: Transcription) -> bool:
    """Prove that a current container reading holds only generated markers.

    Images have their own reading/extraction. Empty, attributed container
    spans do not warrant asking a model to invent a separate interpretation.
    Unknown, unsupported or nonempty material never takes this shortcut.
    """
    from yizkr.pipeline.containers import IMAGE_SOURCE, SLIDE_XML
    from yizkr.pipeline.transcribe import DECK_ENGINE, is_paged, reading_digest

    context = transcription.source_context
    pages = transcription.pages
    if (
        not is_paged(transcription)
        or not pages
        or context is None
        or context.container != transcription.artifact
        or context.limitations
        or len(context.readings) != len(pages)
    ):
        return False
    slide = transcription.engine == DECK_ENGINE
    unit, separator = ("Slide", "\n") if slide else ("Page", "\n\n")
    digest = reading_digest(transcription)
    expected = ""
    for page, span in zip(pages, context.readings, strict=True):
        marks = [f"[{unit} {page.page}]"] + [
            f"[{unit} {page.page}, image {number}: sha1:{sha1}]"
            for number, sha1 in enumerate(page.images, start=1)
        ]
        expected += (separator if expected else "") + "\n".join(marks)
        if (
            page.provenance.method
            not in (
                {SLIDE_XML} if slide else {"text-layer", "none", IMAGE_SOURCE}
            )
            or page.provenance.annotations
            or page.status != "blank"
            or page.start != page.end
            or page.end != len(expected)
            or span.start != page.start
            or span.end != page.end
            or span.state != "attributed"
            or span.owner != transcription.artifact
            or span.reason is not None
            or span.support.container != transcription.artifact
            or span.support.locator.kind != ("slide_body" if slide else "page")
            or span.support.locator.position != page.page
            or span.support.reading_sha256 != digest
        ):
            return False
    return transcription.text == expected


def _extract_empty_container(
    request: ExtractionRequest, dependencies: dict[str, Any]
) -> ExtractionResult:
    """Publish deterministic diagnostics without manufacturing model output."""
    from yizkr.pipeline.extract import run_metadata

    dependencies["process"] = "deterministic:empty-container"
    batch = ClaimBatch(
        extraction_run=run_metadata("deterministic:empty-container"),
        claims=[],
        entities=[],
    )
    artifacts = []
    if request.state_dir is not None:
        coverage = {
            "strategy": "empty-container",
            "reason": "no container-authored text",
            "entities_total": 0,
            "entities_not_extracted": [],
            "entities_without_claims": [],
            "distinct_subject_ids": [],
            "chunks": 0,
            "claims": 0,
            "failed_chunks": [],
            "rejected_claims": 0,
            "rejected_by_reason": {},
            "unread_objects": 0,
            "fallback": {},
        }
        for name in SIDE_OUTPUTS:
            path = request.state_dir / name
            if name == "coverage.json":
                atomic_write_text(path, json.dumps(coverage, indent=2))
                artifacts.append(path)
            else:
                path.unlink(missing_ok=True)
    for path in (
        request.raw_dump,
        request.rejects_dump,
        request.diagnostic_dump,
    ):
        if path is not None:
            path.unlink(missing_ok=True)
    return ExtractionResult(
        status="complete",
        batch=batch,
        diagnostic_batch=None,
        attention=_context_attention(request.context),
        failed_chunks=[],
        failure_reasons={},
        gaps=[],
        artifacts=tuple(artifacts),
        dependencies=dependencies,
    )


def extract(
    request: ExtractionRequest,
    client: LlmClient | None = None,
    *,
    log: Callable[[str], None] | None = None,
) -> ExtractionResult:
    """Extract proposals and classify completeness before caller publication.

    Failure, resumable pause and interruption propagate with their original
    types. An incomplete result keeps usable claims only as diagnostics.
    The caller owns client cleanup, accepted outputs and review decisions.
    Optional interpretation context is a separate, declared input: it never
    changes the primary reading or supplies a claim's supporting quotation.
    """
    if request.transcription.artifact != request.artifact:
        raise ValueError("the transcription names a different source artifact")
    if request.context is not None:
        request.context.validate_target(request.transcription)
    strategy = request.resolved_strategy
    empty_container = (
        strategy in {"single", "chunked"}
        and request.pointer is None
        and empty_container_reading(request.transcription)
    )
    configuration = {"strategy": strategy}
    if strategy == "chunked" and not empty_container:
        configuration.update(
            budget=request.budget,
            retries=request.retries,
            concurrency=max(1, request.concurrency),
            read_preparation=request.read_preparation,
        )
    capture = (
        Capture(
            "extraction",
            "deterministic" if empty_container else "model",
            configuration=configuration,
        )
        if strategy != "file"
        else None
    )
    outputs = [request.diagnostic_dump]
    workspaces = []
    if strategy == "single" or empty_container:
        outputs.extend((request.raw_dump, request.rejects_dump))
    if (
        strategy == "chunked" or empty_container
    ) and request.state_dir is not None:
        outputs.extend(request.state_dir / name for name in SIDE_OUTPUTS)
        workspaces.append(request.state_dir)
    validate_paths(
        inputs=(request.claims_file,), outputs=outputs, workspaces=workspaces
    )
    dependencies: dict[str, Any] = {
        "source_sha1": request.artifact.sha1,
        "transcription_sha256": content_hash(
            request.transcription.model_dump(mode="json")
        ),
        "configuration": configuration,
    }
    if request.context is not None:
        dependencies["interpretation_context_sha256"] = content_hash(
            request.context.model_dump(mode="json")
        )
    if empty_container:
        if log is not None:
            log(
                "  no container-authored text: "
                "empty batch prepared without a model call"
            )
        result = _extract_empty_container(request, dependencies)
        assert capture is not None
        _record_batch_producer(
            result.require_complete(),
            request.transcription,
            capture,
            dependencies,
        )
        return result
    if strategy == "file":
        from yizkr.pipeline.extract import ClaimFileExtractor

        if request.claims_file is None:
            raise ValueError("file extraction requires claims_file")
        try:
            dependencies["claims_file_sha256"] = sha256_file(
                request.claims_file
            )
        except OSError as exc:
            raise ExtractionFailed(
                f"cannot read the claims file {request.claims_file}: "
                f"{exc.strerror or exc}"
            ) from exc
        extractor = ClaimFileExtractor(
            request.claims_file, context=request.context
        )
    elif strategy in ("chunked", "single"):
        if client is None:
            raise ValueError("model extraction requires a borrowed client")
        dependencies["model"] = _model(client)
        if strategy == "chunked":
            from yizkr.pipeline.chunking import ChunkedClaimExtractor

            if request.read_preparation:
                dependencies["pointer_sha256"] = (
                    sha256_text(request.pointer)
                    if request.pointer is not None
                    else None
                )
            extractor = ChunkedClaimExtractor(
                client,
                state_dir=request.state_dir,
                budget=request.budget,
                retries=request.retries,
                concurrency=request.concurrency,
                log=log,
                read_preparation=request.read_preparation,
                pointer=request.pointer,
                context=request.context,
            )
        else:
            from yizkr.pipeline.extract import LlmClaimExtractor

            extractor = LlmClaimExtractor(
                client,
                raw_dump=request.raw_dump,
                rejects_dump=request.rejects_dump,
                context=request.context,
            )
    else:
        raise ValueError(f"unknown extraction strategy {strategy!r}")
    batch = extractor.extract(request.transcription, request.artifact)
    failed = list(getattr(extractor, "failed_chunks", ()))
    failures = dict(getattr(extractor, "failure_reasons", {}))
    reasons = list(getattr(extractor, "incomplete_reasons", ()))
    if failed:
        reasons.append(
            f"{len(failed)} chunks failed "
            f"({', '.join(failed)}; {failed[0]}: "
            f"{failures.get(failed[0], 'unknown cause')}); "
            "re-run to retry them"
        )
    complete = not reasons
    if complete and capture is not None:
        _record_batch_producer(
            batch, request.transcription, capture, dependencies, client=client
        )
    attention = [
        *getattr(extractor, "attention", ()),
        *_context_attention(request.context),
    ]
    if complete and not batch.claims and not attention:
        attention.append("no claims extracted: the supplied batch is empty")
    attention.extend(reason for reason in reasons if reason not in attention)
    paths = []
    if strategy == "chunked" and complete and request.state_dir is not None:
        paths.extend(request.state_dir / name for name in SIDE_OUTPUTS)
    elif strategy == "single":
        paths.extend(
            path
            for path in (request.raw_dump, request.rejects_dump)
            if path is not None
        )
    artifacts = [path for path in paths if path.is_file()]
    if request.diagnostic_dump is not None:
        if complete:
            request.diagnostic_dump.unlink(missing_ok=True)
        else:
            atomic_write_text(
                request.diagnostic_dump,
                json.dumps(
                    {
                        "status": "incomplete",
                        "reasons": reasons,
                        "dependencies": dependencies,
                        "diagnostic_batch": batch.model_dump(mode="json"),
                    },
                    indent=2,
                ),
            )
            artifacts.append(request.diagnostic_dump)
    return ExtractionResult(
        status="complete" if complete else "incomplete",
        batch=batch if complete else None,
        diagnostic_batch=None if complete else batch,
        attention=attention,
        failed_chunks=failed,
        failure_reasons=failures,
        gaps=list(getattr(extractor, "gaps", ())),
        artifacts=tuple(artifacts),
        dependencies=dependencies,
        incomplete_reasons=reasons,
    )


def _record_batch_producer(
    batch, transcription, capture, dependencies, *, client=None
):
    """Record new production; imported products keep their producer."""
    parents = (transcription.provenance,) if transcription.provenance else ()
    record = capture.finish(
        dependencies,
        client=client,
        parents=parents,
        limitations=() if parents else ("input reading producer unavailable",),
    )
    batch.extraction_run = batch.extraction_run.model_copy(
        update={"provenance": record}
    )


@dataclass(frozen=True)
class CritiqueRequest:
    batch: ClaimBatch
    transcription: Transcription
    context: InterpretationContext | None = None


@dataclass(frozen=True)
class CritiqueResult:
    findings: list[Finding | Invalid]
    status: Status
    attention: list[str]
    incomplete_reasons: list[str]
    dependencies: dict[str, Any]
    artifacts: tuple[Path, ...] = ()
    provenance: ProcessRecord | None = None


def critique(request: CritiqueRequest, client: LlmClient) -> CritiqueResult:
    """Return advisory findings, preserving an unfinished critic's status."""
    from yizkr.pipeline.critique import run_critique

    capture = Capture("critique", "model")
    attention = _context_attention(request.context)
    incomplete: list[str] = []
    findings = run_critique(
        client,
        request.batch,
        request.transcription,
        attention=attention,
        incomplete_reasons=incomplete,
        context=request.context,
    )
    attention.extend(
        reason for reason in incomplete if reason not in attention
    )
    result = CritiqueResult(
        findings=findings,
        status="incomplete" if incomplete else "complete",
        attention=attention,
        incomplete_reasons=incomplete,
        dependencies={
            "batch_sha256": content_hash(
                request.batch.model_dump(mode="json")
            ),
            "transcription_sha256": content_hash(
                request.transcription.model_dump(mode="json")
            ),
            "model": _model(client),
            **(
                {
                    "interpretation_context_sha256": content_hash(
                        request.context.model_dump(mode="json")
                    )
                }
                if request.context is not None
                else {}
            ),
        },
    )
    if result.status != "complete":
        return result
    from dataclasses import replace

    parent = request.batch.extraction_run.provenance
    record = capture.finish(
        result.dependencies,
        client=client,
        parents=(parent,) if parent else (),
        limitations=() if parent else ("batch producer unavailable",),
    )
    result.dependencies["provenance"] = record.model_dump(mode="json")
    return replace(result, provenance=record)
