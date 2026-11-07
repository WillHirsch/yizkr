"""Replayable candidate advice from source products and a recorded graph.

The existing roster blocker and adjudicator supply proposals. Accepted
assignments and human review remain separate authority: this module neither
records decisions nor exports an application request. An empty candidate list
is never evidence that creating an entity is safe or authorized.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.claims import container_problems, structure_problems
from yizkr.core.identity_assignments import (
    AssignmentRevision,
    RegistryBinding,
    record_digest,
)
from yizkr.core.write_recovery import EntityBinding
from yizkr.pipeline.graph_projection import GraphProjection
from yizkr.pipeline.identity_review import Application, Proposal
from yizkr.pipeline.llm import LlmClient
from yizkr.pipeline.reconcile import (
    ADJUDICATION_PROMPT,
    PlaceContainment,
    adjudicate,
    block_candidates,
)
from yizkr.pipeline.review_mentions import observe_mentions
from yizkr.pipeline.review_stages import PreparedReview, ReviewSource
from yizkr.pipeline.stages import atomic_write_text, sha256_text
from yizkr.pipeline.transcribe import reading_digest

_HASH = r"^[0-9a-f]{64}$"
_ID = r"^[0-9a-f]{32}$"


class MatchingRefused(ValueError):
    """The supplied products do not describe one inspectable matching input."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdjudicationProvenance(_Record):
    """Configured provider identity, independent of response caching."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    configuration: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None

    @model_validator(mode="after")
    def json_configuration(self):
        record_digest(self.configuration)
        return self


class MatchingSource(_Record):
    artifact_sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    batch_sha256: str = Field(pattern=_HASH)
    reading_sha256: str | None = Field(default=None, pattern=_HASH)


class MatchingReview(_Record):
    proposal_id: str
    proposal_revision: int = Field(gt=0)
    operation: str
    status: Literal["applied", "needs_review", "blocked"]
    decision_id: str | None = None
    decision_revision: int | None = Field(default=None, gt=0)
    outcome: Literal["approve", "reject"] | None = None
    reasons: tuple[str, ...] = ()


class CandidateMatch(_Record):
    """Displayed facts; projection digest and QID identify the full row."""

    qid: str = Field(pattern=r"^Q[1-9][0-9]*$")
    label: str
    kind: str
    facts: str
    binding: RegistryBinding | None = None
    entity_revision: int | None = Field(default=None, gt=0)
    origin: Literal[
        "blocking",
        "accepted_assignment",
        "accepted_same_component",
        "reviewed_assignment",
    ] = "blocking"
    blockers: tuple[str, ...] = ()
    adjudication: Literal["not_requested", "complete", "incomplete"] = (
        "not_requested"
    )
    verdict: Literal["same", "uncertain", "distinct"] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: str = ""

    @model_validator(mode="after")
    def target_and_verdict(self):
        if self.binding and self.binding.binding.qid != self.qid:
            raise ValueError("candidate binding belongs to another QID")
        if (self.adjudication == "complete") != (self.verdict is not None):
            raise ValueError("complete adjudication needs exactly one verdict")
        return self


class MentionMatch(_Record):
    artifact_sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    local_id: str
    mention_id: str = Field(pattern=_ID)
    mention_revision: int = Field(gt=0)
    label: str | None = None
    status: Literal[
        "retained", "candidates", "no_candidates", "blocked", "incomplete"
    ]
    accepted_assignment: AssignmentRevision | None = None
    accepted_binding: RegistryBinding | None = None
    same_component_target: RegistryBinding | None = None
    reviewed_target: EntityBinding | None = None
    candidates: tuple[CandidateMatch, ...] = ()
    review: tuple[MatchingReview, ...] = ()
    issues: tuple[str, ...] = ()

    @model_validator(mode="after")
    def scope(self):
        if self.accepted_assignment is not None:
            if self.accepted_assignment.mention_id != self.mention_id:
                raise ValueError(
                    "accepted assignment belongs to another mention"
                )
            if self.accepted_binding is not None and (
                self.accepted_binding.binding.entity_id
                != self.accepted_assignment.entity_id
            ):
                raise ValueError("accepted binding belongs to another entity")
        elif self.accepted_binding is not None:
            raise ValueError(
                "accepted binding requires an accepted assignment"
            )
        if len({c.qid for c in self.candidates}) != len(self.candidates):
            raise ValueError("mention repeats a candidate")
        if self.status == "retained" and self.accepted_assignment is None:
            raise ValueError("retained status requires an accepted assignment")
        if self.status == "no_candidates" and self.candidates:
            raise ValueError("no-candidates status cannot contain candidates")
        return self


class CohortMatchResult(_Record):
    format: Literal[1] = 1
    kind: Literal["cohort-matching-proposals"] = "cohort-matching-proposals"
    proposal_only: Literal[True] = True
    creation_authorized: Literal[False] = False
    matcher_policy_version: Literal[1] = 1
    projection_sha256: str = Field(pattern=_HASH)
    review_sha256: str = Field(pattern=_HASH)
    sources: tuple[MatchingSource, ...]
    adjudicator: AdjudicationProvenance | None = None
    prompt_sha256: str = Field(pattern=_HASH)
    mentions: tuple[MentionMatch, ...]

    @model_validator(mode="after")
    def unique_inputs(self):
        sources = {source.artifact_sha1 for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("matching result repeats a source")
        keys = {(m.artifact_sha1, m.local_id) for m in self.mentions}
        if len(keys) != len(self.mentions) or len(
            {m.mention_id for m in self.mentions}
        ) != len(self.mentions):
            raise ValueError("matching result repeats a mention")
        if any(m.artifact_sha1 not in sources for m in self.mentions):
            raise ValueError("matching mention has no source input")
        return self

    @property
    def digest(self) -> str:
        return record_digest(self)


def _review_state(proposal: Proposal, app: Application) -> MatchingReview:
    decision = app.decision
    return MatchingReview(
        proposal_id=proposal.id,
        proposal_revision=proposal.revision,
        operation=proposal.operation,
        status=app.status,
        decision_id=decision.id if decision else None,
        decision_revision=decision.revision if decision else None,
        outcome=decision.outcome if decision else None,
        reasons=app.reasons,
    )


def _review_digest(prepared: PreparedReview) -> str:
    return record_digest(
        {
            "mentions": [
                mention.model_dump(mode="json")
                for mention in sorted(
                    prepared.mentions.values(), key=lambda item: item.id
                )
            ],
            "proposals": [
                proposal.model_dump(mode="json")
                for proposal in sorted(prepared.proposals, key=lambda p: p.id)
            ],
            "projection": prepared.projection.model_dump(mode="json"),
        }
    )


def _verify_sources(sources, prepared):
    if len({s.artifact_sha1 for s in sources}) != len(sources):
        raise MatchingRefused("supply one current product per source")
    observed = {
        (observation.artifact, observation.local_id): observation
        for source in sources
        for observation in observe_mentions(
            source.artifact_sha1, source.batch, source.reading
        )
    }
    if observed.keys() != prepared.mentions.keys() or any(
        prepared.mentions[key].observation != observation
        for key, observation in observed.items()
    ):
        raise MatchingRefused(
            "prepared review does not describe the supplied source products"
        )
    references = [m.reference for m in prepared.mentions.values()]
    if sorted(references, key=lambda r: r["id"]) != sorted(
        prepared.projection.mention_revisions, key=lambda r: r["id"]
    ):
        raise MatchingRefused(
            "review projection has different mention revisions"
        )


def match_cohort(
    projection: GraphProjection,
    sources: tuple[ReviewSource, ...],
    *,
    prepared_review: PreparedReview,
    client: LlmClient | None = None,
    provenance: AdjudicationProvenance | None = None,
) -> CohortMatchResult:
    """Match actual source batches without changing review or graph authority.

    The caller supplies current observations prepared through the existing
    review API. Missing contextual evidence remains visible. Accepted mentions
    keep their target even when its display name no longer passes blocking.
    A client is optional; supplying one requires explicit provider provenance.
    """
    projection = GraphProjection.model_validate(
        projection.model_dump(mode="json")
    )
    if (client is None) != (provenance is None):
        raise MatchingRefused(
            "an adjudication client needs explicit provenance"
        )
    _verify_sources(sources, prepared_review)
    snapshot = projection.assignments
    by_entity = {
        binding.binding.entity_id: binding
        for binding in projection.bindings.values()
    }
    roster = {}
    for entity in projection.roster:
        roster.setdefault(entity.qid, []).append(entity)
    containment = PlaceContainment(
        {qid: set(parents) for qid, parents in projection.containment.items()}
    )
    present = {m.id for m in prepared_review.mentions.values()}
    parents = {mention: mention for mention in present}

    def component(mention):
        while parents[mention] != mention:
            parents[mention] = parents[parents[mention]]
            mention = parents[mention]
        return mention

    for first, second in prepared_review.projection.same:
        if first not in present or second not in present:
            raise MatchingRefused(
                "same decision lacks current mention context"
            )
        parents[component(first)] = component(second)
    groups: dict[str, set[str]] = {}
    for mention in present:
        groups.setdefault(component(mention), set()).add(mention)

    component_targets = {}
    inherited_targets = {}
    for root, members in groups.items():
        accepted_targets = {
            snapshot.assignments[mention].entity_id
            for mention in members
            if mention in snapshot.assignments
        }
        targets = accepted_targets | {
            prepared_review.projection.assignments[mention].entity_id
            for mention in members
            if mention in prepared_review.projection.assignments
        }
        component_targets[root] = targets
        if len(targets) == 1 and accepted_targets:
            inherited_targets[root] = next(iter(accepted_targets))

    def current_target(mention):
        accepted = snapshot.assignments.get(mention)
        proposed = prepared_review.projection.assignments.get(mention)
        if accepted:
            return accepted.entity_id
        if proposed:
            return proposed.entity_id
        return (
            inherited_targets.get(component(mention))
            if mention in present
            else None
        )

    proposals = {p.id: p for p in prepared_review.proposals}
    applications = prepared_review.projection.applications
    if any(app.proposal_id not in proposals for app in applications):
        raise MatchingRefused("review application has no supplied proposal")
    decisions = {
        app.decision.id: app.decision
        for app in applications
        if app.decision is not None and app.status == "applied"
    }
    results = []
    for source in sorted(sources, key=lambda item: item.artifact_sha1):
        entities = {entity.id: entity for entity in source.batch.entities}
        blocked = block_candidates(
            source.batch.entities,
            list(projection.roster),
            batch=source.batch,
            containment=containment,
        )
        source_results = []
        offered = {}
        for key, mention in sorted(prepared_review.mentions.items()):
            if key[0] != source.artifact_sha1:
                continue
            entity = entities.get(key[1])
            accepted = snapshot.assignments.get(mention.id)
            accepted_binding = (
                by_entity.get(accepted.entity_id) if accepted else None
            )
            reviewed = prepared_review.projection.assignments.get(mention.id)
            inherited = by_entity.get(
                inherited_targets.get(component(mention.id))
            )
            issues = list(mention.observation.limitations)
            if entity is None:
                issues.append("entity_manifest_unavailable")
            else:
                issues.extend(structure_problems(entity))
                issues.extend(container_problems(entity))
            if len(component_targets[component(mention.id)]) > 1:
                issues.append("same_component_target_conflict")
            if accepted and accepted_binding is None:
                issues.append("accepted_binding_unavailable")
            if reviewed:
                graph_binding = projection.bindings.get(reviewed.qid)
                if not graph_binding or (
                    graph_binding.binding.model_dump(mode="json")
                    != reviewed.model_dump(mode="json")
                ):
                    issues.append("reviewed_binding_unavailable_or_changed")
                if accepted and accepted.entity_id != reviewed.entity_id:
                    issues.append("reassignment_unsupported")
            review = []
            rejected = set()
            related = groups[component(mention.id)]
            for app in applications:
                proposal = proposals[app.proposal_id]
                if not related.intersection(proposal.mentions):
                    continue
                review.append(_review_state(proposal, app))
                if app.status == "blocked":
                    issues.extend(app.reasons)
                elif accepted and app.status == "needs_review":
                    issues.append("accepted_scope_needs_review")
                if (
                    proposal.operation == "assign"
                    and app.decision is not None
                    and app.decision.outcome == "reject"
                    and proposal.target is not None
                ):
                    rejected.add(proposal.target.entity_id)
                if app.decision is not None and (
                    (
                        proposal.operation == "same"
                        and app.decision.outcome == "reject"
                    )
                    or (
                        proposal.operation == "different"
                        and app.decision.outcome == "approve"
                    )
                ):
                    for other in set(proposal.mentions) - related:
                        target = current_target(other)
                        if target:
                            rejected.add(target)
            for constraint in snapshot.constraints:
                if not related.intersection(constraint.mentions):
                    continue
                decision = decisions.get(constraint.decision_id)
                if (
                    not set(constraint.mentions) <= present
                    or decision is None
                    or decision.revision != constraint.decision_revision
                ):
                    issues.append("prior_negative_context_unavailable")
                if constraint.kind == "rejected_target":
                    rejected.add(constraint.entity_id)
                else:
                    for other in set(constraint.mentions) - related:
                        target = current_target(other)
                        if target:
                            rejected.add(target)
            matches = {item.qid: item for item in blocked.get(key[1], ())}
            retained_qid = (
                accepted_binding.binding.qid if accepted_binding else None
            )
            inherited_qid = inherited.binding.qid if inherited else None
            for target in (
                retained_qid,
                inherited_qid,
                reviewed.qid if reviewed else None,
            ):
                if target and target in roster:
                    rows = roster[target]
                    matching_kind = next(
                        (
                            row
                            for row in rows
                            if entity and row.kind == entity.kind
                        ),
                        rows[0],
                    )
                    matches.setdefault(target, matching_kind)
            if inherited and not accepted and inherited_qid not in roster:
                issues.append("same_component_target_not_in_roster")
            candidates = []
            for qid, candidate in sorted(matches.items()):
                binding = projection.bindings.get(qid)
                blockers = []
                if binding is None:
                    blockers.append("durable_binding_unavailable")
                if binding and binding.binding.entity_id in rejected:
                    blockers.append("retained_negative_decision")
                if accepted and (
                    binding is None
                    or binding.binding.entity_id != accepted.entity_id
                ):
                    blockers.append("reassignment_unsupported")
                if reviewed and (
                    binding is None
                    or binding.binding.entity_id != reviewed.entity_id
                ):
                    blockers.append("reviewed_target_conflict")
                if inherited and (
                    binding is None
                    or binding.binding.entity_id != inherited.binding.entity_id
                ):
                    blockers.append("same_component_target_conflict")
                candidates.append(
                    CandidateMatch(
                        qid=qid,
                        label=candidate.label,
                        kind=candidate.kind,
                        facts=candidate.facts(),
                        binding=binding,
                        entity_revision=projection.entity_revisions.get(qid),
                        origin=(
                            "accepted_assignment"
                            if qid == retained_qid
                            else "accepted_same_component"
                            if qid == inherited_qid
                            else "reviewed_assignment"
                            if reviewed and qid == reviewed.qid
                            else "blocking"
                        ),
                        blockers=tuple(blockers),
                    )
                )
            status = "candidates" if candidates else "no_candidates"
            if issues:
                status = "blocked"
            if accepted and not issues:
                status = "retained"
            source_results.append(
                MentionMatch(
                    artifact_sha1=key[0],
                    local_id=key[1],
                    mention_id=mention.id,
                    mention_revision=mention.revision,
                    label=entity.label if entity else None,
                    status=status,
                    accepted_assignment=accepted,
                    accepted_binding=accepted_binding,
                    same_component_target=inherited if not accepted else None,
                    reviewed_target=reviewed,
                    candidates=tuple(candidates),
                    review=tuple(
                        sorted(review, key=lambda item: item.proposal_id)
                    ),
                    issues=tuple(sorted(set(issues))),
                )
            )
            if (
                client is not None
                and candidates
                and not issues
                and not accepted
                and not reviewed
                and not inherited
            ):
                offered[key[1]] = list(matches.values())
        if offered:
            source_results = _adjudicate_source(
                client, source, offered, source_results
            )
        results.extend(source_results)
    return CohortMatchResult(
        projection_sha256=projection.digest,
        review_sha256=_review_digest(prepared_review),
        sources=tuple(
            MatchingSource(
                artifact_sha1=source.artifact_sha1,
                batch_sha256=record_digest(source.batch),
                reading_sha256=(
                    reading_digest(source.reading) if source.reading else None
                ),
            )
            for source in sorted(sources, key=lambda item: item.artifact_sha1)
        ),
        adjudicator=provenance,
        prompt_sha256=sha256_text(ADJUDICATION_PROMPT),
        mentions=tuple(results),
    )


def _adjudicate_source(client, source, offered, mentions):
    try:
        proposals = adjudicate(
            client, source.batch.entities, offered, source.batch
        )
    except ValueError:
        proposals = []
    counts = Counter((p.entity_id, p.qid) for p in proposals)
    by_pair = {(p.entity_id, p.qid): p for p in proposals}
    results = []
    for mention in mentions:
        if mention.local_id not in offered:
            results.append(mention)
            continue
        candidates = []
        issues = list(mention.issues)
        for candidate in mention.candidates:
            pair = mention.local_id, candidate.qid
            proposal = by_pair.get(pair)
            if counts[pair] != 1:
                issue = (
                    "missing_adjudication_pair"
                    if counts[pair] == 0
                    else "duplicate_adjudication_pair"
                )
                issues.append(issue)
                candidates.append(
                    candidate.model_copy(
                        update={
                            "adjudication": "incomplete",
                            "blockers": (*candidate.blockers, issue),
                        }
                    )
                )
                continue
            try:
                updated = CandidateMatch.model_validate(
                    candidate.model_dump(mode="json")
                    | {
                        "adjudication": "complete",
                        "verdict": proposal.verdict,
                        "confidence": proposal.confidence,
                        "evidence": proposal.evidence,
                    }
                )
            except ValueError:
                issues.append("invalid_adjudication_pair")
                updated = candidate.model_copy(
                    update={
                        "adjudication": "incomplete",
                        "blockers": (
                            *candidate.blockers,
                            "invalid_adjudication_pair",
                        ),
                    }
                )
            candidates.append(updated)
        results.append(
            mention.model_copy(
                update={
                    "candidates": tuple(candidates),
                    "issues": tuple(sorted(set(issues))),
                    "status": "incomplete" if issues else mention.status,
                }
            )
        )
    return results


def save_cohort_match(result: CohortMatchResult, path: Path) -> None:
    """Save an inspectable proposal product, independent of model caches."""
    result = CohortMatchResult.model_validate(result.model_dump(mode="json"))
    atomic_write_text(
        Path(path),
        json.dumps(
            {
                "sha256": result.digest,
                "result": result.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
    )


def load_cohort_match(path: Path) -> CohortMatchResult:
    """Validate a saved result without a graph, review store or provider."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"sha256", "result"}:
        raise MatchingRefused("invalid matching product envelope")
    result = CohortMatchResult.model_validate(data["result"])
    if result.digest != data["sha256"]:
        raise MatchingRefused(
            "matching product digest differs from its contents"
        )
    return result
