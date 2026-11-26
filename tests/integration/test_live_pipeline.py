"""Live pipeline flow over a registered certificate: a second source
agreeing with the first, whose reference joins the one statement, and the
item-10 vocabulary. Every claim cites its artifact by the SHA-1 of the
artifact's media (#102), which the writer resolves to the registered
item.

Every label, title and image carries the session's marker
(conftest.fixture_mark), so each session meets the fixture as a first
write: what an earlier session wrote, a live mutant's run included, never
stands on its items. Within the session a re-run finds its own items
again."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import corpus, write_mapped
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import registrar, writer
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter

from .conftest import fictional_png, fixture_mark, register_fictional

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = "#102: a certificate and its test person, and agreeing births"
MARK = fixture_mark(FIXTURE, writer, registrar)
ARTIFACT_TITLE = f"Integration-test certificate {MARK}"
PERSON_LABEL = f"Integration Test Person {MARK}"
# the certificate's media: bytes of the session's own
CERTIFICATE_BYTES = fictional_png(f"yizkr live test: certificate {MARK}")


def tiny_png(tmp_path):
    path = tmp_path / f"integration-test-certificate-{MARK}.png"
    path.write_bytes(CERTIFICATE_BYTES)
    return path


# the certificate the `artifact` fixture registers, by the SHA-1 of its
# media: the key its claims cite
CERTIFICATE = ArtifactRef.from_bytes(CERTIFICATE_BYTES)


def birth_batch(artifact: ArtifactRef, year: int) -> ClaimBatch:
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
                        "local_id": "person-1",
                        "label": PERSON_LABEL,
                        "instance_of": "human",
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": year},
                    "source": {
                        "artifact": artifact.model_dump(mode="json"),
                        "page_number": "1",
                        "transcription_excerpt": {
                            "text": f"born {year}",
                            "language": "en",
                        },
                    },
                }
            ],
        }
    )


@pytest.fixture(scope="module")
def artifact(client, tmp_path_factory):
    media = tiny_png(tmp_path_factory.mktemp("media"))
    return register_artifact(
        client,
        media_path=media,
        artifact_type="certificate",
        title=ARTIFACT_TITLE,
        description="written by tests/integration; safe to delete",
        pages=1,
    )


def stated_in_values(
    client, person_qid: str, year_time: str
) -> tuple[int, set[str]]:
    """(statement count for the year, set of stated-in artifact QIDs)."""
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person_qid,
            "property": properties["date_of_birth"],
        }
    )
    matching = [
        claim
        for claims in data["claims"].values()
        for claim in claims
        if claim["mainsnak"]["datavalue"]["value"]["time"] == year_time
    ]
    sources = {
        snak["datavalue"]["value"]["id"]
        for claim in matching
        for ref in claim.get("references", [])
        for snak in ref["snaks"].get(properties["stated_in"], [])
    }
    return len(matching), sources


def test_agreeing_second_source_appends_reference(client, artifact):
    """A second source asserting the SAME value must not vanish: the
    existing statement gains that source's reference block (R2 — who
    asserts a fact is knowledge), and stays one statement."""
    second = register_fictional(
        client, f"{ARTIFACT_TITLE} (second source)", "certificate"
    )
    # the identity map of the two sources, in which their person is one
    # entity (#109)
    identity = corpus(
        birth_batch(CERTIFICATE, 1901),
        birth_batch(second.ref, 1901),
        entities=(PERSON_LABEL,),
    )
    # the first source's statement, written here
    write_mapped(
        BatchWriter(client), birth_batch(CERTIFICATE, 1901), identity=identity
    )
    writer = BatchWriter(client)
    result = write_mapped(
        writer, birth_batch(second.ref, 1901), identity=identity
    )
    assert result.written == 0
    # the session's first write of the second source appends its reference
    assert (result.referenced, result.skipped) == (1, 0)
    person_qid = result.created_items["person-1"]

    count, sources = stated_in_values(
        client, person_qid, "+1901-00-00T00:00:00Z"
    )
    assert count == 1  # still one statement, not a duplicate
    assert sources == {artifact.artifact_qid, second.qid}

    # re-running the same batch is a no-op (reference already present)
    rerun = write_mapped(
        BatchWriter(client), birth_batch(second.ref, 1901), identity=identity
    )
    assert rerun.written == 0
    assert rerun.referenced == 0
    assert rerun.skipped == 1


def vocabulary_batch(artifact: ArtifactRef) -> ClaimBatch:
    """One claim per item-10 vocabulary addition (quantity, monolingual
    cause, occupation item, burial place)."""
    subject = {
        "kind": "local",
        "local_id": "person-1",
        "label": PERSON_LABEL,
        "instance_of": "human",
    }
    source = {
        "artifact": artifact.model_dump(mode="json"),
        "page_number": "1",
        "transcription_excerpt": {
            "text": "occupation, burial, cause of death, children",
            "language": "en",
        },
    }
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": [
                {
                    "subject": subject,
                    "property": "occupation",
                    "value": {
                        "type": "item",
                        "value": {
                            "kind": "local",
                            "local_id": "occ-1",
                            "label": f"Integration test occupation {MARK}",
                            "instance_of": "occupation_class",
                        },
                    },
                    "source": source,
                },
                {
                    "subject": subject,
                    "property": "burial_place",
                    "value": {
                        "type": "item",
                        "value": {
                            "kind": "local",
                            "local_id": "pl-1",
                            "label": f"Integration Test Cemetery {MARK}",
                            "instance_of": "place",
                        },
                    },
                    "source": source,
                },
                {
                    "subject": subject,
                    "property": "cause_of_death",
                    "value": {
                        "type": "monolingual",
                        "value": {
                            "text": "integration test cause",
                            "language": "en",
                        },
                    },
                    "source": source,
                },
                {
                    "subject": subject,
                    "property": "number_of_children",
                    "value": {"type": "quantity", "amount": 2},
                    "source": source,
                },
            ],
        }
    )


def test_item10_vocabulary_writes_and_is_idempotent(client, artifact):
    """Item-10 vocabulary (occupation, burial place, cause of death,
    number of children) writes end to end: the quantity datavalue and
    verbatim monolingual cause round-trip, the occupation item is an
    instance of the occupation class, and a re-run writes nothing."""
    writer = BatchWriter(client)
    first = write_mapped(writer, vocabulary_batch(CERTIFICATE))
    person_qid = first.created_items["person-1"]
    occupation_qid = first.created_items["occ-1"]

    rerun = write_mapped(BatchWriter(client), vocabulary_batch(CERTIFICATE))
    assert rerun.written == 0
    assert rerun.skipped == 4

    properties, items = client.vocabulary()

    def claim_values(entity, prop_key):
        data = client.api_get(
            {
                "action": "wbgetclaims",
                "entity": entity,
                "property": properties[prop_key],
            }
        )
        return [
            claim["mainsnak"]["datavalue"]["value"]
            for claims in data["claims"].values()
            for claim in claims
        ]

    (quantity,) = claim_values(person_qid, "number_of_children")
    assert quantity["amount"] == "+2"
    (cause,) = claim_values(person_qid, "cause_of_death")
    assert cause == {"text": "integration test cause", "language": "en"}
    burial_targets = {
        value["id"] for value in claim_values(person_qid, "burial_place")
    }
    assert first.created_items["pl-1"] in burial_targets
    occupation_classes = {
        value["id"] for value in claim_values(occupation_qid, "instance_of")
    }
    assert items["occupation_class"] in occupation_classes
