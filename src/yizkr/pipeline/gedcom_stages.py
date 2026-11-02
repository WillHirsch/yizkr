"""Deterministic GEDCOM derivation without workflow or graph ownership.

The operation adapts one source snapshot into a complete validated proposal
and reports its diagnostics and invocation dependencies. Callers own reuse,
publication, review decisions and the lifecycle of persisted products.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline.gedcom import ADAPTER_IDENTIFIER, adapt_gedcom
from yizkr.pipeline.run_provenance import Capture
from yizkr.pipeline.stages import sha256_text


@dataclass(frozen=True)
class GedcomRequest:
    source: Path
    expected_artifact: ArtifactRef | None = None


@dataclass(frozen=True)
class GedcomResult:
    batch: ClaimBatch
    warnings: list[str]
    unsupported_tags: dict[str, int]
    outcomes: dict[str, int]
    dependencies: dict[str, Any]
    status: Literal["complete"] = field(default="complete", init=False)
    artifacts: tuple[Path, ...] = field(default=(), init=False)

    def require_complete(self) -> ClaimBatch:
        """Return the validated proposal; failed derivations raise instead."""
        return self.batch


def derive_gedcom(request: GedcomRequest) -> GedcomResult:
    """Adapt exactly the bytes whose content key every proposal cites.

    An expected artifact binds execution to the caller's discovered source.
    Source changes, decoding, parsing and validation errors propagate without
    producing an accepted result or publishing any file. No model is called.
    """
    capture = Capture(
        "gedcom",
        "deterministic",
        document_kind="gedcom",
        configuration={"adapter": ADAPTER_IDENTIFIER, "encoding": "utf-8"},
    )
    contents = request.source.read_bytes()
    artifact = ArtifactRef.from_bytes(contents)
    if (
        request.expected_artifact is not None
        and artifact != request.expected_artifact
    ):
        raise ValueError("the GEDCOM source no longer matches its artifact")
    text = contents.decode("utf-8")
    adapted = adapt_gedcom(text, artifact)
    result = GedcomResult(
        batch=adapted.batch,
        warnings=adapted.warnings,
        unsupported_tags=adapted.unsupported_tags,
        outcomes=adapted.outcomes,
        dependencies={
            "source_sha1": artifact.sha1,
            "text_sha256": sha256_text(text),
            "configuration": {
                "adapter": ADAPTER_IDENTIFIER,
                "encoding": "utf-8",
            },
        },
    )
    record = capture.finish(
        result.dependencies, semantic_version=ADAPTER_IDENTIFIER
    )
    result.batch.extraction_run = result.batch.extraction_run.model_copy(
        update={"provenance": record}
    )
    return result
