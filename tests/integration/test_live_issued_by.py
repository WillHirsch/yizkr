"""Live write path for a claim whose subject is its own document (#407,
ADR-0035 §3.4).

`issued_by` takes the source document itself as its subject, named by the
SHA-1 of its media as the claim's source is. The writer resolves it to the
artifact item the registrar keyed on that hash and writes the statement
there, with both references (ADR-0003), and a re-run adds nothing. A
document subject naming another document is refused before the first
write, so neither artifact item gains a statement. The tests write
fictional fixture data only (the universe's Standesamt Flörburg, under a
test label). Every title and label carries the session's marker
(conftest.fixture_mark), so each session meets the fixture as a first
write and a re-run within it reuses its entities.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import write_mapped
from tests.integration.conftest import fixture_mark, register_fictional
from yizkr.claims import ClaimBatch, validate_against_manifest
from yizkr.pipeline import registrar, writer
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#407: a birth register extract issued by the Standesamt Flörburg, "
    "and an extract whose claim names a death certificate as its subject"
)
MARK = fixture_mark(FIXTURE, registrar, writer)
TITLE = f"Yizkr Issued-By Test Birth Register Extract {MARK}"
# the refusal test's pair: a source and the other document its claim names
SOURCE_TITLE = f"Yizkr Issued-By Refusal Test Birth Register Extract {MARK}"
OTHER_TITLE = f"Yizkr Issued-By Refusal Test Death Certificate {MARK}"
OFFICE = f"Yizkr Issued-By Test Standesamt Flörburg {MARK}"


def _batch(sha1: str, subject_sha1: str | None = None) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": [
                {
                    "subject": {
                        "kind": "artifact",
                        "sha1": subject_sha1 or sha1,
                    },
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
    result = write_mapped(BatchWriter(client), batch)
    assert (result.written, result.skipped) == (1, 0)
    office = result.created_items["e1"]
    (statement,) = _issued_by(client, extract.qid)
    assert statement["mainsnak"]["datavalue"]["value"]["id"] == office
    assert statement["rank"] == "normal"
    assert _cited(client, statement, "stated_in") == {extract.qid}
    runs = _cited(client, statement, "inferred_from_extraction_run")
    assert runs == {result.run_qid}
    # the statement is the document's, never the office's
    assert _issued_by(client, office) == []

    # a re-run resolves the document to the same item and finds the
    # statement there, sourced
    again = write_mapped(BatchWriter(client), _batch(extract.ref.sha1))
    assert (again.written, again.skipped) == (0, 1)
    assert len(_issued_by(client, extract.qid)) == 1


def test_a_subject_naming_another_document_is_refused(client):
    """The writer holds the document-subject rule itself: `yizkr write`
    takes a batch without validating it against the manifest."""
    extract = register_fictional(client, SOURCE_TITLE, "certificate")
    other = register_fictional(client, OTHER_TITLE, "certificate")
    batch = _batch(extract.ref.sha1, subject_sha1=other.ref.sha1)
    assert validate_against_manifest(batch) != []
    result = write_mapped(BatchWriter(client), batch)
    (claim,) = batch.claims
    assert result.refused == [claim_id(claim)]
    assert (result.written, result.skipped) == (0, 0)
    # refused before any of its refs resolves: the office is never minted
    assert "e1" not in result.created_items
    assert _issued_by(client, other.qid) == []
    assert _issued_by(client, extract.qid) == []
