"""Live write path for a claim whose subject is its own document (#407,
ADR-0035 §3.4).

`issued_by` takes the source document itself as its subject, named by the
SHA-1 of its media as the claim's source is. The writer resolves it to the
artifact item the registrar keyed on that hash and writes the statement
there, with both references (ADR-0003), and a re-run adds nothing. The
tests write fictional fixture data only (the universe's Standesamt
Flörburg, under a test label), so re-runs reuse their entities.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.integration.conftest import register_fictional
from yizkr.claims import ClaimBatch, validate_against_manifest
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

TITLE = "Yizkr Issued-By Test Birth Register Extract"
OFFICE = "Yizkr Issued-By Test Standesamt Flörburg"


def _batch(sha1: str) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": [
                {
                    "subject": {"kind": "artifact", "sha1": sha1},
                    "property": "issued_by",
                    "value": {
                        "type": "item",
                        "value": {
                            "kind": "local",
                            "local_id": "e1",
                            "label": OFFICE,
                        },
                    },
                    "source": {
                        "artifact": {"kind": "artifact", "sha1": sha1},
                        "page_number": "1",
                        "transcription_excerpt": {
                            "text": "Standesamt Flörburg.",
                            "language": "de",
                        },
                    },
                }
            ],
            "entities": [
                {"id": "e1", "label": OFFICE, "kind": "organization"}
            ],
        }
    )


def _issued_by(client, qid: str) -> list[dict]:
    """`qid`'s issued-by statements, as the instance returns them."""
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties["issued_by"],
        }
    )
    return [claim for claims in data["claims"].values() for claim in claims]


def _cited(client, statement: dict, key: str) -> set[str]:
    properties, _ = client.vocabulary()
    return {
        snak["datavalue"]["value"]["id"]
        for reference in statement.get("references", [])
        for snak in reference["snaks"].get(properties[key], [])
    }


def test_a_document_subject_is_written_on_its_artifact_item(client):
    extract = register_fictional(client, TITLE, "certificate")
    batch = _batch(extract.ref.sha1)
    assert validate_against_manifest(batch) == []
    result = BatchWriter(client).write_batch(batch)
    # written, or found written by an earlier session on this instance
    assert result.written + result.skipped == 1
    office = result.created_items["e1"]
    (statement,) = _issued_by(client, extract.qid)
    assert statement["mainsnak"]["datavalue"]["value"]["id"] == office
    assert statement["rank"] == "normal"
    assert _cited(client, statement, "stated_in") == {extract.qid}
    runs = _cited(client, statement, "inferred_from_extraction_run")
    assert runs
    if result.written:
        assert runs == {result.run_qid}
    # the statement is the document's, never the office's
    assert _issued_by(client, office) == []

    # a re-run resolves the document to the same item and finds the
    # statement there, sourced
    again = BatchWriter(client).write_batch(_batch(extract.ref.sha1))
    assert (again.written, again.skipped) == (0, 1)
    assert len(_issued_by(client, extract.qid)) == 1
