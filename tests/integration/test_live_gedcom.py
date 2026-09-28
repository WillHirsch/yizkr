"""Live write path for the GEDCOM adapter's SOUR/OBJE handling (ADR-0021).

A source citation (SOUR) must fold into the birth statement's reference
as a page-number locator, and a multimedia object (OBJE) must become an
un-held media item the person `image`s — an ordinary `artifact` with NO
content hash (its bytes are referenced, never fetched). Both flow through
the generic writer unchanged; this proves they land in the graph. Every
claim cites the export by the SHA-1 of its bytes (#102)."""

from __future__ import annotations

import pytest

from yizkr.claims import ArtifactRef
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

GEDCOM = """\
0 HEAD
1 CHAR UTF-8
0 @S1@ SOUR
1 TITL Integration birth register
0 @O1@ OBJE
1 FILE https://example.test/portrait.jpg
2 FORM jpg
2 TITL Integration studio portrait
1 DATE 1956
0 @I1@ INDI
1 NAME Gedcom Live /Tester/
1 OBJE @O1@
1 BIRT
2 DATE 1950
2 SOUR @S1@
3 PAGE Volume 1, Page 1
0 TRLR
"""


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
    # resolved to the export's item. Only the statement this export gives
    # counts: an instance may hold the statements of earlier versions of
    # the fixture's export (another birth year, #247), each stated in the
    # item of its own bytes
    this_export = [
        claim
        for claims in dob["claims"].values()
        for claim in claims
        if claim["mainsnak"]["datavalue"]["value"]["time"]
        == "+1950-00-00T00:00:00Z"
    ]
    stated_in = {
        snak["datavalue"]["value"]["id"]
        for claim in this_export
        for ref in claim.get("references", [])
        for snak in ref["snaks"].get(properties["stated_in"], [])
    }
    assert stated_in == {export_qid}


def test_gedcom_write_is_idempotent(client, gedcom_artifact):
    """Re-adapting and re-writing the same export writes nothing new."""
    _, export = gedcom_artifact
    first = adapt_gedcom(GEDCOM, artifact=export)
    BatchWriter(client).write_batch(first.batch)
    rerun = BatchWriter(client).write_batch(
        adapt_gedcom(GEDCOM, artifact=export).batch
    )
    assert rerun.written == 0
