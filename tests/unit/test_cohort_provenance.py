"""Explicit cohort origins remain distinct from inferred identity methods."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from wikibaseintegrator.datatypes import String

from tests.unit.test_identity_assignments import delta
from tests.unit.test_identity_provenance import Wiki, values
from tests.unit.test_review_stages import request
from tests.unit.test_run_statements import ITEMS, PROPERTIES
from yizkr.core.provenance import ProcessRecord, RecordRef, StageProvenance
from yizkr.core.reviewed_creation import creation_review_evidence
from yizkr.core.wiki import RecoveryEvidenceError
from yizkr.core.write_recovery import CreationRefused, CreationRegistry
from yizkr.pipeline import identity_provenance
from yizkr.pipeline.cohort_provenance import publish_cohort_assignment
from yizkr.pipeline.identity_provenance import IdentityProvenanceRefused
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.review_stages import ReviewProposal, prepare_review
from yizkr.pipeline.run_provenance import read_record, record_digest


def fixture(
    tmp_path,
    operation="assign",
    *,
    producer=False,
    origin_update=None,
    registered=True,
    inherited=False,
):
    wiki = Wiki(detached=True)
    registry = CreationRegistry(wiki)
    registry.ensure_generation()
    item = wiki.wbi.item.new()
    item.labels.set("en", "Leopold Löbig")
    item.descriptions.set("en", "fictional reviewed identity")
    binding = None
    if operation == "assign":
        binding = registry.create(
            {"kind": "fictional existing person"}, item, summary="fixture"
        )
        registry.index_binding(binding)
    initial = request()
    key = initial.proposals[0].mentions[0]
    origin = {
        "method": (
            "reviewed_assignment"
            if operation == "assign"
            else "reviewed_creation"
        ),
        "cohort_context_sha256": "c" * 64,
        "process": "deterministic",
    }
    original = None
    if producer:
        original = ProcessRecord(
            kind="recorded-cohort-matching",
            metadata={"producer_run_id": "fictional-recorded-matching"},
            stages=(
                StageProvenance(
                    stage="candidate-generation", process="deterministic"
                ),
            ),
        )
        origin["provenance"] = original.model_dump(mode="json")
    origin.update(origin_update or {})
    evidence = {"cohort_candidates": {"fictional_graph_sha256": "d" * 64}}
    if operation == "new_entity":
        evidence["reviewed_creation"] = creation_review_evidence(
            item.get_json()
        )
    proposals = (
        ReviewProposal(
            operation=operation,
            mentions=(key,),
            target=binding,
            origin=origin,
            evidence=evidence,
        ),
    )
    if inherited:
        other = initial.proposals[1].mentions[0]
        proposals += (ReviewProposal(operation="same", mentions=(key, other)),)
    initial = replace(
        initial,
        proposals=proposals,
        current_targets=(binding,) if binding else (),
    )
    authorization = None
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = prepare_review(store, initial)
        decisions = [
            store.decide(
                proposal.id,
                proposal.revision,
                expected_digest=proposal.digest,
                outcome="approve",
                actor="fictional curator",
                time="2026-01-01T12:00:00Z",
            )
            for proposal in prepared.proposals
        ]
        mention = prepared.mentions[key.key]
        if operation == "new_entity":
            authorization = registry.authorize_reviewed_creation(
                decisions[0], mention, initial_payload=item.get_json()
            )
            binding = registry.create(
                authorization.operation,
                item,
                summary="fictional reviewed creation",
            )
            registry.index_binding(binding)
        if inherited:
            mention = prepared.mentions[other.key]
        snapshot = registry.append_assignments(
            delta(
                list(prepared.mentions.values()),
                decisions,
                [(mention, binding)],
            ),
            expected_head=None,
        )
    if registered:
        artifact = wiki.wbi.item.new()
        artifact.labels.set("en", "Fictional family names")
        artifact.claims.add(
            String(prop_nr=PROPERTIES["media_sha1"], value=key.artifact_sha1)
        )
        artifact.write()
    return SimpleNamespace(
        wiki=wiki,
        registry=registry,
        binding=binding,
        mention=mention,
        decision=decisions[0],
        assignment=snapshot.assignments[mention.id],
        authorization=authorization,
        original=original,
        directory=tmp_path / "records",
    )


def publish(case):
    return publish_cohort_assignment(
        case.wiki, case.mention.id, record_dir=case.directory
    )


def anchor(case):
    (statement,) = case.wiki.wbi.item.get(case.binding.qid).claims.get(
        PROPERTIES["identity_record"]
    )
    return statement


@pytest.mark.parametrize("operation", ["assign", "new_entity"])
@pytest.mark.parametrize("producer", [False, True])
def test_direct_human_origin_is_repeatable_without_historical_kind(
    tmp_path, operation, producer
):
    case = fixture(tmp_path, operation, producer=producer)
    before = case.wiki.wbi.item.get(case.binding.qid).get_json()
    result = publish(case)
    assert result.changed and result.record_qid != case.binding.qid
    after = case.wiki.wbi.item.get(case.binding.qid).get_json()
    assert after["labels"] == before["labels"]
    assert after["descriptions"] == before["descriptions"]
    assert set(after["claims"]) == {PROPERTIES["identity_record"]}
    origin = next(
        r for r in anchor(case).references if values(r, "identity_method")
    )
    confirmation = next(
        r
        for r in anchor(case).references
        if values(r, "identity_confirmation_actor")
    )
    method = (
        "identity_reviewed_assignment"
        if operation == "assign"
        else "identity_reviewed_creation"
    )
    assert values(origin, "identity_method")[0]["id"] == ITEMS[method]
    assert not values(origin, "identity_confirmation_actor")
    assert not values(confirmation, "identity_method")
    assert values(confirmation, "identity_confirmation_actor") == [
        "fictional curator"
    ]
    assert values(confirmation, "identity_decision_revision") == [
        f"{case.decision.id}:{case.decision.revision}"
    ]
    (digest,) = values(origin, "run_record_sha256")
    record = read_record(case.directory, RecordRef(sha256=digest))
    retained = record.metadata["identity_cohort"]
    assert retained["assignment"] == case.assignment.model_dump(mode="json")
    assert retained["mention"] == case.mention.model_dump(mode="json")
    assert retained["confirmation"] == case.decision.model_dump(mode="json")
    assert retained["original_origin"] == case.decision.proposal.origin
    assert retained["creation_authorization"] == (
        case.authorization.model_dump(mode="json")
        if case.authorization
        else None
    )
    if producer:
        assert values(origin, "identity_origin_record_sha256") == [
            record_digest(case.original)
        ]
        assert record.stages[:-1] == case.original.stages
        assert (
            read_record(case.directory, record.producer_refs[0])
            == case.original
        )
    else:
        assert not values(origin, "identity_origin_record_sha256")
        assert not values(origin, "identity_origin_run")
        assert record.producer_refs == ()
        assert any("unavailable" in limit for limit in record.limitations)
        assert all(
            s.model_identifier is None and not s.prompt_hashes
            for s in record.stages
        )
    assert record.stages[-1].process == "manual"
    writes = case.wiki.wbi.item.writes
    assert not publish(case).changed
    assert case.wiki.wbi.item.writes == writes
    assert (
        case.registry.assignment_snapshot().assignments[case.mention.id]
        == case.assignment
    )


@pytest.mark.parametrize(
    "origin_update",
    [{"method": "singleton"}, {"cohort_context_sha256": None}],
)
def test_unknown_or_noncohort_origin_refuses_before_effects(
    tmp_path, origin_update
):
    case = fixture(tmp_path, origin_update=origin_update)
    writes = case.wiki.wbi.item.writes
    pages = dict(case.wiki._recovery_pages)
    with pytest.raises(
        IdentityProvenanceRefused, match="cohort decision origin"
    ):
        publish(case)
    assert case.wiki.wbi.item.writes == writes
    assert case.wiki._recovery_pages == pages
    assert not case.directory.exists()


def test_same_inherited_assignment_does_not_become_direct_human_origin(
    tmp_path,
):
    case = fixture(tmp_path, inherited=True)
    writes = case.wiki.wbi.item.writes
    with pytest.raises(
        IdentityProvenanceRefused, match="direct cohort decision"
    ):
        publish(case)
    assert case.wiki.wbi.item.writes == writes


def test_unregistered_source_refuses_before_graph_effects(tmp_path):
    case = fixture(tmp_path, registered=False)
    writes = case.wiki.wbi.item.writes
    pages = dict(case.wiki._recovery_pages)
    with pytest.raises(IdentityProvenanceRefused, match="source"):
        publish(case)
    assert case.wiki.wbi.item.writes == writes
    assert case.wiki._recovery_pages == pages


def test_creation_receipt_damage_refuses_publication(tmp_path):
    case = fixture(tmp_path, "new_entity")
    receipt = case.registry.binding(case.binding.entity_id).receipt_locator
    case.wiki._recovery_edited.add(receipt)
    writes = case.wiki.wbi.item.writes
    pages = dict(case.wiki._recovery_pages)
    with pytest.raises((CreationRefused, RecoveryEvidenceError)):
        publish(case)
    assert case.wiki.wbi.item.writes == writes
    assert case.wiki._recovery_pages == pages
    assert not case.directory.exists()


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_projection_reuses_record_and_exact_confirmation(
    tmp_path, monkeypatch, committed
):
    case = fixture(tmp_path, "new_entity")
    original = identity_provenance.write_existing

    def interrupt(entity, **kwargs):
        if committed:
            original(entity, **kwargs)
        raise OSError("fictional lost projection response")

    with monkeypatch.context() as patch:
        patch.setattr(identity_provenance, "write_existing", interrupt)
        with pytest.raises(OSError, match="lost projection"):
            publish(case)
    items = set(case.wiki.wbi.item.store)
    target = case.wiki.wbi.item.get(case.binding.qid)
    target.labels.set("en", "Leopold")
    target.write()
    result = publish(case)
    assert result.record_qid in items
    assert result.changed is not committed
    assert set(case.wiki.wbi.item.store) == items
    assert (
        case.wiki.wbi.item.get(case.binding.qid).labels.get("en") == "Leopold"
    )
    assert len(list(anchor(case).references)) == 2
