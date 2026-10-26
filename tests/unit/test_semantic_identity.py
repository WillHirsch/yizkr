"""Small identity acceptance cases over authored fictional evidence.

These exercise current clustering and human-reviewed equivalence operations,
not model quality or the future durable-assignment workflow. Temporary local
IDs and input order vary without becoming part of the expected identities.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from tests.fixtures.semantic_acceptance import (
    GUSTAV_QID,
    birth_batch,
    homonym_batches,
)
from yizkr.claims import LocalRef, QidRef
from yizkr.pipeline.clustering import IdentityMap, Manifest
from yizkr.pipeline.reconcile import (
    Proposal,
    RosterEntity,
    apply_equivalences,
    export_equivalences_csv,
    read_approved_equivalences,
)


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


def record_decision(path: Path, decision: str) -> None:
    """Edit the human decision field of the displayed one-match review."""
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = list(reader)
    assert len(rows) == 1
    rows[0]["decision"] = decision
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize(
    ("local_ids", "reverse"),
    [(("person-1", "person-1"), False), (("ref-77", "record-4"), True)],
)
def test_reviewed_equivalence_preserves_both_sourced_dates(
    tmp_path, local_ids, reverse
):
    target = QidRef(qid=GUSTAV_QID)
    roster = [
        RosterEntity(qid=target.qid, label="Gustave Lobig", kind="human")
    ]
    documents = list(
        zip(
            ("birth-extract-1938", "naturalisation-1952"),
            local_ids,
            strict=True,
        )
    )
    if reverse:
        documents.reverse()
    resolved = []

    for source_key, local_id in documents:
        batch = birth_batch(source_key, subject=LocalRef(local_id=local_id))
        original = batch.model_dump(mode="json")
        sheet = tmp_path / f"{source_key}.csv"
        export_equivalences_csv(
            [
                Proposal(
                    entity_id=local_id,
                    qid=target.qid,
                    verdict="same",
                    confidence=0.99,
                    evidence="The curator must confirm this proposed match.",
                )
            ],
            batch.entities,
            roster,
            sheet,
        )

        # Neither a confident machine verdict nor rejection grants consent.
        for decision in ("", "reject"):
            if decision:
                record_decision(sheet, decision)
            unconfirmed = apply_equivalences(
                batch, read_approved_equivalences(sheet)
            )
            assert unconfirmed.model_dump(mode="json") == original

        record_decision(sheet, "approve")
        confirmed = apply_equivalences(
            batch, read_approved_equivalences(sheet)
        )
        assert all(claim.subject == target for claim in confirmed.claims)
        assert {entity.qid for entity in confirmed.entities} == {target.qid}
        assert (
            confirmed.extraction_run.model_dump(mode="json")
            == original["extraction_run"]
        )
        assert [
            claim.source.model_dump(mode="json") for claim in confirmed.claims
        ] == [claim["source"] for claim in original["claims"]]
        assert [
            claim.value.model_dump(mode="json") for claim in confirmed.claims
        ] == [claim["value"] for claim in original["claims"]]
        assert batch.model_dump(mode="json") == original
        resolved.extend(confirmed.claims)

    assert {
        (
            claim.property,
            claim.value.year,
            claim.value.month,
            claim.value.day,
        )
        for claim in resolved
    } == {("date_of_birth", 1919, 1, 7), ("date_of_birth", 1919, 7, 1)}
    assert len({claim.source.artifact.sha1 for claim in resolved}) == 2
