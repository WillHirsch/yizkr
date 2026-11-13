"""Correction inspection preserves evidence without granting authority."""

import copy
from dataclasses import replace

import pytest
from wikibaseintegrator.datatypes import Item, String
from wikibaseintegrator.models import Reference
from wikibaseintegrator.wbi_enums import WikibaseRank

from tests.unit.test_assignment_writes import apply, approved, snapshot
from tests.unit.test_assignment_writes import case as case
from tests.unit.test_claim_review import WHEN, approve, prepare
from yizkr.claims import ArtifactRef, LocalRef
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.assignment_writes import ApplicationInterrupted
from yizkr.pipeline.claim_review import ClaimSelection
from yizkr.pipeline.correction_planning import (
    CorrectionPlan,
    CorrectionRequest,
    _proposed_support,
    plan_correction,
)
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewProposal,
    prepare_review,
)


def request_for(case, local_id="leopold"):
    source = case.registry.binding(case.bindings[local_id].entity_id)
    target = case.wiki.wbi.item.new()
    target.labels.set(
        "en", "Stupfield" if local_id == "rekau" else "Eugen Löbig"
    )
    created = case.registry.create(
        {"kind": "fictional-correction-target", "role": local_id},
        target,
        summary="fictional existing correction target",
    )
    return CorrectionRequest(
        source=source,
        target=case.registry.index_binding(created),
        assignments=tuple(
            a
            for a in case.accepted.snapshot.assignments.values()
            if a.entity_id == source.binding.entity_id
        ),
        reason="Review a possible mistaken mention assignment.",
    )


def inspect(case, request):
    before = snapshot(case)
    writes = case.wiki.wbi.item.writes
    result = plan_correction(case.registry, request)
    assert snapshot(case) == before
    assert case.wiki.wbi.item.writes == writes
    assert not result.complete_correction
    assert not result.mutation_authority
    assert result.original_entity_preserved
    return result


def codes(result):
    return {blocker.code for blocker in result.blockers}


@pytest.mark.parametrize(
    "local_id,role",
    [("leopold", "subject"), ("klara", "object"), ("rekau", "qualifier:")],
)
def test_exact_roles_and_support_are_inspectable_without_changing_authority(
    case, tmp_path, local_id, role
):
    applied = apply(case, approved(case), tmp_path)
    request = request_for(case, local_id)
    result = inspect(case, request)
    assert not result.blockers
    assert result.catalog_complete
    assert result.catalog_plan_ids == (applied.plan_id,)
    assert result.applications == (
        case.registry.application_plan(applied.plan_id),
    )
    (change,) = result.changes
    assert change.before == request.assignments[0]
    assert change.proposed.mention_id == change.before.mention_id
    assert change.proposed.entity_id == request.target.binding.entity_id
    assert change.proposed_revision is None
    (impact,) = result.impacts
    assert len(impact.selected_roles) == 1
    assert impact.selected_roles[0].startswith(role)
    assert impact.claim == result.applications[0].claims[0]
    assert impact.receipt == applied.receipts[0]
    assert impact.observed.complete
    expected = impact.before.model_copy(deep=True)
    if role == "subject":
        expected = expected.model_copy(
            update={"qid": request.target.binding.qid}
        )
    else:
        snak = (
            expected.statement["mainsnak"]
            if role == "object"
            else expected.statement["qualifiers"][
                expected.properties["location"]
            ][0]
        )
        snak["datavalue"]["value"] = {
            "entity-type": "item",
            "numeric-id": int(request.target.binding.qid[1:]),
            "id": request.target.binding.qid,
        }
    assert impact.proposed == expected
    assert impact.proposed.primary == impact.before.primary
    assert impact.proposed.process is None
    assert set(result.required_reviews) >= {
        "identity reassignment",
        "changed historical claims",
        "privacy reconsideration for both entities",
    }
    roundtrip = CorrectionPlan.model_validate_json(result.model_dump_json())
    assert roundtrip == result
    assert inspect(case, request).digest == roundtrip.digest


def test_other_citations_process_context_and_curator_rank_remain_visible(
    case, tmp_path
):
    applied = apply(case, approved(case), tmp_path)
    (receipt,) = applied.receipts
    item = case.wiki.wbi.item.get(receipt.qid)
    statement = next(s for s in item.claims if s.id == receipt.statement_guid)
    statement.rank = WikibaseRank.PREFERRED
    artifact = ArtifactRef.from_bytes(b"fictional second refuge account")
    other = case.wiki.wbi.item.new()
    other.labels.set("en", "Fictional second refuge account")
    other.claims.add(
        String(
            prop_nr=case.wiki.vocabulary()[0]["media_sha1"],
            value=artifact.sha1,
        )
    )
    binding = case.registry.create(
        {"kind": "source", "sha1": artifact.sha1},
        other,
        summary="fictional independent source",
    )
    reference = Reference()
    reference.add(
        Item(
            prop_nr=case.wiki.vocabulary()[0]["stated_in"],
            value=binding.qid,
        )
    )
    statement.references.add(reference)
    item.write()
    result = inspect(case, request_for(case))
    assert not result.blockers
    (affected,) = result.statements
    assert affected.guid == receipt.statement_guid
    assert affected.rank == "preferred"
    assert affected.affected_reference_hashes == (receipt.primary_hash,)
    refs = next(
        s
        for s in case.wiki.wbi.item.get(receipt.qid).claims
        if s.id == receipt.statement_guid
    ).references
    assert {r["hash"] for r in affected.references} == {r.hash for r in refs}
    assert set(affected.other_reference_hashes) == {
        r.hash for r in refs if r.hash != receipt.primary_hash
    }
    assert set(affected.other_reference_hashes) > {
        r["hash"] for r in receipt.observed_process
    }


def test_unselected_assignment_sharing_exact_reference_blocks_detachment(
    case, tmp_path
):
    source = case.request.sources[0]
    batch = source.batch.model_copy(deep=True)
    batch.entities.append(
        batch.entities[0].model_copy(
            update={
                "id": "leopold-second",
                "label": "Leopold",
                "mention_lines": [2],
            }
        )
    )
    second_claim = batch.claims[0].model_copy(deep=True)
    second_claim.subject = LocalRef(local_id="leopold-second")
    batch.claims.append(second_claim)
    request = replace(
        case.request,
        sources=(
            replace(
                source,
                batch=batch,
                reading=source.reading.model_copy(
                    update={"text": source.reading.text + "\nLeopold."}
                ),
            ),
        ),
        proposals=(
            *case.request.proposals,
            ReviewProposal(
                operation="assign",
                mentions=(
                    MentionKey(
                        artifact_sha1=source.artifact_sha1,
                        local_id="leopold-second",
                    ),
                ),
                target=case.bindings["leopold"],
            ),
        ),
    )
    reviewed = prepare_review(case.store, request)
    for proposal in reviewed.proposals:
        previous = case.store.get_decision(proposal.id)
        case.store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="curator",
            time=WHEN,
            expected_decision_revision=previous.revision if previous else None,
        )
    case.accepted = accept_reviewed_assignments(
        case.store, request, case.registry
    )
    case.request = request
    first = apply(case, approved(case), tmp_path)
    second_case = copy.copy(case)
    second_case.request = request
    second_case.selection = ClaimSelection(
        artifact_sha1=source.artifact_sha1, index=1
    )
    second_prepared = prepare(second_case)
    approve(second_case, second_prepared)
    second = apply(second_case, prepare(second_case), tmp_path)
    assert (
        first.receipts[0].statement_guid == second.receipts[0].statement_guid
    )
    assert first.receipts[0].primary_hash == second.receipts[0].primary_hash
    correction = request_for(case)
    original_role = next(
        r
        for r in case.registry.application_plan(first.plan_id).claims[0].roles
        if r["role"] == "subject"
    )
    selected = next(
        a
        for a in correction.assignments
        if a.mention_id == original_role["mention_id"]
    )
    result = inspect(
        case, correction.model_copy(update={"assignments": (selected,)})
    )
    assert "shared_reference" in codes(result)
    (statement,) = result.statements
    assert {
        (authority.plan_id, authority.selected)
        for authority in statement.authorities
    } == {(first.plan_id, True), (second.plan_id, False)}


@pytest.mark.parametrize("fault", ["removed", "deprecated"])
def test_applied_source_must_retain_its_active_byte_identity(
    case, tmp_path, fault
):
    applied = apply(case, approved(case), tmp_path)
    application = case.registry.application_plan(applied.plan_id)
    source = next(
        iter(
            application.claims[0]
            .decision["proposal"]["basis"]["source_bindings"]
            .values()
        )
    )
    item = case.wiki.wbi.item.get(source["binding"]["qid"])
    property_id = case.wiki.vocabulary()[0]["media_sha1"]
    if fault == "removed":
        item.claims.claims.pop(property_id)
    else:
        item.claims.get(property_id)[0].rank = WikibaseRank.DEPRECATED
    item.write()
    result = inspect(case, request_for(case))
    assert "ambiguous_evidence" in codes(result)
    assert result.applications == (application,)


def test_catalog_absence_does_not_claim_a_complete_graph_correction(case):
    result = inspect(case, request_for(case))
    assert result.catalog_complete
    assert result.catalog_plan_ids == ()
    assert result.impacts == ()
    assert "no_recorded_support" in codes(result)
    assert result.coverage == "current-generation reviewed applications"
    assert any(
        "Legacy, manual and unrecorded" in text for text in result.limitations
    )


def test_two_item_qualifiers_keep_their_roles_when_target_already_occurs(
    case, tmp_path
):
    correction = request_for(case, "rekau")
    source = case.request.sources[0]
    batch = source.batch.model_copy(deep=True)
    batch.entities.append(
        batch.entities[2].model_copy(
            update={"id": "stupfield", "label": "Stupfield"}
        )
    )
    claim = batch.claims[0]
    qualifier = claim.qualifiers[0].model_copy(deep=True)
    qualifier.value.value = LocalRef(local_id="stupfield")
    claim.qualifiers.append(qualifier)
    text = source.reading.text.replace("Rekau.", "Rekau and Stupfield.")
    claim.source.transcription_excerpt.text = text
    request = replace(
        case.request,
        sources=(
            replace(
                source,
                batch=batch,
                reading=source.reading.model_copy(update={"text": text}),
            ),
        ),
        proposals=(
            *case.request.proposals,
            ReviewProposal(
                operation="assign",
                mentions=(
                    MentionKey(
                        artifact_sha1=source.artifact_sha1,
                        local_id="stupfield",
                    ),
                ),
                target=correction.target.binding,
            ),
        ),
        current_targets=(
            *case.request.current_targets,
            correction.target.binding,
        ),
    )
    reviewed = prepare_review(case.store, request)
    for proposal in reviewed.proposals:
        previous = case.store.get_decision(proposal.id)
        case.store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="curator",
            time=WHEN,
            expected_decision_revision=previous.revision if previous else None,
        )
    case.accepted = accept_reviewed_assignments(
        case.store, request, case.registry
    )
    case.request = request
    prepared = approved(case)
    selected = next(
        r.assignment
        for r in prepared.claims[0].roles
        if r.role.startswith("qualifier:")
        and r.assignment.entity_id == correction.source.binding.entity_id
    )
    applied = apply(case, prepared, tmp_path)
    correction = correction.model_copy(update={"assignments": (selected,)})
    result = inspect(case, correction)
    assert not result.blockers
    (impact,) = result.impacts
    assert impact.receipt == applied.receipts[0]
    assert len(impact.selected_roles) == 1
    assert impact.selected_roles[0].startswith("qualifier:location:")
    # Mention IDs affect canonical role ordering. Exercise both traversals so
    # this catches lookup in an already-modified qualifier list every time.
    for roles in (impact.claim.roles, tuple(reversed(impact.claim.roles))):
        reordered = impact.claim.model_copy(update={"roles": roles})
        assert (
            _proposed_support(
                reordered,
                impact.before,
                {selected.mention_id},
                correction.source,
                correction.target,
            )
            == impact.proposed
        )
    pid = impact.before.properties["location"]
    before = impact.before.statement["qualifiers"][pid]
    after = impact.proposed.statement["qualifiers"][pid]
    assert {q["datavalue"]["value"]["id"] for q in before} == {
        correction.source.binding.qid,
        correction.target.binding.qid,
    }
    assert [q["datavalue"]["value"]["id"] for q in after] == [
        correction.target.binding.qid
    ] * 2
    assert impact.proposed.primary == impact.before.primary


@pytest.mark.parametrize(
    "fault", ["missing", "changed_reference", "removed_process"]
)
def test_missing_or_changed_applied_evidence_is_a_blocker(
    case, tmp_path, fault
):
    applied = apply(case, approved(case), tmp_path)
    (receipt,) = applied.receipts
    if fault == "missing":
        title = next(
            title
            for title in case.wiki._recovery_pages
            if "/applications/" in title and title.endswith("/receipt")
        )
        del case.wiki._recovery_pages[title]
    else:
        item = case.wiki.wbi.item.get(receipt.qid)
        statement = next(
            s for s in item.claims if s.id == receipt.statement_guid
        )
        remove = (
            receipt.primary_hash
            if fault == "changed_reference"
            else receipt.observed_process[0]["hash"]
        )
        statement.references.references = [
            r for r in statement.references if r.hash != remove
        ]
        item.write()
    result = inspect(case, request_for(case))
    assert (
        "missing_receipt" if fault == "missing" else "changed_support"
    ) in codes(result)
    assert len(result.impacts) == 1


@pytest.mark.parametrize("fault", ["assignment", "binding"])
def test_stale_requested_authority_is_reported_without_rebinding(
    case, tmp_path, fault
):
    apply(case, approved(case), tmp_path)
    request = request_for(case)
    if fault == "assignment":
        request = request.model_copy(
            update={
                "assignments": (
                    request.assignments[0].model_copy(
                        update={"revision_id": "f" * 64}
                    ),
                )
            }
        )
    else:
        request = request.model_copy(
            update={
                "source": request.source.model_copy(
                    update={"receipt_sha256": "f" * 64}
                )
            }
        )
    result = inspect(case, request)
    assert (
        "stale_assignment" if fault == "assignment" else "stale_binding"
    ) in codes(result)


def test_edited_application_catalog_is_not_reported_as_complete(
    case, tmp_path
):
    apply(case, approved(case), tmp_path)
    title = next(
        title for title in case.wiki._recovery_pages if title.endswith("/plan")
    )
    case.wiki._recovery_edited.add(title)
    result = inspect(case, request_for(case))
    assert not result.catalog_complete
    assert "unavailable_authority" in codes(result)


def test_graph_revision_change_during_inspection_is_reported(
    case, tmp_path, monkeypatch
):
    apply(case, approved(case), tmp_path)
    request = request_for(case)
    original = case.wiki.wbi.item.get
    catalog = case.registry.application_catalog
    captures = 0

    def enumerate_plans():
        nonlocal captures
        captures += 1
        return catalog()

    def changed(qid):
        item = original(qid)
        if qid == request.source.binding.qid and captures > 1:
            item.lastrevid += 1
        return item

    monkeypatch.setattr(case.registry, "application_catalog", enumerate_plans)
    monkeypatch.setattr(case.wiki.wbi.item, "get", changed)
    result = inspect(case, request)
    assert "changed_graph" in codes(result)


@pytest.mark.parametrize(
    "fault",
    ["same_target", "empty", "duplicate", "wrong_source", "blank_reason"],
)
def test_malformed_correction_request_refuses(case, fault):
    request = request_for(case)
    raw = request.model_dump(mode="json")
    if fault == "same_target":
        raw["target"] = raw["source"]
    elif fault == "empty":
        raw["assignments"] = []
    elif fault == "duplicate":
        raw["assignments"] *= 2
    elif fault == "wrong_source":
        raw["assignments"][0]["entity_id"] = raw["target"]["binding"][
            "entity_id"
        ]
    else:
        raw["reason"] = "   "
    before = snapshot(case)
    with pytest.raises(ValueError):
        CorrectionRequest.model_validate(raw)
    assert snapshot(case) == before


def test_receipt_published_during_capture_changes_authority(
    case, tmp_path, monkeypatch
):
    pending = []

    def interrupt_receipt(registry, receipt):
        pending.append(receipt)
        raise OSError("fictional interruption before receipt publication")

    with monkeypatch.context() as patch:
        patch.setattr(
            CreationRegistry, "record_application_receipt", interrupt_receipt
        )
        with pytest.raises(ApplicationInterrupted):
            apply(case, approved(case), tmp_path)
    (receipt,) = pending
    assert (
        case.registry.application_receipt(receipt.plan_id, receipt.claim_id)
        is None
    )
    request = request_for(case)
    before_pages, before_entities = snapshot(case)
    catalog = case.registry.application_catalog
    observations = []

    def concurrent_receipt():
        if observations:
            # Another caller completes recovery after this planner has read
            # the absent receipt, without changing the historical statement.
            CreationRegistry(case.wiki).record_application_receipt(receipt)
        observed = catalog()
        observations.append(observed)
        return observed

    monkeypatch.setattr(
        case.registry, "application_catalog", concurrent_receipt
    )
    report = plan_correction(case.registry, request)
    assert len(observations) >= 2
    assert observations[0] == observations[-1]
    assert not report.catalog_complete
    assert "changed_authority" in {b.code for b in report.blockers}
    (impact,) = report.impacts
    assert impact.receipt is None and impact.observed.complete
    assert (
        case.registry.application_receipt(receipt.plan_id, receipt.claim_id)
        == receipt
    )
    after_pages, after_entities = snapshot(case)
    assert after_entities == before_entities
    assert all(after_pages[key] == page for key, page in before_pages.items())
    (published,) = after_pages.keys() - before_pages.keys()
    assert published.endswith(f"/{receipt.claim_id}/receipt")
