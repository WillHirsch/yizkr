"""Prepare and record bounded human identity review, without graph writes.

Python callers supply products, proposed scopes and current validation/binding
state. The file adapters serve human review; CSVs are editable views of stored
proposal revisions, never the authority for carrying consent between runs.
"""

from __future__ import annotations

import csv
import io
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    container_problems,
    structure_problems,
)
from yizkr.core.write_recovery import EntityBinding
from yizkr.pipeline.identity_review import (
    DecisionInput,
    MentionRevision,
    Operation,
    Projection,
    Proposal,
    ReviewRefused,
    ReviewStore,
)
from yizkr.pipeline.review_mentions import (
    canonical_basis,
    observe_mentions,
    source_scope_data,
)
from yizkr.pipeline.stages import atomic_write_text
from yizkr.pipeline.text_stages import validate_paths
from yizkr.pipeline.transcribe import Transcription


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MentionKey(_Input):
    artifact_sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    local_id: str = Field(min_length=1)

    @property
    def key(self) -> tuple[str, str]:
        return self.artifact_sha1, self.local_id


class ReviewProposal(_Input):
    operation: Operation
    mentions: tuple[MentionKey, ...]
    target: EntityBinding | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    origin: dict[str, Any] = Field(default_factory=dict)
    before: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_scope(self):
        keys = [mention.key for mention in self.mentions]
        count = 2 if self.operation in ("same", "different") else 1
        if len(set(keys)) != count or len(keys) != count:
            raise ValueError("review operation has the wrong mention scope")
        if (self.operation == "assign") != (self.target is not None):
            raise ValueError("only assign takes an existing entity binding")
        return self


class ReviewBlocker(_Input):
    mention: MentionKey
    reasons: tuple[str, ...]


class ReviewedRemap(_Input):
    mention: MentionKey
    prior_id: str
    prior_revision: int = Field(gt=0)
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    time: str


@dataclass(frozen=True)
class ReviewSource:
    artifact_sha1: str
    batch: ClaimBatch
    reading: Transcription | None = None

    def __post_init__(self):
        object.__setattr__(
            self, "artifact_sha1", ArtifactRef(sha1=self.artifact_sha1).sha1
        )


@dataclass(frozen=True)
class ReviewRequest:
    sources: tuple[ReviewSource, ...]
    proposals: tuple[ReviewProposal, ...] = ()
    current_targets: tuple[EntityBinding, ...] = ()
    blocked: tuple[ReviewBlocker, ...] = ()
    remaps: tuple[ReviewedRemap, ...] = ()


@dataclass(frozen=True)
class PreparedReview:
    mentions: dict[tuple[str, str], MentionRevision]
    proposals: tuple[Proposal, ...]
    projection: Projection
    labels: dict[str, str] = field(default_factory=dict)


# These limit automatic correspondence, without refusing explicit review of
# the supplied product. Other source limitations are active admission failures.
_CORRESPONDENCE_LIMITS = {
    "reading_unavailable",
    "native_mention_location_unavailable",
    "missing_or_noncontiguous_mention_lines",
    "repeated_source_quote",
    "missing_or_ambiguous_literal_mention",
    "multiple_literal_occurrences",
    "shared_source_occurrence",
}


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False
    )


def _basis(mentions: tuple[str, ...], material: dict, evidence: dict) -> dict:
    return {
        "version": 1,
        "mentions": {key: material[key] for key in sorted(mentions)},
        "evidence": evidence,
    }


def prepare_review(
    store: ReviewStore, request: ReviewRequest
) -> PreparedReview:
    """Observe products, refresh relevant scopes and expose current authority.

    Prior negative scopes are included even if the caller proposes no new pair.
    The projection refuses missing correspondence, changed support, supplied
    blockers and conflicting decisions. It authorizes no historical assertions
    and performs no allocation or graph mutation.
    """
    sources = {source.artifact_sha1: source for source in request.sources}
    if len(sources) != len(request.sources):
        raise ReviewRefused("supply one current product per source")
    observations = [
        observation
        for source in request.sources
        for observation in observe_mentions(
            source.artifact_sha1, source.batch, source.reading
        )
    ]
    by_key = {(item.artifact, item.local_id): item for item in observations}
    requested_keys = [key.key for p in request.proposals for key in p.mentions]
    requested_keys += [b.mention.key for b in request.blocked]
    requested_keys += [r.mention.key for r in request.remaps]
    if any(key not in by_key for key in requested_keys):
        raise ReviewRefused(
            "review names a mention absent from current products"
        )
    if len({r.mention.key for r in request.remaps}) != len(request.remaps):
        raise ReviewRefused("a current occurrence has multiple remappings")
    if len({r.prior_id for r in request.remaps}) != len(request.remaps):
        raise ReviewRefused("a prior occurrence has multiple remappings")
    targets = {target.entity_id: target for target in request.current_targets}
    if len(targets) != len(request.current_targets):
        raise ReviewRefused("supply one current binding per entity")
    for remap in request.remaps:
        store.remap(
            store.get_mention(remap.prior_id, remap.prior_revision),
            by_key[remap.mention.key],
            actor=remap.actor,
            reason=remap.reason,
            time=remap.time,
        )
    mentions = store.observe(observations)
    material, labels, blocked = {}, {}, {}
    for source in request.sources:
        stable = {
            local: mention.id
            for (artifact, local), mention in mentions.items()
            if artifact == source.artifact_sha1
        }
        entities = {entity.id: entity for entity in source.batch.entities}
        for local, durable in stable.items():
            mention = mentions[source.artifact_sha1, local]
            observation = mention.observation
            anchor = observation.anchor
            occurrence = None
            if observation.source_scope is not None:
                occurrence = {
                    "scope": source_scope_data(observation.source_scope),
                    "quote": unicodedata.normalize(
                        "NFC", observation.context_quote or ""
                    ),
                    "correspondence": (
                        anchor.correspondence_key if anchor else None
                    ),
                }
            material[durable] = {
                "interpretation": canonical_basis(source.batch, local, stable),
                "occurrence": occurrence,
            }
            entity = entities.get(local)
            labels[durable] = entity.label if entity else local
            reasons = [
                "source evidence: " + reason.replace("_", " ")
                for reason in observation.limitations
                if reason not in _CORRESPONDENCE_LIMITS
            ]
            if entity:
                reasons += structure_problems(entity) + container_problems(
                    entity
                )
            if reasons:
                blocked[durable] = reasons
    for blocker in request.blocked:
        blocked.setdefault(mentions[blocker.mention.key].id, []).extend(
            blocker.reasons
        )
    # A new request cannot omit a retained negative scope.
    for proposal in store.list_proposals():
        if set(proposal.mentions) <= material.keys():
            evidence = proposal.basis.get("evidence", {})
            store.propose(
                proposal.operation,
                [
                    next(m for m in mentions.values() if m.id == key)
                    for key in proposal.mentions
                ],
                basis=_basis(proposal.mentions, material, evidence),
                origin=proposal.origin,
                target=proposal.target,
                before=proposal.before,
            )
    for proposal in request.proposals:
        scope = [mentions[key.key] for key in proposal.mentions]
        store.propose(
            proposal.operation,
            scope,
            basis=_basis(
                tuple(m.id for m in scope), material, proposal.evidence
            ),
            origin=proposal.origin
            | {"review_labels": {m.id: labels[m.id] for m in scope}},
            target=proposal.target,
            before=proposal.before,
        )
    relevant = tuple(
        p for p in store.list_proposals() if set(p.mentions) & material.keys()
    )
    bases = {
        p.id: _basis(p.mentions, material, p.basis.get("evidence", {}))
        for p in relevant
        if set(p.mentions) <= material.keys()
    }
    projection = store.project(
        list(mentions.values()),
        bases=bases,
        blocked=blocked,
        current_targets=targets,
    )
    return PreparedReview(mentions, relevant, projection, labels)


FIELDS = (
    "proposal_id",
    "proposal_revision",
    "digest",
    "decision_revision",
    "operation",
    "scope",
    "target",
    "before",
    "basis",
    "origin",
    "decision",
)
SHEET_FIELDS = ("sheet_id", *FIELDS, "labels", "status", "blockers")


def _row(store: ReviewStore, proposal: Proposal) -> dict[str, str]:
    scope = []
    for ref in proposal.mention_revisions:
        mention = store.get_mention(ref["id"], ref["revision"])
        scope.append(
            {
                "id": mention.id,
                "revision": mention.revision,
                "observation": mention.observation.model_dump(mode="json"),
            }
        )
    decision = store.get_decision(proposal.id)
    return {
        "proposal_id": proposal.id,
        "proposal_revision": str(proposal.revision),
        "digest": proposal.digest,
        "decision_revision": str(decision.revision) if decision else "",
        "operation": proposal.operation,
        "scope": _json(scope),
        "target": _json(
            proposal.target.model_dump(mode="json")
            if proposal.target
            else None
        ),
        "before": _json(proposal.before),
        "basis": _json(proposal.basis),
        "origin": _json(proposal.origin),
        "decision": "",
    }


def export_review(store: ReviewStore, prepared: PreparedReview) -> str:
    """A review sheet; only decision cells are editable on import.

    Source quotes and full material evidence remain visible. Actor/time are
    recorded at import, rather than guessed from a file's filesystem metadata.
    """
    rows = []
    applications = {
        app.proposal_id: app for app in prepared.projection.applications
    }
    for proposal in prepared.proposals:
        app = applications.get(proposal.id)
        rows.append(
            _row(store, proposal)
            | {
                "labels": _json(proposal.origin.get("review_labels", {})),
                "status": (
                    f"{app.status}: {app.decision.outcome}"
                    if app and app.decision
                    else app.status
                    if app
                    else "blocked"
                ),
                "blockers": _json(
                    app.reasons if app else ["missing current source scope"]
                ),
            }
        )
    sheet_id = store.put_review_sheet(rows)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, SHEET_FIELDS)
    writer.writeheader()
    writer.writerows({"sheet_id": sheet_id, **row} for row in rows)
    return output.getvalue()


def record_review(
    store: ReviewStore, text: str, *, actor: str, time: str | None = None
) -> int:
    """Record explicit decisions atomically; refuse stale or altered scopes."""
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(SHEET_FIELDS):
        raise ReviewRefused("not a durable identity review sheet")
    requests = []
    seen = set()
    sheet_id = None
    stored_rows = {}
    stamp = time or datetime.now(UTC).isoformat()
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ReviewRefused("malformed identity review row")
        if row["proposal_id"] in seen:
            raise ReviewRefused("review sheet repeats a proposal")
        seen.add(row["proposal_id"])
        if sheet_id is None:
            sheet_id = row["sheet_id"]
            stored_rows = {
                item["proposal_id"]: item
                for item in store.get_review_sheet(sheet_id)
            }
        if row["sheet_id"] != sheet_id:
            raise ReviewRefused("review packet mixes exported sheets")
        expected = stored_rows.get(row["proposal_id"])
        if expected is None:
            raise ReviewRefused("proposal was not in the exported sheet")
        for key in SHEET_FIELDS:
            if (
                key not in ("decision", "sheet_id")
                and row[key] != expected[key]
            ):
                raise ReviewRefused(f"review sheet altered protected {key}")
        outcome = row["decision"].strip().lower()
        if not outcome:
            continue
        if outcome not in ("approve", "reject"):
            raise ReviewRefused("decision must be approve, reject or blank")
        requests.append(
            DecisionInput(
                proposal_id=row["proposal_id"],
                proposal_revision=int(row["proposal_revision"]),
                expected_digest=row["digest"],
                outcome=outcome,
                actor=actor,
                time=stamp,
                review_sheet_id=sheet_id,
                expected_decision_revision=int(row["decision_revision"])
                if row["decision_revision"]
                else None,
            )
        )
    return len(store.decide_many(requests))


class _SourceFile(_Input):
    artifact_sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    batch: Path
    reading: Path | None = None


class ReviewFileRequest(_Input):
    format: int = 1
    sources: tuple[_SourceFile, ...]
    proposals: tuple[ReviewProposal, ...] = ()
    current_targets: tuple[EntityBinding, ...] = ()
    blocked: tuple[ReviewBlocker, ...] = ()
    remaps: tuple[ReviewedRemap, ...] = ()


def _store_files(path: Path) -> tuple[Path, ...]:
    # SQLite sidecars are storage, never review output destinations.
    return (
        path,
        *(Path(f"{path}{suffix}") for suffix in ("-journal", "-wal", "-shm")),
    )


def _load_request(path: Path, store: Path, out: Path) -> ReviewRequest:
    spec = ReviewFileRequest.model_validate_json(path.read_text())
    if spec.format != 1:
        raise ReviewRefused("unsupported review request format")
    files = [
        (
            source,
            path.parent / source.batch,
            path.parent / source.reading if source.reading else None,
        )
        for source in spec.sources
    ]
    inputs = [
        path,
        *(item for _, batch, reading in files for item in (batch, reading)),
    ]
    validate_paths(inputs=inputs, outputs=(*_store_files(store), out))
    sources = tuple(
        ReviewSource(
            source.artifact_sha1,
            ClaimBatch.model_validate_json(batch.read_text()),
            Transcription.model_validate_json(reading.read_text())
            if reading
            else None,
        )
        for source, batch, reading in files
    )
    return ReviewRequest(
        sources,
        spec.proposals,
        spec.current_targets,
        spec.blocked,
        spec.remaps,
    )


def prepare_review_file(
    request_path: Path, store_path: Path, out_path: Path
) -> int:
    request = _load_request(request_path, store_path, out_path)
    with ReviewStore(store_path) as store:
        prepared = prepare_review(store, request)
        text = export_review(store, prepared)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_path, text)
    return len(prepared.proposals)


def record_review_file(store_path: Path, csv_path: Path, *, actor: str) -> int:
    validate_paths(inputs=(csv_path,), outputs=_store_files(store_path))
    if not store_path.is_file():
        raise ReviewRefused("identity review store does not exist")
    text = csv_path.read_text()
    with ReviewStore(store_path) as store:
        return record_review(store, text, actor=actor)


def project_review_file(
    request_path: Path, store_path: Path, out_path: Path
) -> Projection:
    request = _load_request(request_path, store_path, out_path)
    with ReviewStore(store_path) as store:
        projection = prepare_review(store, request).projection
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_path, projection.model_dump_json(indent=2))
    return projection
