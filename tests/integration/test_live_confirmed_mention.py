"""Live write path for a document-subject claim whose mention a curator
confirms as an existing item (#670, ADR-0039 §10.1(c)).

The 1958 letter to Leopold encloses Rudolf's death certificate, held as
the scan death-certificate-1941. The letter's batch names the
certificate as an unclassified entity, and its `main_subject` → Rudolf's
death stands on that mention. A curator confirms the mention is the held
scan's item (reconcile.apply_equivalences), and the writer then writes
the claim on that item, with both references (ADR-0003), and a re-run
adds nothing. A person a curator confirmed as an item is no such
subject: its claim is refused before the first write. The tests write
fictional fixture data only, under test labels. Every title and label
carries the session's marker (conftest.fixture_mark).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import write_mapped
from tests.integration.conftest import fixture_mark, register_fictional
from yizkr import claims
from yizkr.claims import ClaimBatch, validate_against_manifest
from yizkr.pipeline import reconcile, registrar, writer
from yizkr.pipeline.reconcile import apply_equivalences
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#670: the 1958 letter's main_subject of the death certificate it "
    "encloses, on the held scan a curator confirms its mention as"
)
MARK = fixture_mark(FIXTURE, claims, reconcile, registrar, writer)
LETTER_TITLE = f"Yizkr Confirmed-Mention Test Letter 1958 {MARK}"
SCAN_TITLE = f"Yizkr Confirmed-Mention Test Death Certificate 1941 {MARK}"
DEATH = f"Yizkr Confirmed-Mention Test Death of Rudolf Löbig {MARK}"
# the refusal test's pair: a source and the item a person is confirmed as
REFUSAL_LETTER_TITLE = f"Yizkr Confirmed-Person Test Letter 1958 {MARK}"
REFUSAL_SCAN_TITLE = (
    f"Yizkr Confirmed-Person Test Death Certificate 1941 {MARK}"
)
ENCLOSING = "Вы просили прислать свидетельство о его смерти, посылаю."


def _batch(sha1: str, kind: str = "unclassified_entity") -> ClaimBatch:
    """The letter's `main_subject` of the entity u1, of `kind`."""
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
                        "kind": "local",
                        "local_id": "u1",
                        "label": "death certificate",
                    },
                    "property": "main_subject",
                    "value": {
                        "type": "item",
                        "value": {
                            "kind": "local",
                            "local_id": "d1",
                            "label": DEATH,
                        },
                    },
                    "source": {
                        "artifact": {"kind": "artifact", "sha1": sha1},
                        "transcription_excerpt": {
                            "text": ENCLOSING,
                            "language": "ru",
                        },
                    },
                }
            ],
            "entities": [
                {"id": "u1", "label": "death certificate", "kind": kind},
                {"id": "d1", "label": DEATH, "kind": "death"},
            ],
        }
    )


def _main_subjects(client, qid: str) -> list[dict]:
    """`qid`'s main-subject statements, as the instance returns them."""
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties["main_subject"],
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


def test_a_confirmed_mention_s_claim_is_written_on_the_held_scan(client):
    letter = register_fictional(client, LETTER_TITLE, "letter")
    scan = register_fictional(client, SCAN_TITLE, "certificate")
    batch = apply_equivalences(_batch(letter.ref.sha1), {"u1": scan.qid})
    assert validate_against_manifest(batch) == []
    result = write_mapped(BatchWriter(client), batch)
    assert (result.written, result.skipped, result.refused) == (1, 0, [])
    assert "u1" not in result.created_items
    death = result.created_items["d1"]
    (statement,) = _main_subjects(client, scan.qid)
    assert statement["mainsnak"]["datavalue"]["value"]["id"] == death
    assert statement["rank"] == "normal"
    assert _cited(client, statement, "stated_in") == {letter.qid}
    runs = _cited(client, statement, "inferred_from_extraction_run")
    assert runs == {result.run_qid}
    # the statement is the scan's, never the letter's
    assert _main_subjects(client, letter.qid) == []

    # a re-run finds the statement on the held scan, sourced
    again = write_mapped(
        BatchWriter(client),
        apply_equivalences(_batch(letter.ref.sha1), {"u1": scan.qid}),
    )
    assert (again.written, again.skipped) == (0, 1)
    assert len(_main_subjects(client, scan.qid)) == 1


def test_a_confirmed_person_is_no_document_subject(client):
    """The writer holds the rule itself: `yizkr write` takes a batch
    without validating it against the manifest."""
    letter = register_fictional(client, REFUSAL_LETTER_TITLE, "letter")
    scan = register_fictional(client, REFUSAL_SCAN_TITLE, "certificate")
    batch = apply_equivalences(
        _batch(letter.ref.sha1, kind="human"), {"u1": scan.qid}
    )
    assert validate_against_manifest(batch) != []
    result = write_mapped(BatchWriter(client), batch)
    (claim,) = batch.claims
    assert result.refused == [claim_id(claim)]
    assert (result.written, result.skipped) == (0, 0)
    # refused before any of its refs resolves: the death is never minted
    assert "d1" not in result.created_items
    assert _main_subjects(client, scan.qid) == []
    assert _main_subjects(client, letter.qid) == []
