"""Real persistence and query acceptance for two fictional source readings.

Authored claims bypass inference, not registration, writing or querying.
Nothing uses the offline corpus fixture's stage substitutions. The two texts
are repository fixtures, and their process metadata names them as authored
fixtures rather than representing a model call that never happened.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from wikibaseintegrator.datatypes import Item, String
from wikibaseintegrator.models import Reference

from tests.fixtures.semantic_acceptance import birth_batch, source_case
from yizkr.claims import Claim, ItemValue, LocalRef, QidRef
from yizkr.core import sparql
from yizkr.core.provenance import ProcessRecord, RecordRef
from yizkr.core.write_recovery import (
    CreationRegistry,
    cluster_creation_operation,
    write_existing,
)
from yizkr.model.bootstrap import bootstrap_instance
from yizkr.model.schema import load_manifest
from yizkr.pipeline.clustering import IdentityMap, ensure_identity_map
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.run_provenance import read_record, record_digest
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance

pytestmark = pytest.mark.live
SOURCES = ("birth-extract-1938", "naturalisation-1952")
DATES = ("+1919-01-07T00:00:00Z", "+1919-07-01T00:00:00Z")
HANG_GUARD = 240
REVIEW_TIME = "2026-01-01T12:00:00Z"


class InterruptedMapOrigin(BaseException):
    """Interrupt after real metadata creation, before its response arrives."""


def _kind(client, person):
    properties, items = client.vocabulary()
    (statement,) = [
        statement
        for statement in client.fetch_claims([person])[person][
            properties["instance_of"]
        ]
        if statement["mainsnak"]["datavalue"]["value"]["id"] == items["human"]
    ]
    return statement


def _preserved_kind(before, after):
    assert {
        key: value for key, value in after.items() if key != "references"
    } == {key: value for key, value in before.items() if key != "references"}
    references = {
        reference["hash"]: reference for reference in after["references"]
    }
    assert all(
        references[reference["hash"]] == reference
        for reference in before["references"]
    )


def _map_origin(client, person, identity, directory):
    properties, _ = client.vocabulary()
    (origin,) = [
        reference["snaks"]
        for reference in _kind(client, person)["references"]
        if properties["identity_cluster"] in reference["snaks"]
    ]

    def value(key):
        (snak,) = origin[properties[key]]
        return snak["datavalue"]["value"]

    assert not set(origin) & {
        properties[key]
        for key in (
            "identity_assignment_revision",
            "identity_decision_revision",
            "identity_confirmation_actor",
            "identity_confirmation_at",
        )
    }
    producer = ProcessRecord.model_validate(identity.data["provenance"])
    digest = value("run_record_sha256")
    record = read_record(directory, RecordRef(sha256=digest))
    assert record.kind == "identity-map-origin" and record.unratified is True
    assert record.metadata["identity_map"] == identity.data
    assert value("identity_origin_record_sha256") == record_digest(producer)
    assert (
        read_record(directory, RecordRef(sha256=record_digest(producer)))
        == producer
    )
    assert value("identity_origin_run") == producer.metadata["producer_run_id"]
    assert value("identity_cluster") in identity.clusters()
    return value("identity_record")["id"], digest, producer


def _operational_origins(client, clusters, origin):
    """Kindless targets carry operational evidence without source claims."""
    properties, _ = client.vocabulary()
    claims = client.fetch_claims(list(clusters))
    for qid, cluster in clusters.items():
        assert set(claims[qid]) == {properties["identity_record"]}
        (statement,) = claims[qid][properties["identity_record"]]
        assert statement["mainsnak"]["datavalue"]["value"]["id"] == origin[0]
        (reference,) = statement["references"]
        snaks = reference["snaks"]
        assert not set(snaks) & {
            properties["stated_in"],
            properties["inferred_from_extraction_run"],
        }
        for key, expected in (
            ("identity_cluster", cluster),
            ("run_record_sha256", origin[1]),
        ):
            (snak,) = snaks[properties[key]]
            assert snak["datavalue"]["value"] == expected
        (record,) = snaks[properties["identity_record"]]
        assert record["datavalue"]["value"]["id"] == origin[0]
    return claims


def _identity_rows(client, qids):
    properties, items = client.vocabulary()
    query = render_for_instance(
        load_templates()["identity-provenance"],
        properties,
        items,
        client.config.concept_uri,
    )
    entities = {client.config.concept_uri + "/entity/" + qid for qid in qids}
    return [
        row
        for row in sparql.select(client.config.sparql_url, query)
        if row["entity"]["value"] in entities
    ]


def _orphan_confirmation(client, person, record_qid):
    """An adversarial reference with no accepted assignment behind it."""
    properties, items = client.vocabulary()
    entity = client.wbi.item.get(person)
    (kind,) = [
        statement
        for statement in entity.claims.get(properties["instance_of"])
        if statement.mainsnak.datavalue["value"]["id"] == items["human"]
    ]
    reference = Reference()
    reference.add(
        Item(prop_nr=properties["identity_record"], value=record_qid)
    )
    for key, value in (
        ("identity_assignment_revision", "e" * 64),
        ("identity_decision_revision", "f" * 32 + ":1"),
        ("identity_confirmation_actor", "authored adversarial fixture"),
        ("identity_confirmation_at", REVIEW_TIME),
    ):
        reference.add(String(prop_nr=properties[key], value=value))
    kind.references.add(reference)
    write_existing(entity, summary="fixture: orphan identity confirmation")


def _assert_unratified_rows(client, rows, identity, origin, person, kindless):
    qid, digest, producer = origin
    _, items = client.vocabulary()
    uri = client.config.concept_uri + "/entity/"
    found = [row for row in rows if row["record"]["value"] == uri + qid]
    assert found
    assert {row["entity"]["value"] for row in found} == {
        uri + qid for qid in (person, *kindless)
    }
    assert all(
        row.get("kind", {}).get("value")
        == (
            uri + items["human"]
            if row["entity"]["value"] == uri + person
            else None
        )
        for row in found
    )
    assert {row["method"]["value"] for row in found} == {
        uri + items["identity_singleton"]
    }
    assert {row["mapHash"]["value"] for row in found} == {identity.hash}
    assert {row["cluster"]["value"] for row in found} == set(
        identity.clusters()
    )
    assert {row["unratified"]["value"] for row in found} == {"true"}
    assert {row["recordSha256"]["value"] for row in found} == {digest}
    assert {row["recordFile"]["value"] for row in found} == {digest + ".json"}
    assert {row["originRecordSha256"]["value"] for row in found} == {
        record_digest(producer)
    }
    assert {row["originRun"]["value"] for row in found} == {
        producer.metadata["producer_run_id"]
    }
    assert {row["stage"]["value"] for row in found} == {
        stage.stage for stage in producer.stages
    }
    assert all(
        stage.process == "deterministic"
        and not stage.model_identifier
        and not stage.prompt_hashes
        for stage in producer.stages
    )
    assert not any(
        set(row)
        & {
            "assignmentRevision",
            "originDecisionRevision",
            "decisionRevision",
            "actor",
            "confirmedAt",
            "model",
            "prompt",
        }
        for row in found
    )


def _synchronize(client):
    """Wait for exact API revisions, including query vocabulary entities."""
    ids = client.list_entity_ids()
    wanted = {}
    for start in range(0, len(ids), 50):
        entities = client.api_get(
            {
                "action": "wbgetentities",
                "ids": "|".join(ids[start : start + 50]),
                "props": "info",
            }
        )["entities"]
        wanted.update(
            (qid, entity["lastrevid"]) for qid, entity in entities.items()
        )
    concept = client.config.concept_uri
    values = " ".join(f"<{concept}/entity/{qid}>" for qid in wanted)
    query = (
        "PREFIX schema: <http://schema.org/>\n"
        "SELECT ?entity ?version WHERE {\n"
        f"VALUES ?entity {{ {values} }}\n"
        "?entity schema:version ?version . }"
    )
    deadline = time.monotonic() + HANG_GUARD
    while True:
        seen = {
            row["entity"]["value"].rsplit("/", 1)[-1]: int(
                row["version"]["value"]
            )
            for row in sparql.select(client.config.sparql_url, query)
        }
        stale = {
            qid: revision
            for qid, revision in wanted.items()
            if seen.get(qid, -1) < revision
        }
        if not stale:
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"Fictional WDQS synchronization failed: {len(stale)} "
                f"entities have not reached their API revision after "
                f"{HANG_GUARD}s. Check the fictional wdqs-updater logs."
            )
        time.sleep(2)


def _persisted_births(client, person, expected):
    """Inspect fresh server reads, including exact reference cardinality."""
    properties, _ = client.vocabulary()
    statements = client.fetch_claims([person])[person][
        properties["date_of_birth"]
    ]
    assert len(statements) == 2
    observed = {}
    for statement in statements:
        value = statement["mainsnak"]["datavalue"]["value"]
        assert statement["rank"] == "normal"
        assert value["precision"] == 11
        assert len(statement["references"]) == 2
        (source,) = [
            ref["snaks"]
            for ref in statement["references"]
            if properties["stated_in"] in ref["snaks"]
        ]
        (process,) = [
            ref["snaks"]
            for ref in statement["references"]
            if properties["inferred_from_extraction_run"] in ref["snaks"]
        ]

        def single(snaks, key):
            (snak,) = snaks[properties[key]]
            return snak["datavalue"]["value"]

        observed[value["time"]] = {
            "source": single(source, "stated_in")["id"],
            "process": single(process, "inferred_from_extraction_run")["id"],
            "page": single(source, "page_number"),
            "excerpt": single(source, "transcription_excerpt"),
        }
    assert observed == expected
    # Statement IDs and reference hashes must survive an identical rewrite.
    return sorted(statements, key=lambda statement: statement["id"])


def _assert_reference_targets(client, artifacts, results):
    """Returned IDs must identify two distinct, correctly populated targets."""
    properties, items = client.vocabulary()
    assert len({artifact.artifact_qid for artifact in artifacts.values()}) == 2
    assert len({result.run_qid for result in results}) == 2
    for key, result in zip(SOURCES, results, strict=True):
        artifact = artifacts[key].artifact_qid
        claims = client.fetch_claims([artifact])[artifact]
        (content_hash,) = claims[properties["media_sha1"]]
        assert (
            content_hash["mainsnak"]["datavalue"]["value"]
            == source_case(key).sha1
        )
        entity = client.api_get(
            {
                "action": "wbgetentities",
                "ids": result.run_qid,
                "props": "labels|descriptions|claims",
                "languages": "en",
            }
        )["entities"][result.run_qid]
        metadata = birth_batch(key).extraction_run
        assert metadata.model_identifier in entity["labels"]["en"]["value"]
        description = entity["descriptions"]["en"]["value"]
        assert metadata.pipeline_version in description
        assert metadata.started_at.isoformat() in description
        kinds = entity["claims"][properties["instance_of"]]
        assert any(
            claim["mainsnak"]["datavalue"]["value"]["id"]
            == items["extraction_run"]
            for claim in kinds
        )


def _query_births(client, person, expected):
    properties, items = client.vocabulary()
    concept = client.config.concept_uri
    query = render_for_instance(
        load_templates()["disagreement-report"], properties, items, concept
    )
    rows = [
        row
        for row in sparql.select(client.config.sparql_url, query)
        if row["entity"]["value"] == f"{concept}/entity/{person}"
        and row["property"]["value"]
        == f"{concept}/entity/{properties['date_of_birth']}"
    ]
    assert len(rows) == 2
    actual = {
        row["value"]["value"].lstrip("+"): (
            row["source"]["value"],
            row["artifact"]["value"],
            int(row["precision"]["value"]),
            row["rank"]["value"],
        )
        for row in rows
    }
    assert actual == {
        date.lstrip("+"): (
            f"{concept}/entity/{evidence['source']}",
            f"{concept}/entity/{evidence['source']}",
            11,
            "http://wikiba.se/ontology#NormalRank",
        )
        for date, evidence in expected.items()
    }
    return actual


def test_fictional_write_query_roundtrip(
    fictional_client, tmp_path, monkeypatch
):
    client = fictional_client
    artifacts = {}
    paths = {}
    for key in SOURCES:
        source = source_case(key)
        path = tmp_path / f"{key}.txt"
        path.write_bytes(source.source_bytes)
        paths[key] = path
        artifacts[key] = register_artifact(
            client,
            media_path=path,
            artifact_type="certificate",
            title=source.title,
            pages=1,
        )
        assert artifacts[key].created
        assert artifacts[key].fragment_qids[1]

    # Run the real deterministic map stage, including its producer capture.
    subject = LocalRef(
        local_id="gustav", label="Gustav Löbig", instance_of="human"
    )
    first = birth_batch(SOURCES[0], subject=subject)
    # A manifest-free authored batch represents both parents only as objects.
    # Their relations are attested; neither omitted nor unsupported kind
    # metadata may invent a sourced type or suppress placement provenance.
    first.entities = []
    for property_key, local_id, label, kind, excerpt in (
        ("father", "sebastian", "Sebastian Löbig", None, "Sebastian Löbig"),
        (
            "mother",
            "hildegard",
            "Hildegard Löbig",
            "family_member",
            "Hildegard Löbig geborene Laudler",
        ),
    ):
        first.claims.append(
            Claim(
                subject=subject,
                property=property_key,
                value=ItemValue(
                    value=LocalRef(
                        local_id=local_id, label=label, instance_of=kind
                    )
                ),
                source=source_case(SOURCES[0]).source_ref(excerpt),
            )
        )
    batch_path = tmp_path / "first-batch.json"
    batch_path.write_text(first.model_dump_json())
    made = ensure_identity_map(
        tmp_path / "maps",
        [
            SimpleNamespace(
                sha1=source_case(SOURCES[0]).sha1,
                kind="text",
                batch=batch_path,
            )
        ],
        run_manifest={"run_id": "fictional-normal-map-origin"},
    )
    identity = IdentityMap(made.identity_map)
    _, clusters = identity.clusters_of(first, [source_case(SOURCES[0]).sha1])
    assert set(clusters) == {"gustav", "sebastian", "hildegard"}
    cluster = clusters["gustav"]
    assert all(
        value["method"] == "singleton"
        for value in identity.data["clusters"].values()
    )
    directory = tmp_path / "records"
    properties, _ = client.vocabulary()
    registry = CreationRegistry(client)
    authority_before = registry.assignment_snapshot()
    interrupted = []
    create = CreationRegistry.create

    def lose_creation_response(registry, operation, item, **kwargs):
        if operation.get("kind") == "identity-map-origin":
            write = item.write

            def interrupted_write(**options):
                written = write(**options)
                interrupted.append(written.id)
                raise InterruptedMapOrigin()

            item.write = interrupted_write
        return create(registry, operation, item, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(CreationRegistry, "create", lose_creation_response)
        with pytest.raises(InterruptedMapOrigin):
            BatchWriter(client, record_dir=directory).write_batch(
                first, identity=identity
            )
    assert len(interrupted) == 1
    binding = registry.lookup(
        cluster_creation_operation(identity.hash, cluster)
    )
    assert binding is not None
    person = binding.qid
    claims_before_retry = client.fetch_claims([person])[person]
    historical_before_retry = {
        properties[key]: claims_before_retry[properties[key]]
        for key in ("date_of_birth", "father", "mother")
    }
    kind_before_retry = _kind(client, person)
    entities_before_retry = set(client.list_entity_ids())
    client = type(client)(client.config)
    first_result = BatchWriter(client, record_dir=directory).write_batch(
        first, identity=identity
    )
    assert first_result.written == first_result.referenced == 0
    assert first_result.skipped == len(first.claims)
    assert first_result.minted == []
    assert first_result.refused == []
    assert first_result.created_items["gustav"] == person
    assert set(client.list_entity_ids()) == entities_before_retry
    after_retry = client.fetch_claims([person])[person]
    assert {
        key: after_retry[key] for key in historical_before_retry
    } == historical_before_retry
    _preserved_kind(kind_before_retry, _kind(client, person))
    origin = _map_origin(client, person, identity, directory)
    assert origin[0] == interrupted[0]
    kindless = {
        first_result.created_items[local_id]: clusters[local_id]
        for local_id in ("sebastian", "hildegard")
    }
    operational = _operational_origins(client, kindless, origin)
    assert CreationRegistry(client).assignment_snapshot() == authority_before
    # The fixture explicitly identifies the second reading's subject.
    # This is authored identity, not a model inference or similarity match.
    second = birth_batch(SOURCES[1], subject=QidRef(qid=person))
    second_result = BatchWriter(client, record_dir=directory).write_batch(
        second
    )
    assert second_result.written == 1
    assert second_result.refused == []
    expected = {}
    for key, date, batch, result in zip(
        SOURCES,
        DATES,
        (first, second),
        (first_result, second_result),
        strict=True,
    ):
        excerpt = batch.claims[0].source.transcription_excerpt
        assert excerpt is not None
        assert excerpt.text in source_case(key).text
        expected[date] = {
            "source": artifacts[key].artifact_qid,
            "process": result.run_qid,
            "page": "1",
            "excerpt": {"language": excerpt.language, "text": excerpt.text},
        }

    # A confirmation-shaped reference cannot supply an assignment that the
    # unratified origin never recorded, even when both name the same record.
    before_orphan = _kind(client, person)
    _orphan_confirmation(client, person, origin[0])
    _preserved_kind(before_orphan, _kind(client, person))
    _assert_reference_targets(client, artifacts, (first_result, second_result))
    persisted = _persisted_births(client, person, expected)
    _synchronize(client)
    queried = _query_births(client, person, expected)
    _assert_unratified_rows(
        client,
        _identity_rows(client, first_result.created_items.values()),
        identity,
        origin,
        person,
        kindless,
    )
    entities = set(client.list_entity_ids())
    kind_before_repeat = _kind(client, person)

    # Reopen all client-side state, then repeat every persistence boundary.
    client = type(client)(client.config)
    counts = bootstrap_instance(client, load_manifest(), log=lambda *_: None)
    assert counts["created"] == counts["updated"] == 0
    assert counts["statements_added"] == 0
    for key in SOURCES:
        again = register_artifact(
            client,
            media_path=paths[key],
            artifact_type="certificate",
            title=source_case(key).title,
            pages=1,
        )
        assert not again.created
        assert again.artifact_qid == artifacts[key].artifact_qid
        assert again.fragment_qids == artifacts[key].fragment_qids
    for batch, original, mapped_identity in (
        (first, first_result, identity),
        (second, second_result, None),
    ):
        again = BatchWriter(client, record_dir=directory).write_batch(
            batch, identity=mapped_identity
        )
        assert again.run_qid == original.run_qid
        assert again.written == again.referenced == 0
        assert again.skipped == len(batch.claims)
        assert again.minted == []
        assert again.refused == []
    assert set(client.list_entity_ids()) == entities
    assert _kind(client, person) == kind_before_repeat
    assert _map_origin(client, person, identity, directory) == origin
    assert _operational_origins(client, kindless, origin) == operational
    assert CreationRegistry(client).assignment_snapshot() == authority_before
    _assert_reference_targets(client, artifacts, (first_result, second_result))
    assert _persisted_births(client, person, expected) == persisted

    _synchronize(client)
    assert _query_births(client, person, expected) == queried
    rows = _identity_rows(client, first_result.created_items.values())
    _assert_unratified_rows(client, rows, identity, origin, person, kindless)
    uri = client.config.concept_uri + "/entity/"
    assert {row["record"]["value"] for row in rows} == {uri + origin[0]}
