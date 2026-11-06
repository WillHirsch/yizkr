"""Evidence for an explicit curator replacement of an identity proposal.

An ordinary approval never changes an algorithmic origin into an override.
This bounded path replaces a rejected proposed target before assignment; it
cannot move an already assigned mention or authorize a merge or split.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.core.identity_assignments import AssignmentSnapshot
from yizkr.core.write_recovery import EntityBinding
from yizkr.pipeline.identity_review import (
    Decision,
    MentionRevision,
    ReviewRefused,
    ReviewStore,
)


class OwnerOverrideEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal[1] = 1
    mention_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    rejected: Decision
    target: EntityBinding
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def exact_replacement(self):
        prior = self.rejected.proposal
        if (
            not self.reason.strip()
            or self.rejected.outcome != "reject"
            or prior.operation != "assign"
            or prior.mentions != (self.mention_id,)
            or prior.target is None
            or prior.target.entity_id == self.target.entity_id
            or prior.target.generation != self.target.generation
            or prior.target.deployment_id != self.target.deployment_id
        ):
            raise ValueError(
                "override must replace one explicitly rejected target"
            )
        return self


def owner_override_evidence(
    store: ReviewStore,
    rejected_proposal_id: str,
    *,
    replacement_mention: MentionRevision,
    target: EntityBinding,
    reason: str,
) -> dict[str, Any]:
    """Prepare material evidence for an explicit replacement assign proposal.

    Put this result in ReviewProposal.evidence and declare its origin method
    separately. Current preparation and affirmative review still apply;
    assignment acceptance remains responsible for refusing an established move.
    """
    if (
        store.get_mention(replacement_mention.id, replacement_mention.revision)
        != replacement_mention
    ):
        raise ReviewRefused(
            "replacement mention differs from its stored observation"
        )
    proposal = store.get_proposal(rejected_proposal_id)
    decision = store.get_decision(rejected_proposal_id)
    if decision is None or decision.proposal != proposal:
        raise ReviewRefused("override needs a current explicit rejection")
    try:
        evidence = OwnerOverrideEvidence(
            mention_id=replacement_mention.id,
            rejected=decision,
            target=target,
            reason=reason,
        )
    except ValueError as exc:
        raise ReviewRefused(str(exc)) from exc
    return {"owner_override": evidence.model_dump(mode="json")}


def verified_owner_override(
    decision: Decision, snapshot: AssignmentSnapshot
) -> OwnerOverrideEvidence | None:
    """Read an override from accepted proof and retained negatives."""
    proposal = decision.proposal
    raw = proposal.basis.get("evidence", {}).get("owner_override")
    if raw is None:
        if proposal.origin.get("method") == "owner_override":
            raise ReviewRefused(
                "owner override origin lacks material evidence"
            )
        return None
    try:
        evidence = OwnerOverrideEvidence.model_validate(raw)
    except ValueError as exc:
        raise ReviewRefused("invalid owner override evidence") from exc
    if (
        decision.outcome != "approve"
        or proposal.operation != "assign"
        or proposal.mentions != (evidence.mention_id,)
        or proposal.target != evidence.target
        or proposal.origin.get("method") != "owner_override"
    ):
        raise ReviewRefused(
            "override evidence differs from confirmed assignment"
        )
    assignment = snapshot.assignments.get(evidence.mention_id)
    if assignment is None or assignment.entity_id != evidence.target.entity_id:
        raise ReviewRefused("override has no matching accepted assignment")
    if not any(
        ref.id == decision.id
        and ref.revision == decision.revision
        and ref.proposal_id == proposal.id
        and ref.basis_digest == proposal.digest
        for ref in assignment.decision_references
    ):
        raise ReviewRefused(
            "override confirmation did not establish this assignment"
        )
    old = evidence.rejected
    constraints = [
        constraint
        for constraint in snapshot.constraints
        if (
            constraint.kind == "rejected_target"
            and constraint.mentions == (evidence.mention_id,)
            and constraint.entity_id == old.proposal.target.entity_id
            and constraint.decision_id == old.id
            and constraint.decision_revision == old.revision
        )
    ]
    if not constraints or not any(
        commit.delta.evidence.decisions.get(old.id)
        == old.model_dump(mode="json")
        and any(
            constraint in commit.delta.constraints
            for constraint in constraints
        )
        for commit in snapshot.history
    ):
        raise ReviewRefused(
            "override's rejected target lacks retained decision evidence"
        )
    # Read back the affirmative record too: a caller-supplied decision object
    # is not authority for projecting provenance onto an established identity.
    if not any(
        commit.delta.evidence.decisions.get(decision.id)
        == decision.model_dump(mode="json")
        for commit in snapshot.history
    ):
        raise ReviewRefused(
            "override confirmation differs from accepted authority"
        )
    return evidence
