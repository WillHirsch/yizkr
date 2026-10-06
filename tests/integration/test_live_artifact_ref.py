"""Live write path for content-addressed sources (#102, ADR-0010).

A claim cites its artifact by the SHA-1 of the media, and the writer
resolves that to the item the registrar keyed on the same hash. The
tests write fictional fixture data only. Every label and image carries
the session's marker (conftest.fixture_mark), so each session meets the
fixture as a first write and a re-run within it reuses its entities.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import write_mapped
from tests.integration.conftest import fictional_png, fixture_mark
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import registrar, writer
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter, UnregisteredArtifactError

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#102: a registered scan and a person born 1903 citing it by its "
    "SHA-1, and an unregistered image a person born 1904 cites"
)
MARK = fixture_mark(FIXTURE, registrar, writer)
ARTIFACT_TITLE = f"Yizkr Artifact-Ref Test Scan {MARK}"
PERSON_LABEL = f"Yizkr Artifact-Ref Test Person {MARK}"
UNWRITTEN_LABEL = f"Yizkr Artifact-Ref Unwritten Person {MARK}"
REGISTERED = fictional_png(f"yizkr live test #102: registered {MARK}")
UNREGISTERED = fictional_png(f"yizkr live test #102: unregistered {MARK}")


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
    media.write_bytes(REGISTERED)
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
    result = write_mapped(
        BatchWriter(client), _batch(ref.sha1, PERSON_LABEL, 1903)
    )
    person_qid = result.created_items["person-1"]
    assert _stated_in(client, person_qid, 1903) == {artifact_qid}

    # a re-run resolves to the same item and finds the statement sourced
    again = write_mapped(
        BatchWriter(client), _batch(ref.sha1, PERSON_LABEL, 1903)
    )
    assert (again.written, again.skipped) == (0, 1)


def test_unregistered_media_is_refused_before_any_write(client):
    unregistered = ArtifactRef.from_bytes(UNREGISTERED)
    with pytest.raises(UnregisteredArtifactError) as raised:
        write_mapped(
            BatchWriter(client),
            _batch(unregistered.sha1, UNWRITTEN_LABEL, 1904),
        )
    assert raised.value.sha1s == [unregistered.sha1]
    # the subject would have been minted first: it never was
    assert client.find_item_by_label(UNWRITTEN_LABEL) is None
