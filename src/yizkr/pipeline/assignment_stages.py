"""Accept current human identity decisions into the shared server authority.

Review observations and decisions remain in their explicit SQLite store. This
operation pins them while appending accepted assignments; it neither creates
entities nor writes historical claims. An unbound creation approval is returned
as awaiting creation, rather than treated as a binding.
"""

from __future__ import annotations

from collections.abc import Collection
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
from yizkr.pipeline.review_stages import (
    PreparedReview,
    ReviewRequest,
    prepare_review,
)

_CURRENT_HEAD = object()


@dataclass(frozen=True)
class AssignmentAcceptance:
    snapshot: AssignmentSnapshot
    awaiting_creation: tuple[tuple[str, ...], ...]
    unassigned: tuple[str, ...]


def assignment_review_scope(
    prepared: PreparedReview,
    snapshot: AssignmentSnapshot,
    scope: Collection[str] | None,
) -> frozenset[str]:
    """The decision context of selected mentions, including durable negatives.

    Context is not publication scope: a related occurrence may be necessary to
    validate a decision without authorizing an assignment for that occurrence.
    """
    present = {mention.id for mention in prepared.mentions.values()}
    selected = present if scope is None else set(scope)
    if (scope is not None and not selected) or not selected <= present:
        raise AssignmentRefused(
            "assignment scope lacks current source mentions"
        )
    relevant = set(selected)
    pairs = [set(p.mentions) for p in prepared.proposals]
    pairs += [set(c.mentions) for c in snapshot.constraints]
    while True:
        expanded = relevant | set().union(
            *(pair for pair in pairs if pair & relevant)
        )
        if expanded == relevant:
            return frozenset(relevant)
        relevant = expanded


def accept_reviewed_assignments(
    store: ReviewStore,
    request: ReviewRequest,
    registry: CreationRegistry,
    *,
    expected_head: str | None | object = _CURRENT_HEAD,
    scope: Collection[str] | None = None,
) -> AssignmentAcceptance:
    """Atomically accept a fully current review scope, or refuse that scope.

    Omit expected_head to use the authority read by this call; explicit None
    names an empty predecessor, including exact lost-ack recovery.
    An optional durable-mention scope limits new positive assignments, while
    related decisions and retained negatives still participate in validation.
    Without it every supplied occurrence remains in scope.
    The durable preparation precedes the pinned transaction, so even a lost
    server response cannot leave a server reference to an uncommitted review.
    """
    prepared = prepare_review(store, request)
    projection = prepared.projection
    all_mentions = {
        mention.id: mention for mention in prepared.mentions.values()
    }
    with store.pin_projection(projection, list(all_mentions.values())):
        snapshot = registry.assignment_snapshot()
        selected = set(all_mentions) if scope is None else set(scope)
        relevant = assignment_review_scope(prepared, snapshot, scope)
        proposals = {p.id: p for p in prepared.proposals}
        applications = [
            app
            for app in projection.applications
            if relevant.intersection(proposals[app.proposal_id].mentions)
        ]
        if any(app.status != "applied" for app in applications):
            raise ReviewRefused(
                "assignment acceptance requires current, unblocked review"
            )
        mentions = {
            key: value
            for key, value in all_mentions.items()
            if key in relevant
        }
        decisions = {
            app.decision.id: app.decision
            for app in applications
            if app.decision is not None
        }
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
            if first in parents and second in parents:
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
            if not active.intersection(group) or not selected.intersection(
                group
            ):
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
                    awaiting.append(
                        tuple(sorted(selected.intersection(group)))
                    )
                continue
            (entity_id,) = chosen
            if entity_id not in targets:
                raise AssignmentRefused(
                    "accepted target needs a current verified binding"
                )
            for mention in group:
                if mention not in selected:
                    continue
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
            selected
            - accepted.assignments.keys()
            - {mention for group in waiting for mention in group}
        )
        return AssignmentAcceptance(
            accepted, waiting, tuple(sorted(unresolved))
        )
