"""Small identity acceptance cases over authored fictional evidence.

The homonym case exercises corpus identity construction. The approved-match
case exercises durable identity review, assignment acceptance and claim
review, over the in-memory Wiki of tests/unit/test_identity_provenance.py,
whose recovery pages hold the assignment authority. Temporary local IDs and
input order vary without becoming part of the expected identities. None of
these cases measures model quality.
"""

from __future__ import annotations

import pytest

from tests.fixtures.semantic_acceptance import birth_review, homonym_batches
from tests.unit.test_identity_provenance import Wiki
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.claim_review import (
    ClaimRefused,
    ClaimSelection,
    prepare_claim_review,
)
from yizkr.pipeline.clustering import IdentityMap, Manifest
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.review_stages import prepare_review

WHEN = "2026-01-01T12:00:00Z"


@pytest.mark.parametrize(
    ("local_ids", "reverse"),
    [(("person-1", "person-1"), False), (("ref-77", "record-4"), True)],
)
def test_distinct_ursula_homonyms_stay_apart(local_ids, reverse):
    daughter, widow = homonym_batches(local_ids=local_ids)
    batches = [daughter, widow]
    if reverse:
        batches.reverse()
    identity = IdentityMap.of(
        [
            Manifest(batch.claims[0].source.artifact.sha1, batch)
            for batch in batches
        ]
    )

    clusters = []
    for batch, local_id in zip((daughter, widow), local_ids, strict=True):
        _, assignments = identity.clusters_of(
            batch, [batch.claims[0].source.artifact.sha1]
        )
        clusters.append(assignments[local_id])

    # The two source-local Ursulas are distinct people despite their names.
    # Their cluster keys and the algorithm's uncertainty categories can change.
    assert clusters[0] != clusters[1]


def decide(store, request, outcome):
    """Enter one curator decision on each displayed match proposal."""
    for proposal in prepare_review(store, request).proposals:
        store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome=outcome,
            actor="fictional curator",
            time=WHEN,
        )


# A rejection is a retained negative of the authority, so each outcome
# starts from an authority and a review store of its own.
@pytest.mark.parametrize("outcome", ["reject", "approve"])
@pytest.mark.parametrize(
    ("local_ids", "reverse"),
    [(("person-1", "person-1"), False), (("ref-77", "record-4"), True)],
)
def test_only_an_approved_match_assigns_both_sourced_dates(
    tmp_path, local_ids, reverse, outcome
):
    registry = CreationRegistry(Wiki(detached=True))
    target, request = birth_review(registry, local_ids, reverse=reverse)
    original = [s.batch.model_dump(mode="json") for s in request.sources]
    selections = tuple(
        ClaimSelection(artifact_sha1=source.artifact_sha1, index=0)
        for source in request.sources
    )
    with ReviewStore(tmp_path / "review.sqlite") as store:
        mentions = {
            mention.id
            for mention in prepare_review(store, request).mentions.values()
        }
        assert len(mentions) == 2
        empty = registry.assignment_snapshot()

        # A confident machine verdict alone grants no consent: acceptance
        # refuses the undecided scope and the authority is unchanged.
        with pytest.raises(ReviewRefused):
            accept_reviewed_assignments(store, request, registry)
        assert registry.assignment_snapshot() == empty

        decide(store, request, outcome)
        accepted = accept_reviewed_assignments(store, request, registry)
        if outcome == "reject":
            assert accepted.snapshot.assignments == {}
            assert set(accepted.unassigned) == mentions
            assert sorted(
                (constraint.kind, constraint.entity_id, constraint.mentions)
                for constraint in accepted.snapshot.constraints
            ) == sorted(
                ("rejected_target", target.entity_id, (mention,))
                for mention in mentions
            )
            with pytest.raises(ClaimRefused) as refused:
                prepare_claim_review(
                    store, request, registry, selections=selections
                )
            assert refused.value.code == "no_assignment"
            return

        assert accepted.unassigned == ()
        assert {
            mention: assignment.entity_id
            for mention, assignment in accepted.snapshot.assignments.items()
        } == dict.fromkeys(mentions, target.entity_id)
        prepared = prepare_claim_review(
            store, request, registry, selections=selections
        )

    assert [
        {role.binding.binding.qid for role in claim.roles}
        for claim in prepared.claims
    ] == [{target.qid}, {target.qid}]
    assert [
        claim.claim.value.model_dump(mode="json") for claim in prepared.claims
    ] == [batch["claims"][0]["value"] for batch in original]
    assert [
        claim.claim.source.model_dump(mode="json") for claim in prepared.claims
    ] == [batch["claims"][0]["source"] for batch in original]
    assert [s.batch.model_dump(mode="json") for s in request.sources] == (
        original
    )
    assert {
        (
            claim.claim.property,
            claim.claim.value.year,
            claim.claim.value.month,
            claim.claim.value.day,
        )
        for claim in prepared.claims
    } == {("date_of_birth", 1919, 1, 7), ("date_of_birth", 1919, 7, 1)}
    assert len(set(prepared.artifact_qids.values())) == 2
