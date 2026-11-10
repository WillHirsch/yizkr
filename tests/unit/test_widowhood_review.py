"""Widowhood concerns enter durable review with separate claim approval."""

from types import SimpleNamespace

import pytest
from wikibaseintegrator.datatypes import String

from tests.fixtures.widowhood import HISTORY, history_batch, source_case
from tests.unit.test_identity_provenance import Wiki
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.claim_review import (
    ClaimDecisionInput,
    ClaimSelection,
    pin_claim_review,
    prepare_claim_review,
    record_claim_decisions,
)
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewProposal,
    ReviewRequest,
    ReviewSource,
    prepare_review,
)

WHEN = "2026-01-01T12:00:00Z"
MODEL_CONCERN = "Check the interpretation of the second marriage."


@pytest.fixture
def history(tmp_path):
    source = source_case(HISTORY)
    batch = history_batch(include_misreading=True)
    # Locate the five distinct mentions in the authored narrative. The union
    # descriptions are the relationship phrases, not invented literal names.
    descriptions = {
        "caecilie": "Cäcilie",
        "first_union": "became a widower",
        "second_union": "Their marriage",
    }
    for entity in batch.entities:
        entity.mention_lines = [2]
        entity.label = descriptions.get(entity.id, entity.label)
    # This durable source is plain text, with no physical page inventory.
    for claim in batch.claims:
        claim.source.page_number = None
    wiki = Wiki(detached=True)
    registry = CreationRegistry(wiki)
    artifact = wiki.wbi.item.new()
    artifact.labels.set("en", source.title)
    artifact.claims.add(
        String(prop_nr=wiki.vocabulary()[0]["media_sha1"], value=source.sha1)
    )
    registry.create(
        {"kind": "source", "sha1": source.sha1},
        artifact,
        summary="authored widowhood source",
    )
    bindings, proposals = [], []
    for entity in batch.entities:
        target = wiki.wbi.item.new()
        target.labels.set("en", entity.label)
        binding = registry.create(
            {"kind": "fictional-widowhood-target", "name": entity.id},
            target,
            summary="authored widowhood identity",
        )
        bindings.append(binding)
        proposals.append(
            ReviewProposal(
                operation="assign",
                mentions=(
                    MentionKey(artifact_sha1=source.sha1, local_id=entity.id),
                ),
                target=binding,
            )
        )
    request = ReviewRequest(
        sources=(ReviewSource(source.sha1, batch, source.transcription()),),
        proposals=tuple(proposals),
        current_targets=tuple(bindings),
    )
    database = tmp_path / "review.sqlite"
    with ReviewStore(database) as store:
        identity = prepare_review(store, request)
        for proposal in identity.proposals:
            store.decide(
                proposal.id,
                proposal.revision,
                expected_digest=proposal.digest,
                outcome="approve",
                actor="curator",
                time=WHEN,
            )
        accepted = accept_reviewed_assignments(store, request, registry)
        assert len(accepted.snapshot.assignments) == 5
        yield SimpleNamespace(
            source=source,
            batch=batch,
            request=request,
            registry=registry,
            store=store,
            database=database,
        )


def ends(history):
    return tuple(
        ClaimSelection(
            artifact_sha1=history.source.sha1,
            index=index,
            warnings=(MODEL_CONCERN,)
            if claim.subject.local_id == "second_union"
            else (),
        )
        for index, claim in enumerate(history.batch.claims)
        if claim.property == "ended_by_death_of"
    )


def test_same_source_concerns_enter_durable_review_without_approving(history):
    prepared = prepare_claim_review(
        history.store,
        history.request,
        history.registry,
        selections=ends(history),
    )
    correct_first, correct_later, misreading = prepared.claims
    assert correct_first.selection.warnings == ()
    assert correct_first.proposal.basis["warnings"] == []
    assert len(misreading.selection.warnings) == 1
    concern = misreading.selection.warnings[0]
    assert "widowhood/death attribution" in concern
    assert correct_later.selection.warnings == (MODEL_CONCERN, concern)
    assert correct_later.proposal.basis["warnings"] == sorted(
        [MODEL_CONCERN, concern]
    )
    assert misreading.proposal.basis["warnings"] == [concern]
    for claim in prepared.claims:
        assert (
            history.store.get_claim_proposal(claim.proposal.id)
            == claim.proposal
        )
        assert claim.decision is None
    with (
        pytest.raises(ReviewRefused, match="affirmative approval"),
        pin_claim_review(history.store, prepared),
    ):
        pytest.fail(
            "accepted identities and advisory concerns are not consent"
        )


def test_approved_review_reopens_without_duplicating_generated_concerns(
    history,
):
    prepared = prepare_claim_review(
        history.store,
        history.request,
        history.registry,
        selections=ends(history),
    )
    decisions = record_claim_decisions(
        history.store,
        prepared,
        [
            ClaimDecisionInput(
                proposal_id=claim.proposal.id,
                expected_digest=claim.proposal.digest,
                outcome="approve",
                actor="curator",
                time=WHEN,
            )
            for claim in prepared.claims
        ],
    )
    # A caller may retain selections with their generated concern. Reopening
    # them must preserve the reviewed content and its separate approval.
    with ReviewStore(history.database) as reopened:
        repeated = prepare_claim_review(
            reopened,
            history.request,
            history.registry,
            selections=tuple(claim.selection for claim in prepared.claims),
        )
        assert [c.selection.warnings for c in repeated.claims] == [
            c.selection.warnings for c in prepared.claims
        ]
        assert [c.proposal for c in repeated.claims] == [
            c.proposal for c in prepared.claims
        ]
        assert tuple(c.decision for c in repeated.claims) == decisions
        with pin_claim_review(reopened, repeated):
            pass
