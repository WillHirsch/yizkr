"""Read-only impact of moving accepted mentions to an existing entity.

The inventory is the current generation's reviewed application catalog, not
the whole graph. A report is evidence for further review, never a write plan
or an approval. In particular, absence from this catalog cannot establish
that a legacy or manually authored statement is unaffected.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.core.claim_applications import (
    ApplicationPlan,
    PlannedClaim,
    SupportIntent,
    SupportReceipt,
)
from yizkr.core.identity_assignments import (
    AssignmentRefused,
    AssignmentRevision,
    AssignmentTarget,
    RegistryBinding,
    record_digest,
)
from yizkr.core.wiki import RecoveryEvidenceError
from yizkr.core.write_recovery import (
    PREFIX,
    CreationRefused,
    CreationRegistry,
    Generation,
)
from yizkr.pipeline.assigned_support import (
    SupportPayload,
    SupportRefused,
    SupportState,
    _inspect,
    _payload,
    _reference_json,
    _reference_key,
    same_statement,
)
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.claim_review import ClaimRole


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CorrectionRequest(_Record):
    source: RegistryBinding
    target: RegistryBinding
    assignments: tuple[AssignmentRevision, ...] = Field(min_length=1)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def explicit_scope(self):
        if not self.reason.strip():
            raise ValueError("correction needs a reason")
        if self.source.binding.entity_id == self.target.binding.entity_id:
            raise ValueError("correction needs a distinct existing target")
        if len({a.mention_id for a in self.assignments}) != len(
            self.assignments
        ):
            raise ValueError("select each accepted mention only once")
        if any(
            a.entity_id != self.source.binding.entity_id
            for a in self.assignments
        ):
            raise ValueError(
                "selected mentions must belong to the source entity"
            )
        return self


class CorrectionBlocker(_Record):
    code: str
    detail: str
    plan_id: str | None = None
    claim_id: str | None = None


class AssignmentChange(_Record):
    before: AssignmentRevision
    proposed: AssignmentTarget
    source: RegistryBinding
    target: RegistryBinding
    # Only a future reviewed acceptance could allocate this revision.
    proposed_revision: None = None


class ClaimImpact(_Record):
    plan_id: str
    claim: PlannedClaim
    selected_roles: tuple[str, ...]
    before: SupportPayload
    proposed: SupportPayload | None = None
    intent: SupportIntent | None = None
    receipt: SupportReceipt | None = None
    observed: SupportState | None = None


class SupportAuthority(_Record):
    plan_id: str
    claim_id: str
    selected: bool
    primary_hash: str
    circumstance_hash: str | None = None


class StatementImpact(_Record):
    qid: str
    revision: int
    guid: str
    rank: str
    references: tuple[dict[str, Any], ...]
    affected_reference_hashes: tuple[str, ...]
    other_reference_hashes: tuple[str, ...]
    authorities: tuple[SupportAuthority, ...]


class CorrectionPlan(_Record):
    format: Literal[1] = 1
    request: CorrectionRequest
    generation: Generation | None
    assignment_head: str | None
    changes: tuple[AssignmentChange, ...]
    catalog_plan_ids: tuple[str, ...]
    catalog_complete: bool
    # Full original decisions, source evidence and producer records for each
    # affected application remain inspectable without a local review database.
    applications: tuple[ApplicationPlan, ...]
    impacts: tuple[ClaimImpact, ...]
    statements: tuple[StatementImpact, ...]
    observed_revisions: dict[str, int]
    blockers: tuple[CorrectionBlocker, ...]
    required_reviews: tuple[str, ...]
    coverage: Literal["current-generation reviewed applications"] = (
        "current-generation reviewed applications"
    )
    limitations: tuple[str, ...] = (
        "Legacy, manual and unrecorded statements, including incoming "
        "references, "
        "are not inventoried. Catalog absence does not prove no impact.",
        "Deleted or suppressed history and current local decision revocations "
        "are not reconstructed from immutable application records.",
        "Statement process references do not establish an individual "
        "source-to-process association.",
        "Cited source items are checked at their recorded bindings; duplicate "
        "source-hash ownership elsewhere in the graph is not inventoried.",
        "Reads are checked for intervening changes, not a locked graph "
        "snapshot. "
        "Future application must freshly validate its own authority.",
    )
    complete_correction: Literal[False] = False
    mutation_authority: Literal[False] = False
    original_entity_preserved: Literal[True] = True

    @property
    def digest(self) -> str:
        return record_digest(self)


_EVIDENCE_ERRORS = (
    CreationRefused,
    AssignmentRefused,
    RecoveryEvidenceError,
    SupportRefused,
    ValueError,
    KeyError,
)


def _proposed_support(claim, payload, selected, source, target):
    """Join approved semantic roles to exact serialized graph positions."""
    semantic = claim.decision["proposal"]["basis"]["claim"]
    result = payload.model_copy(deep=True)
    if (
        payload.statement["mainsnak"]["property"]
        != payload.properties[semantic["property"]]
    ):
        raise ValueError("approved property differs from its graph position")
    roles = [ClaimRole.model_validate(r) for r in claim.roles]
    seen = set()

    def entity(snak, expected):
        value = snak.get("datavalue", {})
        if (
            snak.get("snaktype") != "value"
            or value.get("type") != "wikibase-entityid"
            or value.get("value", {}).get("id") != expected
        ):
            raise ValueError("approved role differs from its graph position")

    for role in roles:
        if role.role in seen:
            raise ValueError("application has repeated assignment roles")
        seen.add(role.role)
        qid = role.binding.binding.qid
        if role.role == "subject":
            reference = semantic["subject"]
            if payload.qid != qid:
                raise ValueError(
                    "approved subject differs from its graph target"
                )
            snak = None
        elif role.role == "object":
            reference = semantic["value"]["value"]
            snak = result.statement["mainsnak"]
            entity(snak, qid)
        elif role.role.startswith("qualifier:"):
            matches = [
                q
                for q in semantic["qualifiers"]
                if role.role
                == "qualifier:" + q["property"] + ":" + record_digest(q)
            ]
            if len(matches) != 1:
                raise ValueError("approved qualifier role is ambiguous")
            qualifier = matches[0]
            reference = qualifier["value"]["value"]
            pid = payload.properties[qualifier["property"]]
            positions = [
                n
                for n, s in enumerate(
                    payload.statement.get("qualifiers", {}).get(pid, [])
                )
                if s.get("datavalue", {}).get("value", {}).get("id") == qid
            ]
            if len(positions) != 1:
                raise ValueError(
                    "approved qualifier has no unique graph position"
                )
            # Locate against the original statement: an earlier proposed
            # replacement may now equal another unchanged qualifier value.
            snak = result.statement["qualifiers"][pid][positions[0]]
            entity(snak, qid)
        else:
            raise ValueError("unsupported assignment role")
        expected = {
            "kind": "mention",
            "id": role.mention_id,
            "assignment_revision": role.assignment.revision_id,
            "entity_id": role.assignment.entity_id,
            "binding_revision": role.binding.binding_revision,
            "qid": qid,
        }
        if reference != expected:
            raise ValueError("semantic role differs from accepted assignment")
        if role.mention_id not in selected:
            continue
        if role.binding != source:
            raise ValueError(
                "selected role differs from the requested source binding"
            )
        if snak is None:
            result = result.model_copy(update={"qid": target.binding.qid})
        else:
            snak["datavalue"]["value"] = {
                "entity-type": "item",
                "numeric-id": int(target.binding.qid[1:]),
                "id": target.binding.qid,
            }
    return result


def _receipt_matches(receipt, observed):
    if not observed.complete or (
        receipt.qid != observed.qid
        or receipt.statement_guid != observed.guid
        or receipt.primary_hash != observed.primary_hash
        or receipt.circumstance_hash != observed.circumstance_hash
    ):
        return False
    for previous, current in (
        (receipt.primary, observed.primary),
        (receipt.circumstance, observed.circumstance),
    ):
        if (previous is None) != (current is None) or (
            previous is not None
            and _reference_key(previous) != _reference_key(current)
        ):
            return False
    current_process = {
        r["hash"]: _reference_key(r) for r in observed.observed_process
    }
    return all(
        current_process.get(r["hash"]) == _reference_key(r)
        for r in receipt.observed_process
    )


def plan_correction(
    registry: CreationRegistry, request: CorrectionRequest
) -> CorrectionPlan:
    """Inspect explicit reassignment impact without modifying any authority.

    The caller supplies exact accepted revisions and both verified bindings.
    This function never prepares a review, acquires write authority, allocates
    an entity, retires support or transfers any previous confirmation.
    """
    request = CorrectionRequest.model_validate(request.model_dump(mode="json"))
    request = request.model_copy(
        update={
            "assignments": tuple(
                sorted(request.assignments, key=lambda a: a.mention_id)
            )
        }
    )
    selected = {a.mention_id: a for a in request.assignments}
    changes = tuple(
        AssignmentChange(
            before=a,
            proposed=AssignmentTarget(
                mention_id=a.mention_id,
                mention_revision=a.mention_revision,
                entity_id=request.target.binding.entity_id,
            ),
            source=request.source,
            target=request.target,
        )
        for a in request.assignments
    )
    blockers, impacts, statements = [], [], []
    applications, catalog, generation, head = (), (), None, None
    complete = False
    items, bindings, source_bindings = {}, {}, {}
    reviews = {
        "identity reassignment",
        "changed historical claims",
        "target suitability",
        "privacy reconsideration for both entities",
        "untracked graph impact",
        "unique current source ownership",
        "support retirement without losing unaffected evidence",
    }

    def block(code, detail, plan_id=None, claim_id=None):
        blockers.append(
            CorrectionBlocker(
                code=code, detail=detail, plan_id=plan_id, claim_id=claim_id
            )
        )

    def binding(entity_id):
        if entity_id not in bindings:
            bindings[entity_id] = registry.binding(entity_id)
        return bindings[entity_id]

    def item(qid):
        if qid not in items:
            check_cancelled()
            value = registry.client.wbi.item.get(qid)
            if (
                value.id != qid
                or type(value.lastrevid) is not int
                or value.lastrevid <= 0
            ):
                raise ValueError(
                    "item read has no exact identity and revision"
                )
            items[qid] = value
        return items[qid]

    try:
        snapshot = registry.assignment_snapshot()
        generation, head = snapshot.generation, snapshot.head
        if generation is None:
            raise ValueError("accepted assignment generation is unavailable")
        for expected in (request.source, request.target):
            if binding(expected.binding.entity_id) != expected:
                block(
                    "stale_binding", "Requested entity binding is not current."
                )
            item(expected.binding.qid)
        for assignment in request.assignments:
            if snapshot.assignments.get(assignment.mention_id) != assignment:
                block(
                    "stale_assignment",
                    "Selected accepted assignment is not current.",
                )
        for constraint in snapshot.constraints:
            if constraint.kind == "rejected_target":
                conflict = (
                    bool(selected.keys() & set(constraint.mentions))
                    and constraint.entity_id
                    == request.target.binding.entity_id
                )
            else:
                targets = {
                    request.target.binding.entity_id
                    if m in selected
                    else snapshot.assignments[m].entity_id
                    for m in constraint.mentions
                    if m in snapshot.assignments
                }
                conflict = (
                    bool(selected.keys() & set(constraint.mentions))
                    and len(targets) < 2
                    and all(
                        m in snapshot.assignments for m in constraint.mentions
                    )
                )
            if conflict:
                block(
                    "assignment_constraint",
                    "Proposed target conflicts with an accepted "
                    "negative constraint.",
                )
        prefix = f"{PREFIX}{generation.generation}/applications/"
        inventory = sorted(registry.client.page_titles(prefix))
        catalog = registry.application_catalog()
        complete = True
        applications = tuple(
            plan
            for plan in catalog
            if any(
                r["mention_id"] in selected
                for claim in plan.claims
                for r in claim.roles
            )
        )
        receipts = []
        observed_statements = {}
        for plan in catalog:
            for claim in plan.claims:
                roles = tuple(ClaimRole.model_validate(r) for r in claim.roles)
                chosen = tuple(
                    sorted(r.role for r in roles if r.mention_id in selected)
                )
                intent = registry.application_intent(
                    plan.plan_id, claim.claim_id
                )
                receipt = registry.application_receipt(
                    plan.plan_id, claim.claim_id
                )
                if receipt is not None:
                    receipts.append((receipt, bool(chosen)))
                if not chosen:
                    continue
                before = SupportPayload.model_validate(claim.support)
                proposed, observed = None, None
                context = (plan.plan_id, claim.claim_id)
                try:
                    for role in roles:
                        if (
                            snapshot.assignments.get(role.mention_id)
                            != role.assignment
                            or binding(role.assignment.entity_id)
                            != role.binding
                        ):
                            block(
                                "stale_role",
                                "An applied role's assignment or binding "
                                "changed.",
                                *context,
                            )
                    basis = claim.decision["proposal"]["basis"]
                    if basis["claim"]["property"] == "depicts":
                        reviews.add("explicit face association confirmation")
                    for sha1, raw in basis["source_bindings"].items():
                        expected = RegistryBinding.model_validate(raw)
                        key = expected.binding.entity_id
                        if key not in source_bindings:
                            source_bindings[key] = registry.verify_binding(
                                expected.binding
                            )
                        if source_bindings[key] != expected:
                            raise ValueError(
                                "applied source creation binding changed"
                            )
                        source_item = item(expected.binding.qid)
                        properties, _ = registry.client.vocabulary()
                        hashes = source_item.claims.get(
                            properties["media_sha1"]
                        )
                        active = []
                        for statement in hashes or ():
                            if statement.rank.value == "deprecated":
                                continue
                            value = statement.mainsnak.datavalue.get("value")
                            if not isinstance(value, str):
                                raise ValueError("invalid source hash value")
                            active.append(value.lower())
                        if set(active) != {sha1}:
                            raise ValueError(
                                "cited source no longer has its "
                                "exact active hash"
                            )
                    proposed = _proposed_support(
                        claim, before, selected, request.source, request.target
                    )
                    # A prior process describes the original interpretation,
                    # never approval or provenance of a new corrected result.
                    if intent is None or receipt is None:
                        block(
                            "missing_receipt",
                            "Applied support lacks a complete intent/receipt "
                            "chain; visible effects may still exist.",
                            *context,
                        )
                    if intent is not None:
                        payload = _payload(
                            registry.client,
                            SupportPayload.model_validate(intent.support),
                        )
                        observed, statement = _inspect(
                            item(payload.qid), payload
                        )
                        if receipt is not None and not _receipt_matches(
                            receipt, observed
                        ):
                            block(
                                "changed_support",
                                "Exact receipted statement, source support or "
                                "process context is missing or changed.",
                                *context,
                            )
                        if statement is not None:
                            observed_statements[statement.id] = (
                                observed,
                                statement,
                            )
                except _EVIDENCE_ERRORS as exc:
                    block("ambiguous_evidence", str(exc), *context)
                impacts.append(
                    ClaimImpact(
                        plan_id=plan.plan_id,
                        claim=claim,
                        selected_roles=chosen,
                        before=before,
                        proposed=proposed,
                        intent=intent,
                        receipt=receipt,
                        observed=observed,
                    )
                )
        for guid, (state, statement) in sorted(observed_statements.items()):
            authorities = tuple(
                SupportAuthority(
                    plan_id=r.plan_id,
                    claim_id=r.claim_id,
                    selected=chosen,
                    primary_hash=r.primary_hash,
                    circumstance_hash=r.circumstance_hash,
                )
                for r, chosen in receipts
                if r.statement_guid == guid
            )
            affected = {
                h
                for a in authorities
                if a.selected
                for h in (a.primary_hash, a.circumstance_hash)
                if h
            }
            retained = {
                h
                for a in authorities
                if not a.selected
                for h in (a.primary_hash, a.circumstance_hash)
                if h
            }
            if affected & retained:
                block(
                    "shared_reference",
                    "Selected and retained authorities share an exact "
                    "source-reference hash on " + guid + ".",
                )
            for ref_hash in affected:
                proposals = [
                    i.proposed
                    for i in impacts
                    if i.proposed is not None
                    and i.receipt is not None
                    and i.receipt.statement_guid == guid
                    and ref_hash
                    in (i.receipt.primary_hash, i.receipt.circumstance_hash)
                ]
                if proposals and any(
                    p.qid != proposals[0].qid
                    or not same_statement(p.statement, proposals[0].statement)
                    for p in proposals[1:]
                ):
                    block(
                        "conflicting_support_change",
                        "One source reference has different proposed "
                        "statement meanings; its authorities need an "
                        "explicit partition on " + guid + ".",
                    )
            references = tuple(
                sorted(
                    (_reference_json(r) for r in statement.references),
                    key=lambda r: r["hash"],
                )
            )
            statements.append(
                StatementImpact(
                    qid=state.qid,
                    revision=state.revision,
                    guid=guid,
                    rank=statement.rank.value,
                    references=references,
                    affected_reference_hashes=tuple(sorted(affected)),
                    other_reference_hashes=tuple(
                        sorted({r["hash"] for r in references} - affected)
                    ),
                    authorities=authorities,
                )
            )
        covered = {r["mention_id"] for i in impacts for r in i.claim.roles}
        if selected.keys() - covered:
            block(
                "no_recorded_support",
                "Selected mentions have no recorded applied roles; "
                "absence does not establish no graph impact.",
            )
        # Recheck only this capture's evidence. No lock or freshness token is
        # granted to a later writer by observing a stable result here.
        if (
            registry.application_catalog() != catalog
            or sorted(registry.client.page_titles(prefix)) != inventory
            or registry.assignment_snapshot() != snapshot
        ):
            complete = False
            block(
                "changed_authority",
                "Application catalog or assignments changed during planning.",
            )
        for entity_id, expected in bindings.items():
            if registry.binding(entity_id) != expected:
                block(
                    "changed_authority",
                    "Entity binding changed during planning.",
                )
        for expected in source_bindings.values():
            if registry.verify_binding(expected.binding) != expected:
                block(
                    "changed_authority",
                    "Source binding changed during planning.",
                )
        for qid, value in items.items():
            current = registry.client.wbi.item.get(qid)
            if current.id != qid or current.lastrevid != value.lastrevid:
                block(
                    "changed_graph",
                    "Observed graph item changed during planning.",
                )
        if registry.check_generation() != generation:
            block("changed_authority", "Deployment generation changed.")
    except _EVIDENCE_ERRORS as exc:
        complete = False
        block("unavailable_authority", str(exc))
    return CorrectionPlan(
        request=request,
        generation=generation,
        assignment_head=head,
        changes=changes,
        catalog_plan_ids=tuple(p.plan_id for p in catalog),
        catalog_complete=complete,
        applications=applications,
        impacts=tuple(impacts),
        statements=tuple(statements),
        observed_revisions={
            qid: items[qid].lastrevid for qid in sorted(items)
        },
        blockers=tuple(
            sorted(
                set(blockers),
                key=lambda b: (
                    b.code,
                    b.plan_id or "",
                    b.claim_id or "",
                    b.detail,
                ),
            )
        ),
        required_reviews=tuple(sorted(reviews)),
    )
