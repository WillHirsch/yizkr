"""Live pipeline flow: register -> write, idempotency, and the overwrite
regression (two values on one property must COEXIST — the second write
must never replace the first; ADR-0002)."""

from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime

import pytest

from yizkr.claims import ClaimBatch
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

ARTIFACT_TITLE = "Integration-test certificate"
PERSON_LABEL = "Integration Test Person"


def tiny_png(tmp_path):
    def chunk(tag, data):
        payload = tag + data
        return (
            struct.pack(">I", len(data))
            + payload
            + struct.pack(">I", zlib.crc32(payload))
        )

    raw = b"".join(b"\x00" + b"\x32\x32\xc8" * 4 for _ in range(4))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path = tmp_path / "integration-test-certificate.png"
    path.write_bytes(png)
    return path


def birth_batch(artifact_qid: str, year: int) -> ClaimBatch:
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
                        "artifact": {"kind": "qid", "qid": artifact_qid},
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


def test_register_creates_artifact_fragment_and_media(client, artifact):
    assert artifact.artifact_qid.startswith("Q")
    assert artifact.fragment_qids[1].startswith("Q")
    assert artifact.file_page.startswith("File:")


def test_register_is_idempotent(client, artifact):
    again = register_artifact(
        client,
        media_path=None,
        artifact_type="certificate",
        title=ARTIFACT_TITLE,
        pages=1,
    )
    assert again.created is False
    assert again.artifact_qid == artifact.artifact_qid
    assert again.fragment_qids == artifact.fragment_qids


def birth_years_on(client, qid: str) -> set[str]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties["date_of_birth"],
        }
    )
    return {
        claim["mainsnak"]["datavalue"]["value"]["time"]
        for claims in data["claims"].values()
        for claim in claims
    }


def test_conflicting_values_coexist(client, artifact):
    """Writing a second value must never delete the first (ADR-0002).

    Regression for the WikibaseIntegrator REPLACE_ALL default that once
    silently overwrote a source-attributed statement.
    """
    writer = BatchWriter(client)
    first = writer.write_batch(birth_batch(artifact.artifact_qid, 1901))
    person_qid = first.created_items["person-1"]

    writer = BatchWriter(client)  # fresh entity cache
    writer.write_batch(birth_batch(artifact.artifact_qid, 1902))

    years = birth_years_on(client, person_qid)
    assert "+1901-00-00T00:00:00Z" in years
    assert "+1902-00-00T00:00:00Z" in years


def test_write_is_idempotent_and_dual_referenced(client, artifact):
    writer = BatchWriter(client)
    result = writer.write_batch(birth_batch(artifact.artifact_qid, 1901))
    assert result.written == 0
    assert result.skipped == 1

    properties, _ = client.vocabulary()
    person_qid = result.created_items["person-1"]
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person_qid,
            "property": properties["date_of_birth"],
        }
    )
    for claims in data["claims"].values():
        for claim in claims:
            assert claim["rank"] == "normal"  # ranks default to normal
            reference_properties = {
                pid for ref in claim["references"] for pid in ref["snaks"]
            }
            assert properties["stated_in"] in reference_properties  # source ref
            assert properties["inferred_from_extraction_run"] in (
                reference_properties
            )  # process ref


def stated_in_values(client, person_qid: str, year_time: str) -> tuple[int, set[str]]:
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
    second = register_artifact(
        client,
        media_path=None,
        artifact_type="certificate",
        title=f"{ARTIFACT_TITLE} (second source)",
        description="written by tests/integration; safe to delete",
    )
    writer = BatchWriter(client)
    result = writer.write_batch(birth_batch(second.artifact_qid, 1901))
    assert result.written == 0
    # first suite run appends the reference; later runs find it present
    assert (result.referenced, result.skipped) in ((1, 0), (0, 1))
    person_qid = result.created_items["person-1"]

    count, sources = stated_in_values(client, person_qid, "+1901-00-00T00:00:00Z")
    assert count == 1  # still one statement, not a duplicate
    assert {artifact.artifact_qid, second.artifact_qid} <= sources

    # re-running the same batch is a no-op (reference already present)
    rerun = BatchWriter(client).write_batch(birth_batch(second.artifact_qid, 1901))
    assert rerun.written == 0
    assert rerun.referenced == 0
    assert rerun.skipped == 1


def test_curated_claim_carries_no_run_reference(client, artifact):
    """A curator-attributed claim (correction / living flag) is a human
    write: artifact reference only, no extraction-run reference
    (ADR-0003 — process provenance is for machine writes)."""
    batch = birth_batch(artifact.artifact_qid, 1990)
    death = batch.claims[0].model_copy(
        update={"property": "date_of_death", "curated": True}
    )
    curated_batch = batch.model_copy(update={"claims": [death]})
    writer = BatchWriter(client)
    result = writer.write_batch(curated_batch)
    person_qid = result.created_items["person-1"]

    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person_qid,
            "property": properties["date_of_death"],
        }
    )
    (claim,) = [
        claim
        for claims in data["claims"].values()
        for claim in claims
        if claim["mainsnak"]["datavalue"]["value"]["time"].startswith("+1990")
    ]
    reference_properties = {pid for ref in claim["references"] for pid in ref["snaks"]}
    assert properties["stated_in"] in reference_properties
    assert properties["inferred_from_extraction_run"] not in reference_properties


def vocabulary_batch(artifact_qid: str) -> ClaimBatch:
    """One claim per item-10 vocabulary addition (quantity, monolingual
    cause, occupation item, burial place)."""
    subject = {
        "kind": "local",
        "local_id": "person-1",
        "label": PERSON_LABEL,
        "instance_of": "human",
    }
    source = {
        "artifact": {"kind": "qid", "qid": artifact_qid},
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
                            "label": "Integration test occupation",
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
                            "label": "Integration Test Cemetery",
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
    first = writer.write_batch(vocabulary_batch(artifact.artifact_qid))
    person_qid = first.created_items["person-1"]
    occupation_qid = first.created_items["occ-1"]

    rerun = BatchWriter(client).write_batch(vocabulary_batch(artifact.artifact_qid))
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
    burial_targets = {value["id"] for value in claim_values(person_qid, "burial_place")}
    assert first.created_items["pl-1"] in burial_targets
    occupation_classes = {
        value["id"] for value in claim_values(occupation_qid, "instance_of")
    }
    assert items["occupation_class"] in occupation_classes
