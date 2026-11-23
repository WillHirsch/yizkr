"""Offline acceptance of attributed evidence and explicit face approval.

Gustav's contradictory birth dates pass through durable identity review,
claim decisions and reviewed claim application over the in-memory Wiki of
tests/unit/test_identity_provenance.py, and the assertions inspect the
statements and references of its stored items. A face proposal cannot enter
claim review: it names its photograph and an enrolled person by QID and
locates its evidence by image region, with no excerpt (ADR-0005). The face
refusal case therefore checks only the depicts gates of claim decisions and
claim application, on a face claim put in place of an approved birth claim;
it is not a route for face claims. Only
the face confirmed-write test still drives the review sheet and BatchWriter
directly, over FakeWiki from test_pipeline, whose detached items retain the
emitted WBI JSON. These checks establish transformation and gate behavior,
not server acceptance, query results or model/detector quality. Known
fictional QIDs keep identity matching outside the face scenario.
"""

from __future__ import annotations

import copy
import csv
from dataclasses import replace

import pytest
from wikibaseintegrator.datatypes import Item

from tests.fixtures import example_universe as universe
from tests.fixtures.semantic_acceptance import (
    FACE_ARTIFACT_QID,
    FACE_PEOPLE,
    FACE_PERSON_QIDS,
    FACE_SOURCE,
    birth_review,
    source_case,
)
from tests.unit.test_identity_provenance import Wiki
from tests.unit.test_pipeline import ITEMS, PROPERTIES, FakeWiki
from yizkr.claims import ItemValue, QidRef, RegionPercent, StringValue
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.assignment_writes import apply_reviewed_claims
from yizkr.pipeline.claim_review import (
    ClaimDecisionInput,
    ClaimSelection,
    prepare_claim_review,
    record_claim_decisions,
)
from yizkr.pipeline.faces import (
    DetectedFace,
    DetectionResult,
    Gallery,
    PixelBox,
    proposals_to_batch,
)
from yizkr.pipeline.identity_review import (
    ClaimDecision,
    ReviewRefused,
    ReviewStore,
)
from yizkr.pipeline.review import (
    apply_review,
    claim_id,
    export_review_csv,
)
from yizkr.pipeline.review_stages import prepare_review
from yizkr.pipeline.writer import BatchWriter, UnapprovedDepictsError

WHEN = "2026-01-01T12:00:00Z"
CURATOR = "fictional curator"


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


def _reference_items(statement, property_id):
    return {
        snak["datavalue"]["value"]["id"]
        for reference in statement.get("references", [])
        for snak in reference["snaks"].get(property_id, [])
    }


def _source_block(statement, stated_in):
    (source,) = [
        reference["snaks"]
        for reference in statement["references"]
        if stated_in in reference["snaks"]
    ]
    return source


def _graph(wiki):
    """The durable wiki's stored items and recovery pages."""
    return (
        {
            qid: copy.deepcopy(item.get_json())
            for qid, item in wiki.wbi.item.store.items()
        },
        {title: wiki.immutable_page(title) for title in wiki.page_titles("")},
    )


def _accepted_births(store):
    """Gustav's two mentions assigned to his item by approved review."""
    wiki = Wiki(detached=True)
    registry = CreationRegistry(wiki)
    target, request = birth_review(registry)
    for proposal in prepare_review(store, request).proposals:
        store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor=CURATOR,
            time=WHEN,
        )
    accept_reviewed_assignments(store, request, registry)
    return wiki, registry, target, request


def _prepared(store, request, registry, source):
    return prepare_claim_review(
        store,
        request,
        registry,
        selections=(
            ClaimSelection(artifact_sha1=source.artifact_sha1, index=0),
        ),
    )


def _approved(store, request, registry, source):
    """Approve the source's first claim, then prepare it with that decision."""
    prepared = _prepared(store, request, registry, source)
    record_claim_decisions(
        store,
        prepared,
        [
            ClaimDecisionInput(
                proposal_id=claim.proposal.id,
                expected_digest=claim.proposal.digest,
                outcome="approve",
                actor=CURATOR,
                time=WHEN,
            )
            for claim in prepared.claims
        ],
    )
    return _prepared(store, request, registry, source)


def _birth_evidence(wiki, qid):
    properties, _ = wiki.vocabulary()
    statements = wiki.wbi.item.get(qid).get_json()["claims"][
        properties["date_of_birth"]
    ]
    assert len(statements) == 2
    evidence = {}
    for statement in statements:
        value = statement["mainsnak"]["datavalue"]["value"]
        source = _source_block(statement, properties["stated_in"])
        (excerpt,) = source[properties["transcription_excerpt"]]
        (page,) = source[properties["page_number"]]
        evidence[value["time"]] = {
            "precision": value["precision"],
            "rank": statement["rank"],
            "sources": _reference_items(statement, properties["stated_in"]),
            "processes": _reference_items(
                statement, properties["inferred_from_extraction_run"]
            ),
            "page": page["datavalue"]["value"],
            "excerpt": excerpt["datavalue"]["value"],
        }
    return evidence


def test_conflicting_birth_dates_keep_their_attribution_after_review(tmp_path):
    records = tmp_path / "records"
    dates = {
        "birth-extract-1938": "+1919-01-07T00:00:00Z",
        "naturalisation-1952": "+1919-07-01T00:00:00Z",
    }
    with ReviewStore(tmp_path / "review.sqlite") as store:
        wiki, registry, target, request = _accepted_births(store)
        sources = {source.artifact_sha1: source for source in request.sources}
        expected = {}
        applied = []
        # Each source's claim is approved and applied on its own.
        for key, date in dates.items():
            case = source_case(key)
            source = sources[case.sha1]
            approved = _approved(store, request, registry, source)
            applied.append(
                (
                    approved,
                    apply_reviewed_claims(
                        store, approved, wiki, record_dir=records
                    ),
                )
            )
            excerpt = source.batch.claims[0].source.transcription_excerpt
            assert excerpt.text in case.text
            expected[date] = {
                # Wikibase day precision, not a chosen winner.
                "precision": 11,
                "rank": "normal",
                "sources": {approved.artifact_qids[source.artifact_sha1]},
                "page": "1",
                "excerpt": {
                    "language": excerpt.language,
                    "text": excerpt.text,
                },
            }

        evidence = _birth_evidence(wiki, target.qid)
        processes = [entry.pop("processes") for entry in evidence.values()]
        assert evidence == expected
        # Each date cites a reviewed-application process item of its own,
        # not its source document or the person.
        properties, items = wiki.vocabulary()
        documents = {target.qid}.union(
            *(review.artifact_qids.values() for review, _ in applied)
        )
        for process in processes:
            (qid,) = process
            assert qid not in documents
            claims = wiki.wbi.item.get(qid).get_json()["claims"]
            classes = [
                statement["mainsnak"]["datavalue"]["value"]["id"]
                for statement in claims.get(properties["instance_of"], [])
            ]
            kinds = [
                statement["mainsnak"]["datavalue"]["value"]
                for statement in claims.get(properties["process_kind"], [])
            ]
            assert classes == [items["extraction_run"]]
            assert kinds == ["reviewed-claim-application"]
        assert processes[0] != processes[1]

        before = _graph(wiki)
        for approved, result in applied:
            assert (
                apply_reviewed_claims(
                    store, approved, wiki, record_dir=records
                )
                == result
            )
        assert _graph(wiki) == before


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


def test_face_depicts_needs_its_confirmed_approval_to_apply(tmp_path):
    records = tmp_path / "records"
    with ReviewStore(tmp_path / "review.sqlite") as store:
        wiki, registry, _, request = _accepted_births(store)
        # The replaced birth claim is approved, so a fresh preparation passes
        # the gates and each refusal below comes from the face claim.
        prepared = _approved(store, request, registry, request.sources[0])
        (births,) = prepared.claims
        # The machine's suggestion of Wastl, in place of a reviewable claim.
        face = _face_proposals().claims[0]
        basis = {**births.basis, "claim": face.model_dump(mode="json")}
        proposal = store.propose_claim(
            basis, origin={"method": "fictional face proposal"}
        )
        undecided = replace(
            prepared,
            claims=(
                replace(
                    births,
                    claim=face,
                    basis=basis,
                    proposal=proposal,
                    decision=None,
                ),
            ),
        )
        before = _graph(wiki)

        with pytest.raises(ReviewRefused, match="explicit approval"):
            apply_reviewed_claims(store, undecided, wiki, record_dir=records)

        # A general approval does not confirm the displayed association.
        approval = ClaimDecisionInput(
            proposal_id=proposal.id,
            expected_digest=proposal.digest,
            outcome="approve",
            actor=CURATOR,
            time=WHEN,
        )
        with pytest.raises(ReviewRefused):
            record_claim_decisions(store, undecided, [approval])
        assert store.get_claim_decision(proposal.id) is None
        unconfirmed = ClaimDecision(
            id="1" * 32,
            revision=1,
            proposal=proposal,
            outcome="approve",
            actor=CURATOR,
            time=WHEN,
        )
        with pytest.raises(ReviewRefused, match="displayed association"):
            apply_reviewed_claims(
                store,
                replace(
                    undecided,
                    claims=(
                        replace(undecided.claims[0], decision=unconfirmed),
                    ),
                ),
                wiki,
                record_dir=records,
            )
        assert _graph(wiki) == before

        # Explicit confirmation of the displayed association is recorded.
        (confirmed,) = record_claim_decisions(
            store,
            undecided,
            [approval.model_copy(update={"depicts_confirmed": True})],
        )
        assert confirmed.depicts_confirmed


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
    sheet = tmp_path / "faces.csv"

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
    assert _reference_items(depicts, PROPERTIES["stated_in"]) == {
        FACE_ARTIFACT_QID
    }
    assert _reference_items(
        depicts, PROPERTIES["inferred_from_extraction_run"]
    ) == {result.run_qid}
    (located,) = _source_block(depicts, PROPERTIES["stated_in"])[
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
