"""Immutable reviewed-write plans, support intents and observed receipts.

The creation registry stores these records alongside its existing identity
authority. They describe bounded applications, not a second claim database:
accepted statements and their individual source references remain in Wikibase.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.core.identity_assignments import record_digest
from yizkr.core.provenance import ProcessRecord

_HASH = r"^[0-9a-f]{64}$"
_ID = r"^[0-9a-f]{32}$"


class ApplicationRefused(ValueError):
    """A reviewed application lacks current authority or exact support."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal[1] = 1


def claim_application_id(decision: dict) -> str:
    """Meaning plus exact human decision; local display IDs are not keys."""
    return record_digest(
        {
            "version": 1,
            "basis": decision["proposal"]["digest"],
            "decision": {
                "id": decision["id"],
                "revision": decision["revision"],
            },
        }
    )


class PlannedClaim(_Record):
    claim_id: str = Field(pattern=_HASH)
    decision: dict[str, Any]
    roles: tuple[dict[str, Any], ...]
    # Exact graph value/support, before a process item has been allocated.
    support: dict[str, Any]

    @model_validator(mode="after")
    def identity(self):
        from yizkr.pipeline.assigned_support import SupportPayload
        from yizkr.pipeline.claim_review import ClaimRole
        from yizkr.pipeline.identity_review import ClaimDecision, _actor_time

        decision = ClaimDecision.model_validate(self.decision)
        _actor_time(decision.actor, decision.time)
        digest = record_digest(
            {"basis_version": 1, "basis": decision.proposal.basis}
        )
        if decision.outcome != "approve" or decision.proposal.digest != digest:
            raise ValueError(
                "application needs exact affirmative claim evidence"
            )
        if (
            decision.proposal.basis["claim"]["property"] == "depicts"
            and not decision.depicts_confirmed
        ):
            raise ValueError("depicts lacks explicit confirmation")
        roles = tuple(ClaimRole.model_validate(r) for r in self.roles)
        expected = [
            {
                "role": r.role,
                "mention_id": r.mention_id,
                "assignment_revision": r.assignment.revision_id,
                "binding": r.binding.model_dump(mode="json"),
            }
            for r in roles
        ]
        if sorted(expected, key=record_digest) != sorted(
            decision.proposal.basis["roles"], key=record_digest
        ):
            raise ValueError("application roles differ from approved meaning")
        SupportPayload.model_validate(self.support)
        if self.claim_id != claim_application_id(self.decision):
            raise ValueError(
                "claim application identifier differs from review"
            )
        if self.support.get("process") is not None:
            raise ValueError("a plan cannot preallocate process provenance")
        return self


def plan_identity(deployment_id: str, generation: str, claims) -> str:
    return record_digest(
        {
            "version": 1,
            "deployment_id": deployment_id,
            "generation": generation,
            "claims": sorted(c.claim_id for c in claims),
        }
    )


class ApplicationPlan(_Record):
    kind: Literal["reviewed-application-plan"] = "reviewed-application-plan"
    plan_id: str = Field(pattern=_HASH)
    deployment_id: str = Field(pattern=_ID)
    generation: str = Field(pattern=_ID)
    claims: tuple[PlannedClaim, ...]
    record: ProcessRecord
    producers: tuple[ProcessRecord, ...] = ()

    @model_validator(mode="after")
    def identity(self):
        from yizkr.pipeline.run_provenance import (
            record_digest as process_digest,
        )

        keys = [claim.claim_id for claim in self.claims]
        if not keys or keys != sorted(set(keys)):
            raise ValueError("application plan must have unique sorted claims")
        if self.plan_id != plan_identity(
            self.deployment_id, self.generation, self.claims
        ):
            raise ValueError("application plan identifier differs from scope")
        if (
            self.record.metadata.get("plan_id") != self.plan_id
            or self.record.metadata.get("claims")
            != [claim.model_dump(mode="json") for claim in self.claims]
            or {r.sha256 for r in self.record.producer_refs}
            != {process_digest(p) for p in self.producers}
        ):
            raise ValueError(
                "application process record differs from its plan"
            )
        if any(
            claim.decision["proposal"]["basis"].get("deployment_id")
            != self.deployment_id
            or claim.decision["proposal"]["basis"].get("generation")
            != self.generation
            for claim in self.claims
        ):
            raise ValueError("approved meaning belongs to another generation")
        return self


class SupportIntent(_Record):
    kind: Literal["reviewed-support-intent"] = "reviewed-support-intent"
    plan_id: str = Field(pattern=_HASH)
    claim_id: str = Field(pattern=_HASH)
    deployment_id: str = Field(pattern=_ID)
    generation: str = Field(pattern=_ID)
    support: dict[str, Any]
    base_revision: int = Field(gt=0)
    prior_guid: str | None = None


class SupportDispatch(_Record):
    kind: Literal["reviewed-support-dispatch"] = "reviewed-support-dispatch"
    deployment_id: str = Field(pattern=_ID)
    generation: str = Field(pattern=_ID)
    qid: str = Field(pattern=r"^Q[1-9][0-9]*$")
    base_revision: int = Field(gt=0)
    plan_id: str = Field(pattern=_HASH)
    claim_id: str = Field(pattern=_HASH)
    intent_sha256: str = Field(pattern=_HASH)
    attempt: str = Field(pattern=_ID)


class SupportReceipt(_Record):
    kind: Literal["reviewed-support-receipt"] = "reviewed-support-receipt"
    plan_id: str = Field(pattern=_HASH)
    claim_id: str = Field(pattern=_HASH)
    deployment_id: str = Field(pattern=_ID)
    generation: str = Field(pattern=_ID)
    intent_sha256: str = Field(pattern=_HASH)
    qid: str = Field(pattern=r"^Q[1-9][0-9]*$")
    revision: int = Field(gt=0)
    statement_guid: str = Field(min_length=1)
    primary_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    circumstance_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{40}$"
    )
    primary: dict[str, Any]
    circumstance: dict[str, Any] | None = None
    # Statement-level context, not an invented source-to-process association.
    observed_process: tuple[dict[str, Any], ...] = ()


def assert_plan_claim(plan: ApplicationPlan, claim_id: str) -> PlannedClaim:
    found = [claim for claim in plan.claims if claim.claim_id == claim_id]
    if len(found) != 1:
        raise ApplicationRefused("support is outside the reviewed plan")
    return found[0]


def same_reviewed_scope(left: ApplicationPlan, right: ApplicationPlan) -> bool:
    """Equivalent current support retains its original concrete reading proof.

    The approved basis already binds source parts, located quotes and stable
    assignments. A compatible reading's digest/offsets do not rewrite earlier
    graph reference evidence. Property bindings and graph values must agree.
    """
    from yizkr.pipeline.assigned_support import same_statement

    if left.plan_id != right.plan_id or len(left.claims) != len(right.claims):
        return False
    return all(
        a.claim_id == b.claim_id
        and a.decision == b.decision
        and a.roles == b.roles
        and same_statement(a.support["statement"], b.support["statement"])
        and all(
            a.support[key] == b.support[key] for key in ("qid", "properties")
        )
        for a, b in zip(left.claims, right.claims, strict=True)
    )
