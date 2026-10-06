"""Live validation of the under-witnessed reappraisal queue (ADR-0023 §5).

A fictional fixture exercises every clause of the query's contract on the
running stack, then pins the queue's rows for the fixture subject to the
exact statements the API holds:

* a machine statement with ONE stated-in source and no curator sibling is
  listed — even beside a coexisting machine value from another source
  (machine siblings are not witnesses; ADR-0002 coexistence stays visible);
* the subject's type statement, which the writer wrote when it minted the
  subject, is such a machine statement (#88), but the second batch, which
  has no entity manifest, gives the same kind on its local ref: the
  statement gains source B as a witness and is not listed (#240);
* an agreeing second source (the writer's appended reference) takes a
  statement OUT of the queue;
* a curator (run-reference-free) sibling on the same subject+property takes
  the machine statement out, and curator statements are never listed;
* two same-value statements that differ only by a qualifier are two
  distinct assertions (ADR-0002) and are listed as two rows;
* a time value keeps its stated precision (rule 5: never narrow);
* the documented name-case scope (a ``VALUES ?prop`` filter) works.

The whole graph is queried, and rows are filtered to the fixture subject, so
unrelated data on the instance cannot make the assertions flaky. The
subject's and the town's labels and the sources' titles carry the
session's marker (conftest.fixture_mark), so each session meets the
fixture as a first write: statements an earlier session wrote, a live
mutant's run included, never stand on its subject.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import by_label, write_mapped
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance

from .conftest import (
    fixture_mark,
    register_fictional,
    run_sparql,
    wait_for_query_service,
)

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "ADR-0023 §5: one subject's machine statements from sources A and B, "
    "with a curator's sibling, and a town of residence"
)
MARK = fixture_mark(FIXTURE, writer)
SUBJECT = {
    "kind": "local",
    "local_id": "queue-person",
    "label": f"Queue Fixture Subject {MARK}",
    "instance_of": "human",
}
TOWN = {
    "kind": "local",
    "local_id": "queue-town",
    "label": f"Queue Fixture Town {MARK}",
    "instance_of": "place",
}
YEAR_PRECISION = "9"  # Wikibase time precision for a bare year


def year(value: int) -> dict:
    return {"type": "time", "year": value}


def text(value: str) -> dict:
    return {"type": "monolingual", "value": {"text": value, "language": "en"}}


def claim(
    prop: str,
    value: dict,
    artifact: ArtifactRef,
    qualifiers: tuple = (),
    curated: bool = False,
) -> dict:
    return {
        "subject": SUBJECT,
        "property": prop,
        "value": value,
        "qualifiers": list(qualifiers),
        "source": {
            "artifact": artifact.model_dump(mode="json"),
            "page_number": "1",
            "transcription_excerpt": {
                "text": "fictional under-witnessed fixture",
                "language": "en",
            },
        },
        "curated": curated,
    }


def batch(claims: list[dict]) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": claims,
        }
    )


@pytest.fixture(scope="module")
def fixture_graph(client, config):
    """Write the fixture (idempotent: a re-run within the session reuses
    and skips everything) and wait until the query service holds the
    subject's latest revision. Returns (subject QID, source A QID, source B
    QID)."""
    registered_a, registered_b = (
        register_fictional(
            client,
            f"Under-witnessed fixture source {letter} {MARK}",
            "certificate",
        )
        for letter in ("A", "B")
    )
    source_a, source_b = registered_a.ref, registered_b.ref
    town = {"type": "item", "value": TOWN}
    first_batch = batch(
        [
            claim("date_of_birth", year(1911), source_a),
            claim("date_of_death", year(1970), source_a),
            claim("nickname", text("Fixie"), source_a),
            claim("married_name", text("Queue Fixture Wedded"), source_a),
            # same value, different qualifier: two distinct assertions
            claim(
                "residence",
                town,
                source_a,
                ({"property": "start_time", "value": year(1930)},),
            ),
            claim(
                "residence",
                town,
                source_a,
                ({"property": "start_time", "value": year(1940)},),
            ),
        ]
    )
    second_batch = batch(
        [
            # a coexisting machine value from another source
            claim("date_of_birth", year(1912), source_b),
            # an agreeing second source: appends a reference
            claim("date_of_death", year(1970), source_b),
        ]
    )
    curated_batch = batch(
        [
            # a curator sibling beside the machine nickname
            claim("nickname", text("Fixy"), source_a, curated=True),
            # a curator-only property
            claim(
                "birth_name",
                text("Queue Fixture Born"),
                source_a,
                curated=True,
            ),
        ]
    )
    # the map of the three, in which the subject is one entity (#109)
    identity = by_label(first_batch, second_batch, curated_batch)
    first = write_mapped(BatchWriter(client), first_batch, identity=identity)
    # written again, source A's batch finds every statement
    again = write_mapped(BatchWriter(client), first_batch, identity=identity)
    assert (again.written, again.referenced) == (0, 0)
    # source B's batch: its refs to the subject also give the subject's
    # kind, which the writer records beside source A's (#240)
    write_mapped(BatchWriter(client), second_batch, identity=identity)
    write_mapped(BatchWriter(client), curated_batch, identity=identity)
    subject = first.created_items["queue-person"]
    wait_for_query_service(config, client, [subject])
    return subject, registered_a.qid, registered_b.qid


def statement_uri(config, guid: str) -> str:
    """API statement GUID (``Q5$uuid``) -> its RDF node URI."""
    return f"{config.concept_uri}/entity/statement/{guid.replace('$', '-', 1)}"


def statements(client, subject: str, prop_key: str) -> list[dict]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": subject,
            "property": properties[prop_key],
        }
    )
    return [c for cs in data["claims"].values() for c in cs]


def value_of(statement: dict):
    value = statement["mainsnak"]["datavalue"]["value"]
    if isinstance(value, dict) and "time" in value:
        return value["time"][1:5]  # "+1911-00-00T..." -> "1911"
    if isinstance(value, dict) and "text" in value:
        return value["text"]
    return value


def queue_rows(client, config, subject: str, scope: list[str] = ()) -> dict:
    """The queue's rows for `subject`, keyed by statement URI. `scope`
    applies the documented ``VALUES ?prop`` filter (property keys)."""
    properties, items = client.vocabulary()
    template = load_templates()["under-witnessed-statements"]
    if scope:
        values = " ".join(f"wd:{{P:{key}}}" for key in scope)
        template = template.replace(
            "WHERE {", f"WHERE {{\n  VALUES ?prop {{ {values} }}", 1
        )
    query = render_for_instance(
        template, properties, items, config.concept_uri
    )
    subject_uri = f"{config.concept_uri}/entity/{subject}"
    return {
        row["statement"]["value"]: row
        for row in run_sparql(config, query)
        if row["subject"]["value"] == subject_uri
    }


def pick(client, subject, prop_key, value) -> dict:
    (statement,) = [
        s
        for s in statements(client, subject, prop_key)
        if value_of(s) == value
    ]
    return statement


def test_queue_lists_exactly_the_under_witnessed_machine_statements(
    client, config, fixture_graph
):
    subject, source_a, source_b = fixture_graph
    rows = queue_rows(client, config, subject)

    residences = statements(client, subject, "residence")
    assert len(residences) == 2  # the fixture's two distinct assertions
    # the kind the writer wrote when it minted the subject, from source A,
    # and that the second batch's ref gave from source B (#240)
    (kind,) = statements(client, subject, "instance_of")
    properties, _ = client.vocabulary()
    assert [
        snak["datavalue"]["value"]["id"]
        for block in kind["references"]
        for snak in block["snaks"].get(properties["stated_in"], [])
    ] == [source_a, source_b]
    expected = {
        statement_uri(config, s["id"])
        for s in [
            pick(client, subject, "date_of_birth", "1911"),
            pick(client, subject, "date_of_birth", "1912"),
            pick(client, subject, "married_name", "Queue Fixture Wedded"),
            *residences,
        ]
    }
    # one row per statement: the same-value residences are NOT collapsed
    assert set(rows) == expected

    # and, spelled out, what the queue must leave out
    excluded = {
        "a kind a later source's ref witnesses": kind,
        "agreeing second source": pick(
            client, subject, "date_of_death", "1970"
        ),
        "machine value beside a curator sibling": pick(
            client, subject, "nickname", "Fixie"
        ),
        "curator statement": pick(client, subject, "nickname", "Fixy"),
        "curator-only property": pick(
            client, subject, "birth_name", "Queue Fixture Born"
        ),
    }
    for reason, statement in excluded.items():
        assert statement_uri(config, statement["id"]) not in rows, reason


def test_each_row_names_its_single_source(client, config, fixture_graph):
    subject, source_a, source_b = fixture_graph
    rows = queue_rows(client, config, subject)
    source_of = {
        uri: row["source"]["value"].rsplit("/", 1)[-1]
        for uri, row in rows.items()
    }
    for value, source in (("1911", source_a), ("1912", source_b)):
        uri = statement_uri(
            config, pick(client, subject, "date_of_birth", value)["id"]
        )
        assert source_of[uri] == source


def test_time_values_keep_their_stated_precision(
    client, config, fixture_graph
):
    """Rule 5: a year-precision date must not display as 1 January."""
    subject, _, _ = fixture_graph
    rows = queue_rows(client, config, subject)
    for value in ("1911", "1912"):
        uri = statement_uri(
            config, pick(client, subject, "date_of_birth", value)["id"]
        )
        assert rows[uri]["precision"]["value"] == YEAR_PRECISION


def test_name_scope_filter(client, config, fixture_graph):
    """The header's documented name-case scope: only the machine married
    name is under-witnessed (the nickname has a curator sibling, and the
    birth name is curator-only)."""
    subject, _, _ = fixture_graph
    rows = queue_rows(
        client,
        config,
        subject,
        scope=["birth_name", "married_name", "nickname", "official_name"],
    )
    married = pick(client, subject, "married_name", "Queue Fixture Wedded")
    assert set(rows) == {statement_uri(config, married["id"])}
