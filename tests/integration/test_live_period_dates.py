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
  or end, and names the two qualifiers that bound each date (#309);
* the disagreement report compares each end through its own range: a
  start "between 1946 and 1947" and one in 1948 disagree, while 8 February
  1947 lies inside it, and an end "between 1956 and 1957" and one in 1955
  disagree, while 2 January 1957 lies inside it.

Each source is registered with a fictional PNG of its own
(register_fictional), which its claims cite by SHA-1 (#102). Like every
live test it writes fictional data to the one instance, so the session
that runs it ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import write_mapped
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.disagreement import disagreements
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
    "#231: one marriage as three sources date it, and a residence "
    "observed at a date between two years"
)
MARK = fixture_mark(FIXTURE, writer)


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
    subject: dict, prop: str, value: dict, artifact: ArtifactRef, qualifiers
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
            "artifact": artifact.model_dump(mode="json"),
            "page_number": "1",
            "transcription_excerpt": {
                "text": "fictional period fixture",
                "language": "en",
            },
        },
    }


def married(artifact: ArtifactRef, *qualifiers) -> dict:
    return claim(
        PERSON,
        "spouse",
        {"type": "item", "value": SPOUSE},
        artifact,
        qualifiers,
    )


def write_fixture(client, artifacts: dict[str, str], claims: list[dict]):
    """Write the fixture's claims under item labels that carry a marker: six
    hex digits of a hash over the claims, each cited SHA-1 replaced by its
    artifact's letter in `artifacts`, as test_live_disagreement.py's
    write_fixture marks its own. The writer finds a local item by its label, so
    a version of the fixture with other claims would otherwise write onto an
    earlier version's items and see their statements beside its own. With the
    marker each version writes items of its own, and a re-run of one version
    finds its own items again. The hash keeps each reference's kind, so the
    marker is not the one the fixture had when its claims cited their sources
    by QID. The hash also covers the module's marker (conftest.fixture_mark:
    the writer's source and the session's random bytes), so each session writes
    items of its own: what an earlier session wrote, a live mutant's run
    included, never meets it, and a re-run within the session finds its own
    items again. Returns the write's result and the marker."""
    content = [
        {
            **claim,
            "source": {
                **claim["source"],
                "artifact": {
                    **claim["source"]["artifact"],
                    "sha1": artifacts[claim["source"]["artifact"]["sha1"]],
                },
            },
        }
        for claim in claims
    ]
    marker = hashlib.sha256(
        json.dumps(content, sort_keys=True).encode() + MARK.encode()
    ).hexdigest()[:6]

    def marked(value):
        """`value` with the marker on the label of every local item."""
        if isinstance(value, dict):
            value = {key: marked(item) for key, item in value.items()}
            if value.get("kind") == "local":
                value["label"] = f"{value['label']} ({marker})"
        elif isinstance(value, list):
            value = [marked(item) for item in value]
        return value

    written = write_mapped(
        BatchWriter(client),
        ClaimBatch.model_validate(
            {
                "extraction_run": {
                    "model_identifier": "integration-test",
                    "pipeline_version": "0.0.0",
                    "started_at": datetime.now(UTC).isoformat(),
                },
                "claims": marked(claims),
            }
        ),
    )
    return written, marker


@pytest.fixture(scope="module")
def fixture_graph(client, config):
    """Write the fixture under its marked labels (write_fixture) and wait
    until the query service holds every item it made. Returns (QIDs by
    role, source QIDs by letter, the marked labels by role)."""
    registered = {
        letter: register_fictional(
            client, f"Period fixture source {letter} {MARK}", "certificate"
        )
        for letter in "ABC"
    }
    source = {letter: registered[letter].ref for letter in "ABC"}
    written, marker = write_fixture(
        client,
        {ref.sha1: letter for letter, ref in source.items()},
        [
            # per A: married between 1946 and 1947, the marriage over
            # between 1956 and 1957, and observed in 1950
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
    )
    qids = {
        role: written.created_items[ref["local_id"]]
        for role, ref in (("person", PERSON), ("spouse", SPOUSE))
    }
    labels = {
        role: f"{ref['label']} ({marker})"
        for role, ref in (("spouse", SPOUSE), ("town", TOWN))
    }
    # every item the timeline names is the session's own, the town too
    wait_for_query_service(
        config, client, list(written.created_items.values())
    )
    return qids, {letter: registered[letter].qid for letter in "ABC"}, labels


def day(value: str) -> str:
    """A stored time's day, as the query service or the API gives it."""
    return value.lstrip("+")[:10]


def test_the_spouse_statement_carries_each_bound_as_written(
    client, fixture_graph
):
    qids, _, _ = fixture_graph
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
    qids, source, labels = fixture_graph
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
    found = run_sparql(config, query)
    rows = {
        (
            row["what"]["value"],
            day(row["date"]["value"]),
            row["precision"]["value"],
            day(row["earliest"]["value"]) if "earliest" in row else None,
            day(row["latest"]["value"]) if "latest" in row else None,
            letter[row["source"]["value"].rsplit("/", 1)[-1]],
        )
        for row in found
    }
    start = f"spouse: {labels['spouse']} (start time)"
    end = f"spouse: {labels['spouse']} (end time)"
    observed = f"spouse: {labels['spouse']} (point in time)"
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
            f"residence: {labels['town']} (point in time)",
            "1938-01-01",
            "9",
            "1938-01-01",
            "1939-01-01",
            "A",
        ),
    }

    # each date names the pair of qualifiers that bounds it, stated or not,
    # by which the static site names each bound (#309)
    key_of = {pid: key for key, pid in properties.items()}
    bounds = {
        (
            row["what"]["value"],
            key_of[row["low"]["value"].rsplit("/", 1)[-1]],
            key_of[row["high"]["value"].rsplit("/", 1)[-1]],
        )
        for row in found
    }
    assert bounds == {
        (start, "earliest_date", "latest_start_date"),
        (end, "earliest_end_date", "latest_date"),
        (observed, "earliest_date", "latest_date"),
        (
            f"residence: {labels['town']} (point in time)",
            "earliest_date",
            "latest_date",
        ),
    }


def test_the_report_compares_each_end_through_its_own_range(
    client, config, fixture_graph
):
    qids, source, _ = fixture_graph
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
