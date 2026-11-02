"""Accept current human identity decisions into the shared server authority.

Review observations and decisions remain in their explicit SQLite store. This
operation pins them while appending accepted assignments; it neither creates
entities nor writes historical claims. An unbound creation approval is returned
as awaiting creation, rather than treated as a binding.
"""

from __future__ import annotations

from dataclasses import dataclass

from yizkr.core.identity_assignments import (
    AssignmentConstraint,
    AssignmentDelta,
    AssignmentEvidence,
    AssignmentRefused,
    AssignmentSnapshot,
    AssignmentTarget,
    validated_revisions,
)
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.review_stages import ReviewRequest, prepare_review

_CURRENT_HEAD = object()


@dataclass(frozen=True)
class AssignmentAcceptance:
    snapshot: AssignmentSnapshot
    awaiting_creation: tuple[tuple[str, ...], ...]
    unassigned: tuple[str, ...]


def accept_reviewed_assignments(
    store: ReviewStore,
    request: ReviewRequest,
    registry: CreationRegistry,
    *,
    expected_head: str | None | object = _CURRENT_HEAD,
) -> AssignmentAcceptance:
    """Atomically accept a fully current review scope, or refuse that scope.

    Omit expected_head to use the authority read by this call; explicit None
    names an empty predecessor, including exact lost-ack recovery.
    Callers can separate unrelated sources, but stale or blocked proposals in
    this request are never skipped.
    The durable preparation precedes the pinned transaction, so even a lost
    server response cannot leave a server reference to an uncommitted review.
    """
    prepared = prepare_review(store, request)
    projection = prepared.projection
    if any(app.status != "applied" for app in projection.applications):
        raise ReviewRefused(
            "assignment acceptance requires current, unblocked review"
        )
    mentions = {mention.id: mention for mention in prepared.mentions.values()}
    decisions = {
        app.decision.id: app.decision
        for app in projection.applications
        if app.decision is not None
    }
    with store.pin_projection(projection, list(mentions.values())):
        snapshot = registry.assignment_snapshot()
        head = (
            snapshot.head if expected_head is _CURRENT_HEAD else expected_head
        )
        targets = {
            binding.entity_id: binding for binding in request.current_targets
        }
        # These are authoritative receipt checks, not name/QID correspondence.
        verified = {
            key: registry.verify_binding(binding)
            for key, binding in targets.items()
        }

        parents = {mention: mention for mention in mentions}

        def component(mention):
            while parents[mention] != mention:
                parents[mention] = parents[parents[mention]]
                mention = parents[mention]
            return mention

        for first, second in projection.same:
            parents[component(first)] = component(second)
        groups: dict[str, list[str]] = {}
        for mention in mentions:
            groups.setdefault(component(mention), []).append(mention)
        active = {
            mention
            for decision in decisions.values()
            if decision.outcome == "approve"
            and decision.proposal.operation in ("same", "assign", "new_entity")
            for mention in decision.proposal.mentions
        }
        assignments = []
        awaiting = []
        for group in groups.values():
            if not active.intersection(group):
                continue
            chosen = {
                projection.assignments[mention].entity_id
                for mention in group
                if mention in projection.assignments
            } | {
                snapshot.assignments[mention].entity_id
                for mention in group
                if mention in snapshot.assignments
            }
            authorizations = set(group).intersection(projection.new_entities)
            if len(chosen) > 1:
                raise AssignmentRefused(
                    "accepted entities cannot be merged or reassigned"
                )
            if chosen and authorizations:
                raise AssignmentRefused(
                    "creation approval conflicts with an accepted target"
                )
            if len(authorizations) > 1:
                raise AssignmentRefused(
                    "same component has multiple creation approvals"
                )
            if not chosen:
                if authorizations:
                    awaiting.append(tuple(sorted(group)))
                continue
            (entity_id,) = chosen
            if entity_id not in targets:
                raise AssignmentRefused(
                    "accepted target needs a current verified binding"
                )
            for mention in group:
                assignments.append(
                    AssignmentTarget(
                        mention_id=mention,
                        mention_revision=mentions[mention].revision,
                        entity_id=entity_id,
                    )
                )

        constraints = []
        for decision in decisions.values():
            proposal = decision.proposal
            if (
                proposal.operation == "different"
                and decision.outcome == "approve"
            ) or (
                proposal.operation == "same" and decision.outcome == "reject"
            ):
                constraints.append(
                    AssignmentConstraint(
                        kind="different",
                        mentions=tuple(sorted(proposal.mentions)),
                        decision_id=decision.id,
                        decision_revision=decision.revision,
                    )
                )
            elif (
                proposal.operation == "assign" and decision.outcome == "reject"
            ):
                constraints.append(
                    AssignmentConstraint(
                        kind="rejected_target",
                        mentions=proposal.mentions,
                        entity_id=proposal.target.entity_id,
                        decision_id=decision.id,
                        decision_revision=decision.revision,
                    )
                )
        delta = AssignmentDelta(
            assignments=tuple(
                sorted(assignments, key=lambda item: item.mention_id)
            ),
            constraints=tuple(constraints),
            evidence=AssignmentEvidence(
                mentions={
                    key: value.model_dump(mode="json")
                    for key, value in mentions.items()
                },
                decisions={
                    key: value.model_dump(mode="json")
                    for key, value in decisions.items()
                },
            ),
        )
        used_targets = {item.entity_id for item in assignments} | {
            item.entity_id
            for item in constraints
            if item.entity_id is not None
        }
        # Let the authority recognize an identical lost-ack retry at its old
        # head. A stale, different delta must not publish even binding aliases.
        if head == snapshot.head:
            # Reuse the authority's validator before writing binding indexes;
            # omitted or altered negative evidence must fail before effects.
            validated_revisions(
                snapshot,
                delta,
                lambda entity_id: (
                    verified.get(entity_id) or registry.binding(entity_id)
                ),
            )
            for entity_id in sorted(used_targets):
                registry.index_binding(targets[entity_id])
        accepted = registry.append_assignments(delta, expected_head=head)
        waiting = tuple(sorted(awaiting))
        unresolved = (
            set(mentions)
            - accepted.assignments.keys()
            - {mention for group in waiting for mention in group}
        )
        return AssignmentAcceptance(
            accepted, waiting, tuple(sorted(unresolved))
        )
