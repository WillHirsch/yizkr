"""Capture truthful producer metadata and freeze ordinary immutable JSON files.

Records describe observed execution. They never discover a graph, initialize a
model client, change response keys, or invent missing legacy facts.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yizkr.core.provenance import (
    CodeProvenance,
    ProcessRecord,
    RecordRef,
    StageProvenance,
)
from yizkr.pipeline.llm import client_metadata
from yizkr.pipeline.rundir import code_version
from yizkr.pipeline.stages import canonical_json

DEFAULT_RECORD_DIR = Path("var/process-records")


def now() -> datetime:
    return datetime.now(UTC)


def record_bytes(record: ProcessRecord) -> bytes:
    return (
        json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def record_digest(record: ProcessRecord) -> str:
    return hashlib.sha256(record_bytes(record)).hexdigest()


def freeze_record(directory: Path, record: ProcessRecord) -> RecordRef:
    """Publish a content-addressed record without replacing evidence."""
    payload = record_bytes(record)
    digest = hashlib.sha256(payload).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".process-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError(
                    "existing process record differs from its digest"
                ) from None
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary)
    return RecordRef(sha256=digest)


def read_record(directory: Path, reference: RecordRef) -> ProcessRecord:
    payload = (Path(directory) / f"{reference.sha256}.json").read_bytes()
    if hashlib.sha256(payload).hexdigest() != reference.sha256:
        raise ValueError("process record content does not match its reference")
    record = ProcessRecord.model_validate_json(payload)
    if record_bytes(record) != payload:
        raise ValueError("process record is not canonically encoded")
    return record


def legacy_record(metadata: Any) -> ProcessRecord:
    """Describe only facts carried by an older batch's metadata."""
    if getattr(metadata, "provenance", None) is not None:
        return metadata.provenance
    code = CodeProvenance(
        version=metadata.pipeline_version, commit=metadata.software_commit
    )

    missing = (
        "legacy producer lacks dirty flag, end time and stage dependencies",
    )
    identifier = metadata.model_identifier
    deterministic = identifier == "gedcom-adapter" or identifier.startswith(
        "deterministic:"
    )
    manual = identifier == "manual"
    return ProcessRecord(
        kind="legacy-producer",
        code=code,
        started_at=metadata.started_at,
        stages=(
            StageProvenance(
                stage="gedcom"
                if identifier == "gedcom-adapter"
                else "extraction",
                process="deterministic"
                if deterministic
                else "manual"
                if manual
                else "unknown",
                model_identifier=None
                if deterministic or manual
                else identifier,
                code=code,
                started_at=metadata.started_at,
                limitations=missing,
            ),
        ),
        metadata={"legacy_producer_identifier": identifier},
        limitations=missing,
    )


def records_from_detail(detail: Mapping | None) -> tuple[ProcessRecord, ...]:
    """Complete non-product stage records, without inferring legacy origins."""
    if detail is None:
        return ()
    records = []
    outcomes = detail.get("stage_results") or {}
    if not isinstance(outcomes, Mapping):
        raise ValueError("stage results are not a provenance mapping")
    for result in outcomes.values():
        if (
            not isinstance(result, Mapping)
            or result.get("status") != "complete"
        ):
            continue
        dependencies = result.get("dependencies") or {}
        if not isinstance(dependencies, Mapping):
            raise ValueError("completed stage dependencies are malformed")
        if "provenance" in dependencies:
            records.append(
                ProcessRecord.model_validate(dependencies["provenance"])
            )
    return tuple(records)


def application_record(
    producers: Sequence[ProcessRecord],
    *,
    kind: str,
    stage: str,
    metadata: Mapping[str, Any],
    configuration: Mapping[str, Any] | None = None,
) -> ProcessRecord:
    """A repeatable preparation snapshot, never a claim that writes completed.

    Producers remain inline; references are added only by a caller that also
    freezes those producer records. There is no application wall-clock value
    to turn an identical retry into a different creation operation.
    """
    from yizkr.model.schema import DEFAULT_MANIFEST

    code = CodeProvenance.model_validate(code_version(fresh=True))
    config = dict(configuration or {})
    stages = []
    seen = set()
    for producer in producers:
        for record in producer.stages:
            key = record.model_dump_json()
            if key not in seen:
                stages.append(record)
                seen.add(key)
    stages.append(
        StageProvenance(
            stage=stage,
            process="deterministic",
            code=code,
            semantic_version=1,
            configuration=config,
            config_hash=hashlib.sha256(
                canonical_json(config).encode()
            ).hexdigest(),
        )
    )
    limitations = (
        tuple(
            dict.fromkeys(
                reason
                for producer in producers
                for reason in producer.limitations
            )
        )
        if producers
        else ("producer provenance unavailable",)
    )
    return ProcessRecord(
        kind=kind,
        stages=tuple(stages),
        code=code,
        metadata=dict(metadata),
        manifest_hash=hashlib.sha256(
            DEFAULT_MANIFEST.read_bytes()
        ).hexdigest(),
        limitations=limitations,
    )


def _loaded_templates(stage: str, configuration: Mapping) -> dict[str, str]:
    """The loaded template/fragment values, distinct from resource bytes.

    A long-lived process can retain a template after its source file changes.
    Names identify Python constants because some fragments are trimmed or
    composed during import, and transcription already interpolates its kinds.
    This is the operation's template dependency set, not a log of requests.
    """
    if stage == "transcription":
        if configuration.get("route") not in {"vision", "paged"}:
            return {}
        from yizkr.pipeline.transcribe import TRANSCRIPTION_PROMPT

        return {
            "pipeline.transcribe.TRANSCRIPTION_PROMPT": TRANSCRIPTION_PROMPT
        }
    if stage == "critique":
        from yizkr.pipeline.critique import CRITIQUE_PROMPT

        return {"pipeline.critique.CRITIQUE_PROMPT": CRITIQUE_PROMPT}
    if stage == "extraction":
        from yizkr.pipeline import extract

        templates = {
            f"pipeline.extract.{name}": getattr(extract, name)
            for name in (
                "DOCUMENT_ITSELF",
                "PLACED_ARTIFACTS",
                "NO_PLACED_ARTIFACTS",
            )
        }
        if configuration.get("strategy") != "chunked":
            templates["pipeline.extract.EXTRACTION_PROMPT"] = (
                extract.EXTRACTION_PROMPT
            )
            return templates
        from yizkr.pipeline import chunking, preparation

        templates.update(
            {
                f"pipeline.chunking.{name}": getattr(chunking, name)
                for name in (
                    "SEGMENTATION_PROMPT",
                    "CHUNK_PROMPT",
                    "LINE_ASSIGNMENT_BLOCK",
                    "ENTITY_ASSIGNMENT_BLOCK",
                    "ENTITY_MANIFEST_PROMPT",
                    "MISSING_FIELD_NOTE",
                )
            }
        )
        if configuration.get("read_preparation"):
            templates.update(
                {
                    f"pipeline.preparation.{name}": getattr(preparation, name)
                    for name in ("MANIFEST_BLOCK", "POINTER_BLOCK")
                }
            )
        return templates
    return {}


class Capture:
    """Snapshot execution inputs before work; the caller owns execution."""

    def __init__(
        self,
        stage: str,
        process: str,
        *,
        document_kind: str = "text",
        configuration: Mapping[str, Any] | None = None,
        processing: Mapping[str, Any] | None = None,
        prompt_templates: Mapping[str, str] | None = None,
    ):
        from yizkr.pipeline.stage_planning import (
            STAGES,
            processing_dependencies,
        )

        self.stage = stage
        self.process = process
        self.document_kind = document_kind
        self.configuration = deepcopy(dict(configuration or {}))
        self.started_at = now()
        self.code = CodeProvenance.model_validate(code_version(fresh=True))
        if processing is None and stage in STAGES:
            processing = processing_dependencies(
                stage,
                configuration=self.configuration,
                source_sha1="",
                document_kind=document_kind,
            )
        self.processing = deepcopy(dict(processing or {}))
        templates = (
            _loaded_templates(stage, self.configuration)
            if prompt_templates is None
            else prompt_templates
        )
        self.prompt_hashes = {
            name: hashlib.sha256(template.encode("utf-8")).hexdigest()
            for name, template in templates.items()
        }

    def finish(
        self,
        dependencies: Mapping[str, Any],
        *,
        client: Any = None,
        semantic_version: str | int | None = None,
        parents: Sequence[ProcessRecord] = (),
        limitations: Sequence[str] = (),
    ) -> ProcessRecord:
        ended_at = now()
        configuration = (
            dict(dependencies.get("configuration") or {}) | self.configuration
        )
        processing = deepcopy(self.processing)
        # Bind the source the operation actually read without observing any
        # resource again: files may have changed during the provider call.
        if "source_sha1" in processing:
            processing["source_sha1"] = str(
                dependencies.get("source_sha1") or ""
            )
        resources = dict(processing.get("resources") or {})
        prompts = dict(self.prompt_hashes) if client is not None else {}
        observed = client_metadata(client) if client is not None else {}
        dependency_hash = hashlib.sha256(
            canonical_json(
                {
                    "operation": dict(dependencies),
                    "processing": processing,
                    "prompt_templates": prompts,
                }
            ).encode()
        ).hexdigest()
        version = semantic_version
        if version is None and resources:
            version = hashlib.sha256(
                canonical_json(
                    {
                        "resources": resources,
                        "libraries": processing.get("libraries", {}),
                        "prompt_templates": prompts,
                    }
                ).encode()
            ).hexdigest()
        current = StageProvenance(
            stage=self.stage,
            process=self.process,
            started_at=self.started_at,
            ended_at=ended_at,
            code=self.code,
            configuration=configuration,
            prompt_hashes=prompts,
            resource_hashes=resources,
            semantic_version=version,
            dependency_hash=dependency_hash,
            limitations=tuple(limitations),
            **observed,
        )
        stages = []
        seen = set()
        for stage in [
            *(stage for parent in parents for stage in parent.stages),
            current,
        ]:
            key = stage.model_dump_json()
            if key not in seen:
                stages.append(stage)
                seen.add(key)
        manifest = resources.get("model/manifest.yaml")
        return ProcessRecord(
            kind="producer",
            stages=tuple(stages),
            code=self.code,
            started_at=self.started_at,
            ended_at=ended_at,
            manifest_hash=manifest,
            limitations=tuple(limitations),
            metadata={
                "dependencies": dict(dependencies),
                "processing": processing,
            },
        )
