"""Live write path for a relative claim's kinship qualifiers (#410).

The interview's relative claim, Klara as Nicholas's aunt, lands with its
kinship type, kinship class and degree as qualifiers (ADR-0035 §3.3),
the degree a quantity kept as "+0", and a re-run finds it again. The
test writes fictional fixture data only (the universe's interview, under
test labels). Every title and label carries the session's marker
(conftest.fixture_mark), so each session meets the fixture as a first
write and a re-run within it reuses its entities.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import write_mapped
from tests.integration.conftest import fixture_mark, register_fictional
from yizkr.claims import ClaimBatch, validate_against_manifest
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#410: the interview's relative claim, Klara as Nicholas's aunt "
    "(kinship_type aunt, kinship_class kin_aunt_or_uncle, degree 0)"
)
MARK = fixture_mark(FIXTURE, writer)
STARTED = datetime.now(UTC).isoformat()

EXCERPT = (
    "Die am 8. Februar 1947 vor dem Standesbeamten in Flörburg "
    "geschlossene Ehe der Parteien wird geschieden"
)


def local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": f"{label} {MARK}",
        "instance_of": kind,
    }


def item(ref: dict) -> dict:
    return {"type": "item", "value": ref}


def batch_of(sha1: str, *claims: dict) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": STARTED,
            },
            "claims": [
                dict(
                    claim,
                    source={
                        "artifact": {"kind": "artifact", "sha1": sha1},
                        "page_number": "1",
                        "transcription_excerpt": {
                            "text": EXCERPT,
                            "language": "de",
                        },
                    },
                )
                for claim in claims
            ],
        }
    )


def statements(client, qid: str, key: str) -> list[dict]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {"action": "wbgetclaims", "entity": qid, "property": properties[key]}
    )
    return [claim for claims in data["claims"].values() for claim in claims]


def test_kinship_class_qualifiers_land_and_a_rerun_adds_nothing(client):
    """The interview's "Aunt Pünktchen": Klara, Nicholas's relative, with
    `kinship_type` aunt, class aunt or uncle, and degree 0 (ADR-0035 §3.3).
    The quantity qualifier 0 lands as "+0" and is found again on a
    re-run."""
    interview = register_fictional(
        client, f"Yizkr Notes Test Interview with Nicholas {MARK}", "artifact"
    )
    properties, items = client.vocabulary()
    klara = local("i1", "Yizkr Notes Test Klara Löbig", "human")
    nicholas = local("i2", "Yizkr Notes Test Nicholas Lobig", "human")
    batch = batch_of(
        interview.ref.sha1,
        {
            "subject": klara,
            "property": "relative",
            "value": item(nicholas),
            "qualifiers": [
                {
                    "property": "kinship_type",
                    "value": {"type": "string", "value": "aunt"},
                },
                {
                    "property": "kinship_class",
                    "value": item(
                        {"kind": "vocab", "key": "kin_aunt_or_uncle"}
                    ),
                },
                {
                    "property": "degree",
                    "value": {"type": "quantity", "amount": 0},
                },
            ],
        },
    )
    assert validate_against_manifest(batch) == []
    result = write_mapped(BatchWriter(client), batch)
    assert result.written == 1
    (statement,) = statements(client, result.created_items["i1"], "relative")
    assert (
        statement["mainsnak"]["datavalue"]["value"]["id"]
        == result.created_items["i2"]
    )
    qualifiers = statement["qualifiers"]
    (kin,) = qualifiers[properties["kinship_class"]]
    assert kin["datavalue"]["value"]["id"] == items["kin_aunt_or_uncle"]
    (degree,) = qualifiers[properties["degree"]]
    assert degree["datavalue"]["value"]["amount"] == "+0"
    (term,) = qualifiers[properties["kinship_type"]]
    assert term["datavalue"]["value"] == "aunt"
    again = write_mapped(BatchWriter(client), batch)
    assert (again.written, again.skipped) == (0, 1)
