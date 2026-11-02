"""Structured run provenance through real writing and WDQS queries.

Metadata is authored fictional evidence, not a claim that a provider was
called. The actual operations exercised are immutable record publication,
Wikibase writes, revision synchronization, SPARQL and an identical retry.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from tests.fixtures.semantic_acceptance import birth_batch, source_case
from yizkr.claims import ArtifactRef, QidRef
from yizkr.core import sparql
from yizkr.core.provenance import (
    CodeProvenance,
    ProcessRecord,
    RecordRef,
    StageProvenance,
)
from yizkr.core.wiki import WikiClient
from yizkr.core.write_recovery import CreationRegistry
from yizkr.model.bootstrap import bootstrap_instance
from yizkr.model.schema import load_manifest
from yizkr.pipeline.places import ensure_places_run_item
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.run_provenance import read_record
from yizkr.pipeline.writer import BatchWriter

from .test_write_query import _synchronize

pytestmark = pytest.mark.live
START = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)
EXTRACTOR = "authored-fictional-extractor"
PROPOSER = "authored-fictional-place-proposer"
CODE = CodeProvenance(version="0.1.0", commit="a" * 40, dirty=False)


def _producer(stage, model):
    return ProcessRecord(
        kind="producer",
        code=CODE,
        started_at=START,
        ended_at=START + timedelta(seconds=5),
        stages=(
            StageProvenance(
                stage=stage,
                process="model",
                model=model,
                model_identifier=model,
                effort="low" if stage == "extraction" else "high",
                prompt_hashes={"authored-fixture": "b" * 64},
                config_hash="c" * 64,
                resource_hashes={"model/manifest.yaml": "d" * 64},
                semantic_version=3,
                code=CODE,
                cli_version="fictional-cli-1.0",
                started_at=START,
                ended_at=START + timedelta(seconds=5),
            ),
        ),
    )


def _entity(client, qid):
    return client.api_get(
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "info|claims",
        }
    )["entities"][qid]


def _values(snaks):
    return [snak["datavalue"]["value"] for snak in snaks]


def _record_reference(statement, pid):
    return {
        value
        for reference in statement.get("references", [])
        for value in _values(reference["snaks"].get(pid, []))
    }


def _assert_metadata(client, qid, stage_name, model, directory):
    properties, _ = client.vocabulary()
    entity = _entity(client, qid)
    statements = [
        item for group in entity["claims"].values() for item in group
    ]
    refs = [
        _record_reference(item, properties["run_record_sha256"])
        for item in statements
    ]
    assert all(len(values) == 1 for values in refs)
    assert len(set().union(*refs)) == 1
    digest = next(iter(refs[0]))
    path = directory / f"{digest}.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    record = read_record(directory, RecordRef(sha256=digest))
    producer = next(
        stage for stage in record.stages if stage.stage == stage_name
    )
    assert producer.model_identifier == model
    assert producer.code == CODE
    assert producer.ended_at == START + timedelta(seconds=5)
    # Write preparation is a snapshot, never a completed-write timing claim.
    assert record.started_at is None and record.ended_at is None
    assert not entity["claims"].get(properties["process_ended_at"])
    stages = [
        item
        for item in entity["claims"][properties["run_stage"]]
        if item["mainsnak"]["datavalue"]["value"] == stage_name
    ]
    assert len(stages) == 1
    qualifiers = stages[0]["qualifiers"]
    for key, wanted in {
        "model_identifier": model,
        "model_effort": producer.effort,
        "prompt_hash": "authored-fixture:" + "b" * 64,
        "cli_config_hash": "c" * 64,
        "provider_cli_version": "fictional-cli-1.0",
        "stage_version": "3",
        "software_commit": "a" * 40,
        "software_dirty": "false",
        "model_manifest_hash": "d" * 64,
        "process_started_at": "2026-09-29T09:00:00Z",
        "process_ended_at": "2026-09-29T09:00:05Z",
    }.items():
        assert _values(qualifiers[properties[key]]) == [wanted]
    assert _values(
        [
            item["mainsnak"]
            for item in entity["claims"][properties["unratified_run"]]
        ]
    ) == ["true"]
    assert _values(
        [
            item["mainsnak"]
            for item in entity["claims"][properties["run_manifest_hash"]]
        ]
    ) == ["e" * 64]
    return entity, digest


def _query(client, runs):
    properties, _ = client.vocabulary()
    concept = client.config.concept_uri
    query = f"""
PREFIX p: <{concept}/prop/>
PREFIX ps: <{concept}/prop/statement/>
PREFIX pq: <{concept}/prop/qualifier/>
PREFIX pr: <{concept}/prop/reference/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?run ?stage ?model ?effort ?ended ?record WHERE {{
  VALUES ?run {{ {" ".join(f"<{concept}/entity/{qid}>" for qid in runs)} }}
  ?run p:{properties["run_stage"]} ?statement .
  ?statement ps:{properties["run_stage"]} ?stage ;
    pq:{properties["model_identifier"]} ?model ;
    pq:{properties["model_effort"]} ?effort ;
    pq:{properties["process_ended_at"]} ?ended ;
    prov:wasDerivedFrom ?reference .
  ?reference pr:{properties["run_record_sha256"]} ?record .
}}
"""
    return {
        tuple(
            row[key]["value"]
            for key in ("run", "stage", "model", "effort", "ended", "record")
        )
        for row in sparql.select(client.config.sparql_url, query)
    }


def test_structured_run_records_roundtrip_without_rewriting(
    fictional_client, tmp_path
):
    client = WikiClient(fictional_client.config)
    directory = tmp_path / "process-records"
    case = source_case("birth-extract-1938")
    # This session shares its graph with the independent write/query case.
    # Distinct fixture bytes keep that case's fresh-registration assertion
    # meaningful; the added header makes no new historical assertion.
    source = tmp_path / "fictional-provenance-birth.txt"
    source.write_bytes(b"Fictional provenance fixture\n" + case.source_bytes)
    artifact = ArtifactRef.from_path(source)
    assert artifact.sha1 != case.sha1
    registered = register_artifact(
        client,
        source,
        "certificate",
        "Provenance fixture: " + case.title,
        pages=1,
    )
    assert registered.created
    # The existing round-trip case owns the session's identity map. This
    # authored person is supplied as an explicit QID, so provenance testing
    # neither reserves another map nor publishes a conflicting map label.
    person = client.wbi.item.new()
    person.labels.set("en", "Provenance fixture: Gustav Löbig")
    person = CreationRegistry(client).create(
        {"kind": "fictional-provenance-subject", "source": artifact.sha1},
        person,
        summary="fictional provenance fixture person",
    )
    batch = birth_batch(case.key, subject=QidRef(qid=person.qid))
    for claim in batch.claims:
        claim.source = claim.source.model_copy(update={"artifact": artifact})
        assert claim.source.transcription_excerpt.text in source.read_text()
    batch.extraction_run = batch.extraction_run.model_copy(
        update={
            "model_identifier": EXTRACTOR,
            "software_commit": CODE.commit,
            "started_at": START,
            "provenance": _producer("extraction", EXTRACTOR),
        }
    )
    marks = {
        "scratch_run": "fictional-provenance",
        "run_manifest_hash": "e" * 64,
    }
    result = BatchWriter(
        client, run_marks=marks, record_dir=directory
    ).write_batch(batch)
    assert result.written == 1 and not result.refused
    fingerprint = hashlib.sha256(
        b"fictional place review provenance"
    ).hexdigest()
    proposer = _producer("places", PROPOSER)
    place_run = ensure_places_run_item(
        client,
        fingerprint,
        "curator-reviewed",
        producer=proposer,
        marks=marks,
        record_dir=directory,
    )
    expected = {}
    before = {}
    for qid, stage, model in (
        (result.run_qid, "extraction", EXTRACTOR),
        (place_run, "places", PROPOSER),
    ):
        before[qid], expected[qid] = _assert_metadata(
            client, qid, stage, model, directory
        )
    _synchronize(client)
    queried = _query(client, expected)
    concept = client.config.concept_uri
    assert queried == {
        (
            f"{concept}/entity/{qid}",
            stage,
            model,
            effort,
            "2026-09-29T09:00:05Z",
            expected[qid],
        )
        for qid, stage, model, effort in (
            (result.run_qid, "extraction", EXTRACTOR, "low"),
            (place_run, "places", PROPOSER, "high"),
        )
    }
    ids = set(client.list_entity_ids())
    files = {path.name: path.read_bytes() for path in directory.iterdir()}
    client = WikiClient(client.config)
    for _ in range(2):
        counts = bootstrap_instance(
            client, load_manifest(), log=lambda *_: None
        )
        assert (
            counts["created"]
            == counts["updated"]
            == counts["statements_added"]
            == 0
        )
        client.forget_vocabulary()
    again = BatchWriter(
        client, run_marks=marks, record_dir=directory
    ).write_batch(batch)
    assert again.run_qid == result.run_qid
    assert again.written == again.referenced == 0
    assert again.minted == [] and not again.refused
    assert (
        ensure_places_run_item(
            client,
            fingerprint,
            "curator-reviewed",
            producer=proposer,
            marks=marks,
            record_dir=directory,
        )
        == place_run
    )
    assert set(client.list_entity_ids()) == ids
    assert {
        path.name: path.read_bytes() for path in directory.iterdir()
    } == files
    assert {qid: _entity(client, qid) for qid in before} == before
    _synchronize(client)
    assert _query(client, expected) == queried
