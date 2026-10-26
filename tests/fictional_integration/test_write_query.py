"""Real persistence and query acceptance for two fictional source readings.

Authored claims bypass inference, not registration, writing or querying.
Nothing uses the offline corpus fixture's stage substitutions. The two texts
are repository fixtures, and their process metadata names them as authored
fixtures rather than representing a model call that never happened.
"""

from __future__ import annotations

import time

import pytest
from tests.fixtures.identity import mapped
from tests.fixtures.semantic_acceptance import birth_batch, source_case

from yizkr.claims import QidRef
from yizkr.core import sparql
from yizkr.model.bootstrap import bootstrap_instance
from yizkr.model.schema import load_manifest
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance

pytestmark = pytest.mark.live
SOURCES = ("birth-extract-1938", "naturalisation-1952")
DATES = ("+1919-01-07T00:00:00Z", "+1919-07-01T00:00:00Z")
HANG_GUARD = 240


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


def test_fictional_write_query_roundtrip(fictional_client, tmp_path):
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

    # The first source mints the fictional person through a declared map.
    first = birth_batch(SOURCES[0])
    identity = mapped(first)
    first_result = BatchWriter(client).write_batch(first, identity=identity)
    assert first_result.written == 1
    assert first_result.refused == []
    person = first_result.created_items["gustav"]
    # The fixture explicitly identifies the second reading's subject.
    # This is authored identity, not a model inference or similarity match.
    second = birth_batch(SOURCES[1], subject=QidRef(qid=person))
    second_result = BatchWriter(client).write_batch(second)
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

    _assert_reference_targets(client, artifacts, (first_result, second_result))
    persisted = _persisted_births(client, person, expected)
    _synchronize(client)
    queried = _query_births(client, person, expected)
    entities = set(client.list_entity_ids())

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
        again = BatchWriter(client).write_batch(
            batch, identity=mapped_identity
        )
        assert again.run_qid == original.run_qid
        assert again.written == again.referenced == 0
        assert again.skipped == 1
        assert again.minted == []
        assert again.refused == []
    assert set(client.list_entity_ids()) == entities
    _assert_reference_targets(client, artifacts, (first_result, second_result))
    assert _persisted_births(client, person, expected) == persisted
    _synchronize(client)
    assert _query_births(client, person, expected) == queried
