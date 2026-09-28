"""Live validation of how a period is dated (#231; docs/MODELING.md,
"Marriages, residences and other periods").

A fictional fixture writes one marriage as three sources date it, and a
residence observed at a date between two years, and reads them back
through the stack:

* the spouse statement carries each end's bounds as written: the start's
  earliest date and latest start date, the end's earliest end date and
  latest date, and a point in time;
* the person timeline lists each start and end with its own range, the
  point in time beside them with none, and the residence's point in time
  with the earliest and latest dates of its statement, which has no start
  or end;
* the disagreement report compares each end through its own range: a
  start "between 1946 and 1947" and one in 1948 disagree, while 8 February
  1947 lies inside it, and an end "between 1956 and 1957" and one in 1955
  disagree, while 2 January 1957 lies inside it.

Like every live test it writes fictional data to the one instance, so the
session that runs it ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from yizkr.claims import ClaimBatch
from yizkr.disagreement import disagreements
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance

from .conftest import run_sparql, wait_for_query_service

pytestmark = pytest.mark.live


def local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": kind,
    }


PERSON = local("period-person", "Period Fixture Person", "human")
SPOUSE = local("period-spouse", "Period Fixture Spouse", "human")
TOWN = local("period-town", "Period Fixture Town", "place")


def date(year: int, month: int | None = None, day: int | None = None):
    value = {"type": "time", "year": year}
    if month:
        value["month"] = month
    if day:
        value["day"] = day
    return value


def claim(
    subject: dict, prop: str, value: dict, artifact_qid: str, qualifiers
) -> dict:
    return {
        "subject": subject,
        "property": prop,
        "value": value,
        "qualifiers": [
            {"property": key, "value": qualifier}
            for key, qualifier in qualifiers
        ],
        "source": {
            "artifact": {"kind": "qid", "qid": artifact_qid},
            "page_number": "1",
            "transcription_excerpt": {
                "text": "fictional period fixture",
                "language": "en",
            },
        },
    }


def married(artifact_qid: str, *qualifiers) -> dict:
    return claim(
        PERSON,
        "spouse",
        {"type": "item", "value": SPOUSE},
        artifact_qid,
        qualifiers,
    )


@pytest.fixture(scope="module")
def fixture_graph(client, config):
    """Write the fixture (idempotent: re-runs reuse and skip everything)
    and wait until the query service holds the person's latest revision.
    Returns (QIDs by role, source QIDs by letter)."""
    source = {
        letter: register_artifact(
            client,
            media_path=None,
            artifact_type="certificate",
            title=f"Period fixture source {letter}",
            description="written by tests/integration; safe to delete",
        ).artifact_qid
        for letter in "ABC"
    }
    written = BatchWriter(client).write_batch(
        ClaimBatch.model_validate(
            {
                "extraction_run": {
                    "model_identifier": "integration-test",
                    "pipeline_version": "0.0.0",
                    "started_at": datetime.now(UTC).isoformat(),
                },
                "claims": [
                    # per A: married between 1946 and 1947, the marriage
                    # over between 1956 and 1957, and observed in 1950
                    married(
                        source["A"],
                        ("start_time", date(1946)),
                        ("earliest_date", date(1946)),
                        ("latest_start_date", date(1947)),
                        ("end_time", date(1956)),
                        ("earliest_end_date", date(1956)),
                        ("latest_date", date(1957)),
                        ("point_in_time", date(1950)),
                    ),
                    # per B: 1948 to 1955, outside both of A's ranges
                    married(
                        source["B"],
                        ("start_time", date(1948)),
                        ("end_time", date(1955)),
                    ),
                    # per C: 8 February 1947 to 2 January 1957, inside them
                    married(
                        source["C"],
                        ("start_time", date(1947, 2, 8)),
                        ("end_time", date(1957, 1, 2)),
                    ),
                    # observed in the town sometime between 1938 and 1939
                    claim(
                        PERSON,
                        "residence",
                        {"type": "item", "value": TOWN},
                        source["A"],
                        (
                            ("point_in_time", date(1938)),
                            ("earliest_date", date(1938)),
                            ("latest_date", date(1939)),
                        ),
                    ),
                ],
            }
        )
    )
    qids = {
        role: written.created_items[ref["local_id"]]
        for role, ref in (("person", PERSON), ("spouse", SPOUSE))
    }
    wait_for_query_service(config, client, [qids["person"]])
    return qids, source


def day(value: str) -> str:
    """A stored time's day, as the query service or the API gives it."""
    return value.lstrip("+")[:10]


def test_the_spouse_statement_carries_each_bound_as_written(
    client, fixture_graph
):
    qids, _ = fixture_graph
    properties, _ = client.vocabulary()
    key_of = {pid: key for key, pid in properties.items()}
    found = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qids["person"],
            "property": properties["spouse"],
        }
    )
    (ranged,) = [
        statement
        for statements in found["claims"].values()
        for statement in statements
        if properties["latest_start_date"] in statement.get("qualifiers", {})
    ]
    written = {
        key_of[pid]: [
            day(snak["datavalue"]["value"]["time"]) for snak in snaks
        ]
        for pid, snaks in ranged["qualifiers"].items()
    }
    assert written == {
        "start_time": ["1946-00-00"],
        "earliest_date": ["1946-00-00"],
        "latest_start_date": ["1947-00-00"],
        "end_time": ["1956-00-00"],
        "earliest_end_date": ["1956-00-00"],
        "latest_date": ["1957-00-00"],
        "point_in_time": ["1950-00-00"],
    }


def test_the_timeline_dates_each_end_with_its_own_range(
    client, config, fixture_graph
):
    qids, source = fixture_graph
    properties, items = client.vocabulary()
    query = render_for_instance(
        load_templates()["person-timeline"],
        properties,
        items,
        config.concept_uri,
    ).replace(
        "WHERE {\n",
        f"WHERE {{\n  VALUES ?person {{ wd:{qids['person']} }}\n",
        1,
    )
    letter = {qid: name for name, qid in source.items()}
    rows = {
        (
            row["what"]["value"],
            day(row["date"]["value"]),
            row["precision"]["value"],
            day(row["earliest"]["value"]) if "earliest" in row else None,
            day(row["latest"]["value"]) if "latest" in row else None,
            letter[row["source"]["value"].rsplit("/", 1)[-1]],
        )
        for row in run_sparql(config, query)
    }
    start = "spouse: Period Fixture Spouse (start time)"
    end = "spouse: Period Fixture Spouse (end time)"
    observed = "spouse: Period Fixture Spouse (point in time)"
    assert rows == {
        (start, "1946-01-01", "9", "1946-01-01", "1947-01-01", "A"),
        (end, "1956-01-01", "9", "1956-01-01", "1957-01-01", "A"),
        # beside a start and an end, a point in time has no range
        (observed, "1950-01-01", "9", None, None, "A"),
        (start, "1948-01-01", "9", None, None, "B"),
        (end, "1955-01-01", "9", None, None, "B"),
        (start, "1947-02-08", "11", None, None, "C"),
        (end, "1957-01-02", "11", None, None, "C"),
        (
            "residence: Period Fixture Town (point in time)",
            "1938-01-01",
            "9",
            "1938-01-01",
            "1939-01-01",
            "A",
        ),
    }


def test_the_report_compares_each_end_through_its_own_range(
    client, config, fixture_graph
):
    qids, source = fixture_graph
    properties, items = client.vocabulary()
    letter = {qid: name for name, qid in source.items()}

    def described(side) -> str:
        value = side.value
        shown = day(value.term.value)
        if value.earliest or value.latest:
            shown += (
                f" [{day(value.earliest.value)}..{day(value.latest.value)}]"
            )
        return shown + " per " + ",".join(letter[s.qid] for s in side.sources)

    rows = {
        (
            row.property,
            row.qualifier,
            frozenset({described(row.a), described(row.b)}),
        )
        for row in disagreements(
            lambda query: run_sparql(config, query),
            properties,
            items,
            config.concept_uri,
        )
        if row.entity_qid == qids["person"]
    }
    # A's ranges hold C's days, which differ from them only in resolution
    assert rows == {
        (
            "spouse",
            "start_time",
            frozenset(
                {
                    "1946-01-01 [1946-01-01..1947-01-01] per A",
                    "1948-01-01 per B",
                }
            ),
        ),
        (
            "spouse",
            "start_time",
            frozenset({"1947-02-08 per C", "1948-01-01 per B"}),
        ),
        (
            "spouse",
            "end_time",
            frozenset(
                {
                    "1956-01-01 [1956-01-01..1957-01-01] per A",
                    "1955-01-01 per B",
                }
            ),
        ),
        (
            "spouse",
            "end_time",
            frozenset({"1955-01-01 per B", "1957-01-02 per C"}),
        ),
    }
