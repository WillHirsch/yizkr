"""Reviewed applications preserve authority through effects and recovery."""

import copy
from dataclasses import replace

import pytest

from tests.unit.test_claim_review import approve, prepare
from tests.unit.test_claim_review import case as case
from yizkr.core.claim_applications import SupportReceipt
from yizkr.core.identity_assignments import RegistryBinding
from yizkr.core.write_recovery import CreationRefused, CreationRegistry
from yizkr.pipeline import assignment_writes
from yizkr.pipeline.assignment_writes import (
    ApplicationInterrupted,
    apply_reviewed_claims,
)
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore


def approved(case):
    prepared = prepare(case)
    approve(case, prepared)
    return prepare(case)


def apply(case, prepared, tmp_path):
    return apply_reviewed_claims(
        case.store, prepared, case.wiki, record_dir=tmp_path / "records"
    )


def snapshot(case):
    return (
        copy.deepcopy(case.wiki._recovery_pages),
        {
            key: item.get_json()
            for key, item in case.wiki.wbi.item.store.items()
        },
    )


def test_identity_approval_alone_has_no_application_effect(case, tmp_path):
    prepared = prepare(case)
    before = snapshot(case)
    with pytest.raises(ReviewRefused, match="explicit approval"):
        apply(case, prepared, tmp_path)
    assert snapshot(case) == before


def test_exact_receipt_reuses_completed_effect_after_reopen(case, tmp_path):
    prepared = approved(case)
    result = apply(case, prepared, tmp_path)
    (receipt,) = result.receipts
    plan = case.registry.application_plan(result.plan_id)
    assert {r["role"] for r in plan.claims[0].roles} >= {"subject", "object"}
    assert any(
        r["role"].startswith("qualifier:") for r in plan.claims[0].roles
    )
    assert (
        case.registry.application_receipt(result.plan_id, receipt.claim_id)
        == receipt
    )
    before = snapshot(case)
    with ReviewStore(case.database) as reopened:
        assert (
            apply_reviewed_claims(
                reopened, prepared, case.wiki, record_dir=tmp_path / "records"
            )
            == result
        )
    assert snapshot(case) == before
    assert (
        SupportReceipt.model_validate_json(receipt.model_dump_json())
        == receipt
    )


def test_lost_receipt_ack_recovers_exact_publication(
    case, tmp_path, monkeypatch
):
    prepared = approved(case)
    original = case.wiki.create_page_once
    lost = []

    def lose(title, text, summary):
        page = original(title, text, summary)
        if (
            "/applications/" in title
            and title.endswith("/receipt")
            and not lost
        ):
            lost.append(title)
            raise OSError("fictional lost receipt acknowledgement")
        return page

    monkeypatch.setattr(case.wiki, "create_page_once", lose)
    result = apply(case, prepared, tmp_path)
    assert len(lost) == len(result.receipts) == 1
    before = snapshot(case)
    assert apply(case, prepared, tmp_path) == result
    assert snapshot(case) == before


def test_receipt_interruption_reconciles_without_another_statement_write(
    case, tmp_path, monkeypatch
):
    prepared = approved(case)

    def interrupt(*args):
        raise OSError("fictional receipt storage interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(
            CreationRegistry, "record_application_receipt", interrupt
        )
        with pytest.raises(ApplicationInterrupted) as stopped:
            apply(case, prepared, tmp_path)
    assert stopped.value.receipts == ()
    target = case.bindings["leopold"].qid
    item_before = case.wiki.wbi.item.get(target).get_json()
    result = apply(case, prepared, tmp_path)
    assert result.plan_id == stopped.value.plan_id
    assert case.wiki.wbi.item.get(target).get_json() == item_before
    assert len(result.receipts) == 1


def test_an_intervening_curator_edit_does_not_rebase_a_reserved_intent(
    case, tmp_path, monkeypatch
):
    prepared = approved(case)
    original = assignment_writes.apply_support
    edited = []

    def edit_then_apply(client, payload, **kwargs):
        if not edited:
            item = client.wbi.item.get(payload.qid)
            item.labels.set("en", "Curator's preserved label")
            item.write()
            edited.append(item.get_json())
        return original(client, payload, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(assignment_writes, "apply_support", edit_then_apply)
        with pytest.raises(ApplicationInterrupted, match="revision changed"):
            apply(case, prepared, tmp_path)
    target = case.bindings["leopold"].qid
    before = case.wiki.wbi.item.get(target).get_json()
    with pytest.raises(ApplicationInterrupted, match="dispatch reserved"):
        apply(case, prepared, tmp_path)
    assert case.wiki.wbi.item.get(target).get_json() == before
    assert not before.get("claims")


def test_source_binding_is_rechecked_after_process_creation(
    case, tmp_path, monkeypatch
):
    prepared = approved(case)
    source = RegistryBinding.model_validate(
        next(iter(prepared.claims[0].provenance["artifact_bindings"].values()))
    )
    original = assignment_writes._process_item

    def change_source_authority(*args):
        qid = original(*args)
        case.wiki._recovery_edited.add(source.receipt_locator)
        return qid

    monkeypatch.setattr(
        assignment_writes, "_process_item", change_source_authority
    )
    with pytest.raises(ApplicationInterrupted, match="edited"):
        apply(case, prepared, tmp_path)
    assert all(
        not case.wiki.wbi.item.get(binding.qid).get_json().get("claims")
        for binding in case.bindings.values()
    )


def test_current_changed_claim_does_not_reuse_a_prepared_approval(
    case, tmp_path
):
    prepared = approved(case)
    source = case.request.sources[0]
    changed = source.batch.model_copy(deep=True)
    changed.claims[0].qualifiers[1].value.year += 1
    request = replace(case.request, sources=(replace(source, batch=changed),))
    before = snapshot(case)
    with pytest.raises(ReviewRefused):
        apply(case, replace(prepared, request=request), tmp_path)
    assert snapshot(case) == before


def test_wrong_generation_intent_cannot_change_authority(case, tmp_path):
    result = apply(case, approved(case), tmp_path)
    intent = case.registry.application_intent(
        result.plan_id, result.receipts[0].claim_id
    )
    proposed = intent.model_copy(update={"generation": "f" * 32})
    before = snapshot(case)
    with pytest.raises(CreationRefused, match="another generation"):
        case.registry.record_application_intent(proposed)
    assert snapshot(case) == before


def test_changed_completed_reference_is_not_silently_repaired(case, tmp_path):
    prepared = approved(case)
    result = apply(case, prepared, tmp_path)
    (receipt,) = result.receipts
    item = case.wiki.wbi.item.get(receipt.qid)
    statement = next(s for s in item.claims if s.id == receipt.statement_guid)
    statement.references.references = [
        r for r in statement.references if r.hash != receipt.primary_hash
    ]
    item.write()
    before = snapshot(case)
    with pytest.raises(ApplicationInterrupted, match="support has changed"):
        apply(case, prepared, tmp_path)
    assert snapshot(case) == before


@pytest.mark.parametrize("case", [True], indirect=True)
def test_proven_reading_remap_keeps_original_applied_support(case, tmp_path):
    from yizkr.pipeline.transcribe import reading_digest

    original = approved(case)
    result = apply(case, original, tmp_path)
    source = case.request.sources[0]
    batch = source.batch.model_copy(deep=True)
    reading = source.reading.model_copy(deep=True)
    reading.text = "\n" + reading.text
    span = reading.source_context.readings[0]
    span.start += 1
    span.end += 1
    span.support.reading_sha256 = reading_digest(reading)
    batch.claims[0].source.support = span.support.model_copy(deep=True)
    for entity in batch.entities:
        entity.mention_lines = [2]
    request = replace(
        case.request, sources=(replace(source, batch=batch, reading=reading),)
    )
    current = prepare(case, request)
    assert current.claims[0].decision == original.claims[0].decision
    before = snapshot(case)
    assert apply(case, current, tmp_path) == result
    assert snapshot(case) == before


def test_judgement_note_applies_its_actual_process_block(case, tmp_path):
    from yizkr.claims import JudgementNote
    from yizkr.pipeline.review_stages import prepare_review

    case.request.sources[0].batch.claims[0].note = JudgementNote(
        category="relation", text="The passage is read as giving refuge."
    )
    current = prepare_review(case.store, case.request)
    for proposal in current.proposals:
        previous = case.store.get_decision(proposal.id)
        case.store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="curator",
            time="2026-01-01T12:00:00Z",
            expected_decision_revision=previous.revision,
        )
    result = apply(case, approved(case), tmp_path)
    (receipt,) = result.receipts
    note = case.wiki.vocabulary()[0]["judgement_note"]
    assert any(note in block["snaks"] for block in receipt.observed_process)
    assert apply(case, prepare(case), tmp_path) == result


def test_same_property_qualifier_order_keeps_approval_and_receipt(
    case, tmp_path
):
    from yizkr.pipeline.review_stages import prepare_review

    claim = case.request.sources[0].batch.claims[0]
    extra = claim.qualifiers[1].model_copy(deep=True)
    extra.value.year += 1
    claim.qualifiers.append(extra)
    identity = prepare_review(case.store, case.request)
    for proposal in identity.proposals:
        previous = case.store.get_decision(proposal.id)
        case.store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="curator",
            time="2026-01-01T12:00:00Z",
            expected_decision_revision=previous.revision,
        )
    original = approved(case)
    result = apply(case, original, tmp_path)
    claim.qualifiers.reverse()
    current = prepare(case)
    assert current.claims[0].decision == original.claims[0].decision
    before = snapshot(case)
    assert apply(case, current, tmp_path) == result
    assert snapshot(case) == before


def test_reserved_dispatch_without_visible_effect_is_not_resent(
    case, tmp_path, monkeypatch
):
    prepared = approved(case)
    calls = []

    def lost(*args, **kwargs):
        calls.append(1)
        raise OSError("fictional connection lost before outcome is known")

    monkeypatch.setattr(assignment_writes, "apply_support", lost)
    with pytest.raises(ApplicationInterrupted):
        apply(case, prepared, tmp_path)
    with pytest.raises(ApplicationInterrupted, match="dispatch reserved"):
        apply(case, prepared, tmp_path)
    assert calls == [1]


@pytest.mark.parametrize("fault", ["digest", "roles", "scope", "producers"])
def test_malformed_plan_cannot_publish_authority(case, tmp_path, fault):
    from yizkr.core.claim_applications import ApplicationPlan

    result = apply(case, approved(case), tmp_path)
    plan = case.registry.application_plan(result.plan_id)
    raw = plan.model_dump(mode="json")
    if fault == "digest":
        raw["claims"][0]["decision"]["proposal"]["basis"]["claim"][
            "property"
        ] = "father"
    elif fault == "roles":
        raw["claims"][0]["roles"] = []
    elif fault == "scope":
        raw["record"]["metadata"]["plan_id"] = "a" * 64
    else:
        raw["record"]["producer_refs"] = []
    before = snapshot(case)
    with pytest.raises(ValueError):
        case.registry.record_application_plan(
            ApplicationPlan.model_validate(raw)
        )
    assert snapshot(case) == before
