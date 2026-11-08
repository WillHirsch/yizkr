"""Current graph advice plus separately recorded creation interpretations.

Approved creation context is operational evidence, not a historical graph
fact. It participates only in deterministic candidate blocking; it is never
inserted into the graph roster or passed to an adjudication prompt.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from yizkr.core.identity_assignments import record_digest
from yizkr.core.instance import watermark
from yizkr.core.reviewed_creation import ReviewedCreationAuthorization
from yizkr.core.write_recovery import AmbiguousCreation, EntityBinding
from yizkr.pipeline.cohort_matching import (
    CandidateMatch,
    CohortMatchResult,
    MentionMatch,
    match_cohort,
)
from yizkr.pipeline.graph_projection import (
    GraphProjection,
    capture_graph_projection,
)
from yizkr.pipeline.reconcile import (
    PlaceContainment,
    RosterEntity,
    block_candidates,
)

_DISPLAY = {
    "label",
    "aliases",
    "kind_label",
    "spouses",
    "fathers",
    "mothers",
}
_KIN = (
    "spouse_qids",
    "father_qids",
    "mother_qids",
    "child_qids",
    "sibling_qids",
)


class CohortContextRefused(ValueError):
    """Graph and operational authority do not describe a coherent capture."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CreationContext(_Record):
    authorization: ReviewedCreationAuthorization
    binding: EntityBinding | None = None
    ambiguity: str | None = None
    current_terms: tuple[str, ...] = ()


class CohortContext(_Record):
    projection: GraphProjection
    creations: tuple[CreationContext, ...]
    uncovered_qids: tuple[str, ...]

    @property
    def coverage_blockers(self) -> tuple[str, ...]:
        return ("uncovered_kindless_entities",) if self.uncovered_qids else ()

    @property
    def digest(self) -> str:
        return record_digest(self)


class ContextCandidate(_Record):
    qid: str
    binding: EntityBinding | None
    graph: CandidateMatch | None
    creations: tuple[CreationContext, ...]
    current_facts: tuple[dict[str, Any], ...]
    blockers: tuple[str, ...]


class ContextMention(_Record):
    match: MentionMatch
    candidates: tuple[ContextCandidate, ...]
    pending: tuple[CreationContext, ...]
    blockers: tuple[str, ...]


class CohortCandidates(_Record):
    graph: CohortMatchResult
    mentions: tuple[ContextMention, ...]


def identity_facts(projection: GraphProjection, qid: str) -> tuple[dict, ...]:
    """Current matching facts without mutable display terms or revisions.

    Asserted names remain evidence. Kin display names are excluded together
    with the item's labels; their stable related-entity QIDs remain available.
    """
    facts = []
    for row in projection.roster:
        if row.qid != qid:
            continue
        value = row.model_dump(mode="json", exclude=_DISPLAY)
        for key, field in value.items():
            if isinstance(field, list):
                value[key] = sorted(set(field))
        facts.append(value)
    return tuple(sorted(facts, key=record_digest))


def capture_cohort_context(registry) -> CohortContext:
    """Capture all authorizations, including unindexed and unfinished work."""
    before = registry.reviewed_creation_authorizations()
    projection = capture_graph_projection(registry)
    creations = []
    bindings = {
        qid: registered.binding
        for qid, registered in projection.bindings.items()
    }
    entities = {binding.entity_id: qid for qid, binding in bindings.items()}
    for authorization in before:
        if authorization.generation != projection.generation:
            raise CohortContextRefused(
                "creation belongs to another generation"
            )
        try:
            binding = registry.reviewed_creation_binding(authorization)
        except AmbiguousCreation as exc:
            creations.append(
                CreationContext(
                    authorization=authorization, ambiguity=str(exc)
                )
            )
            continue
        if binding is not None:
            if (
                binding.generation != projection.generation.generation
                or binding.deployment_id != projection.generation.deployment_id
                or binding.qid not in projection.entity_revisions
                or binding.revision > projection.entity_revisions[binding.qid]
                or (
                    binding.qid in bindings
                    and bindings[binding.qid].model_dump()
                    != binding.model_dump()
                )
                or entities.get(binding.entity_id, binding.qid) != binding.qid
            ):
                raise CohortContextRefused(
                    "reviewed creation differs from current graph identity"
                )
            bindings[binding.qid] = binding
            entities[binding.entity_id] = binding.qid
        creations.append(
            CreationContext(
                authorization=authorization,
                binding=binding,
                current_terms=(
                    _current_terms(registry.client, binding.qid)
                    if binding is not None
                    else ()
                ),
            )
        )
    after = registry.reviewed_creation_authorizations()
    if (
        before != after
        or registry.generation != projection.generation
        or watermark(registry.client) != projection.capture.watermark_after
    ):
        raise CohortContextRefused(
            "graph or creation context changed during capture"
        )
    _, vocabulary = registry.client.vocabulary()
    covered = (
        {row.qid for row in projection.roster}
        | set(vocabulary.values())
        | {c.binding.qid for c in creations if c.binding is not None}
    )
    uncovered = tuple(
        sorted(
            qid
            for qid in projection.entity_revisions
            if qid.startswith("Q") and qid not in covered
        )
    )
    return CohortContext(
        projection=projection,
        creations=tuple(creations),
        uncovered_qids=uncovered,
    )


def _current_terms(client, qid):
    entity = client.wbi.item.get(qid).get_json()
    return tuple(
        sorted(
            {term["value"] for term in entity.get("labels", {}).values()}
            | {
                term["value"]
                for aliases in entity.get("aliases", {}).values()
                for term in aliases
            }
        )
    )


def _blocking_row(context, creation):
    approved = creation.authorization.context
    qid = creation.binding.qid if creation.binding is not None else ""
    # An empty transport key is not an entity identifier. Pending creations
    # are blocked separately and never exported as candidates with fake QIDs.
    kin = {
        key: sorted(
            {
                related
                for row in context.projection.roster
                if row.qid == qid
                for related in getattr(row, key)
            }
        )
        for key in _KIN
    }
    return RosterEntity(
        qid=qid,
        label=approved.label,
        aliases=sorted(set(approved.aliases) | set(creation.current_terms)),
        kind=approved.kind,
        **kin,
    )


def matching_candidates(
    context, sources, prepared_review, *, previous_qids=None
) -> CohortCandidates:
    """Union ordinary advice and approved creation context by verified QID.

    This does not authorize an assignment or creation. An unfinished matching
    authorization blocks that mention's creation; unknown untyped items make
    creation coverage incomplete for every mention, conservatively.
    """
    graph = match_cohort(
        context.projection, sources, prepared_review=prepared_review
    )
    containment = PlaceContainment(
        {
            qid: set(parents)
            for qid, parents in context.projection.containment.items()
        }
    )
    operational = {}
    for source in sources:
        for creation in context.creations:
            matched = block_candidates(
                source.batch.entities,
                [_blocking_row(context, creation)],
                batch=source.batch,
                containment=containment,
            )
            for local_id in matched:
                operational.setdefault(
                    (source.artifact_sha1, local_id), []
                ).append(creation)
    mentions = []
    for match in graph.mentions:
        selected = operational.get((match.artifact_sha1, match.local_id), ())
        pending = tuple(c for c in selected if c.binding is None)
        by_qid = {candidate.qid: [] for candidate in match.candidates}
        for creation in selected:
            if creation.binding is not None:
                by_qid.setdefault(creation.binding.qid, []).append(creation)
        graph_candidates = {
            candidate.qid: candidate for candidate in match.candidates
        }
        previous = (previous_qids or {}).get(
            (match.artifact_sha1, match.local_id), ()
        )
        for qid in sorted(set(previous)):
            by_qid.setdefault(qid, [])
            for creation in context.creations:
                if (
                    creation.binding is not None
                    and creation.binding.qid == qid
                    and creation not in by_qid[qid]
                ):
                    by_qid[qid].append(creation)
            if qid not in graph_candidates:
                row = next(
                    (
                        row
                        for row in context.projection.roster
                        if row.qid == qid
                    ),
                    None,
                )
                if row is not None:
                    graph_candidates[qid] = CandidateMatch(
                        qid=qid,
                        label=row.label,
                        kind=row.kind,
                        facts=row.facts(),
                        binding=context.projection.bindings.get(qid),
                        entity_revision=context.projection.entity_revisions[
                            qid
                        ],
                    )
        candidates = []
        for qid, creations in sorted(by_qid.items()):
            candidate = graph_candidates.get(qid)
            registered = context.projection.bindings.get(qid)
            binding = (
                registered.binding
                if registered is not None
                else creations[0].binding
                if creations
                else None
            )
            blockers = set(candidate.blockers if candidate is not None else ())
            if qid not in context.projection.entity_revisions:
                blockers.add("candidate_unavailable")
            if binding is None:
                blockers.add("durable_binding_unavailable")
            else:
                blockers.discard("durable_binding_unavailable")
                for target, reason in (
                    (
                        match.accepted_binding.binding
                        if match.accepted_binding
                        else None,
                        "reassignment_unsupported",
                    ),
                    (match.reviewed_target, "reviewed_target_conflict"),
                    (
                        match.same_component_target.binding
                        if match.same_component_target
                        else None,
                        "same_component_target_conflict",
                    ),
                ):
                    if (
                        target is not None
                        and target.entity_id != binding.entity_id
                    ):
                        blockers.add(reason)
                rejected = prepared_review.projection.rejected_targets.get(
                    match.mention_id, ()
                )
                constraints = context.projection.assignments.constraints
                if any(
                    target.entity_id == binding.entity_id
                    for target in rejected
                ) or any(
                    constraint.kind == "rejected_target"
                    and match.mention_id in constraint.mentions
                    and constraint.entity_id == binding.entity_id
                    for constraint in constraints
                ):
                    blockers.add("retained_negative_decision")
            candidates.append(
                ContextCandidate(
                    qid=qid,
                    binding=binding,
                    graph=candidate,
                    creations=tuple(creations),
                    current_facts=identity_facts(context.projection, qid),
                    blockers=tuple(sorted(blockers)),
                )
            )
        blockers = {*match.issues, *context.coverage_blockers}
        for creation in pending:
            blockers.add(
                "ambiguous_reviewed_creation"
                if creation.ambiguity is not None
                else "pending_reviewed_creation"
            )
        mentions.append(
            ContextMention(
                match=match,
                candidates=tuple(candidates),
                pending=pending,
                blockers=tuple(sorted(blockers)),
            )
        )
    return CohortCandidates(graph=graph, mentions=tuple(mentions))
