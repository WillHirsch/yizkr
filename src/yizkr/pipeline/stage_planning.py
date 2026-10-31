"""Fixed document stages and side-effect-free decisions about saved views.

Processing fingerprints describe declared generation settings and the package
code/resources that interpret them. They are not provider session/cache keys;
the invocation still checks the actual engine configuration when it dispatches.
Callers own file loading, validation, publication and review decisions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from typing import Any, Literal

from yizkr.pipeline.stages import canonical_json


@dataclass(frozen=True)
class StageDescriptor:
    name: str
    role: str
    phase: int
    artifact: str
    input_artifacts: tuple[str, ...]
    process: Literal["deterministic", "model", "mixed"]
    document_kinds: tuple[str, ...]


_READ_KINDS = ("presentation", "pdf", "text", "media", "excerpt")
STAGES = {
    stage.name: stage
    for stage in (
        StageDescriptor(
            "transcription",
            "transcribe",
            0,
            "transcription.json",
            ("transcription.corrected.txt",),
            "mixed",
            _READ_KINDS,
        ),
        StageDescriptor(
            "extraction",
            "extract",
            1,
            "batch.json",
            (
                "transcription.json",
                "preparation.pointer.txt",
                "interpretation-context.json",
            ),
            "model",
            _READ_KINDS,
        ),
        StageDescriptor(
            "gedcom",
            "extract",
            1,
            "batch.json",
            (),
            "deterministic",
            ("gedcom",),
        ),
        StageDescriptor(
            "critique",
            "critique",
            2,
            "critique.json",
            (
                "batch.json",
                "transcription.json",
                "interpretation-context.json",
            ),
            "model",
            _READ_KINDS,
        ),
    )
}
DOCUMENT_STAGES = {
    kind: tuple(
        stage.name for stage in STAGES.values() if kind in stage.document_kinds
    )
    for kind in ("gedcom", *_READ_KINDS)
}


def stages_for(kind: str) -> tuple[StageDescriptor, ...]:
    """The declared operations for one document kind, in execution order."""
    return tuple(STAGES[name] for name in DOCUMENT_STAGES[kind])


def batch_stage(kind: str) -> StageDescriptor:
    """The document's one producer of the accepted proposal batch."""
    (stage,) = (
        stage for stage in stages_for(kind) if stage.artifact == "batch.json"
    )
    return stage


MODEL_ROLES = tuple(dict.fromkeys(stage.role for stage in STAGES.values()))
STAGE_ROLES = {stage.name: stage.role for stage in STAGES.values()}
# A deterministic adapter may share a refresh role, but produces no model
# events. Only stages that call models define the role-to-event mapping.
ROLE_STAGES = {
    stage.role: stage.name
    for stage in STAGES.values()
    if stage.process != "deterministic"
}
PHASES = {stage.name: stage.phase for stage in STAGES.values()}
VIEWS = {stage.name: stage.artifact for stage in STAGES.values()}


@dataclass(frozen=True)
class FreshnessPlan:
    action: Literal["reuse", "recompute", "blocked"]
    reasons: tuple[str, ...]
    changed_dependencies: tuple[str, ...]


def _changes(before: Mapping, after: Mapping, prefix: str = "") -> list[str]:
    changed = []
    for key in sorted(before.keys() | after.keys()):
        name = f"{prefix}{key}"
        if key not in before or key not in after:
            changed.append(name)
        elif before[key] != after[key]:
            if isinstance(before[key], Mapping) and isinstance(
                after[key], Mapping
            ):
                changed.extend(_changes(before[key], after[key], name + "."))
            else:
                changed.append(name)
    return changed


def plan_view(
    *,
    exists: bool,
    recorded_inputs: Mapping[str, Any] | None,
    current_inputs: Mapping[str, Any],
    recorded_processing: Mapping[str, Any] | None = None,
    processing: Mapping[str, Any] | None = None,
    refresh: bool = False,
    blocked_reason: str | None = None,
) -> FreshnessPlan:
    """Compare supplied metadata without loading or changing any product.

    Missing processing metadata makes a migrated stage stale. Passing no
    current processing descriptor retains input-only compatibility for stages
    whose adapters have not migrated. A recompute does not itself revoke a
    review: callers separately compare the meaning of the completed result.
    An unavailable dependency observation blocks a decision; the caller keeps
    its diagnostic reason rather than supplying guessed fingerprints.
    """
    if blocked_reason is not None:
        return FreshnessPlan("blocked", ("dependency-unavailable",), ())
    # Persisted JSON may be damaged or from an unsupported representation.
    # It supplies no evidence of freshness until it is a dependency mapping.
    if not isinstance(recorded_inputs, Mapping):
        recorded_inputs = None
    if not isinstance(recorded_processing, Mapping):
        recorded_processing = None
    reasons = []
    changed = []
    if not exists:
        reasons.append("missing-output")
    if refresh:
        reasons.append("refresh")
    if recorded_inputs != current_inputs:
        reasons.append("inputs-changed")
        changed.extend(_changes(recorded_inputs or {}, current_inputs))
    if processing is not None:
        if recorded_processing is None:
            reasons.append("missing-processing")
            changed.append("processing")
        elif recorded_processing != processing:
            reasons.append("processing-changed")
            changed.extend(
                _changes(recorded_processing, processing, "processing.")
            )
    return FreshnessPlan(
        "recompute" if reasons else "reuse",
        tuple(reasons),
        tuple(sorted(set(changed))),
    )


# Explicit package-relative closures, not the Git revision or whole repository.
# Shared claim/schema code belongs to every operation that validates its data.
# Prompt/resource bytes remain exact: whitespace can change a model request.
_SCHEMA = (
    "claims.py",
    "source_structure.py",
    "core/dates.py",
    "core/verbatim.py",
    "model/schema.py",
    "model/manifest.yaml",
)
_READING = (
    "pipeline/transcribe.py",
    "pipeline/source_context.py",
    "pipeline/language.py",
    "pipeline/transcription_record.py",
    "pipeline/transcription_check.py",
)
_SHARED = ("pipeline/text_stages.py", "pipeline/stages.py")
_EXTRACTION = (
    "pipeline/extract.py",
    "pipeline/chunking.py",
    "pipeline/interpretation_context.py",
    "pipeline/preparation.py",
    "pipeline/names.py",
    "pipeline/review.py",
    "pipeline/writer.py",
    "pipeline/places.py",
    "pipeline/llm.py",
    "pipeline/prompts/__init__.py",
)
_EXTRACTION_PROMPTS = tuple(
    f"pipeline/prompts/{name}.md"
    for name in (
        "extraction",
        "document-itself",
        "placed-artifacts",
        "no-placed-artifacts",
        "entity-manifest",
        "segmentation",
        "chunk-extraction",
        "entity-assignment",
        "line-assignment",
        "missing-field",
        "preparation-manifest",
        "preparation-pointer",
    )
)
# Media operations retain the same model prompts and source bindings as their
# format readers. Reading freshness must also observe the code that prepares
# those inputs; extraction consumes the resulting located transcription.
MEDIA_READING_RESOURCES = (
    "core/corpus.py",
    "pipeline/media_stages.py",
    "pipeline/containers.py",
    "pipeline/container_inventory.py",
    "pipeline/container_sources.py",
)
PDF_READING_RESOURCES = (
    "pipeline/pdf_inventory.py",
    "pipeline/presentation.py",
    "pipeline/textlayer.py",
)
PRESENTATION_READING_RESOURCES = (
    "pipeline/presentation.py",
    "pipeline/pptx_inventory.py",
)
VISION_READING_RESOURCES = (
    "pipeline/llm.py",
    "pipeline/prompts/__init__.py",
    "pipeline/prompts/transcription.md",
)
PROCESSING_RESOURCES = {
    "transcription": tuple(sorted((*_SHARED, *_SCHEMA, *_READING))),
    "extraction": tuple(
        sorted(
            (
                *_SHARED,
                *_SCHEMA,
                *_READING,
                *_EXTRACTION,
                *_EXTRACTION_PROMPTS,
            )
        )
    ),
    "critique": tuple(
        sorted(
            (
                *_SHARED,
                *_SCHEMA,
                *_READING,
                "pipeline/critique.py",
                "pipeline/chunking.py",
                "pipeline/extract.py",
                "pipeline/interpretation_context.py",
                "pipeline/review.py",
                "pipeline/llm.py",
                "pipeline/prompts/__init__.py",
                "pipeline/prompts/critique.md",
            )
        )
    ),
    "gedcom": tuple(
        sorted(
            (
                *_SCHEMA,
                "pipeline/gedcom_stages.py",
                "pipeline/stages.py",
                "pipeline/gedcom.py",
                "pipeline/names.py",
                "pipeline/writer.py",
                "pipeline/clustering.py",
                "pipeline/extract.py",
                "pipeline/gedcom_placeholders.yaml",
            )
        )
    ),
}


def _libraries(*, pdf: bool = False) -> dict[str, str | None]:
    observed = {}
    names = ("pydantic", "PyYAML", "py3langid")
    if pdf:
        names += ("pypdf", "pypdfium2")
    for name in names:
        try:
            observed[name] = version(name)
        except PackageNotFoundError:
            observed[name] = None
    return observed


def processing_dependencies(
    stage: str,
    *,
    configuration: Mapping[str, Any],
    source_sha1: str,
    document_kind: str = "text",
    package=None,
) -> dict[str, Any]:
    """Fingerprint explicit settings and packaged semantic dependencies.

    ``package`` is an optional package-root Traversable, useful for fictional
    resource trees. A missing dependency raises rather than making two missing
    files compare equal. Installed semantic-library versions are also recorded.
    No model client, host setting or runtime state is read.
    ``document_kind`` names the caller's inspected format, including PDF bytes
    stored with another suffix; it selects format-specific reader dependencies.
    Whole-file hashes conservatively reprocess the affected stage after a code
    edit; compatible model responses can still be reused. Documentation outside
    the declared prompt resources has no bearing on this fingerprint.
    """
    paths = set(PROCESSING_RESOURCES[stage])
    pdf_reading = stage == "transcription" and document_kind == "pdf"
    if stage == "transcription" and document_kind != "text":
        paths.update(MEDIA_READING_RESOURCES)
        if pdf_reading:
            paths.update(PDF_READING_RESOURCES)
        elif document_kind == "presentation":
            paths.update(PRESENTATION_READING_RESOURCES)
        if configuration.get("route") in {"vision", "paged"}:
            paths.update(VISION_READING_RESOURCES)
    root = files("yizkr") if package is None else package
    return {
        "format": 1,
        "stage": stage,
        "source_sha1": source_sha1,
        "configuration": json.loads(canonical_json(configuration)),
        "libraries": _libraries(pdf=pdf_reading),
        "resources": {
            path: hashlib.sha256(root.joinpath(path).read_bytes()).hexdigest()
            for path in sorted(paths)
        },
    }
