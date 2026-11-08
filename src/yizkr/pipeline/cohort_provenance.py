"""Publish accepted, explicit cohort choices without inferring their origin.

The immutable assignment history supplies the original human decision and
source observation. Publication adds an operational identity link, never a
historical kind or an assignment. Creation decisions additionally require
their original immutable authorization and verified registry receipt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from yizkr.core.identity_assignments import AssignmentRevision
from yizkr.core.provenance import ProcessRecord, StageProvenance
from yizkr.core.reviewed_creation import ReviewedCreationAuthorization
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.identity_provenance import (
    IdentityProvenanceRefused,
    IdentityPublication,
    IdentitySource,
    identity_references,
    publish_identity_record,
)
from yizkr.pipeline.identity_review import Decision, MentionRevision
from yizkr.pipeline.run_provenance import freeze_record, read_record

_METHODS = {"assign": "reviewed_assignment", "new_entity": "reviewed_creation"}


@dataclass(frozen=True)
class _Accepted:
    assignment: AssignmentRevision
    decision: Decision
    supporting_decisions: tuple[Decision, ...]
    mention: MentionRevision
    source: IdentitySource
    creation: ReviewedCreationAuthorization | None


def _accepted_cohort(registry, mention_id):
    snapshot = registry.assignment_snapshot()
    assignment = snapshot.assignments.get(mention_id)
    if assignment is None:
        raise IdentityProvenanceRefused("mention has no accepted assignment")
    decisions = {}
    mentions = {}
    for commit in snapshot.history:
        for raw in commit.delta.evidence.decisions.values():
            decision = Decision.model_validate(raw)
            for ref in assignment.decision_references:
                if (
                    ref.id == decision.id
                    and ref.revision == decision.revision
                    and ref.proposal_id == decision.proposal.id
                    and ref.proposal_revision == decision.proposal.revision
                    and ref.basis_digest == decision.proposal.digest
                ):
                    key = decision.id, decision.revision
                    if key in decisions and decisions[key] != decision:
                        raise IdentityProvenanceRefused(
                            "assignment has conflicting decision evidence"
                        )
                    decisions[key] = decision
        raw = commit.delta.evidence.mentions.get(mention_id)
        if raw is not None:
            mention = MentionRevision.model_validate(raw)
            if mention.revision == assignment.mention_revision:
                key = mention.id, mention.revision
                if key in mentions and mentions[key] != mention:
                    raise IdentityProvenanceRefused(
                        "assignment has conflicting observation evidence"
                    )
                mentions[key] = mention
    direct = [
        d
        for d in decisions.values()
        if d.outcome == "approve"
        and d.proposal.operation in _METHODS
        and d.proposal.mentions == (mention_id,)
    ]
    if (
        len(direct) != 1
        or len(mentions) != 1
        or len(decisions) != len(assignment.decision_references)
    ):
        raise IdentityProvenanceRefused(
            "assignment lacks one direct cohort decision and observation"
        )
    (decision,) = direct
    method = _METHODS[decision.proposal.operation]
    origin = decision.proposal.origin
    digest = origin.get("cohort_context_sha256")
    if (
        origin.get("method") != method
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise IdentityProvenanceRefused(
            "assignment has no recorded cohort decision origin"
        )
    mention = next(iter(mentions.values()))
    scope = mention.observation.source_scope
    if scope is None or scope.owner.sha1 != mention.observation.artifact:
        raise IdentityProvenanceRefused(
            "cohort decision lacks its verified source occurrence"
        )
    binding = registry.binding(assignment.entity_id)
    if (
        binding is None
        or binding.binding_revision != assignment.binding_revision
    ):
        raise IdentityProvenanceRefused("cohort target binding has changed")
    creation = None
    if decision.proposal.operation == "new_entity":
        if not registry.verify_reviewed_creation(
            decision, assignment.entity_id
        ):
            raise IdentityProvenanceRefused(
                "cohort creation lacks its verified original receipt"
            )
        creation = registry.reviewed_creation_authorization(decision)
    elif decision.proposal.target != binding.binding:
        raise IdentityProvenanceRefused(
            "cohort assignment differs from its approved target"
        )
    return _Accepted(
        assignment,
        decision,
        tuple(decisions[key] for key in sorted(decisions)),
        mention,
        IdentitySource(
            mention_id, mention.observation.artifact, None, binding, method
        ),
        creation,
    )


def publish_cohort_assignment(
    client, mention_id: str, *, record_dir: Path
) -> IdentityPublication:
    """Project an accepted direct cohort choice and its human confirmation.

    Unknown origins and decisions merely inherited through ``same`` cannot
    acquire a direct manual origin. Only an actual supplied ``ProcessRecord``
    is preserved as producer metadata. The original proposal retains all
    recorded graph, matching and source evidence independently of publication.
    """
    check_cancelled()
    registry = CreationRegistry(client)
    accepted = _accepted_cohort(registry, mention_id)
    decision = accepted.decision
    origin = decision.proposal.origin
    producer = None
    producer_refs = ()
    limitations = ()
    if "provenance" in origin:
        producer = ProcessRecord.model_validate(origin["provenance"])
        producer_refs = (freeze_record(record_dir, producer),)
    else:
        limitations = (
            "Original proposal producer metadata is unavailable; recorded "
            "origin and review evidence are retained without inferred model "
            "or prompt.",
        )
    record = ProcessRecord(
        kind="cohort-identity-confirmation",
        stages=(producer.stages if producer else ())
        + (
            StageProvenance(
                stage="cohort-identity-confirmation",
                process="manual",
                started_at=datetime.fromisoformat(decision.time),
            ),
        ),
        producer_refs=producer_refs,
        metadata={
            "identity_cohort": {
                "assignment": accepted.assignment.model_dump(mode="json"),
                "mention": accepted.mention.model_dump(mode="json"),
                "binding": accepted.source.binding.model_dump(mode="json"),
                "confirmation": decision.model_dump(mode="json"),
                "supporting_decisions": [
                    d.model_dump(mode="json")
                    for d in accepted.supporting_decisions
                ],
                "original_origin": origin,
                "creation_authorization": (
                    accepted.creation.model_dump(mode="json")
                    if accepted.creation is not None
                    else None
                ),
            }
        },
        limitations=limitations + (producer.limitations if producer else ()),
    )
    ref = freeze_record(record_dir, record)

    def revalidate():
        if _accepted_cohort(registry, mention_id) != accepted:
            raise IdentityProvenanceRefused(
                "cohort assignment authority changed during publication"
            )
        if read_record(record_dir, ref) != record or (
            producer is not None
            and read_record(record_dir, producer_refs[0]) != producer
        ):
            raise IdentityProvenanceRefused(
                "cohort process record differs from verified evidence"
            )

    def references(target, properties, items, qid):
        return identity_references(
            accepted.assignment,
            decision,
            properties,
            items,
            qid,
            record_sha256=ref.sha256,
            method=target.method,
            producer=producer,
        )

    return publish_identity_record(
        client,
        registry=registry,
        record=record,
        record_sha256=ref.sha256,
        operation={
            "kind": "cohort-assignment-provenance",
            "assignment": accepted.assignment.revision_id,
            "record": ref.sha256,
        },
        label=f"Reviewed identity record {ref.sha256[:16]}",
        sources=(accepted.source,),
        references=references,
        revalidate=revalidate,
    )
