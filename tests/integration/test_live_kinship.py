"""Live check of the kinship query's `relative` direction (ADR-0018, #229).

A kinship term a source asserts is stored with the person the term names
as the statement's subject and the ego as its value. The universe's
interview calls Klara "Aunt Pünktchen" from Nicholas's view, recorded as
``Klara —relative→ Nicholas ; kinship_type = "aunt"`` (docs/EXAMPLES.md).
The kinship query reads each row as "?relative is ?subject's
?relationship", so on the running stack that statement is one row for
Nicholas, with Klara as his aunt, and no row for Klara.

The fixture writes the two people under labels of its own, so a re-run
reuses its entities, and the query is scoped to them, as its header
describes for one subject, so no row about anyone else is fetched. Like
every live test it writes fictional data to the one instance, so the
session that runs it ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures import example_universe as u
from yizkr.claims import ClaimBatch
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance

from .conftest import run_sparql, wait_for_query_service

pytestmark = pytest.mark.live

# the universe's interview: "Aunt Pünktchen", from Nicholas's view
AUNT = "klara-loebig"
EGO = "nicholas-lobig"
TERM = "aunt"
EXCERPT = "Aunt Pünktchen"


def person(key: str) -> dict:
    """A local ref to one of the universe's people, under this fixture's
    own label, so that no other test's item is reused."""
    return {
        "kind": "local",
        "local_id": f"kinship-{key}",
        "label": f"{u.by_key()[key].label} (kinship fixture)",
        "instance_of": "human",
    }


@pytest.fixture(scope="module")
def fixture_graph(client, config) -> dict[str, str]:
    """Write the fixture (idempotent: re-runs reuse and skip everything)
    and wait until the query service holds both people's latest
    revisions. Returns their QIDs by universe key."""
    source = register_artifact(
        client,
        media_path=None,
        artifact_type="artifact",
        title="Kinship fixture source",
        description="written by tests/integration; safe to delete",
    ).artifact_qid
    batch = ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": [
                {
                    "subject": person(AUNT),
                    "property": "relative",
                    "value": {"type": "item", "value": person(EGO)},
                    "qualifiers": [
                        {
                            "property": "kinship_type",
                            "value": {"type": "string", "value": TERM},
                        }
                    ],
                    "source": {
                        "artifact": {"kind": "qid", "qid": source},
                        "page_number": "1",
                        "transcription_excerpt": {
                            "text": EXCERPT,
                            "language": "en",
                        },
                    },
                }
            ],
        }
    )
    written = BatchWriter(client).write_batch(batch)
    qids = {
        key: written.created_items[f"kinship-{key}"] for key in (AUNT, EGO)
    }
    wait_for_query_service(config, client, list(qids.values()))
    return qids


def relative_statements(client, qid: str) -> list[dict]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties["relative"],
        }
    )
    return [c for cs in data["claims"].values() for c in cs]


def kinship_rows(client, config, qids: dict[str, str]) -> set[tuple]:
    """The kinship query's rows whose subject is one of the fixture's
    people, as (subject, relative, relationship), each person given by
    universe key. The query is scoped to them, and the rows are filtered
    to them as well, so no row about anyone else reaches an assertion."""
    properties, items = client.vocabulary()
    template = load_templates()["kinship"]
    assert template.count("WHERE {") == 1
    scope = " ".join(f"wd:{qid}" for qid in qids.values())
    query = render_for_instance(
        template.replace(
            "WHERE {", f"WHERE {{\n  VALUES ?subject {{ {scope} }}"
        ),
        properties,
        items,
        config.concept_uri,
    )
    key_of = {qid: key for key, qid in qids.items()}

    def key(row: dict, var: str) -> str:
        qid = row[var]["value"].rsplit("/", 1)[-1]
        return key_of.get(qid, qid)

    return {
        (
            key(row, "subject"),
            key(row, "relative"),
            row["relationship"]["value"],
        )
        for row in run_sparql(config, query)
        if key(row, "subject") in qids
    }


def test_the_fixture_is_stored_as_adr_0018_records_it(client, fixture_graph):
    """The aunt is the statement's subject and the ego its value, so the
    query below reads what ADR-0018 writes."""
    properties, _ = client.vocabulary()
    (statement,) = relative_statements(client, fixture_graph[AUNT])
    value = statement["mainsnak"]["datavalue"]["value"]["id"]
    assert value == fixture_graph[EGO]
    (term,) = statement["qualifiers"][properties["kinship_type"]]
    assert term["datavalue"]["value"] == TERM
    assert relative_statements(client, fixture_graph[EGO]) == []


def test_the_aunt_is_the_egos_relative(client, config, fixture_graph):
    # Klara is Nicholas's aunt: his row, with her as the relative, and no
    # row that makes him hers
    assert kinship_rows(client, config, fixture_graph) == {(EGO, AUNT, TERM)}
