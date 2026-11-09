"""Review and apply bounded later-cohort identities under server exclusion.

A saved graph or matching product is advice. Application reacquires current
source correspondence and candidate evidence while the server excludes other
writers. Historical claims retain their separate review and support receipts.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from pathlib import Path

from yizkr.core.mutation_guard import protected_mutation
from yizkr.core.reviewed_creation import creation_review_evidence
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline.assignment_stages import (
    AssignmentAcceptance,
    accept_reviewed_assignments,
    assignment_review_scope,
)
from yizkr.pipeline.cohort_context import (
    CohortCandidates,
    CohortContext,
    capture_cohort_context,
    matching_candidates,
)
from yizkr.pipeline.identity_provenance import IdentityPublication
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.review_stages import (
    MentionKey,
    PreparedReview,
    ReviewProposal,
    ReviewRequest,
    prepare_review,
)
from yizkr.pipeline.source_bindings import unique_source_qids

_EVIDENCE = "cohort_candidates"
_METHODS = {"assign": "reviewed_assignment", "new_entity": "reviewed_creation"}


@dataclass(frozen=True)
class PreparedCohort:
    request: ReviewRequest
    review: PreparedReview
    context: CohortContext
    candidates: CohortCandidates


@dataclass(frozen=True)
class CohortApplication:
    acceptance: AssignmentAcceptance
    publications: tuple[IdentityPublication, ...]
    request: ReviewRequest
    protection: dict


def _scope(proposal, mentions):
    return (
        proposal.operation,
        tuple(sorted(mentions)),
        proposal.target.entity_id if proposal.target else None,
        # The review store separately includes before in its scope key.
        json.dumps(proposal.before, sort_keys=True),
    )


def _creation_payload(client, label, mention_id):
    item = client.wbi.item.new()
    item.labels.set("en", label)
    item.descriptions.set("en", f"Reviewed source identity {mention_id}")
    return item.get_json()


def _candidate_basis(context, match, own_proposal):
    candidates = []
    for candidate in match.candidates:
        # An exact creation's own result is expected on interrupted retries.
        # Its current source interpretation and original payload are still
        # reviewed separately, and every competing candidate remains visible.
        if any(
            c.authorization.decision.proposal.id == own_proposal
            for c in candidate.creations
        ):
            continue
        candidates.append(
            {
                "qid": candidate.qid,
                "binding": (
                    candidate.binding.model_dump(mode="json")
                    if candidate.binding
                    else None
                ),
                "facts": list(candidate.current_facts),
                "creation_contexts": sorted(
                    c.authorization.digest for c in candidate.creations
                ),
            }
        )
    pending = [
        c.authorization.digest
        for c in match.pending
        if c.authorization.decision.proposal.id != own_proposal
    ]
    return {
        "format": 1,
        "generation": context.projection.generation.model_dump(mode="json"),
        "candidates": candidates,
        "pending_creations": sorted(pending),
        "uncovered_qids": list(context.uncovered_qids),
    }


def prepare_cohort_additions(
    store: ReviewStore, request: ReviewRequest, registry: CreationRegistry
) -> PreparedCohort:
    """Refresh explicit choices with current, scoped graph decision material.

    ``assign`` and ``new_entity`` are explicit human choices, never inferred
    from an empty matcher result. Existing same/different review context is
    preserved. Display renames, unrelated graph edits and previously accepted
    identities do not revoke unchanged source decisions. This read-only graph
    capture is repeated inside the enforced application boundary.
    """
    context = capture_cohort_context(registry)
    targets = {b.entity_id: b for b in request.current_targets}
    for registered in context.projection.bindings.values():
        targets.setdefault(registered.binding.entity_id, registered.binding)
    # Observe first without resetting an existing enriched proposal to the
    # caller's original, unrefreshed evidence on every invocation.
    observed = prepare_review(
        store,
        replace(
            request, proposals=(), current_targets=tuple(targets.values())
        ),
    )
    keys = {
        mention.id: MentionKey(artifact_sha1=key[0], local_id=key[1])
        for key, mention in observed.mentions.items()
    }
    old = {
        _scope(p, p.mentions): p
        for p in observed.proposals
        if set(p.mentions) <= keys.keys()
    }
    choices = {}
    previous = {}
    for key, proposal in old.items():
        if proposal.origin.get("method") not in _METHODS.values():
            continue
        choices[key] = ReviewProposal(
            operation=proposal.operation,
            mentions=tuple(keys[m] for m in proposal.mentions),
            target=proposal.target,
            evidence=proposal.basis.get("evidence", {}),
            origin=proposal.origin,
            before=proposal.before,
        )
        for mention in proposal.mentions:
            previous.setdefault(keys[mention].key, set()).update(
                candidate["qid"]
                for candidate in proposal.basis.get("evidence", {})
                .get(_EVIDENCE, {})
                .get("candidates", ())
            )
    for proposal in request.proposals:
        scope = [observed.mentions[key.key].id for key in proposal.mentions]
        choices[_scope(proposal, scope)] = proposal
        if proposal.target is not None:
            previous.setdefault(proposal.mentions[0].key, set()).add(
                proposal.target.qid
            )
    candidates = matching_candidates(
        context, request.sources, observed, previous_qids=previous
    )
    by_mention = {m.match.mention_id: m for m in candidates.mentions}
    proposals = []
    for key, proposal in choices.items():
        if proposal.operation not in _METHODS:
            proposals.append(proposal)
            continue
        mention = observed.mentions[proposal.mentions[0].key]
        prior = old.get(key)
        evidence = dict(proposal.evidence)
        if (
            mention.id in context.projection.assignments.assignments
            and prior is not None
            and _EVIDENCE in prior.basis.get("evidence", {})
        ):
            # The server assignment remains authority. New alternatives cannot
            # silently reopen or move an already accepted identity.
            graph_basis = prior.basis["evidence"][_EVIDENCE]
        else:
            graph_basis = _candidate_basis(
                context,
                by_mention[mention.id],
                prior.id
                if prior and proposal.operation == "new_entity"
                else None,
            )
        evidence[_EVIDENCE] = graph_basis
        if proposal.operation == "new_entity":
            original = (
                prior.basis.get("evidence", {}).get("reviewed_creation")
                if prior
                else None
            )
            evidence["reviewed_creation"] = (
                original
                or creation_review_evidence(
                    _creation_payload(
                        registry.client,
                        observed.labels[mention.id],
                        mention.id,
                    )
                )
            )
        proposals.append(
            proposal.model_copy(
                update={
                    "evidence": evidence,
                    "origin": proposal.origin
                    | {
                        "proposal_origin": proposal.origin.get(
                            "proposal_origin", proposal.origin
                        ),
                        "method": _METHODS[proposal.operation],
                        "process": "deterministic",
                        "cohort_context_sha256": context.digest,
                    },
                }
            )
        )
    refreshed = replace(
        request,
        proposals=tuple(proposals),
        current_targets=tuple(targets.values()),
    )
    return PreparedCohort(
        refreshed, prepare_review(store, refreshed), context, candidates
    )


def _approved_scope(prepared, scope):
    review = prepared.review
    snapshot = prepared.context.projection.assignments
    selected = (
        {m.id for m in review.mentions.values()}
        if scope is None
        else set(scope)
    )
    relevant = assignment_review_scope(review, snapshot, selected)
    proposals = {p.id: p for p in review.proposals}
    applications = [
        app
        for app in review.projection.applications
        if set(proposals[app.proposal_id].mentions) & relevant
    ]
    if any(app.status != "applied" for app in applications):
        raise ReviewRefused(
            "cohort application needs current, unblocked review"
        )
    approved = [
        app.decision
        for app in applications
        if app.decision is not None
        and app.decision.outcome == "approve"
        and app.decision.proposal.operation in _METHODS
        and set(app.decision.proposal.mentions) & selected
    ]
    observed = {m.id: m for m in review.mentions.values()}
    matches = {
        m.match.mention_id: m.match for m in prepared.candidates.mentions
    }
    for decision in approved:
        proposal = decision.proposal
        mention = proposal.mentions[0]
        retained = snapshot.assignments.get(mention)
        if retained is not None:
            if proposal.operation == "assign":
                valid = proposal.target.entity_id == retained.entity_id
            else:
                valid = any(
                    creation.authorization.decision == decision
                    and creation.binding is not None
                    and creation.binding.entity_id == retained.entity_id
                    for creation in prepared.context.creations
                )
            if not valid:
                raise ReviewRefused("accepted identities cannot be reassigned")
        elif (
            proposal.operation == "new_entity"
            and matches[mention].same_component_target is not None
        ):
            raise ReviewRefused(
                "creation conflicts with an accepted same target"
            )
    for mention in selected - snapshot.assignments.keys():
        observation = observed[mention].observation
        if (
            observation.anchor is None
            or observation.source_scope is None
            or observation.limitations
            or matches[mention].issues
        ):
            raise ReviewRefused(
                "cohort addition lacks complete source correspondence "
                "or matching context"
            )
        matching = [d for d in approved if mention in d.proposal.mentions]
        if len(matching) != 1:
            raise ReviewRefused(
                "each new assignment needs one explicit cohort choice"
            )
        decision = matching[0]
        proposal = decision.proposal
        evidence = proposal.basis.get("evidence", {}).get(_EVIDENCE)
        if (
            proposal.origin.get("method") != _METHODS[proposal.operation]
            or evidence is None
        ):
            raise ReviewRefused(
                "assignment lacks current cohort candidate review"
            )
        if (
            evidence["uncovered_qids"]
            or evidence["pending_creations"]
            or any(c["binding"] is None for c in evidence["candidates"])
        ):
            raise ReviewRefused("cohort candidate coverage is incomplete")
        if proposal.operation == "assign" and not any(
            c["binding"]["entity_id"] == proposal.target.entity_id
            for c in evidence["candidates"]
        ):
            raise ReviewRefused(
                "chosen target is absent from current candidates"
            )
    creations = [d for d in approved if d.proposal.operation == "new_entity"]
    if len(creations) > 1:
        raise ReviewRefused(
            "apply at most one reviewed creation per protected operation"
        )
    return selected, approved


def apply_cohort_additions(
    store: ReviewStore,
    request: ReviewRequest,
    registry: CreationRegistry,
    *,
    state_path: Path,
    operation_id: str,
    record_dir: Path,
    scope: Collection[str] | None = None,
    admit: Callable[[], None] | None = None,
) -> CohortApplication:
    """Accept identities and publish their origins; do not approve claims.

    The configured server guard is mandatory. A single creation per call lets
    the next choice inspect its predecessor as a candidate. Retry with the same
    review store, source products, capability path and operation ID. No saved
    projection, local lock or caller-supplied evidence substitutes for capture.
    A caller may recheck external inputs through ``admit`` after guarded
    preparation, before any creation, assignment or provenance publication.
    """
    from yizkr.pipeline.cohort_provenance import publish_cohort_assignment

    with protected_mutation(
        registry.client, state_path=state_path, operation_id=operation_id
    ) as guard:
        current = prepare_cohort_additions(store, request, registry)
        selected, approved = _approved_scope(current, scope)
        mentions = {m.id: m for m in current.review.mentions.values()}
        properties, _ = registry.client.vocabulary()
        unique_source_qids(
            registry.client,
            properties["media_sha1"],
            {mentions[m].observation.artifact for m in selected},
        )
        if admit is not None:
            admit()
        with store.pin_projection(
            current.review.projection, list(mentions.values())
        ):
            targets = {b.entity_id: b for b in current.request.current_targets}
            for decision in approved:
                if decision.proposal.operation != "new_entity":
                    continue
                guard.assert_active()
                payload = decision.proposal.basis["evidence"][
                    "reviewed_creation"
                ]["initial_payload"]
                authorization = registry.authorize_reviewed_creation(
                    decision,
                    mentions[decision.proposal.mentions[0]],
                    initial_payload=payload,
                    origin={"protection": guard.evidence},
                )
                item = registry.client.wbi.item.new()
                for field in ("labels", "descriptions"):
                    for language, term in payload.get(field, {}).items():
                        getattr(item, field).set(language, term["value"])
                if item.get_json() != payload:
                    raise ReviewRefused(
                        "creation payload cannot be reproduced"
                    )
                binding = registry.create(
                    authorization.operation,
                    item,
                    summary="identity: explicit reviewed creation",
                )
                registry.index_binding(binding)
                targets[binding.entity_id] = binding
        # Acceptance opens its own pinned transaction and proves the exact
        # creation receipt. No invented assign proposal is inserted here.
        guarded_request = replace(
            current.request, current_targets=tuple(targets.values())
        )
        guard.assert_active()
        acceptance = accept_reviewed_assignments(
            store, guarded_request, registry, scope=selected
        )
        publication_mentions = set()
        for mention in selected:
            assignment = acceptance.snapshot.assignments.get(mention)
            if assignment is None:
                continue
            references = {
                (ref.id, ref.revision)
                for ref in assignment.decision_references
            }
            for commit in acceptance.snapshot.history:
                for raw in commit.delta.evidence.decisions.values():
                    proposal = raw["proposal"]
                    if (
                        (raw["id"], raw["revision"]) in references
                        and proposal["mentions"] == [mention]
                        and proposal["operation"] in _METHODS
                        and proposal["origin"].get("method")
                        == _METHODS[proposal["operation"]]
                    ):
                        publication_mentions.add(mention)
        publications = tuple(
            publish_cohort_assignment(
                registry.client, mention, record_dir=record_dir
            )
            for mention in sorted(publication_mentions)
        )
        guard.assert_active()
        return CohortApplication(
            acceptance, publications, guarded_request, guard.evidence
        )
