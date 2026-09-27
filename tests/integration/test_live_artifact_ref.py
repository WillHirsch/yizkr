"""Live write path for content-addressed sources (#102, ADR-0010).

A claim cites its artifact by the SHA-1 of the media, and the writer
resolves that to the item the registrar keyed on the same hash. The
tests write fictional fixture data only, under deterministic labels, so
re-runs reuse their entities.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.integration.test_live_pipeline import png_bytes
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter, UnregisteredArtifactError

pytestmark = pytest.mark.live

ARTIFACT_TITLE = "Yizkr Artifact-Ref Test Scan"
PERSON_LABEL = "Yizkr Artifact-Ref Test Person"
UNWRITTEN_LABEL = "Yizkr Artifact-Ref Unwritten Person"
# fills no other live test uses, so these bytes are this module's alone
REGISTERED_FILL = b"\x5a\x10\x9c"
UNREGISTERED_FILL = b"\x5a\x10\x9d"


def _batch(sha1: str, label: str, year: int) -> ClaimBatch:
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
                        "label": label,
                        "instance_of": "human",
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": year},
                    "source": {
                        "artifact": {"kind": "artifact", "sha1": sha1},
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
def registered(client, tmp_path_factory):
    media = tmp_path_factory.mktemp("media") / "artifact-ref-test.png"
    media.write_bytes(png_bytes(fill=REGISTERED_FILL))
    result = register_artifact(
        client,
        media_path=media,
        artifact_type="certificate",
        title=ARTIFACT_TITLE,
        description="written by tests/integration; safe to delete",
    )
    return result.artifact_qid, ArtifactRef.from_path(media)


def _stated_in(client, qid: str, year: int) -> set[str]:
    """The items cited `stated in` by `qid`'s birth statements for
    `year`."""
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties["date_of_birth"],
        }
    )
    return {
        snak["datavalue"]["value"]["id"]
        for claims in data["claims"].values()
        for claim in claims
        if claim["mainsnak"]["datavalue"]["value"]["time"].startswith(
            f"+{year}-"
        )
        for reference in claim.get("references", [])
        for snak in reference["snaks"].get(properties["stated_in"], [])
    }


def test_sha1_source_cites_the_registered_item(client, registered):
    artifact_qid, ref = registered
    result = BatchWriter(client).write_batch(
        _batch(ref.sha1, PERSON_LABEL, 1903)
    )
    person_qid = result.created_items["person-1"]
    assert _stated_in(client, person_qid, 1903) == {artifact_qid}

    # a re-run resolves to the same item and finds the statement sourced
    again = BatchWriter(client).write_batch(
        _batch(ref.sha1, PERSON_LABEL, 1903)
    )
    assert (again.written, again.skipped) == (0, 1)


def test_unregistered_media_is_refused_before_any_write(client):
    unregistered = ArtifactRef.from_bytes(png_bytes(fill=UNREGISTERED_FILL))
    with pytest.raises(UnregisteredArtifactError) as raised:
        BatchWriter(client).write_batch(
            _batch(unregistered.sha1, UNWRITTEN_LABEL, 1904)
        )
    assert raised.value.sha1s == [unregistered.sha1]
    # the subject would have been minted first: it never was
    assert client.find_item_by_label(UNWRITTEN_LABEL) is None
