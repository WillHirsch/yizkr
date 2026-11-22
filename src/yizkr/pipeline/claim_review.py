"""Scoped historical approval over current source evidence and assignments.

This operation uses the identity review database and the existing server
assignment authority. It neither substitutes QIDs into source products nor
publishes graph effects. Local claim indices select inputs only; durable
approval binds the complete canonical meaning and located support. A caller
prepares explicit selections, all or nothing, or every claim of named sources,
each of which is then prepared or refused with its reason code.
"""

from __future__ import annotations

import json
import unicodedata
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
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
    AssignmentSnapshot,
    RegistryBinding,
    record_digest,
)
from yizkr.core.write_recovery import CreationRefused, CreationRegistry
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
    ReviewSource,
    prepare_review,
)
from yizkr.pipeline.source_bindings import source_owners
from yizkr.pipeline.source_context import placed_owners
from yizkr.pipeline.transcribe import Transcription, reading_digest

COMPARISON_VERSION = 1

# The codes of the source-location check (_located_source), one for each way
# a claim's support can fail to lie uniquely, readably and under its own owner
# in the current reading.
LOCATION_REFUSALS = (
    "other_reading",
    "no_excerpt",
    "no_unique_page",
    "excerpt_absent",
    "excerpt_repeated",
    "no_readable_page",
    "printed_page_differs",
    "owner_differs",
    "part_differs",
    "stale_reading",
    "no_reading_context",
)
# Every reason a claim cannot be prepared, with its category: support (the
# claim's reading, its located excerpt and the registered source it cites),
# roles (the references the claim makes), assignments (each role's accepted
# assignment and verified binding), identity_review (the current review those
# assignments rest on) and admission (whether the claim is one this review
# takes). The checks below raise them as ClaimRefused.
REFUSAL_CATEGORIES = {
    "no_reading": "support",
    "other_artifact": "support",
    **dict.fromkeys(LOCATION_REFUSALS, "support"),
    "source_not_unique": "support",
    "source_unbound": "support",
    "bare_qid": "roles",
    "unsupported_reference": "roles",
    "mention_not_located": "roles",
    "artifact_subject_not_unique": "roles",
    "artifact_subject_unbound": "roles",
    "no_assignment": "assignments",
    "binding_not_current": "assignments",
    "binding_unverified": "assignments",
    "identity_not_current": "identity_review",
    "identity_conflicts": "identity_review",
    "decision_revoked": "identity_review",
    "validation": "admission",
    "resolved_claim": "admission",
    "resolved_entities": "admission",
}


class ClaimRefused(ReviewRefused):
    """A claim, or each claim of one source, that cannot be prepared."""

    def __init__(self, code: str, message: str) -> None:
        if code not in REFUSAL_CATEGORIES:
            raise ValueError(f"unknown claim refusal code {code!r}")
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ClaimRefusal:
    """A claim of a named source that preparation refused, and why."""

    artifact_sha1: str
    index: int
    code: str
    message: str

    @property
    def category(self) -> str:
        return REFUSAL_CATEGORIES[self.code]


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
    refusals: tuple[ClaimRefusal, ...] = ()


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
    """Locate one support in the reading, or refuse with a location code."""
    if reading.artifact != source.artifact:
        raise ClaimRefused(
            "other_reading", "claim support belongs to another source reading"
        )
    excerpt = source.transcription_excerpt
    quote = excerpt.text if excerpt else ""
    if not quote.strip():
        raise ClaimRefused(
            "no_excerpt",
            "claim approval needs a located literal source excerpt",
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
            raise ClaimRefused(
                "no_unique_page",
                "claim support names no unique physical reading page",
            )
        page = pages[0]
        ranges = [
            (a, b) for a, b in ranges if page.start <= a and b <= page.end
        ]
    if not ranges:
        raise ClaimRefused("excerpt_absent", "claim source excerpt is absent")
    if len(ranges) > 1:
        raise ClaimRefused(
            "excerpt_repeated", "claim source excerpt is ambiguous"
        )
    first, last = ranges[0]
    page = None
    if reading.pages:
        pages = [
            p
            for p in reading.pages
            if p.start <= first and last <= p.end and p.status == "transcribed"
        ]
        if len(pages) != 1:
            raise ClaimRefused(
                "no_readable_page",
                "claim source excerpt has no unique readable page",
            )
        page = pages[0]
        if (
            source.printed_page_number is not None
            and source.printed_page_number != page.printed_page
        ):
            raise ClaimRefused(
                "printed_page_differs",
                "claim printed page differs from current reading",
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
            raise ClaimRefused(
                "owner_differs",
                "claim support has unsupported or different ownership",
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
            raise ClaimRefused(
                "part_differs",
                "claim support differs from current source context",
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
            raise ClaimRefused(
                "stale_reading", "claim support names a stale reading"
            )
    elif source.support is not None:
        raise ClaimRefused(
            "no_reading_context",
            "claim structural support has no current reading context",
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
                raise ClaimRefused(
                    "identity_conflicts",
                    "current identity review conflicts with accepted "
                    "assignments; merge or split is unsupported",
                )
        elif proposal.operation == "assign" and assigned[0] is not None:
            same = assigned[0].entity_id == proposal.target.entity_id
            if same != approved:
                raise ClaimRefused(
                    "identity_conflicts",
                    "current target review conflicts with an accepted "
                    "assignment; reassignment is unsupported",
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
                raise ClaimRefused(
                    "identity_conflicts",
                    "current creation review would replace an accepted "
                    "assignment",
                )


def _with_concern(
    selection: ClaimSelection, concern: str | None
) -> ClaimSelection:
    """Add the claim's widowhood review concern to the caller's warnings."""
    if concern and concern not in selection.warnings:
        return selection.model_copy(
            update={"warnings": (*selection.warnings, concern)}
        )
    return selection


def _cited(source: ReviewSource, claim: Claim) -> set[str]:
    """The artifacts whose registration the claim's review rests on."""
    cited = {source.artifact_sha1}
    if isinstance(claim.subject, ArtifactRef):
        cited.add(claim.subject.sha1)
    return cited


def _admit_product(source: ReviewSource, claim: Claim) -> None:
    """Refuse a claim its current products cannot carry, before any effect.

    The checks of the source come first, so they refuse each of its claims
    alike.
    """
    if source.reading is None:
        raise ClaimRefused("no_reading", "claim source has no current reading")
    if any(e.qid for e in source.batch.entities):
        raise ClaimRefused(
            "resolved_entities",
            "claim review requires original unresolved local references; "
            "the source's entity records already carry QIDs",
        )
    if claim.source.artifact.sha1 != source.artifact_sha1:
        raise ClaimRefused(
            "other_artifact", "claim support cites another artifact"
        )
    if claim.confirmed_from is not None:
        raise ClaimRefused(
            "resolved_claim",
            "claim review requires original unresolved local references; "
            "the claim is already resolved to a QID subject",
        )
    placed = {source.artifact_sha1: placed_owners(source.reading)}
    problems = validate_against_manifest(
        source.batch.model_copy(update={"claims": [claim]}), placed=placed
    )
    if problems:
        raise ClaimRefused(
            "validation",
            "selected claim fails current validation: " + "; ".join(problems),
        )


@dataclass(frozen=True)
class _Authority:
    """Identity review and registry state read once for every claim."""

    identity: PreparedReview
    snapshot: AssignmentSnapshot
    artifact_qids: dict[str, str]
    artifact_bindings: dict[str, dict[str, Any]]
    unbound: dict[str, str]


def _authority(
    store: ReviewStore,
    request: ReviewRequest,
    registry: CreationRegistry,
    cited: set[str],
) -> _Authority:
    """Read current authority, refusing the call only for the request.

    An artifact that is not uniquely registered, or whose creation binding
    the registry cannot verify, is recorded against that artifact, so that it
    refuses only the claims citing it.
    """
    identity = prepare_review(store, request)
    snapshot = registry.assignment_snapshot()
    if snapshot.generation is None:
        raise ReviewRefused(
            "claim approval requires an established deployment generation"
        )
    properties, _ = registry.client.vocabulary()
    owners = source_owners(registry.client, properties["media_sha1"], cited)
    qids = {sha1: qid for sha1, qid in owners.items() if qid is not None}
    bindings, unbound = {}, {}
    for sha1, qid in qids.items():
        try:
            binding = registry.lookup({"kind": "source", "sha1": sha1})
            if binding is None:
                unbound[sha1] = (
                    "registered source has no verified creation binding"
                )
            elif binding.qid != qid:
                unbound[sha1] = (
                    "registered source differs from its creation binding"
                )
            else:
                bindings[sha1] = registry.verify_binding(binding).model_dump(
                    mode="json"
                )
        except CreationRefused as exc:
            # A changed deployment generation refuses the call itself.
            registry.check_generation()
            unbound[sha1] = str(exc)
    return _Authority(identity, snapshot, qids, bindings, unbound)


def _registered(
    authority: _Authority, sha1: str, not_unique: str, unbound: str
) -> None:
    if sha1 not in authority.artifact_qids:
        raise ClaimRefused(
            not_unique,
            "claim artifact is unregistered or registered more than once",
        )
    if sha1 in authority.unbound:
        raise ClaimRefused(unbound, authority.unbound[sha1])


def _admit_claim(
    store: ReviewStore,
    registry: CreationRegistry,
    authority: _Authority,
    selection: ClaimSelection,
    source: ReviewSource,
    claim: Claim,
) -> tuple:
    """Check one claim against current authority; nothing is stored here."""
    _registered(
        authority, source.artifact_sha1, "source_not_unique", "source_unbound"
    )
    if isinstance(claim.subject, ArtifactRef):
        _registered(
            authority,
            claim.subject.sha1,
            "artifact_subject_not_unique",
            "artifact_subject_unbound",
        )
    identity, snapshot = authority.identity, authority.snapshot
    roles = []
    current_mentions = {}

    def reference(ref, role):
        if isinstance(ref, QidRef):
            raise ClaimRefused(
                "bare_qid",
                "bare QID cannot establish a historical assignment role",
            )
        if isinstance(ref, VocabRef):
            return {"kind": "vocab", "key": ref.key}
        if isinstance(ref, ArtifactRef):
            return {
                "kind": "artifact",
                "sha1": ref.sha1,
                "qid": authority.artifact_qids[ref.sha1],
            }
        if not isinstance(ref, LocalRef):
            raise ClaimRefused(
                "unsupported_reference", "unsupported claim reference"
            )
        mention = identity.mentions.get((source.artifact_sha1, ref.local_id))
        if (
            mention is None
            or mention.observation.anchor is None
            or mention.observation.limitations
        ):
            raise ClaimRefused(
                "mention_not_located",
                "claim role lacks a unique current source occurrence",
            )
        assignment = snapshot.assignments.get(mention.id)
        if assignment is None:
            raise ClaimRefused(
                "no_assignment", "claim role has no accepted assignment"
            )
        binding = registry.binding(assignment.entity_id)
        if (
            binding is None
            or binding.binding_revision != assignment.binding_revision
        ):
            raise ClaimRefused(
                "binding_not_current",
                "claim role has no current verified binding",
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

    def value(raw, role):
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
            "qualifier:" + qualifier.property + ":" + record_digest(semantic)
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
            raise ClaimRefused(
                "identity_not_current",
                "claim roles require current unblocked identity review",
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
                raise ClaimRefused(
                    "identity_conflicts",
                    "current identity review differs from accepted "
                    "assignment binding",
                )
            # An explicit fresh approval can confirm the same target after
            # material review. The accepted assignment keeps its original
            # revision and origin; it is not silently rewritten here.
            continue
        # Carry-forward without a freshly applicable positive projection
        # is narrower: original accepted confirmations must remain current.
        for confirmed in assigned.decision_references:
            decision = store.get_decision(confirmed.proposal_id)
            if decision is None or (
                decision.id != confirmed.id
                or decision.revision != confirmed.revision
                or decision.outcome != "approve"
                or decision.proposal.digest != confirmed.basis_digest
            ):
                raise ClaimRefused(
                    "decision_revoked",
                    "accepted assignment decision was revoked or replaced",
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
                sha1: authority.artifact_bindings[sha1]
                for sha1 in sorted(_cited(source, claim))
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
        "assignments": [r.assignment.model_dump(mode="json") for r in roles],
        "artifact_bindings": authority.artifact_bindings,
        "batch_sha256": record_digest(source.batch.model_dump(mode="json")),
    }
    return (
        selection,
        claim,
        tuple(sorted(roles, key=lambda r: r.role)),
        basis,
        provenance,
    )


def _admitted(store, registry, authority, selection, source, claim):
    try:
        return _admit_claim(
            store, registry, authority, selection, source, claim
        )
    except CreationRefused as exc:
        # A changed deployment generation refuses the call itself.
        registry.check_generation()
        raise ClaimRefused("binding_unverified", str(exc)) from exc


def _proposed(
    store: ReviewStore,
    request: ReviewRequest,
    authority: _Authority,
    pending: Sequence[tuple],
    selections: tuple[ClaimSelection, ...],
    refusals: tuple[ClaimRefusal, ...],
) -> PreparedClaimReview:
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
    cited = {
        sha1 for claim in prepared for sha1 in claim.basis["source_bindings"]
    }
    return PreparedClaimReview(
        request,
        selections,
        authority.identity,
        tuple(prepared),
        {sha1: authority.artifact_qids[sha1] for sha1 in sorted(cited)},
        authority.snapshot.head,
        refusals,
    )


def prepare_claim_review(
    store: ReviewStore,
    request: ReviewRequest,
    registry: CreationRegistry,
    *,
    selections: tuple[ClaimSelection, ...],
) -> PreparedClaimReview:
    """Prepare explicit claims; identity consent is not claim approval.

    The first selected claim that cannot be prepared refuses the whole call
    with its ClaimRefused, and no claim proposal is stored.
    """
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
    for selection in selections:
        source = sources.get(selection.artifact_sha1)
        if source is None or selection.index >= len(source.batch.claims):
            raise ReviewRefused(
                "claim selection is absent from current products"
            )
        claim = source.batch.claims[selection.index]
        selection = _with_concern(
            selection, concerns[selection.artifact_sha1].get(selection.index)
        )
        _admit_product(source, claim)
        selected.append((selection, source, claim))
    authority = _authority(
        store,
        request,
        registry,
        set().union(*(_cited(source, claim) for _, source, claim in selected)),
    )
    pending = [
        _admitted(store, registry, authority, *item) for item in selected
    ]
    # No proposal mutation until every selected claim has passed admission.
    return _proposed(store, request, authority, pending, selections, ())


def prepare_source_claims(
    store: ReviewStore,
    request: ReviewRequest,
    registry: CreationRegistry,
    *,
    sources: Sequence[str],
    warnings: Mapping[tuple[str, int], Sequence[str]] | None = None,
) -> PreparedClaimReview:
    """Prepare every claim of the named sources, refusing claims one by one.

    `sources` names sources of the request by artifact SHA-1; the request's
    other sources are identity context only. `warnings` gives the caller's
    warnings for a claim, keyed by its source's SHA-1 and its index. Each
    claim is prepared or listed in `refusals` with its reason code, and a
    refused claim stores no proposal and stops no other claim. A problem of
    one source refuses each of its claims and no other source's. A request
    that prepare_review refuses, or no established deployment generation,
    refuses the whole call. The result's selections are its prepared claims',
    and an explicit preparation of one gives the same proposal.
    """
    named = tuple(sources)
    products = {source.artifact_sha1: source for source in request.sources}
    if (
        not named
        or len(set(named)) != len(named)
        or any(sha1 not in products for sha1 in named)
    ):
        raise ReviewRefused(
            "claim review needs nonempty, unique sources of its request"
        )
    warnings = dict(warnings or {})
    if any(
        sha1 not in named or not 0 <= index < len(products[sha1].batch.claims)
        for sha1, index in warnings
    ):
        raise ReviewRefused(
            "claim warnings name a claim absent from the named sources"
        )
    refusals, selected = [], []
    for sha1 in named:
        source = products[sha1]
        concerns = widowhood_review_notes(source.batch)
        for index, claim in enumerate(source.batch.claims):
            selection = _with_concern(
                ClaimSelection(
                    artifact_sha1=sha1,
                    index=index,
                    warnings=tuple(warnings.get((sha1, index), ())),
                ),
                concerns.get(index),
            )
            try:
                _admit_product(source, claim)
            except ClaimRefused as refused:
                refusals.append(
                    ClaimRefusal(sha1, index, refused.code, str(refused))
                )
            else:
                selected.append((selection, source, claim))
    authority = _authority(
        store,
        request,
        registry,
        set().union(*(_cited(source, claim) for _, source, claim in selected)),
    )
    pending = []
    for selection, source, claim in selected:
        try:
            pending.append(
                _admitted(store, registry, authority, selection, source, claim)
            )
        except ClaimRefused as refused:
            refusals.append(
                ClaimRefusal(
                    selection.artifact_sha1,
                    selection.index,
                    refused.code,
                    str(refused),
                )
            )
    order = {sha1: n for n, sha1 in enumerate(named)}
    refusals.sort(key=lambda r: (order[r.artifact_sha1], r.index))
    return _proposed(
        store,
        request,
        authority,
        pending,
        tuple(item[0] for item in pending),
        tuple(refusals),
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
