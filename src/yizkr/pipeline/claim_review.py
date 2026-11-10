"""Scoped historical approval over current source evidence and assignments.

This operation uses the identity review database and the existing server
assignment authority. It neither substitutes QIDs into source products nor
publishes graph effects. Local claim indices select inputs only; durable
approval binds the complete canonical meaning and located support.
"""

from __future__ import annotations

import json
import unicodedata
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.claims import (
    ArtifactRef,
    Claim,
    ItemValue,
    LocalRef,
    QidRef,
    SourceReference,
    VocabRef,
    validate_against_manifest,
    widowhood_review_notes,
)
from yizkr.core.identity_assignments import (
    AssignmentRevision,
    RegistryBinding,
    record_digest,
)
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline.assignment_stages import assignment_review_scope
from yizkr.pipeline.identity_review import (
    ClaimDecision,
    ClaimDecisionInput,
    ClaimProposal,
    ReviewRefused,
    ReviewStore,
)
from yizkr.pipeline.review_mentions import source_scope_data
from yizkr.pipeline.review_stages import (
    PreparedReview,
    ReviewRequest,
    prepare_review,
)
from yizkr.pipeline.source_bindings import unique_source_qids
from yizkr.pipeline.source_context import placed_owners
from yizkr.pipeline.transcribe import Transcription, reading_digest

COMPARISON_VERSION = 1


class ClaimSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    index: int = Field(ge=0)
    warnings: tuple[str, ...] = ()


class ClaimRole(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: str
    mention_id: str
    assignment: AssignmentRevision
    binding: RegistryBinding

    @model_validator(mode="after")
    def consistent_assignment(self):
        if (
            self.mention_id != self.assignment.mention_id
            or self.assignment.entity_id != self.binding.binding.entity_id
            or self.assignment.binding_revision
            != self.binding.binding_revision
        ):
            raise ValueError("claim role differs from its assignment binding")
        payload = self.assignment.model_dump(
            mode="json", exclude={"revision_id"}
        )
        if record_digest(payload) != self.assignment.revision_id:
            raise ValueError(
                "claim role assignment differs from its revision digest"
            )
        return self


@dataclass(frozen=True)
class PreparedClaim:
    selection: ClaimSelection
    claim: Claim
    roles: tuple[ClaimRole, ...]
    basis: dict[str, Any]
    provenance: dict[str, Any]
    proposal: ClaimProposal
    decision: ClaimDecision | None


@dataclass(frozen=True)
class PreparedClaimReview:
    request: ReviewRequest
    selections: tuple[ClaimSelection, ...]
    identity: PreparedReview
    claims: tuple[PreparedClaim, ...]
    artifact_qids: dict[str, str]
    assignment_head: str | None


def _canonical(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _json(value: Any) -> str:
    return json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _support_material(value: dict[str, Any]) -> dict[str, Any]:
    value = {
        key: item for key, item in value.items() if key != "reading_sha256"
    }
    if "placements" in value:
        value["placements"] = sorted(value["placements"], key=_json)
    return _canonical(value)


def _located_source(
    source: SourceReference, reading: Transcription
) -> dict[str, Any]:
    if reading.artifact != source.artifact:
        raise ReviewRefused("claim support belongs to another source reading")
    excerpt = source.transcription_excerpt
    quote = excerpt.text if excerpt else ""
    if not quote.strip():
        raise ReviewRefused(
            "claim approval needs a located literal source excerpt"
        )
    # Exact offsets are provenance only. Repeated wording cannot silently pick
    # one occurrence, even if both occurrences lie on the same reading line.
    ranges = []
    start = reading.text.find(quote)
    while start >= 0:
        ranges.append((start, start + len(quote)))
        start = reading.text.find(quote, start + 1)
    if source.page_number is not None:
        page_number = source.page_number.strip()
        pages = [p for p in reading.pages or () if str(p.page) == page_number]
        if len(pages) != 1:
            raise ReviewRefused(
                "claim support names no unique physical reading page"
            )
        page = pages[0]
        ranges = [
            (a, b) for a, b in ranges if page.start <= a and b <= page.end
        ]
    if len(ranges) != 1:
        raise ReviewRefused("claim source excerpt is absent or ambiguous")
    first, last = ranges[0]
    page = None
    if reading.pages:
        pages = [
            p
            for p in reading.pages
            if p.start <= first and last <= p.end and p.status == "transcribed"
        ]
        if len(pages) != 1:
            raise ReviewRefused(
                "claim source excerpt has no unique readable page"
            )
        page = pages[0]
        if (
            source.printed_page_number is not None
            and source.printed_page_number != page.printed_page
        ):
            raise ReviewRefused(
                "claim printed page differs from current reading"
            )
    if reading.source_context is not None:
        overlapping = [
            s
            for s in reading.source_context.readings
            if s.start < last and first < s.end
        ]
        if not overlapping or any(
            s.state != "attributed" or s.owner != source.artifact
            for s in overlapping
        ):
            raise ReviewRefused(
                "claim support has unsupported or different ownership"
            )
        matching = [
            s
            for s in overlapping
            if s.start <= first
            and last <= s.end
            and s.support == source.support
        ]
        if (
            not matching
            or source.support is None
            or any(s.support != source.support for s in overlapping)
        ):
            raise ReviewRefused(
                "claim support differs from current source context"
            )
        digest = reading_digest(reading)
        if any(
            s.end > len(reading.text)
            or (
                s.support.reading_sha256 is not None
                and s.support.reading_sha256 != digest
            )
            for s in overlapping
        ):
            raise ReviewRefused("claim support names a stale reading")
    elif source.support is not None:
        raise ReviewRefused(
            "claim structural support has no current reading context"
        )
    material = source.model_dump(mode="json")
    if source.support:
        material["support"] = _support_material(material["support"])
    return {
        "material": _canonical(material),
        "physical_page": page.page if page else None,
        "reading_sha256": reading_digest(reading),
        "start": first,
        "end": last,
    }


def _additive_identity_basis(previous: dict, current: dict) -> bool:
    """Retain identity review when semantic evidence is only added."""
    old = json.loads(_json(previous))
    new = json.loads(_json(current))
    try:
        old_mentions, new_mentions = old["mentions"], new["mentions"]
        if old_mentions.keys() != new_mentions.keys():
            return False
        for key in old_mentions:
            old_claims = old_mentions[key]["interpretation"]["claims"]
            new_claims = new_mentions[key]["interpretation"]["claims"]
            if Counter(map(_json, old_claims)) - Counter(
                map(_json, new_claims)
            ):
                return False
            old_mentions[key]["interpretation"]["claims"] = []
            new_mentions[key]["interpretation"]["claims"] = []
    except (KeyError, TypeError):
        return False
    return old == new


def _current_assignment_consistency(
    applications, snapshot, *, creation=None
) -> None:
    """Refuse current review that changes an accepted identity component."""
    for application in applications:
        decision = application.decision
        if application.status != "applied" or decision is None:
            continue
        proposal = decision.proposal
        approved = decision.outcome == "approve"
        assigned = [snapshot.assignments.get(key) for key in proposal.mentions]
        if proposal.operation in ("same", "different"):
            first, second = assigned
            if first is None or second is None:
                continue
            same = first.entity_id == second.entity_id
            opposite = (proposal.operation == "same" and not approved) or (
                proposal.operation == "different" and approved
            )
            if (opposite and same) or (
                proposal.operation == "same" and approved and not same
            ):
                raise ReviewRefused(
                    "current identity review conflicts with accepted "
                    "assignments; merge or split is unsupported"
                )
        elif proposal.operation == "assign" and assigned[0] is not None:
            same = assigned[0].entity_id == proposal.target.entity_id
            if same != approved:
                raise ReviewRefused(
                    "current target review conflicts with an accepted "
                    "assignment; reassignment is unsupported"
                )
        elif (
            proposal.operation == "new_entity"
            and approved
            and assigned[0] is not None
        ):
            assignment = assigned[0]
            original = any(
                reference.id == decision.id
                and reference.revision == decision.revision
                and reference.proposal_id == proposal.id
                and reference.proposal_revision == proposal.revision
                and reference.basis_digest == proposal.digest
                for reference in assignment.decision_references
            )
            if (
                not original
                or creation is None
                or not creation(decision, assignment.entity_id)
            ):
                raise ReviewRefused(
                    "current creation review would replace an accepted "
                    "assignment"
                )


def prepare_claim_review(
    store: ReviewStore,
    request: ReviewRequest,
    registry: CreationRegistry,
    *,
    selections: tuple[ClaimSelection, ...],
) -> PreparedClaimReview:
    """Prepare explicit claims; identity consent is not claim approval."""
    if not selections or len(
        {(s.artifact_sha1, s.index) for s in selections}
    ) != len(selections):
        raise ReviewRefused("claim review needs a nonempty, unique selection")
    sources = {source.artifact_sha1: source for source in request.sources}
    concerns = {
        sha1: widowhood_review_notes(source.batch)
        for sha1, source in sources.items()
    }
    selected = []
    wanted = set()
    for selection in selections:
        source = sources.get(selection.artifact_sha1)
        if source is None or selection.index >= len(source.batch.claims):
            raise ReviewRefused(
                "claim selection is absent from current products"
            )
        claim = source.batch.claims[selection.index]
        concern = concerns[selection.artifact_sha1].get(selection.index)
        if concern and concern not in selection.warnings:
            selection = selection.model_copy(
                update={"warnings": (*selection.warnings, concern)}
            )
        if (
            claim.source.artifact.sha1 != source.artifact_sha1
            or source.reading is None
        ):
            raise ReviewRefused(
                "selected claim lacks its current owner reading"
            )
        if claim.confirmed_from is not None or any(
            e.qid for e in source.batch.entities
        ):
            raise ReviewRefused(
                "claim review requires original unresolved local references"
            )
        placed = {source.artifact_sha1: placed_owners(source.reading)}
        problems = validate_against_manifest(
            source.batch.model_copy(update={"claims": [claim]}), placed=placed
        )
        if problems:
            raise ReviewRefused(
                "selected claim fails current validation: "
                + "; ".join(problems)
            )
        wanted.add(source.artifact_sha1)
        if isinstance(claim.subject, ArtifactRef):
            wanted.add(claim.subject.sha1)
        selected.append((selection, source, claim))
    identity = prepare_review(store, request)
    snapshot = registry.assignment_snapshot()
    if snapshot.generation is None:
        raise ReviewRefused(
            "claim approval requires an established deployment generation"
        )
    properties, _ = registry.client.vocabulary()
    artifact_qids = unique_source_qids(
        registry.client, properties["media_sha1"], wanted
    )
    artifact_bindings = {}
    for sha1, qid in artifact_qids.items():
        binding = registry.lookup({"kind": "source", "sha1": sha1})
        if binding is None:
            raise ReviewRefused(
                "registered source has no verified creation binding"
            )
        if binding.qid != qid:
            raise ReviewRefused(
                "registered source differs from its creation binding"
            )
        artifact_bindings[sha1] = registry.verify_binding(binding).model_dump(
            mode="json"
        )
    pending = []
    for selection, source, claim in selected:
        roles = []
        current_mentions = {}

        def reference(
            ref,
            role,
            *,
            source=source,
            roles=roles,
            current_mentions=current_mentions,
        ):
            if isinstance(ref, QidRef):
                raise ReviewRefused(
                    "bare QID cannot establish a historical assignment role"
                )
            if isinstance(ref, VocabRef):
                return {"kind": "vocab", "key": ref.key}
            if isinstance(ref, ArtifactRef):
                return {
                    "kind": "artifact",
                    "sha1": ref.sha1,
                    "qid": artifact_qids[ref.sha1],
                }
            if not isinstance(ref, LocalRef):
                raise ReviewRefused("unsupported claim reference")
            mention = identity.mentions.get(
                (source.artifact_sha1, ref.local_id)
            )
            if (
                mention is None
                or mention.observation.anchor is None
                or mention.observation.limitations
            ):
                raise ReviewRefused(
                    "claim role lacks a unique current source occurrence"
                )
            assignment = snapshot.assignments.get(mention.id)
            if assignment is None:
                raise ReviewRefused("claim role has no accepted assignment")
            binding = registry.binding(assignment.entity_id)
            if (
                binding is None
                or binding.binding_revision != assignment.binding_revision
            ):
                raise ReviewRefused(
                    "claim role has no current verified binding"
                )
            roles.append(
                ClaimRole(
                    role=role,
                    mention_id=mention.id,
                    assignment=assignment,
                    binding=binding,
                )
            )
            current_mentions[mention.id] = mention
            return {
                "kind": "mention",
                "id": mention.id,
                "assignment_revision": assignment.revision_id,
                "entity_id": assignment.entity_id,
                "binding_revision": binding.binding_revision,
                "qid": binding.binding.qid,
            }

        def value(raw, role, *, reference=reference):
            if isinstance(raw, ItemValue):
                return {"type": "item", "value": reference(raw.value, role)}
            return _canonical(raw.model_dump(mode="json"))

        subject = reference(claim.subject, "subject")
        object_value = value(claim.value, "object")
        # Qualifier roles use canonical semantic content, never input ordering.
        qualifiers = []
        for qualifier in claim.qualifiers:
            before = len(roles)
            v = value(qualifier.value, "qualifier")
            semantic = {"property": qualifier.property, "value": v}
            role = (
                "qualifier:"
                + qualifier.property
                + ":"
                + record_digest(semantic)
            )
            for n in range(before, len(roles)):
                roles[n] = roles[n].model_copy(update={"role": role})
            qualifiers.append(semantic)
        relevant = (
            assignment_review_scope(identity, snapshot, set(current_mentions))
            if current_mentions
            else frozenset()
        )
        by_id = {p.id: p for p in identity.proposals}
        applications = [
            a
            for a in identity.projection.applications
            if relevant.intersection(by_id[a.proposal_id].mentions)
        ]
        for application in applications:
            if application.status == "applied":
                continue
            previous = application.decision
            current = by_id[application.proposal_id]
            if not (
                application.status == "needs_review"
                and previous is not None
                and previous.outcome == "approve"
                and _additive_identity_basis(
                    previous.proposal.basis, current.basis
                )
            ):
                raise ReviewRefused(
                    "claim roles require current unblocked identity review"
                )
        _current_assignment_consistency(
            applications, snapshot, creation=registry.verify_reviewed_creation
        )
        for mention_id in current_mentions:
            supplied = identity.projection.assignments.get(mention_id)
            assigned = snapshot.assignments[mention_id]
            if supplied is not None:
                binding = next(
                    role.binding.binding
                    for role in roles
                    if role.mention_id == mention_id
                )
                if supplied.model_dump(mode="json") != binding.model_dump(
                    mode="json"
                ):
                    raise ReviewRefused(
                        "current identity review differs from accepted "
                        "assignment binding"
                    )
                # An explicit fresh approval can confirm the same target after
                # material review. The accepted assignment keeps its original
                # revision and origin; it is not silently rewritten here.
                continue
            # Carry-forward without a freshly applicable positive projection
            # is narrower: original accepted confirmations must remain current.
            for reference in assigned.decision_references:
                decision = store.get_decision(reference.proposal_id)
                if decision is None or (
                    decision.id != reference.id
                    or decision.revision != reference.revision
                    or decision.outcome != "approve"
                    or decision.proposal.digest != reference.basis_digest
                ):
                    raise ReviewRefused(
                        "accepted assignment decision was revoked or replaced"
                    )
        located = _located_source(claim.source, source.reading)
        circumstance = (
            _located_source(claim.circumstance_source, source.reading)
            if claim.circumstance_source
            else None
        )
        canonical_claim = {
            "subject": subject,
            "property": claim.property,
            "value": object_value,
            "qualifiers": sorted(qualifiers, key=_json),
            "source": located["material"],
            "physical_page": located["physical_page"],
            "circumstance_source": circumstance["material"]
            if circumstance
            else None,
            "circumstance_physical_page": circumstance["physical_page"]
            if circumstance
            else None,
            "note": claim.note.model_dump(mode="json") if claim.note else None,
            "curated": claim.curated,
        }
        basis = _canonical(
            {
                "comparison_version": COMPARISON_VERSION,
                "deployment_id": snapshot.generation.deployment_id,
                "generation": snapshot.generation.generation,
                "claim": canonical_claim,
                "source_bindings": {
                    sha1: artifact_bindings[sha1]
                    for sha1 in sorted(
                        {source.artifact_sha1}
                        | (
                            {claim.subject.sha1}
                            if isinstance(claim.subject, ArtifactRef)
                            else set()
                        )
                    )
                },
                "roles": sorted(
                    [
                        {
                            "role": r.role,
                            "mention_id": r.mention_id,
                            "assignment_revision": r.assignment.revision_id,
                            "binding": r.binding.model_dump(mode="json"),
                        }
                        for r in roles
                    ],
                    key=_json,
                ),
                "occurrences": {
                    key: {
                        "scope": source_scope_data(m.observation.anchor),
                        "quote": m.observation.anchor.quote,
                        "correspondence": (
                            m.observation.anchor.correspondence_key
                        ),
                    }
                    for key, m in current_mentions.items()
                },
                "warnings": sorted(set(selection.warnings)),
            }
        )
        provenance = {
            "source": located,
            "circumstance_source": circumstance,
            "mentions": {
                key: m.model_dump(mode="json")
                for key, m in current_mentions.items()
            },
            "assignments": [
                r.assignment.model_dump(mode="json") for r in roles
            ],
            "artifact_bindings": artifact_bindings,
            "batch_sha256": record_digest(
                source.batch.model_dump(mode="json")
            ),
        }
        pending.append(
            (
                selection,
                claim,
                tuple(sorted(roles, key=lambda r: r.role)),
                basis,
                provenance,
            )
        )
    # No proposal mutation until every selected claim has passed admission.
    prepared = []
    for selection, claim, roles, basis, provenance in pending:
        proposal = store.propose_claim(
            basis, origin={"source_artifact": selection.artifact_sha1}
        )
        prepared.append(
            PreparedClaim(
                selection,
                claim.model_copy(deep=True),
                roles,
                basis,
                provenance,
                proposal,
                store.get_claim_decision(proposal.id),
            )
        )
    return PreparedClaimReview(
        request,
        selections,
        identity,
        tuple(prepared),
        artifact_qids,
        snapshot.head,
    )


def record_claim_decisions(
    store: ReviewStore,
    prepared: PreparedClaimReview,
    inputs: Sequence[ClaimDecisionInput],
) -> tuple[ClaimDecision, ...]:
    proposed = {claim.proposal.id: claim.proposal for claim in prepared.claims}
    if any(
        i.proposal_id not in proposed
        or i.expected_digest != proposed[i.proposal_id].digest
        for i in inputs
    ):
        raise ReviewRefused(
            "claim decision is outside its prepared review scope"
        )
    return store.decide_claims(
        inputs,
        projection=prepared.identity.projection,
        mentions=list(prepared.identity.mentions.values()),
    )


@contextmanager
def pin_claim_review(
    store: ReviewStore, prepared: PreparedClaimReview
) -> Iterator[None]:
    """Pin freshly prepared identity and claim decisions through all effects.

    The caller refreshes preparation from original products and checks server
    preconditions before application. A saved PreparedClaimReview is not a
    replacement for that current source/assignment admission.
    """
    with store.pin_application(
        prepared.identity.projection,
        list(prepared.identity.mentions.values()),
        [(claim.proposal, claim.decision) for claim in prepared.claims],
    ):
        yield
