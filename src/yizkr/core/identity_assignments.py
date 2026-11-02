"""Versioned assignment records owned by the existing creation registry.

These are records and validation helpers, not another identity store. Human
authority is supplied by the fresh, pinned review importer. The registry also
checks its own bindings, previous assignments and retained negative decisions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.core.write_recovery import EntityBinding, Generation

_ID = r"^[0-9a-f]{32}$"
_HASH = r"^[0-9a-f]{64}$"


class AssignmentRefused(ValueError):
    """Current authority does not support this assignment publication."""


class StaleAssignments(AssignmentRefused):
    """Another assignment publication changed the expected head."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal[1] = 1


class RegistryBinding(_Record):
    binding_revision: Literal[1] = 1
    binding: EntityBinding
    receipt_locator: str
    receipt_sha256: str = Field(pattern=_HASH)


class AssignmentTarget(_Record):
    mention_id: str = Field(pattern=_ID)
    mention_revision: int = Field(gt=0)
    entity_id: str = Field(pattern=_ID)


class AssignmentConstraint(_Record):
    kind: Literal["different", "rejected_target"]
    mentions: tuple[str, ...]
    entity_id: str | None = Field(default=None, pattern=_ID)
    decision_id: str = Field(pattern=_ID)
    decision_revision: int = Field(gt=0)

    @model_validator(mode="after")
    def scope(self):
        import re

        count = 2 if self.kind == "different" else 1
        if (
            len(self.mentions) != count
            or len(set(self.mentions)) != count
            or any(not re.fullmatch(_ID, key) for key in self.mentions)
            or tuple(sorted(self.mentions)) != self.mentions
            or (self.entity_id is None) != (self.kind == "different")
        ):
            raise ValueError("invalid assignment constraint scope")
        return self


class AssignmentEvidence(_Record):
    # Full immutable review records retain source observations, material basis,
    # original method, actor and time. They are checked against the review data
    # models below; only the importer can verify current store authority.
    mentions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    decisions: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AssignmentDelta(_Record):
    assignments: tuple[AssignmentTarget, ...] = ()
    constraints: tuple[AssignmentConstraint, ...] = ()
    evidence: AssignmentEvidence


class DecisionReference(_Record):
    id: str = Field(pattern=_ID)
    revision: int = Field(gt=0)
    proposal_id: str = Field(pattern=_ID)
    proposal_revision: int = Field(gt=0)
    basis_digest: str = Field(pattern=_HASH)


class AssignmentRevision(AssignmentTarget):
    revision_id: str = Field(pattern=_HASH)
    binding_revision: Literal[1] = 1
    previous: None = None
    decision_references: tuple[DecisionReference, ...]


class AssignmentCommit(_Record):
    kind: Literal["assignment-commit"] = "assignment-commit"
    deployment_id: str = Field(pattern=_ID)
    generation: str = Field(pattern=_ID)
    previous: str | None = Field(default=None, pattern=_HASH)
    delta: AssignmentDelta
    revisions: tuple[AssignmentRevision, ...]


class AssignmentSnapshot(_Record):
    generation: Generation | None = None
    head: str | None = Field(default=None, pattern=_HASH)
    assignments: dict[str, AssignmentRevision] = Field(default_factory=dict)
    constraints: tuple[AssignmentConstraint, ...] = ()
    history: tuple[AssignmentCommit, ...] = ()


def record_json(value: BaseModel | dict) -> str:
    data = (
        value.model_dump(mode="json")
        if isinstance(value, BaseModel)
        else value
    )
    return json.dumps(
        data,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def record_digest(value: BaseModel | dict) -> str:
    return hashlib.sha256(record_json(value).encode()).hexdigest()


def canonical_delta(delta: AssignmentDelta) -> AssignmentDelta:
    """Ordering is not an assignment or concurrency precondition."""
    delta = AssignmentDelta.model_validate(delta.model_dump(mode="json"))
    if len({a.mention_id for a in delta.assignments}) != len(
        delta.assignments
    ):
        raise AssignmentRefused("assignment delta repeats a mention")
    unique = {record_json(c): c for c in delta.constraints}
    return delta.model_copy(
        update={
            "assignments": tuple(
                sorted(delta.assignments, key=lambda a: a.mention_id)
            ),
            "constraints": tuple(unique[key] for key in sorted(unique)),
        }
    )


def _evidence(delta: AssignmentDelta):
    # Use the existing review schemas, not a competing decision vocabulary.
    from yizkr.pipeline.identity_review import Decision, MentionRevision

    try:
        mentions = {
            key: MentionRevision.model_validate(value)
            for key, value in delta.evidence.mentions.items()
        }
        decisions = {
            key: Decision.model_validate(value)
            for key, value in delta.evidence.decisions.items()
        }
    except ValueError as exc:
        raise AssignmentRefused("invalid complete review evidence") from exc
    if any(key != item.id for key, item in mentions.items()) or any(
        key != item.id for key, item in decisions.items()
    ):
        raise AssignmentRefused("review evidence key differs from its record")
    for decision in decisions.values():
        proposal = decision.proposal
        scope = {
            "operation": proposal.operation,
            "mentions": proposal.mentions,
            "target": proposal.target.model_dump(mode="json")
            if proposal.target
            else None,
            "before": proposal.before,
            "basis_version": proposal.basis_version,
            "basis": proposal.basis,
        }
        if record_digest(scope) != proposal.digest:
            raise AssignmentRefused(
                "review proposal digest does not match its basis"
            )
        if not set(proposal.mentions) <= mentions.keys():
            raise AssignmentRefused(
                "review decision lacks current mention evidence"
            )
    return mentions, decisions


def validated_revisions(
    snapshot: AssignmentSnapshot,
    delta: AssignmentDelta,
    binding: Callable[[str], RegistryBinding | None],
) -> tuple[AssignmentRevision, ...]:
    """Check a complete delta against current authority; never publish it."""
    mentions, decisions = _evidence(delta)
    constraints = {
        record_json(c): c for c in (*snapshot.constraints, *delta.constraints)
    }
    for constraint in delta.constraints:
        decision = decisions.get(constraint.decision_id)
        if (
            decision is None
            or decision.revision != constraint.decision_revision
        ):
            raise AssignmentRefused(
                "constraint lacks its exact decision evidence"
            )
        proposal = decision.proposal
        negative = (
            proposal.operation == "different" and decision.outcome == "approve"
        ) or (
            proposal.operation in ("same", "assign")
            and decision.outcome == "reject"
        )
        expected_kind = (
            "rejected_target"
            if proposal.operation == "assign"
            else "different"
        )
        if (
            not negative
            or constraint.kind != expected_kind
            or constraint.mentions != tuple(sorted(proposal.mentions))
            or constraint.entity_id
            != (proposal.target.entity_id if proposal.target else None)
        ):
            raise AssignmentRefused("constraint disagrees with its decision")
    for decision in decisions.values():
        p = decision.proposal
        if (p.operation == "different" and decision.outcome == "approve") or (
            p.operation in ("same", "assign") and decision.outcome == "reject"
        ):
            expected = AssignmentConstraint(
                kind="rejected_target"
                if p.operation == "assign"
                else "different",
                mentions=tuple(sorted(p.mentions)),
                entity_id=p.target.entity_id if p.target else None,
                decision_id=decision.id,
                decision_revision=decision.revision,
            )
            if record_json(expected) not in constraints:
                raise AssignmentRefused(
                    "negative decision was omitted from the delta"
                )

    parent = {key: key for key in mentions}

    def find(key):
        while parent[key] != key:
            key = parent[key]
        return key

    positives = []
    for decision in decisions.values():
        p = decision.proposal
        if p.operation == "same" and decision.outcome == "approve":
            a, b = p.mentions
            parent[find(b)] = find(a)
        if p.operation in ("same", "assign") and decision.outcome == "approve":
            positives.append(decision)
    desired = dict(snapshot.assignments)
    for target in delta.assignments:
        mention = mentions.get(target.mention_id)
        if mention is None or mention.revision != target.mention_revision:
            raise AssignmentRefused(
                "assignment lacks its current mention revision"
            )
        old = snapshot.assignments.get(target.mention_id)
        if old is not None and old.entity_id != target.entity_id:
            raise AssignmentRefused(
                "reassignment, merge and split are unsupported"
            )
        desired[target.mention_id] = target
        found = binding(target.entity_id)
        if (
            found is None
            or snapshot.generation is None
            or (
                found.binding.generation != snapshot.generation.generation
                or found.binding.deployment_id
                != snapshot.generation.deployment_id
            )
        ):
            raise AssignmentRefused(
                "assignment has no current verified entity binding"
            )

    groups = {}
    for key in mentions:
        groups.setdefault(find(key), set()).add(key)
    touched = {a.mention_id for a in delta.assignments}
    touched.update(
        mention
        for decision in positives
        if decision.proposal.operation == "same"
        for mention in decision.proposal.mentions
    )
    for members in groups.values():
        if members & touched:
            touched.update(members)
        targets = {desired[m].entity_id for m in members if m in desired}
        if len(targets) > 1:
            raise AssignmentRefused(
                "same decision would merge established entities"
            )
    for constraint in constraints.values():
        if set(constraint.mentions) & touched:
            current = decisions.get(constraint.decision_id)
            if (
                current is None
                or current.revision != constraint.decision_revision
                or not set(constraint.mentions) <= mentions.keys()
            ):
                raise AssignmentRefused(
                    "relevant retained negative evidence is unavailable"
                )
            previous = next(
                (
                    c.delta.evidence.decisions[constraint.decision_id]
                    for c in snapshot.history
                    if constraint in c.delta.constraints
                ),
                None,
            )
            if (
                previous is not None
                and current.model_dump(mode="json") != previous
            ):
                raise AssignmentRefused(
                    "retained negative decision evidence has changed"
                )
        if constraint.kind == "different":
            a, b = constraint.mentions
            if (
                a in desired
                and b in desired
                and desired[a].entity_id == desired[b].entity_id
            ):
                raise AssignmentRefused(
                    "assignment violates a retained different decision"
                )
            if a in parent and b in parent and find(a) == find(b):
                raise AssignmentRefused(
                    "same decision violates a retained different decision"
                )
        else:
            mention = constraint.mentions[0]
            members = groups[find(mention)] if mention in parent else {mention}
            if any(
                m in desired and desired[m].entity_id == constraint.entity_id
                for m in members
            ):
                raise AssignmentRefused(
                    "assignment violates a retained rejected target"
                )

    revisions = []
    for target in delta.assignments:
        members = groups[find(target.mention_id)]
        support = [d for d in positives if set(d.proposal.mentions) <= members]
        direct = [d for d in support if d.proposal.operation == "assign"]
        for decision in direct:
            proposed = decision.proposal.target
            verified = binding(proposed.entity_id)
            if (
                verified is None
                or verified.binding.model_dump(mode="json")
                != proposed.model_dump(mode="json")
                or proposed.entity_id != target.entity_id
            ):
                raise AssignmentRefused(
                    "approved target binding is unavailable or changed"
                )
        established = any(
            m in snapshot.assignments
            and snapshot.assignments[m].entity_id == target.entity_id
            for m in members
        )
        if not direct and not (established and support):
            raise AssignmentRefused(
                "assignment has no applicable affirmative decision"
            )
        if any(
            d.proposal.operation == "new_entity"
            and d.outcome == "approve"
            and set(d.proposal.mentions) & members
            for d in decisions.values()
        ):
            raise AssignmentRefused(
                "approved creation does not authorize an existing assignment"
            )
        if target.mention_id in snapshot.assignments:
            continue
        refs = tuple(
            DecisionReference(
                id=d.id,
                revision=d.revision,
                proposal_id=d.proposal.id,
                proposal_revision=d.proposal.revision,
                basis_digest=d.proposal.digest,
            )
            for d in sorted(support, key=lambda d: d.id)
        )
        data = target.model_dump(mode="json") | {
            "binding_revision": 1,
            "previous": None,
            "decision_references": [r.model_dump(mode="json") for r in refs],
        }
        revisions.append(
            AssignmentRevision(**data, revision_id=record_digest(data))
        )
    return tuple(revisions)


def advance(
    snapshot: AssignmentSnapshot, commit: AssignmentCommit
) -> AssignmentSnapshot:
    constraints = {
        record_json(c): c
        for c in (*snapshot.constraints, *commit.delta.constraints)
    }
    return AssignmentSnapshot(
        generation=snapshot.generation,
        head=record_digest(commit),
        assignments=snapshot.assignments
        | {r.mention_id: r for r in commit.revisions},
        constraints=tuple(constraints[key] for key in sorted(constraints)),
        history=(*snapshot.history, commit),
    )
