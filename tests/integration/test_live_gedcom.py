"""Live write path for the GEDCOM adapter's SOUR/OBJE handling (ADR-0021)
and a residence's date period (#308).

A source citation (SOUR) must fold into the birth statement's reference
as a page-number locator, and a multimedia object (OBJE) must become an
un-held media item the person `image`s — an ordinary `artifact` with NO
content hash (its bytes are referenced, never fetched). A residence's
date period (FROM x TO y) must become the residence statement's start and
end time, each with the range its own phrase states (docs/MODELING.md,
"Marriages, residences and other periods"). All flow through the generic
writer unchanged; this proves they land in the graph. Every claim cites
the export by the SHA-1 of its bytes (#102)."""

from __future__ import annotations

import hashlib
import json

import pytest

from yizkr.claims import ArtifactRef
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

# The export's text, with {marker} in each label it gives an item: the
# person's, the media object's and the place's (_marker).
TEMPLATE = """\
0 HEAD
1 CHAR UTF-8
0 @S1@ SOUR
1 TITL Integration birth register
0 @O1@ OBJE
1 FILE https://example.test/portrait.jpg
2 FORM jpg
2 TITL Integration studio portrait {marker}
1 DATE 1956
0 @I1@ INDI
1 NAME Gedcom Live /Tester {marker}/
1 OBJE @O1@
1 BIRT
2 DATE 1950
2 SOUR @S1@
3 PAGE Volume 1, Page 1
1 RESI
2 DATE FROM BET 1956 AND 1957 TO BET 1974 AND 1984
2 PLAC Integration Town {marker}
0 TRLR
"""


def _marker() -> str:
    """Six hex digits of a hash over the export's text and the batch the
    adapter makes of it, as test_live_period_dates.py's write_fixture
    marks its labels. The writer finds an item by its label, so a version
    of the fixture, or of the adapter, that gives other statements for
    the export would otherwise write onto the items an earlier version
    wrote, and see their statements beside its own. With the marker each
    version writes items of its own, and a re-run of one version finds
    its own items again. The batch is made under a fixed artifact key,
    since the export's own key is a hash of the marked text, and is
    hashed without its run details, which differ from run to run."""
    made = adapt_gedcom(TEMPLATE, artifact=ArtifactRef.from_bytes(b"")).batch
    content = made.model_dump(mode="json", exclude={"extraction_run"})
    return hashlib.sha256(
        (TEMPLATE + json.dumps(content, sort_keys=True)).encode()
    ).hexdigest()[:6]


GEDCOM = TEMPLATE.replace("{marker}", _marker())


@pytest.fixture(scope="module")
def gedcom_artifact(client, tmp_path_factory):
    """(the export's item, the key its claims cite it by)."""
    path = tmp_path_factory.mktemp("gedcom") / "integration-tree.ged"
    path.write_text(GEDCOM)
    registered = register_artifact(
        client,
        media_path=path,
        artifact_type="genealogical_report",
        title="Integration GEDCOM tree",
        description="written by tests/integration; safe to delete",
    )
    return registered.artifact_qid, ArtifactRef.from_path(path)


def _claim_values(client, entity_qid, prop_key):
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": entity_qid,
            "property": properties[prop_key],
        }
    )
    return [
        claim["mainsnak"]["datavalue"]["value"]
        for claims in data["claims"].values()
        for claim in claims
    ]


def test_gedcom_sour_folds_and_obje_becomes_unheld_media(
    client, gedcom_artifact
):
    export_qid, export = gedcom_artifact
    result = adapt_gedcom(GEDCOM, artifact=export)
    write = BatchWriter(client).write_batch(result.batch)
    person_qid = write.created_items["i1"]
    media_qid = write.created_items["md1"]
    properties, items = client.vocabulary()

    # OBJE -> an un-held media item the person `image`s, typed `artifact`
    image_targets = {
        v["id"] for v in _claim_values(client, person_qid, "image")
    }
    assert media_qid in image_targets
    media_classes = {
        v["id"] for v in _claim_values(client, media_qid, "instance_of")
    }
    assert items["artifact"] in media_classes
    # un-held: no content hash was written (bytes are not held)
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": media_qid,
            "property": properties["media_sha1"],
        }
    )
    assert not data["claims"]

    # SOUR -> the birth statement's reference gains the PAGE as a locator
    dob = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person_qid,
            "property": properties["date_of_birth"],
        }
    )
    pages = [
        snak["datavalue"]["value"]
        for claims in dob["claims"].values()
        for claim in claims
        for ref in claim.get("references", [])
        for snak in ref["snaks"].get(properties["page_number"], [])
    ]
    assert any("Volume 1" in page for page in pages)
    # the claim cited the export by its bytes' SHA-1, which the writer
    # resolved to the export's item. The person's label carries this
    # version's marker, so each birth statement the person holds is this
    # export's
    stated_in = {
        snak["datavalue"]["value"]["id"]
        for claims in dob["claims"].values()
        for claim in claims
        for ref in claim.get("references", [])
        for snak in ref["snaks"].get(properties["stated_in"], [])
    }
    assert stated_in == {export_qid}


def test_gedcom_residence_period_is_a_start_and_an_end(
    client, gedcom_artifact
):
    """FROM BET 1956 AND 1957 TO BET 1974 AND 1984 on a RESI: a start
    between 1956 and 1957 and an end between 1974 and 1984, each end's
    range in its own pair of qualifiers, and no point in time."""
    _, export = gedcom_artifact
    write = BatchWriter(client).write_batch(
        adapt_gedcom(GEDCOM, artifact=export).batch
    )
    person_qid = write.created_items["i1"]
    place_qid = write.created_items["pl1"]
    properties, _ = client.vocabulary()
    key_of = {pid: key for key, pid in properties.items()}
    found = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person_qid,
            "property": properties["residence"],
        }
    )
    (residence,) = [
        statement
        for statements in found["claims"].values()
        for statement in statements
    ]
    assert residence["mainsnak"]["datavalue"]["value"]["id"] == place_qid
    written = {
        key_of[pid]: [
            (
                snak["datavalue"]["value"]["time"],
                snak["datavalue"]["value"]["precision"],
            )
            for snak in snaks
        ]
        for pid, snaks in residence.get("qualifiers", {}).items()
    }
    year = 9  # Wikibase's precision for a year
    assert written == {
        "start_time": [("+1956-00-00T00:00:00Z", year)],
        "earliest_date": [("+1956-00-00T00:00:00Z", year)],
        "latest_start_date": [("+1957-00-00T00:00:00Z", year)],
        "end_time": [("+1974-00-00T00:00:00Z", year)],
        "earliest_end_date": [("+1974-00-00T00:00:00Z", year)],
        "latest_date": [("+1984-00-00T00:00:00Z", year)],
    }


def test_gedcom_write_is_idempotent(client, gedcom_artifact):
    """Re-adapting and re-writing the same export writes nothing new."""
    _, export = gedcom_artifact
    first = adapt_gedcom(GEDCOM, artifact=export)
    BatchWriter(client).write_batch(first.batch)
    rerun = BatchWriter(client).write_batch(
        adapt_gedcom(GEDCOM, artifact=export).batch
    )
    assert rerun.written == 0
