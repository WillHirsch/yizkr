"""Apply explicitly reviewed historical claims through durable assignments.

The existing registry owns plans, per-support intents and receipts. The review
store is pinned through the effects; immutable accepted assignments need no
new mutable identity lock. Only a subject item's statements are changed, under
its base revision. No entity is minted or given implicit aliases/kind claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yizkr.claims import ArtifactRef, LocalRef, VocabRef
from yizkr.core.claim_applications import (
    ApplicationPlan,
    ApplicationRefused,
    PlannedClaim,
    SupportIntent,
    SupportReceipt,
    claim_application_id,
    plan_identity,
    same_reviewed_scope,
)
from yizkr.core.identity_assignments import RegistryBinding, record_digest
from yizkr.core.instance import require_retained_code
from yizkr.core.provenance import RecordRef
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline.assigned_support import (
    SupportPayload,
    apply_support,
    inspect_support,
    prepare_support,
)
from yizkr.pipeline.cancellation import check_cancelled
from yizkr.pipeline.claim_review import (
    PreparedClaimReview,
    pin_claim_review,
    prepare_claim_review,
)
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.run_provenance import (
    application_record,
    freeze_record,
    legacy_record,
)
from yizkr.pipeline.run_provenance import (
    record_digest as process_digest,
)
from yizkr.pipeline.run_statements import (
    ensure_run_statements,
    validate_run_statements,
)
from yizkr.pipeline.source_bindings import unique_source_qids
from yizkr.pipeline.source_context import placed_owners
from yizkr.pipeline.writer import (
    BatchWriter,
    pre_plan_refusals,
    self_references,
    unsupported_dates,
)


@dataclass(frozen=True)
class ApplicationResult:
    plan_id: str
    receipts: tuple[SupportReceipt, ...]


class ApplicationInterrupted(ApplicationRefused):
    """A stopped plan retains its durable completed support receipts."""

    def __init__(self, plan_id, receipts, reason):
        self.plan_id = plan_id
        self.receipts = tuple(receipts)
        super().__init__(
            f"application {plan_id} stopped after {len(receipts)} receipts: "
            f"{reason}"
        )


def _local_qids(prepared, claim):
    roles = {role.mention_id: role for role in claim.roles}
    return {
        local_id: roles[mention.id].binding.binding.qid
        for (artifact, local_id), mention in prepared.identity.mentions.items()
        if artifact == claim.selection.artifact_sha1 and mention.id in roles
    }


def _support(writer, prepared, claim, run_qid=None):
    return prepare_support(
        writer,
        claim.claim,
        local_qids=_local_qids(prepared, claim),
        artifact_qids=prepared.artifact_qids,
        run_qid=run_qid,
    )


def _guard_claim(writer, prepared, selected):
    """Keep intrinsic claim admission independent of persisted approval."""
    source = next(
        source
        for source in prepared.request.sources
        if source.artifact_sha1 == selected.selection.artifact_sha1
    )
    batch = source.batch.model_copy(update={"claims": [selected.claim]})
    placed = {source.artifact_sha1: placed_owners(source.reading)}
    refused = pre_plan_refusals(batch, {}, placed, set(prepared.artifact_qids))
    dates = unsupported_dates(batch)
    if refused or dates:
        reasons = [message for _, message in refused.values()] + dates
        raise ApplicationRefused(
            "claim admission refused: " + "; ".join(reasons)
        )
    locals_ = _local_qids(prepared, selected)

    def resolve(ref):
        if isinstance(ref, LocalRef):
            return locals_[ref.local_id]
        if isinstance(ref, ArtifactRef):
            return prepared.artifact_qids[ref.sha1]
        if isinstance(ref, VocabRef):
            return writer.items[ref.key]
        raise ApplicationRefused("unverified entity reference")

    if self_references(batch, resolve):
        raise ApplicationRefused(
            "claim would refer to its own assigned subject"
        )
    # Exercise judgement/process serialization before any metadata is created.
    # This concrete dummy QID is never written or retained as a binding.
    _support(writer, prepared, selected, "Q1")


def _authority(registry, prepared):
    snapshot = registry.assignment_snapshot()
    if snapshot.generation is None:
        raise ApplicationRefused("application generation is missing")
    # Repeated roles and corroborating claims share checks only within this
    # read-only pass. Every later mutation checkpoint starts fresh.
    entities = {}
    sources = {}
    for claim in prepared.claims:
        if (
            claim.basis["deployment_id"] != snapshot.generation.deployment_id
            or claim.basis["generation"] != snapshot.generation.generation
        ):
            raise ApplicationRefused("application generation changed")
        for role in claim.roles:
            if snapshot.assignments.get(role.mention_id) != role.assignment:
                raise ApplicationRefused(
                    "accepted assignment or binding changed"
                )
            entity_id = role.assignment.entity_id
            if entity_id not in entities:
                entities[entity_id] = registry.binding(entity_id)
            if entities[entity_id] != role.binding:
                raise ApplicationRefused(
                    "accepted assignment or binding changed"
                )
        for raw in claim.provenance["artifact_bindings"].values():
            binding = RegistryBinding.model_validate(raw)
            entity_id = binding.binding.entity_id
            if entity_id not in sources:
                sources[entity_id] = registry.verify_binding(binding.binding)
            if sources[entity_id] != binding:
                raise ApplicationRefused("registered source binding changed")
    properties, _ = registry.client.vocabulary()
    if (
        unique_source_qids(
            registry.client, properties["media_sha1"], prepared.artifact_qids
        )
        != prepared.artifact_qids
    ):
        raise ApplicationRefused("source hash ownership changed")
    return snapshot.generation


def _reviewed_scope(prepared):
    result = {}
    for claim in prepared.claims:
        if claim.decision is None or claim.decision.outcome != "approve":
            raise ReviewRefused("every selected claim needs explicit approval")
        if (
            claim.claim.property == "depicts"
            and not claim.decision.depicts_confirmed
        ):
            raise ReviewRefused(
                "depicts needs its explicit displayed association"
            )
        result[(claim.selection.artifact_sha1, claim.selection.index)] = (
            claim.proposal,
            claim.decision,
        )
    return result


def _plan(writer, prepared, generation):
    claims = {}
    for claim in prepared.claims:
        decision = claim.decision.model_dump(mode="json")
        planned = PlannedClaim(
            claim_id=claim_application_id(decision),
            decision=decision,
            roles=tuple(role.model_dump(mode="json") for role in claim.roles),
            support=_support(writer, prepared, claim, "Q1")
            .model_copy(update={"process": None})
            .model_dump(mode="json"),
        )
        prior = claims.setdefault(planned.claim_id, planned)
        if prior != planned:
            raise ApplicationRefused(
                "one approval has incompatible graph supports"
            )
    ordered = tuple(claims[key] for key in sorted(claims))
    plan_id = plan_identity(
        generation.deployment_id, generation.generation, ordered
    )
    producers = {}
    for source in prepared.request.sources:
        if source.artifact_sha1 not in {
            claim.selection.artifact_sha1 for claim in prepared.claims
        }:
            continue
        producer = source.batch.extraction_run.provenance or legacy_record(
            source.batch.extraction_run
        )
        producers[process_digest(producer)] = producer
    origins = tuple(producers[key] for key in sorted(producers))
    record = application_record(
        origins,
        kind="reviewed-claim-application",
        stage="assignment-write-preparation",
        metadata={
            "plan_id": plan_id,
            "claims": [claim.model_dump(mode="json") for claim in ordered],
            "source_products": [claim.provenance for claim in prepared.claims],
        },
        configuration={"application_format": 1},
    ).model_copy(
        update={
            "producer_refs": tuple(
                RecordRef(sha256=key) for key in sorted(producers)
            ),
            "limitations": tuple(
                dict.fromkeys(
                    reason
                    for origin in origins
                    for reason in origin.limitations
                )
            )
            + (
                "Preparation is not completion; individual support receipts "
                "identify observed graph effects.",
                "Existing statement process references do not establish an "
                "individual source-to-process association.",
            ),
        }
    )
    validate_run_statements(writer.client, record)
    return ApplicationPlan(
        plan_id=plan_id,
        deployment_id=generation.deployment_id,
        generation=generation.generation,
        claims=ordered,
        record=record,
        producers=origins,
    )


def _freeze(plan, directory):
    for producer in plan.producers:
        freeze_record(directory, producer)
    return freeze_record(directory, plan.record)


def _process_item(client, registry, plan, reference):
    item = client.wbi.item.new()
    item.labels.set("en", f"Reviewed claim application {plan.plan_id[:16]}")
    binding = registry.create(
        {"kind": "reviewed-claim-application", "plan": plan.plan_id},
        item,
        summary="ingest: reviewed application process",
    )
    ensure_run_statements(client, binding.qid, plan.record, reference.sha256)
    return binding.qid


def _receipt(intent, state):
    if not state.complete:
        raise ApplicationRefused("application has no exact observed support")
    return SupportReceipt(
        plan_id=intent.plan_id,
        claim_id=intent.claim_id,
        deployment_id=intent.deployment_id,
        generation=intent.generation,
        intent_sha256=record_digest(intent),
        qid=state.qid,
        revision=state.revision,
        statement_guid=state.guid,
        primary_hash=state.primary_hash,
        circumstance_hash=state.circumstance_hash,
        primary=state.primary,
        circumstance=state.circumstance,
        observed_process=state.observed_process,
    )


def _check_receipt(receipt, state):
    if not state.complete or (
        receipt.qid != state.qid
        or receipt.statement_guid != state.guid
        or receipt.primary_hash != state.primary_hash
        or receipt.circumstance_hash != state.circumstance_hash
        or not {r["hash"] for r in receipt.observed_process}
        <= set(state.process_hashes)
    ):
        raise ApplicationRefused("previously applied support has changed")


def apply_reviewed_claims(
    store: ReviewStore,
    prepared: PreparedClaimReview,
    client,
    *,
    record_dir: Path,
) -> ApplicationResult:
    """Apply current approval, retaining partial receipts on interruption.

    Every source is already registered and every historical entity already has
    an accepted assignment. Changes to assignments, claims, review or support
    refuse; legacy bulk/row approval is never substituted for these decisions.
    An uncertain update is reread before any retry. If support is absent after
    an intervening item edit, the old intent cannot be rebased automatically.
    """
    expected = _reviewed_scope(prepared)
    registry = CreationRegistry(client)
    fresh = prepare_claim_review(
        store, prepared.request, registry, selections=prepared.selections
    )
    if _reviewed_scope(fresh) != expected:
        raise ReviewRefused("claim review changed after preparation")
    writer = BatchWriter(client, record_dir=record_dir)
    with pin_claim_review(store, fresh):
        generation = _authority(registry, fresh)
        for claim in fresh.claims:
            _guard_claim(writer, fresh, claim)
        plan = _plan(writer, fresh, generation)
        existing = registry.application_plan(plan.plan_id)
        if existing is not None:
            if not same_reviewed_scope(existing, plan):
                raise ApplicationRefused(
                    "current support differs from saved plan"
                )
            plan = existing
        for claim in plan.claims:
            inspect_support(
                client, SupportPayload.model_validate(claim.support)
            )
        reference = _freeze(plan, record_dir)
        require_retained_code(client)
        _authority(registry, fresh)
        plan = registry.record_application_plan(plan)
        # Another host can reserve the same meaning first. Retain its exact
        # producer/application evidence through recovery and subsequent writes.
        reference = _freeze(plan, record_dir)
        selected = {
            claim_application_id(claim.decision.model_dump(mode="json")): claim
            for claim in fresh.claims
        }
        completed = []
        run_qid = None
        try:
            for claim in plan.claims:
                check_cancelled()
                _authority(registry, fresh)
                current = selected[claim.claim_id]
                intent = registry.application_intent(
                    plan.plan_id, claim.claim_id
                )
                if intent is None:
                    payload = SupportPayload.model_validate(claim.support)
                    state = inspect_support(client, payload)
                    # Existing exact support keeps its actual process context.
                    # A new judgement needs its process block even when the
                    # ordinary source block is already present.
                    needs_process = not current.claim.curated and (
                        not state.complete or current.claim.note is not None
                    )
                    if needs_process:
                        if run_qid is None:
                            require_retained_code(client)
                            _authority(registry, fresh)
                            run_qid = _process_item(
                                client, registry, plan, reference
                            )
                        process = _support(writer, fresh, current, run_qid)
                        payload = payload.model_copy(
                            update={"process": process.process}
                        )
                        state = inspect_support(client, payload)
                    intent = registry.record_application_intent(
                        SupportIntent(
                            plan_id=plan.plan_id,
                            claim_id=claim.claim_id,
                            deployment_id=plan.deployment_id,
                            generation=plan.generation,
                            support=payload.model_dump(mode="json"),
                            base_revision=state.revision,
                            prior_guid=state.guid,
                        )
                    )
                payload = SupportPayload.model_validate(intent.support)
                state = inspect_support(client, payload)
                receipt = registry.application_receipt(
                    plan.plan_id, claim.claim_id
                )
                if receipt is not None:
                    _check_receipt(receipt, state)
                    completed.append(receipt)
                    continue
                if not state.complete:
                    _authority(registry, fresh)
                    require_retained_code(client)
                    try:
                        registry.reserve_support_dispatch(intent)
                        state = apply_support(
                            client,
                            payload,
                            expected_revision=intent.base_revision,
                            summary="ingest: approved source support",
                        )
                    except Exception:
                        # Never resend a failed/uncertain transport here. A
                        # complete observed effect can still receive a receipt.
                        state = inspect_support(client, payload)
                        if not state.complete:
                            raise
                _authority(registry, fresh)
                receipt = registry.record_application_receipt(
                    _receipt(intent, state)
                )
                completed.append(receipt)
        except Exception as exc:
            raise ApplicationInterrupted(plan.plan_id, completed, exc) from exc
        return ApplicationResult(plan.plan_id, tuple(completed))
