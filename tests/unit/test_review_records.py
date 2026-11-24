"""Review records stored earlier still validate and keep their bytes.

review_records.json holds fictional records as they were stored: two mention
revisions with their source observations and an identity decision, as rows
of the review store; a claim decision, also a row; a claim role of a planned
application; and the bytes of a process record. Beside them is an extraction
batch whose claim locates its source by an image region, with the product
digest and identity basis derived from it. Each record is read back and
dumped again in the encoding it was stored in. The source-occurrence keys
are derived again from a stored observation, and the product digest and
identity basis from the batch. A change that makes one of these records
fail to validate or dump to other bytes, or that changes a key, digest or
basis derived from them, fails here before it reaches a stored review
database or registry page. A loosened validator, or a changed default that
the stored records give explicitly, does not fail here.

A review decision also needs its actor and a timezone-aware time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yizkr.claims import ClaimBatch
from yizkr.core.identity_assignments import record_json
from yizkr.core.provenance import ProcessRecord
from yizkr.pipeline.claim_review import ClaimRole
from yizkr.pipeline.identity_review import (
    ClaimDecision,
    Decision,
    MentionRevision,
    ReviewRefused,
    ReviewStore,
)
from yizkr.pipeline.review_mentions import canonical_basis, observe_mentions
from yizkr.pipeline.run_provenance import record_bytes, record_digest

STORED = json.loads(
    Path(__file__).with_name("review_records.json").read_text("utf-8")
)
REVIEWED_AT = "2026-11-24T09:30:00+01:00"


@pytest.mark.parametrize(
    "model, stored",
    [
        pytest.param(MentionRevision, STORED["mentions"][0], id="mention"),
        pytest.param(MentionRevision, STORED["mentions"][1], id="mention-2"),
        pytest.param(Decision, STORED["decision"], id="decision"),
        pytest.param(ClaimDecision, STORED["claim_decision"], id="claim"),
        pytest.param(ClaimRole, STORED["claim_role"], id="claim-role"),
    ],
)
def test_a_stored_record_validates_and_dumps_to_its_stored_bytes(
    model, stored
):
    # The review store's rows are in the encoding the registry's record
    # digests hash.
    assert record_json(model.model_validate_json(stored)) == stored


def test_a_stored_observation_keeps_its_source_occurrence_keys():
    observation = MentionRevision.model_validate_json(
        STORED["mentions"][0]
    ).observation
    # The stored reading is in decomposed Unicode and lists its native
    # placements out of their canonical order.
    assert observation.correspondence_key == STORED["correspondence_key"]
    assert observation.anchor.scope_key == STORED["scope_key"]


def test_a_stored_process_record_keeps_its_bytes_and_digest():
    stored = STORED["process_record"].encode("utf-8")
    record = ProcessRecord.model_validate_json(stored)
    assert record_bytes(record) == stored
    assert record_digest(record) == STORED["process_digest"]


def test_a_batch_keeps_its_product_digest_and_identity_basis():
    stored = STORED["batch"]
    batch = ClaimBatch.model_validate_json(stored["data"])
    observations = observe_mentions(stored["artifact"], batch)
    assert {item.product_sha256 for item in observations} == {
        stored["product_sha256"]
    }
    # The batch's excerpt is in decomposed Unicode, and its region gives
    # integral percentages as floats; the basis is in the encoding the
    # review store digests.
    basis = canonical_basis(batch, "e1", stored["stable_mentions"])
    assert record_json(basis) == stored["basis"]


@pytest.mark.parametrize(
    "actor, time",
    [
        pytest.param(" ", REVIEWED_AT, id="no-actor"),
        pytest.param("curator", "24.11.2026 09:30", id="not-iso"),
        pytest.param("curator", "2026-11-24T09:30:00", id="no-timezone"),
    ],
)
def test_a_review_decision_needs_its_actor_and_timezone(tmp_path, actor, time):
    observations = [
        MentionRevision.model_validate_json(row).observation
        for row in STORED["mentions"]
    ]
    with ReviewStore(tmp_path / "review.sqlite") as store:
        revisions = store.observe(observations)
        mention = revisions[observations[0].artifact, observations[0].local_id]
        proposal = store.propose(
            "new_entity",
            [mention],
            basis=json.loads(STORED["batch"]["basis"]),
            origin={"method": "human-proposal"},
        )

        def decide(actor, time):
            return store.decide(
                proposal.id,
                proposal.revision,
                outcome="approve",
                actor=actor,
                time=time,
                expected_digest=proposal.digest,
            )

        with pytest.raises(ReviewRefused):
            decide(actor, time)
        assert store.get_decision(proposal.id) is None
        assert decide("curator", REVIEWED_AT).time == REVIEWED_AT
