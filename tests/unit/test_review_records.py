"""Review records stored earlier still validate and keep their bytes.

review_records.json holds fictional records as they were stored: two mention
revisions with their source observations and an identity decision, as rows
of the review store; a claim decision, also a row; a claim role of a planned
application; and the bytes of a process record. Each record is read back
and dumped again in the encoding it was stored in, and the source-occurrence
keys are derived again from a stored observation. A change to a record's
fields, validators or canonical form fails here before it reaches a stored
review database or registry page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yizkr.core.identity_assignments import record_json
from yizkr.core.provenance import ProcessRecord
from yizkr.pipeline.claim_review import ClaimRole
from yizkr.pipeline.identity_review import (
    ClaimDecision,
    Decision,
    MentionRevision,
)
from yizkr.pipeline.run_provenance import record_bytes, record_digest

STORED = json.loads(
    Path(__file__).with_name("review_records.json").read_text("utf-8")
)


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
