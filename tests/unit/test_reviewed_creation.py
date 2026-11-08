"""Explicit creation decisions survive receipt-backed initial assignments."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from wikibaseintegrator.datatypes import String

from tests.unit.test_identity_assignments import delta
from tests.unit.test_identity_provenance import Wiki
from tests.unit.test_review_stages import request as fictional_request
from yizkr.claims import Claim
from yizkr.core.identity_assignments import (
    AssignmentRefused,
    record_digest,
    validated_revisions,
)
from yizkr.core.reviewed_creation import (
    creation_review_evidence,
    reviewed_creation_operation,
)
from yizkr.core.wiki import RecoveryEvidenceError
from yizkr.core.write_recovery import (
    AmbiguousCreation,
    CreationRefused,
    CreationRegistry,
    write_existing,
)
from yizkr.pipeline.claim_review import ClaimSelection, prepare_claim_review
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.review_stages import prepare_review

WHEN = "2026-01-01T12:00:00Z"


@pytest.fixture
def creation(tmp_path):
    wiki = Wiki(detached=True)
    registry = CreationRegistry(wiki)
    registry.ensure_generation()
    item = wiki.wbi.item.new()
    item.labels.set("en", "Leopold Löbig")
    item.descriptions.set("en", "fictional reviewed person")
    request = fictional_request()
    source = request.sources[0]
    source.batch.claims.append(
        Claim.model_validate(
            {
                "subject": {"kind": "local", "local_id": "leopold"},
                "property": "birth_name",
                "value": {
                    "type": "monolingual",
                    "value": {"text": "Leopold Löbig", "language": "de"},
                },
                "source": {
                    "artifact": {"sha1": source.artifact_sha1},
                    "transcription_excerpt": {
                        "text": "Leopold Löbig.",
                        "language": "de",
                    },
                },
            }
        )
    )
    request = replace(
        request,
        proposals=(
            request.proposals[0].model_copy(
                update={
                    "evidence": {
                        "reviewed_creation": creation_review_evidence(
                            item.get_json()
                        )
                    }
                }
            ),
        ),
    )
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = prepare_review(store, request)
        proposal = prepared.proposals[0]
        decision = store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="fictional curator",
            time=WHEN,
        )
        mention = prepared.mentions[source.artifact_sha1, "leopold"]
        yield wiki, registry, store, request, decision, mention, item


def authorize(case):
    _, registry, _, _, decision, mention, item = case
    return registry.authorize_reviewed_creation(
        decision,
        mention,
        initial_payload=item.get_json(),
        origin={"method": "explicit-fictional-review"},
    )


def fulfill(case):
    _, registry, _, _, decision, mention, item = case
    authorization = authorize(case)
    binding = registry.create(
        authorization.operation, item, summary="fictional reviewed creation"
    )
    registry.index_binding(binding)
    request = delta([mention], [decision], [(mention, binding)])
    accepted = registry.append_assignments(request, expected_head=None)
    return authorization, binding, request, accepted


def test_authorization_records_operational_context_without_graph_assertions(
    creation,
):
    wiki, registry, _, _, decision, mention, _ = creation
    writes = wiki.wbi.item.writes
    authorization = authorize(creation)
    pages = dict(wiki._recovery_pages)
    assert authorization.decision == decision
    assert authorization.mention == mention
    assert authorization.context.kind == "human"
    assert authorization.context.label == "Leopold Löbig"
    assert not authorization.initial_payload["claims"]
    assert registry.reviewed_creation_binding(authorization) is None
    assert registry.reviewed_creation_authorizations() == (authorization,)
    assert registry.reviewed_creation_authorization(decision) == authorization
    assert not registry.verify_reviewed_creation(decision, "f" * 32)
    assert wiki.wbi.item.writes == writes
    assert wiki._recovery_pages == pages
    assert authorize(creation) == authorization
    assert wiki._recovery_pages == pages


def test_original_creation_decision_establishes_assignment_and_replays(
    creation,
):
    wiki, registry, _, _, decision, mention, _ = creation
    authorization, binding, request, accepted = fulfill(creation)
    assignment = accepted.assignments[mention.id]
    (reference,) = assignment.decision_references
    assert reference.id == decision.id
    assert reference.proposal_id == decision.proposal.id
    assert (
        accepted.history[0].delta.evidence.decisions[decision.id]["proposal"][
            "operation"
        ]
        == "new_entity"
    )
    assert set(request.model_dump()) == {
        "format",
        "assignments",
        "constraints",
        "evidence",
    }
    assert accepted.head == record_digest(accepted.history[0])
    original = copy.deepcopy(wiki._recovery_pages)
    reopened = CreationRegistry(copy.copy(wiki))
    assert reopened.assignment_snapshot() == accepted
    assert reopened.verify_reviewed_creation(decision, binding.entity_id)
    assert reopened.reviewed_creation_binding(authorization).qid == binding.qid
    assert reopened.append_assignments(request, expected_head=None) == accepted
    assert wiki._recovery_pages == original


def test_default_validator_still_refuses_creation_without_receipt_proof(
    creation,
):
    _, registry, _, _, decision, mention, item = creation
    authorization = authorize(creation)
    binding = registry.create(
        authorization.operation, item, summary="fictional creation"
    )
    registered = registry.index_binding(binding)
    request = delta([mention], [decision], [(mention, binding)])
    with pytest.raises(AssignmentRefused, match="affirmative creation"):
        validated_revisions(
            registry.assignment_snapshot(), request, lambda _: registered
        )


def test_creation_approval_cannot_adopt_an_unrelated_existing_entity(creation):
    wiki, registry, _, _, decision, mention, _ = creation
    authorize(creation)
    unrelated = wiki.wbi.item.new()
    unrelated.labels.set("en", "Klara Löbig")
    binding = registry.create(
        {"kind": "fictional unrelated target"}, unrelated, summary="fixture"
    )
    registry.index_binding(binding)
    with pytest.raises(AssignmentRefused, match="affirmative creation"):
        registry.append_assignments(
            delta([mention], [decision], [(mention, binding)]),
            expected_head=None,
        )


def test_authorization_cannot_be_added_after_an_unreviewed_creation(creation):
    _, registry, _, _, decision, mention, item = creation
    payload = item.get_json()
    registry.create(
        reviewed_creation_operation(decision),
        item,
        summary="unratified fixture",
    )
    with pytest.raises(CreationRefused, match="no prior review"):
        registry.authorize_reviewed_creation(
            decision, mention, initial_payload=payload
        )


def test_changed_decision_and_payload_cannot_replace_original_authorization(
    creation,
):
    wiki, registry, store, request, decision, mention, item = creation
    authorization = authorize(creation)
    payload = copy.deepcopy(item.get_json())
    payload["labels"]["en"]["value"] = "Leopold"
    with pytest.raises(ValueError, match="initial payload"):
        registry.authorize_reviewed_creation(
            decision, mention, initial_payload=payload
        )
    changed = replace(
        request,
        proposals=(
            request.proposals[0].model_copy(
                update={
                    "evidence": {
                        "reviewed_creation": creation_review_evidence(payload)
                    }
                }
            ),
        ),
    )
    proposal = prepare_review(store, changed).proposals[0]
    revised = store.decide(
        proposal.id,
        proposal.revision,
        expected_digest=proposal.digest,
        outcome="approve",
        actor="fictional curator",
        time=WHEN,
        expected_decision_revision=decision.revision,
    )
    assert reviewed_creation_operation(revised) == authorization.operation
    pages = dict(wiki._recovery_pages)
    with pytest.raises(CreationRefused, match="differs from its decision"):
        registry.authorize_reviewed_creation(
            revised, mention, initial_payload=payload
        )
    assert wiki._recovery_pages == pages


def test_reviewed_creation_rejects_historical_payload(creation):
    _, _, _, _, _, _, item = creation
    payload = item.get_json()
    payload["claims"] = {"P1": [{"fictional": "historical statement"}]}
    with pytest.raises(ValueError, match="only display"):
        creation_review_evidence(payload)


def test_another_creation_proposal_cannot_reauthorize_the_same_mention(
    creation,
):
    wiki, registry, store, request, original, mention, item = creation
    authorize(creation)
    changed = replace(
        request,
        proposals=(
            request.proposals[0].model_copy(
                update={"before": {"reason": "fictional alternative"}}
            ),
        ),
    )
    proposal = next(
        proposal
        for proposal in prepare_review(store, changed).proposals
        if proposal.id != original.proposal.id
    )
    decision = store.decide(
        proposal.id,
        proposal.revision,
        expected_digest=proposal.digest,
        outcome="approve",
        actor="fictional curator",
        time=WHEN,
    )
    pages = dict(wiki._recovery_pages)
    with pytest.raises(
        CreationRefused, match="another creation authorization"
    ):
        registry.authorize_reviewed_creation(
            decision, mention, initial_payload=item.get_json()
        )
    assert wiki._recovery_pages == pages


def test_harmless_local_renumbering_reuses_original_authorization(creation):
    wiki, registry, store, request, decision, mention, item = creation
    original = authorize(creation)
    source = request.sources[0]
    batch = source.batch.model_copy(deep=True)
    batch.entities[0].id = "renumbered"
    batch.claims[0].subject.local_id = "renumbered"
    revised = replace(
        request,
        sources=(replace(source, batch=batch),),
        proposals=(
            request.proposals[0].model_copy(
                update={
                    "mentions": (
                        request.proposals[0]
                        .mentions[0]
                        .model_copy(update={"local_id": "renumbered"}),
                    )
                }
            ),
        ),
    )
    prepared = prepare_review(store, revised)
    current = prepared.mentions[source.artifact_sha1, "renumbered"]
    assert current.id == mention.id and current.revision > mention.revision
    assert prepared.proposals[0] == decision.proposal
    pages = dict(wiki._recovery_pages)
    assert (
        registry.authorize_reviewed_creation(
            decision,
            current,
            initial_payload=item.get_json(),
            origin={"method": "refreshed execution"},
        )
        == original
    )
    assert wiki._recovery_pages == pages


def test_lost_receipt_recovers_original_creation_after_curator_rename(
    creation, monkeypatch
):
    wiki, registry, _, _, decision, mention, item = creation
    authorization = authorize(creation)
    create_page = wiki.create_page_once

    def lost_receipt(title, text, summary):
        if title.endswith("/receipt"):
            raise RuntimeError("fictional interrupted receipt")
        return create_page(title, text, summary)

    monkeypatch.setattr(wiki, "create_page_once", lost_receipt)
    with pytest.raises(RuntimeError, match="interrupted receipt"):
        registry.create(
            authorization.operation, item, summary="fictional creation"
        )
    writes = wiki.wbi.item.writes
    recovered = registry.reviewed_creation_binding(authorization)
    with pytest.raises(CreationRefused):
        registry.verify_reviewed_creation(decision, recovered.entity_id)
    renamed = wiki.wbi.item.get(recovered.qid)
    renamed.labels.set("en", "Leopold")
    write_existing(renamed, summary="fictional curator rename")
    monkeypatch.setattr(wiki, "create_page_once", create_page)
    binding = registry.create(
        authorization.operation, item, summary="fictional retry"
    )
    assert binding.qid == recovered.qid
    assert wiki.wbi.item.writes == writes + 1
    assert wiki.wbi.item.get(binding.qid).labels.get("en") == "Leopold"
    registry.index_binding(binding)
    accepted = registry.append_assignments(
        delta([mention], [decision], [(mention, binding)]), expected_head=None
    )
    assert accepted.assignments[mention.id].entity_id == recovered.entity_id
    assert (
        registry.reviewed_creation_authorizations()[0].context.label
        == "Leopold Löbig"
    )


def test_ambiguous_dispatch_stays_unresolved_and_is_not_resent(creation):
    _, registry, _, _, _, _, item = creation
    authorization = authorize(creation)
    attempts = []

    def uncertain(**kwargs):
        attempts.append(kwargs)
        raise RuntimeError("fictional uncertain transport")

    item.write = uncertain
    for _ in range(2):
        with pytest.raises(AmbiguousCreation):
            registry.create(
                authorization.operation, item, summary="fictional creation"
            )
    assert len(attempts) == 1
    with pytest.raises(AmbiguousCreation):
        registry.reviewed_creation_binding(authorization)
    assert registry.reviewed_creation_authorizations() == (authorization,)


def test_altered_authorization_cannot_support_assignment_replay(creation):
    wiki, registry, _, _, _, _, _ = creation
    fulfill(creation)
    title = next(
        t for t in wiki._recovery_pages if "/reviewed-creations/" in t
    )
    wiki._recovery_edited.add(title)
    with pytest.raises((CreationRefused, RecoveryEvidenceError)):
        registry.assignment_snapshot()


def test_fulfilled_creation_supports_ordinary_claim_review(creation):
    wiki, registry, store, request, _, mention, _ = creation
    _, binding, _, accepted = fulfill(creation)
    source = request.sources[0]
    document = wiki.wbi.item.new()
    document.labels.set("en", "Fictional family names")
    document.claims.add(
        String(
            prop_nr=wiki.vocabulary()[0]["media_sha1"],
            value=source.artifact_sha1,
        )
    )
    registry.create(
        {"kind": "source", "sha1": source.artifact_sha1},
        document,
        summary="fixture source",
    )
    prepared = prepare_claim_review(
        store,
        request,
        registry,
        selections=(
            ClaimSelection(artifact_sha1=source.artifact_sha1, index=0),
        ),
    )
    assert prepared.claims[0].decision is None
    assert (
        prepared.claims[0].roles[0].assignment
        == accepted.assignments[mention.id]
    )
    assert (
        prepared.claims[0].roles[0].binding.binding.entity_id
        == binding.entity_id
    )
