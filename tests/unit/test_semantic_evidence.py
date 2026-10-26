"""Offline acceptance of attributed evidence and explicit face approval.

Review, face-proposal construction and BatchWriter are production code.
FakeWiki from test_pipeline supplies scripted vocabulary/artifact reads and
recording storage; its detached items retain the actual emitted WBI JSON.
Reusing that transport avoids another simulated Wikibase implementation.
These checks establish transformation and gate behavior, not server
acceptance, durable persistence, query results or model/detector quality.
Known fictional QIDs keep identity matching outside these scenarios.
"""

from __future__ import annotations

import csv

import pytest
from wikibaseintegrator.datatypes import Item

from tests.fixtures import example_universe as universe
from tests.fixtures.semantic_acceptance import (
    FACE_ARTIFACT_QID,
    FACE_PEOPLE,
    FACE_PERSON_QIDS,
    FACE_SOURCE,
    GUSTAV_QID,
    birth_batch,
    source_case,
)
from tests.unit.test_pipeline import ITEMS, PROPERTIES, FakeWiki
from yizkr.claims import ItemValue, QidRef, RegionPercent, StringValue
from yizkr.pipeline.faces import (
    DetectedFace,
    DetectionResult,
    Gallery,
    PixelBox,
    proposals_to_batch,
)
from yizkr.pipeline.review import (
    accept_all,
    apply_review,
    claim_id,
    export_review_csv,
)
from yizkr.pipeline.writer import BatchWriter, UnapprovedDepictsError


def _recording_wiki(artifacts, people):
    """Seed only the known sources and people supplied by each scenario."""
    wiki = FakeWiki(
        {source_case(key).sha1: qid for key, qid in artifacts.items()},
        detached=True,
    )
    labels = {
        qid: source_case(key).title for key, qid in artifacts.items()
    } | people
    for qid, label in labels.items():
        item = wiki.wbi.item.new()
        item.id = qid
        item.labels.set("en", label)
        if qid in people:
            item.claims.add(
                Item(prop_nr=PROPERTIES["instance_of"], value=ITEMS["human"])
            )
        item.write()
    wiki.wbi.item.sent.clear()
    return wiki


def _approve(batch, path, selected=None):
    """Enter decisions in an exported sheet; leave other rows undecided."""
    export_review_csv(batch, path)
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    for row in rows:
        if selected is None or row["claim_id"] in selected:
            row["decision"] = "approve"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return apply_review(batch, path)


def _emitted_statements(wiki, qid, property_key):
    """Inspect the last serialized edit for this item, not writer helpers."""
    edits = [item for item in wiki.wbi.item.sent if item["id"] == qid]
    assert edits, f"No item payload emitted for {qid}"
    return edits[-1]["claims"].get(PROPERTIES[property_key], [])


def _reference_items(statement, property_key):
    return {
        snak["datavalue"]["value"]["id"]
        for reference in statement.get("references", [])
        for snak in reference["snaks"].get(PROPERTIES[property_key], [])
    }


def _source_block(statement):
    (source,) = [
        reference["snaks"]
        for reference in statement["references"]
        if PROPERTIES["stated_in"] in reference["snaks"]
    ]
    return source


def _birth_evidence(wiki):
    statements = _emitted_statements(wiki, GUSTAV_QID, "date_of_birth")
    assert len(statements) == 2
    evidence = {}
    for statement in statements:
        value = statement["mainsnak"]["datavalue"]["value"]
        source = _source_block(statement)
        (excerpt,) = source[PROPERTIES["transcription_excerpt"]]
        (page,) = source[PROPERTIES["page_number"]]
        evidence[value["time"]] = {
            "precision": value["precision"],
            "rank": statement["rank"],
            "sources": _reference_items(statement, "stated_in"),
            "processes": _reference_items(
                statement, "inferred_from_extraction_run"
            ),
            "page": page["datavalue"]["value"],
            "excerpt": excerpt["datavalue"]["value"],
        }
    return evidence


def test_conflicting_birth_dates_keep_their_attribution_after_review(tmp_path):
    sources = {
        "birth-extract-1938": "Q7020",
        "naturalisation-1952": "Q7021",
    }
    wiki = _recording_wiki(
        sources, {GUSTAV_QID: universe.by_key()["gustav-loebig"].label}
    )
    expected = {}
    approved_batches = []
    dates = {
        "birth-extract-1938": "+1919-01-07T00:00:00Z",
        "naturalisation-1952": "+1919-07-01T00:00:00Z",
    }
    for key, artifact_qid in sources.items():
        proposed = birth_batch(key, subject=QidRef(qid=GUSTAV_QID))
        approved, warnings = _approve(proposed, tmp_path / f"{key}.csv")
        assert warnings == []
        assert len(approved.claims) == 1
        approved_batches.append(approved)
        result = BatchWriter(wiki).write_batch(approved)
        excerpt = proposed.claims[0].source.transcription_excerpt
        assert excerpt is not None
        assert excerpt.text in source_case(key).text
        expected[dates[key]] = {
            "precision": 11,  # Wikibase day precision, not a chosen winner.
            "rank": "normal",
            "sources": {artifact_qid},
            "processes": {result.run_qid},
            "page": "1",
            "excerpt": {"language": excerpt.language, "text": excerpt.text},
        }

    assert _birth_evidence(wiki) == expected
    for approved in approved_batches:
        BatchWriter(wiki).write_batch(approved)
    assert _birth_evidence(wiki) == expected


def _face_proposals():
    """Toy features for the fictional album photograph, without detection."""
    gallery = Gallery()
    faces = []
    for index, (key, qid) in enumerate(
        zip(FACE_PEOPLE, FACE_PERSON_QIDS, strict=True)
    ):
        embedding = [float(at == index) for at in range(8)]
        gallery.enroll(universe.by_key()[key].label, embedding, qid=qid)
        faces.append(
            DetectedFace(
                index=index,
                bbox=PixelBox(
                    x=100 + index * 300, y=50, width=200, height=250
                ),
                detection_score=0.99,
                embedding=embedding,
            )
        )
    proposed, unmatched = proposals_to_batch(
        DetectionResult(
            photo_file="album-photo-1906.png",
            photo_sha1=source_case(FACE_SOURCE).sha1,
            artifact_qid=FACE_ARTIFACT_QID,
            image_width=1000,
            image_height=500,
            model_identifier="fictional-toy-features",
            faces=faces,
        ),
        gallery,
    )
    assert unmatched == []
    assert len(proposed.claims) == 2
    return proposed


@pytest.mark.parametrize("changed", ["person", "region"])
def test_face_confirmation_binds_the_displayed_person_and_region(
    tmp_path, changed
):
    proposed = _face_proposals()
    wiki = _recording_wiki(
        {FACE_SOURCE: FACE_ARTIFACT_QID},
        {
            qid: universe.by_key()[key].label
            for key, qid in zip(FACE_PEOPLE, FACE_PERSON_QIDS, strict=True)
        },
    )
    with pytest.raises(UnapprovedDepictsError):
        BatchWriter(wiki).write_batch(proposed)
    assert wiki.wbi.item.sent == []

    sheet = tmp_path / "faces.csv"
    export_review_csv(proposed, sheet)
    undecided, _ = apply_review(proposed, sheet)
    assert undecided.claims == []
    with pytest.raises(ValueError):
        accept_all(sheet, tmp_path / "bulk.csv")

    # Confirm Wastl's association only; Hilde's remains a proposal.
    (wastl,) = [
        claim
        for claim in proposed.claims
        if claim.property == "depicts"
        and claim.value == ItemValue(value=QidRef(qid=FACE_PERSON_QIDS[0]))
    ]
    approved, _ = _approve(proposed, sheet, selected={claim_id(wastl)})
    assert len(approved.claims) == 1
    result = BatchWriter(wiki).write_batch(approved)
    (depicts,) = _emitted_statements(wiki, FACE_ARTIFACT_QID, "depicts")
    assert (
        depicts["mainsnak"]["datavalue"]["value"]["id"]
        == (FACE_PERSON_QIDS[0])
    )
    assert depicts["rank"] == "normal"
    (region,) = depicts["qualifiers"][
        PROPERTIES["relative_position_within_image"]
    ]
    assert region["datavalue"]["value"] == "10%,10%,20%,50%"
    assert _reference_items(depicts, "stated_in") == {FACE_ARTIFACT_QID}
    assert _reference_items(depicts, "inferred_from_extraction_run") == {
        result.run_qid
    }
    (located,) = _source_block(depicts)[
        PROPERTIES["relative_position_within_image"]
    ]
    assert located["datavalue"]["value"] == "10%,10%,20%,50%"

    altered = approved.model_copy(deep=True)
    claim = altered.claims[0]
    if changed == "person":
        claim.value = ItemValue(value=QidRef(qid=FACE_PERSON_QIDS[1]))
    else:
        claim.source.region = RegionPercent(x=15, y=10, width=20, height=50)
        (region_qualifier,) = [
            qualifier
            for qualifier in claim.qualifiers
            if qualifier.property == "relative_position_within_image"
        ]
        region_qualifier.value = StringValue(value="15%,10%,20%,50%")
    before = list(wiki.wbi.item.sent)
    with pytest.raises(UnapprovedDepictsError):
        BatchWriter(wiki).write_batch(altered)
    assert wiki.wbi.item.sent == before
