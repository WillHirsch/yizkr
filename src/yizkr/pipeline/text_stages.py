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
from yizkr.pipeline.chunking import (
    AUTO_CHUNK_THRESHOLD,
    DEFAULT_CHUNK_BUDGET,
    SIDE_OUTPUTS,
)
from yizkr.pipeline.critique import Finding, Invalid
from yizkr.pipeline.extract import ExtractionFailed
from yizkr.pipeline.language import LanguageResolution, resolve_language
from yizkr.pipeline.llm import LlmClient
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


def read_text(request: TextReadRequest) -> TextReadResult:
    """Read supplied text, retaining the media's separate source identity.

    A curator correction preserves the existing unchecked correction route.
    An ordinary text representation passes TextFileTranscriber's check.
    """
    from yizkr.pipeline.transcribe import TextFileTranscriber

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
    return TextReadResult(
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
    """
    if request.transcription.artifact != request.artifact:
        raise ValueError("the transcription names a different source artifact")
    strategy = request.resolved_strategy
    outputs = [request.diagnostic_dump]
    workspaces = []
    if strategy == "single":
        outputs.extend((request.raw_dump, request.rejects_dump))
    elif strategy == "chunked" and request.state_dir is not None:
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
        "configuration": {"strategy": strategy},
    }
    configuration = dependencies["configuration"]
    if strategy == "file":
        from yizkr.pipeline.extract import ClaimFileExtractor

        if request.claims_file is None:
            raise ValueError("file extraction requires claims_file")
        dependencies["claims_file_sha256"] = sha256_file(request.claims_file)
        extractor = ClaimFileExtractor(request.claims_file)
    elif strategy in ("chunked", "single"):
        if client is None:
            raise ValueError("model extraction requires a borrowed client")
        dependencies["model"] = _model(client)
        if strategy == "chunked":
            from yizkr.pipeline.chunking import ChunkedClaimExtractor

            configuration.update(
                budget=request.budget,
                retries=request.retries,
                concurrency=max(1, request.concurrency),
                read_preparation=request.read_preparation,
            )
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
            )
        else:
            from yizkr.pipeline.extract import LlmClaimExtractor

            extractor = LlmClaimExtractor(
                client,
                raw_dump=request.raw_dump,
                rejects_dump=request.rejects_dump,
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
    attention = list(getattr(extractor, "attention", ()))
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


@dataclass(frozen=True)
class CritiqueRequest:
    batch: ClaimBatch
    transcription: Transcription


@dataclass(frozen=True)
class CritiqueResult:
    findings: list[Finding | Invalid]
    status: Status
    attention: list[str]
    incomplete_reasons: list[str]
    dependencies: dict[str, Any]
    artifacts: tuple[Path, ...] = ()


def critique(request: CritiqueRequest, client: LlmClient) -> CritiqueResult:
    """Return advisory findings, preserving an unfinished critic's status."""
    from yizkr.pipeline.critique import run_critique

    attention: list[str] = []
    incomplete: list[str] = []
    findings = run_critique(
        client,
        request.batch,
        request.transcription,
        attention=attention,
        incomplete_reasons=incomplete,
    )
    attention.extend(
        reason for reason in incomplete if reason not in attention
    )
    return CritiqueResult(
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
        },
    )
