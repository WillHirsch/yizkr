"""Publish an explicitly rejected candidate and confirmed replacement.

This reads the existing assignment authority. It cannot establish an override,
move a mention, create a historical kind statement or turn ordinary approval
into a curator replacement. Complete review evidence remains content-addressed;
the graph receives separate origin and human-confirmation references.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from yizkr.core.provenance import ProcessRecord, StageProvenance
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.identity_origins import verified_owner_override
from yizkr.pipeline.identity_provenance import (
    IdentityProvenanceRefused,
    IdentityPublication,
    IdentitySource,
    identity_references,
    publish_identity_record,
)
from yizkr.pipeline.identity_review import Decision, MentionRevision
from yizkr.pipeline.run_provenance import freeze_record, read_record


def _accepted_override(registry, mention_id):
    snapshot = registry.assignment_snapshot()
    assignment = snapshot.assignments.get(mention_id)
    if assignment is None:
        raise IdentityProvenanceRefused("mention has no accepted assignment")
    decisions = {}
    mentions = {}
    for commit in snapshot.history:
        for raw in commit.delta.evidence.decisions.values():
            decision = Decision.model_validate(raw)
            if any(
                ref.id == decision.id
                and ref.revision == decision.revision
                and ref.proposal_id == decision.proposal.id
                and ref.proposal_revision == decision.proposal.revision
                and ref.basis_digest == decision.proposal.digest
                for ref in assignment.decision_references
            ):
                decisions[decision.id, decision.revision] = decision
        raw = commit.delta.evidence.mentions.get(mention_id)
        if raw is not None:
            mention = MentionRevision.model_validate(raw)
            if mention.revision == assignment.mention_revision:
                mentions[mention.id, mention.revision] = mention
    matching = []
    for decision in decisions.values():
        evidence = verified_owner_override(decision, snapshot)
        if evidence is not None and evidence.mention_id == mention_id:
            matching.append((decision, evidence))
    if len(matching) != 1 or len(mentions) != 1:
        raise IdentityProvenanceRefused(
            "assignment lacks one verified owner override and observation"
        )
    decision, evidence = matching[0]
    mention = next(iter(mentions.values()))
    binding = registry.binding(assignment.entity_id)
    if (
        binding is None
        or binding.binding != evidence.target
        or binding.binding_revision != assignment.binding_revision
    ):
        raise IdentityProvenanceRefused("override target binding has changed")
    try:
        kind = decision.proposal.basis["mentions"][mention_id][
            "interpretation"
        ]["interpretation"]["kind"]
    except (KeyError, TypeError) as exc:
        raise IdentityProvenanceRefused(
            "override has no reviewed source kind"
        ) from exc
    scope = mention.observation.source_scope
    if (
        not isinstance(kind, str)
        or not kind
        or scope is None
        or scope.owner.sha1 != mention.observation.artifact
    ):
        raise IdentityProvenanceRefused(
            "override has no verified source occurrence and kind"
        )
    source = IdentitySource(
        mention_id,
        mention.observation.artifact,
        kind,
        binding,
        "owner_override",
    )
    return assignment, decision, evidence, mention, source


def publish_owner_override(
    client, mention_id: str, *, record_dir: Path
) -> IdentityPublication:
    """Append provenance for a verified replacement before first assignment.

    A typed original producer in the rejected proposal's ``origin.provenance``
    is preserved and projected. Other origin fields remain verbatim in the
    immutable record, with an explicit limit when actual producer metadata is
    unavailable. Repeating or resuming publication uses the same record and
    appends only missing references to the existing source-backed kind.
    """
    check_cancelled()
    registry = CreationRegistry(client)
    accepted = _accepted_override(registry, mention_id)
    assignment, decision, evidence, mention, source = accepted
    origin = evidence.rejected.proposal.origin
    producer = None
    producer_refs = ()
    limitations = ()
    if "provenance" in origin:
        producer = ProcessRecord.model_validate(origin["provenance"])
        producer_refs = (freeze_record(record_dir, producer),)
    else:
        limitations = (
            "Original identity proposal producer metadata is unavailable; "
            "raw recorded origin is retained without inferred model or "
            "prompt.",
        )
    record = ProcessRecord(
        kind="owner-override-confirmation",
        stages=(producer.stages if producer else ())
        + (
            StageProvenance(
                stage="owner-override-confirmation",
                process="manual",
                started_at=datetime.fromisoformat(decision.time),
            ),
        ),
        producer_refs=producer_refs,
        metadata={
            "identity_owner_override": {
                "assignment": assignment.model_dump(mode="json"),
                "mention": mention.model_dump(mode="json"),
                "evidence": evidence.model_dump(mode="json"),
                "confirmation": decision.model_dump(mode="json"),
                "original_origin": origin,
            }
        },
        limitations=limitations + (producer.limitations if producer else ()),
    )
    ref = freeze_record(record_dir, record)

    def revalidate():
        if _accepted_override(registry, mention_id) != accepted:
            raise IdentityProvenanceRefused(
                "override authority changed during publication"
            )
        if read_record(record_dir, ref) != record or (
            producer is not None
            and read_record(record_dir, producer_refs[0]) != producer
        ):
            raise IdentityProvenanceRefused(
                "override process record differs from verified evidence"
            )

    def references(target, properties, items, qid):
        return identity_references(
            assignment,
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
            "kind": "owner-override-provenance",
            "assignment": assignment.revision_id,
            "record": ref.sha256,
        },
        label=f"Identity override record {ref.sha256[:16]}",
        sources=(source,),
        references=references,
        revalidate=revalidate,
    )
