"""Slide reading attribution survives real registration, writing and queries.

Authored answers and a renderer substitute isolate the graph boundary. The
production readers/extractor bind evidence before the real registrar/writer
publish it; this test does not measure model quality or Office rendering.
"""

from __future__ import annotations

import json

import pytest

from tests.fixtures.identity import write_mapped
from tests.fixtures.slide_attribution import prepare_case
from yizkr.claims import source_support_locators
from yizkr.core import sparql
from yizkr.core.provenance import RecordRef
from yizkr.pipeline.extract import ExtractionFailed
from yizkr.pipeline.registrar import Registrar
from yizkr.pipeline.run_provenance import read_record, record_digest
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance

from .test_container_sources import locators, statements
from .test_write_query import _synchronize

pytestmark = pytest.mark.live
TITLE = "Slide attribution fixture: Library Talk 2018"


def values(snaks, property):
    return [snak["datavalue"]["value"] for snak in snaks.get(property, [])]


def assert_reference(client, statement, claim, source_qid, run_qid):
    """One historical witness plus a distinct machine-process reference."""
    properties, _ = client.vocabulary()
    references = [reference["snaks"] for reference in statement["references"]]
    assert len(references) == 2
    (source,) = [ref for ref in references if properties["stated_in"] in ref]
    (process,) = [
        ref
        for ref in references
        if properties["inferred_from_extraction_run"] in ref
    ]
    assert source is not process
    assert [
        item["id"] for item in values(source, properties["stated_in"])
    ] == [source_qid]
    assert [
        item["id"]
        for item in values(process, properties["inferred_from_extraction_run"])
    ] == [run_qid]
    assert properties["stated_in"] not in process
    assert properties["inferred_from_extraction_run"] not in source
    assert values(source, properties["transcription_excerpt"]) == [
        claim.source.transcription_excerpt.model_dump()
    ]
    assert values(source, properties["page_number"]) == (
        [claim.source.page_number] if claim.source.page_number else []
    )
    assert {
        json.dumps(record, sort_keys=True)
        for record in locators(client, [statement])
    } == {
        json.dumps(json.loads(record), sort_keys=True)
        for record in source_support_locators(claim.source.support)
    }


def run_record(client, run_qid, batch, reading, directory):
    properties, _ = client.vocabulary()
    data = client.fetch_claims([run_qid])[run_qid]
    digests = {
        digest
        for group in data.values()
        for statement in group
        for reference in statement.get("references", [])
        for digest in values(
            reference["snaks"], properties["run_record_sha256"]
        )
    }
    assert len(digests) == 1
    record = read_record(directory, RecordRef(sha256=next(iter(digests))))
    producer = batch.extraction_run.provenance
    expected = {
        record_digest(origin): origin
        for origin in (producer, reading.provenance)
    }
    assert {reference.sha256 for reference in record.producer_refs} == set(
        expected
    )
    for reference in record.producer_refs:
        assert read_record(directory, reference) == expected[reference.sha256]
    assert {stage.stage for stage in record.stages} >= {
        "transcription",
        "extraction",
        "write-preparation",
    }
    stages = data[properties["run_stage"]]
    assert {
        statement["mainsnak"]["datavalue"]["value"] for statement in stages
    } >= {"transcription", "extraction", "write-preparation"}
    return record


def assertion_rows(client, excerpts, subjects):
    properties, items = client.vocabulary()
    query = render_for_instance(
        load_templates()["artifact-assertions"],
        properties,
        items,
        client.config.concept_uri,
    )
    return [
        row
        for row in sparql.select(client.config.sparql_url, query)
        if row.get("excerpt", {}).get("value") in excerpts
        and row["entity"]["value"].rsplit("/", 1)[-1] in subjects
    ]


def test_slide_readings_preserve_attribution_through_write_and_query(
    fictional_client, tmp_path, monkeypatch
):
    client = fictional_client
    records = tmp_path / "process-records"
    with prepare_case(tmp_path / "slides", monkeypatch) as case:
        assert [
            page.provenance.method for page in case.transcription.pages
        ] == [
            "vision",
            "vision",
            "slide-xml",
        ]
        assert all(
            page.provenance.method == "slide-xml" for page in case.native.pages
        )
        assert case.native.artifact == case.transcription.artifact
        assert set(case.native.pages[0].images) == {case.image_owner.sha1}
        assert (
            case.transcription.pages[0].images == [case.image_owner.sha1] * 2
        )
        assert case.transcription.pages[0].provenance.native_layout is not None
        assert case.transcription.pages[1].provenance.native_layout is None
        registration = Registrar(client)
        parent = registration.register_artifact(
            case.source, "presentation", TITLE
        )
        owners = registration.register_container_sources(
            case.sources, case.directory, title=TITLE
        )
        assert set(owners) == {case.image_owner.sha1}
        image_qid = owners[case.image_owner.sha1]
        placed = {case.transcription.artifact.sha1: set(owners)}

        def write(batch):
            reading = (
                case.transcription
                if batch is case.parent_batch
                else case.image_transcription
            )
            return write_mapped(
                BatchWriter(
                    client,
                    record_dir=records,
                    process_records=(reading.provenance,),
                ),
                batch,
                placed=placed,
            )

        parent_write = write(case.parent_batch)
        image_write = write(case.image_batch)
        assert (parent_write.written, image_write.written) == (4, 1)
        assert not parent_write.refused and not image_write.refused
        assert parent_write.minted == []
        (image_claim,) = case.image_batch.claims
        person = image_write.created_items[image_claim.subject.local_id]
        address = image_write.created_items[image_claim.value.value.local_id]

        caption_statements = statements(client, image_qid, "caption")
        assert len(caption_statements) == 4
        by_caption = {
            statement["mainsnak"]["datavalue"]["value"]["text"]: statement
            for statement in caption_statements
        }
        for claim in case.parent_batch.claims:
            assert_reference(
                client,
                by_caption[claim.value.value.text],
                claim,
                parent.artifact_qid,
                parent_write.run_qid,
            )
        assert statements(client, parent.artifact_qid, "caption") == []
        (residence,) = statements(client, person, "residence")
        assert residence["mainsnak"]["datavalue"]["value"]["id"] == address
        assert_reference(
            client, residence, image_claim, image_qid, image_write.run_qid
        )
        expected_placements = {
            p.id
            for p in case.sources.sources[case.image_owner.sha1].placements
            if p.occurrence == "paint"
        }
        assert len(expected_placements) == 2
        assert {
            record["placement"] for record in locators(client, [residence])
        } == expected_placements
        assert {
            record["placement"]
            for record in locators(
                client, statements(client, image_qid, "part_of")
            )
            if record["container"] == case.transcription.artifact.sha1
        } == expected_placements
        assert {
            claim.source.support.locator.kind
            for claim in case.parent_batch.claims
        } == {"slide_body", "speaker_notes"}
        assert {
            claim.source.support.locator.position
            for claim in case.parent_batch.claims
        } == {1, 2, 3}

        parent_process = run_record(
            client,
            parent_write.run_qid,
            case.parent_batch,
            case.transcription,
            records,
        )
        run_record(
            client,
            image_write.run_qid,
            case.image_batch,
            case.image_transcription,
            records,
        )
        prompts = {
            key
            for stage in parent_process.stages
            for key in stage.prompt_hashes
        }
        assert "pipeline.slide_layout_reading.NATIVE_LAYOUT_PROMPT" in prompts
        assert "pipeline.transcribe.TRANSCRIPTION_PROMPT" in prompts
        assert {
            stage.model_identifier
            for stage in parent_process.stages
            if stage.model_identifier
        } == {
            "authored-slide-attribution-reading",
            "authored-slide-attribution-extraction",
        }

        tracked = {
            parent.artifact_qid,
            image_qid,
            person,
            address,
            parent_write.run_qid,
            image_write.run_qid,
        }
        before = client.fetch_claims(sorted(tracked))
        ids = set(client.list_entity_ids())
        files = {path.name: path.read_bytes() for path in records.iterdir()}
        again_parent = Registrar(client).register_artifact(
            case.source, "presentation", TITLE
        )
        assert again_parent.artifact_qid == parent.artifact_qid
        assert (
            Registrar(client).register_container_sources(
                case.sources, case.directory, title=TITLE
            )
            == owners
        )
        for batch, first in (
            (case.parent_batch, parent_write),
            (case.image_batch, image_write),
        ):
            again = write(batch)
            assert again.run_qid == first.run_qid
            assert again.written == again.referenced == 0
            assert again.minted == [] and not again.refused
        with pytest.raises(ValueError, match="excluded|contributing image"):
            case.refuse_excluded()
        with pytest.raises(ExtractionFailed):
            write(case.refuse_ambiguous())
        assert client.fetch_claims(sorted(tracked)) == before
        assert set(client.list_entity_ids()) == ids
        assert {
            path.name: path.read_bytes() for path in records.iterdir()
        } == files

        # Synchronize only once, after both successful writes and replay.
        # The production query must not fold independently owned images into
        # their containing presentation, or treat two placements as witnesses.
        _synchronize(client)
        excerpts = {
            claim.source.transcription_excerpt.text
            for batch in (case.parent_batch, case.image_batch)
            for claim in batch.claims
        }
        rows = assertion_rows(client, excerpts, {image_qid, person})
        uri = client.config.concept_uri + "/entity/"
        expected = {
            (
                uri + parent.artifact_qid,
                uri + parent.artifact_qid,
                uri + image_qid,
                claim.source.transcription_excerpt.text,
                claim.source.page_number,
            )
            for claim in case.parent_batch.claims
        } | {
            (
                uri + image_qid,
                uri + image_qid,
                uri + person,
                image_claim.source.transcription_excerpt.text,
                image_claim.source.page_number,
            )
        }
        assert len(rows) == len(expected) == 5
        assert len({row["statement"]["value"] for row in rows}) == 5
        assert {
            tuple(
                row.get(key, {}).get("value")
                for key in ("artifact", "source", "entity", "excerpt", "page")
            )
            for row in rows
        } == expected
