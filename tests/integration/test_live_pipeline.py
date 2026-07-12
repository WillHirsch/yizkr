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
