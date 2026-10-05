"""Live validation of how a century is dated (#241; docs/MODELING.md,
"Uncertainty and time").

Centuries are counted as Wikibase counts them: the 19th century runs from
1801 to 1900, and a claim gives it by its first year. A fictional fixture
writes one person's birth as three sources date it, in the 19th century,
in 1900 and in 1800, and an event as two sources date it, in the 18th
century before the common era and in 1701 BCE, its last year, and reads
them back through the stack:

* the writer stores the 19th century as 1801 at century precision, and
  Wikibase shows it as "19. century"; it stores the 18th century BCE as
  -1800, which Wikibase shows as "18. century BCE";
* the query service serves each century's year as stored, and the static
  site's format_time names the century Wikibase shows;
* it serves 1701 BCE, at year precision, one year on, as -1700 in XSD
  1.1's count;
* the disagreement report gives the 19th century the span Wikibase
  counts: 1900, its last year, lies inside it, and 1800 does not. It
  gives the 18th century BCE its span in the count its years are served
  in, so 1701 BCE lies inside it.

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

from yizkr.claims import ArtifactRef, ClaimBatch, TimeValue
from yizkr.disagreement import disagreements
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import prefix_block
from yizkr.staticsite import format_time

from .conftest import (
    fixture_mark,
    register_fictional,
    run_sparql,
    wait_for_query_service,
)

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#241: one person's birth as three sources date it, in the 19th "
    "century, in 1900 and in 1800, and an event in the 18th century BCE "
    "and in 1701 BCE"
)
MARK = fixture_mark(FIXTURE, writer)


def local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": kind,
    }


PERSON = local("century-person", "Century Fixture Person", "human")
EVENT = local("century-event", "Century Fixture Event", "event")


def date(year: int, precision: str | None = None) -> dict:
    value = {"type": "time", "year": year}
    if precision:
        value["precision"] = precision
    return value


def claim(subject: dict, prop: str, value: dict, artifact: ArtifactRef):
    return {
        "subject": subject,
        "property": prop,
        "value": value,
        "source": {
            "artifact": artifact.model_dump(mode="json"),
            "page_number": "1",
            "transcription_excerpt": {
                "text": "fictional century fixture",
                "language": "en",
            },
        },
    }


def write_fixture(client, artifacts: dict[str, str], claims: list[dict]):
    """Write the fixture's claims under item labels that carry a marker:
    six hex digits of a hash over the claims, each cited SHA-1 replaced by
    its artifact's letter in `artifacts`, as test_live_period_dates.py's
    write_fixture marks its own. The writer finds a local item by its
    label, so a version of the fixture with other claims would otherwise
    write onto an earlier version's items and see their statements beside
    its own. With the marker each version writes items of its own, and a
    re-run of one version finds its own items again. The hash also covers
    each claim's time as TimeValue.to_wikibase gives it, the datavalue the
    writer's time conversion starts from, so a to_wikibase that gives
    another time (a mutant of the harness, say) writes items of its own
    too, and never leaves a second value on these. A change inside
    BatchWriter._to_wbi_claim is not hashed, but the writer's source is,
    through the module's marker (conftest.fixture_mark), with the session's
    random bytes: each session writes items of its own, so what an earlier
    session wrote, a live mutant's run included, never meets it. Returns
    the write's result and the marker."""
    stored = [
        TimeValue.model_validate(claim["value"]).to_wikibase()
        for claim in claims
    ]
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
        json.dumps([content, stored], sort_keys=True).encode() + MARK.encode()
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

    written = BatchWriter(client).write_batch(
        ClaimBatch.model_validate(
            {
                "extraction_run": {
                    "model_identifier": "integration-test",
                    "pipeline_version": "0.0.0",
                    "started_at": datetime.now(UTC).isoformat(),
                },
                "claims": marked(claims),
            }
        )
    )
    return written, marker


@pytest.fixture(scope="module")
def fixture_graph(client, config):
    """Write the fixture under its marked labels (write_fixture;
    idempotent: a re-run within the session reuses and skips everything)
    and wait until the query service holds each subject's latest revision.
    Returns (QIDs by role, source QIDs by letter)."""
    registered = {
        letter: register_fictional(
            client, f"Century fixture source {letter} {MARK}", "certificate"
        )
        for letter in "ABC"
    }
    source = {letter: registered[letter].ref for letter in "ABC"}
    letters = {ref.sha1: letter for letter, ref in source.items()}
    claims = [
        # per A: born in the 19th century, given by its first year
        claim(PERSON, "date_of_birth", date(1801, "century"), source["A"]),
        # per B: in 1900, the 19th century's last year
        claim(PERSON, "date_of_birth", date(1900), source["B"]),
        # per C: in 1800, the 18th century's last year
        claim(PERSON, "date_of_birth", date(1800), source["C"]),
        # per A: the event in the 18th century BCE, 1800 to 1701 BCE
        claim(EVENT, "point_in_time", date(-1800, "century"), source["A"]),
        # per B: in 1701 BCE, the 18th century BCE's last year
        claim(EVENT, "point_in_time", date(-1701), source["B"]),
    ]
    written, _ = write_fixture(client, letters, claims)
    # written again, the fixture finds every item and statement
    again, _ = write_fixture(client, letters, claims)
    assert (again.written, again.referenced) == (0, 0)
    assert again.created_items == written.created_items
    qids = {
        role: written.created_items[ref["local_id"]]
        for role, ref in (("person", PERSON), ("event", EVENT))
    }
    wait_for_query_service(config, client, list(qids.values()))
    return qids, {letter: registered[letter].qid for letter in "ABC"}


# each subject's century: the property that dates it, its value as the
# writer stores it, as Wikibase shows it, as the query service serves it
# and as the site shows it
CENTURIES = {
    "person": (
        "date_of_birth",
        "+1801-00-00T00:00:00Z",
        "19. century",
        "1801-01-01T00:00:00Z",
        "19th century",
    ),
    "event": (
        "point_in_time",
        "-1800-00-00T00:00:00Z",
        "18. century BCE",
        "-1800-01-01T00:00:00Z",
        "−18th century",
    ),
}


def centuries(client, qid: str, pid: str) -> list[dict]:
    """The century-precision time datavalues of an entity's statements
    of one property, as the API gives them."""
    found = client.api_get(
        {"action": "wbgetclaims", "entity": qid, "property": pid}
    )
    return [
        statement["mainsnak"]["datavalue"]["value"]
        for statement in found["claims"].get(pid, [])
        if statement["mainsnak"]["datavalue"]["value"]["precision"] == 7
    ]


@pytest.mark.parametrize("role", sorted(CENTURIES))
def test_wikibase_shows_the_intended_century(client, fixture_graph, role):
    qids, _ = fixture_graph
    properties, _ = client.vocabulary()
    prop, stored, shown, _, _ = CENTURIES[role]
    (value,) = centuries(client, qids[role], properties[prop])
    assert value["time"] == stored
    # as Wikibase's own formatter shows the value on the item's page
    formatted = client.api_get(
        {
            "action": "wbformatvalue",
            "generate": "text/plain",
            "datatype": "time",
            "datavalue": json.dumps({"type": "time", "value": value}),
            "options": json.dumps({"lang": "en"}),
        }
    )
    assert formatted["result"] == shown


@pytest.mark.parametrize("role", sorted(CENTURIES))
def test_the_site_names_the_century_wikibase_shows(
    client, config, fixture_graph, role
):
    qids, _ = fixture_graph
    properties, _ = client.vocabulary()
    prop, _, _, served, shown = CENTURIES[role]
    query = prefix_block(config.concept_uri) + (
        "SELECT ?value ?precision WHERE {\n"
        f"  wd:{properties[prop]} wikibase:claim ?p ;\n"
        "    wikibase:statementValue ?psv .\n"
        f"  wd:{qids[role]} ?p ?statement .\n"
        "  ?statement ?psv ?node .\n"
        "  ?node wikibase:timeValue ?value ;\n"
        "    wikibase:timePrecision ?precision .\n"
        "  FILTER(?precision = 7)\n"
        "}"
    )
    (row,) = run_sparql(config, query)
    value, precision = row["value"]["value"], row["precision"]["value"]
    assert (value, precision) == (served, "7")
    assert format_time(value, precision) == shown


def test_the_report_spans_the_century_wikibase_shows(
    client, config, fixture_graph
):
    qids, source = fixture_graph
    properties, items = client.vocabulary()
    letter = {qid: name for name, qid in source.items()}

    def described(side) -> str:
        day = side.value.term.value.lstrip("+")[:10]
        return f"{day}/{side.value.precision} per " + ",".join(
            letter[s.qid] for s in side.sources
        )

    found = disagreements(
        lambda query: run_sparql(config, query),
        properties,
        items,
        config.concept_uri,
    )
    rows = {
        frozenset({described(row.a), described(row.b)})
        for row in found
        if row.entity_qid == qids["person"]
    }
    # 1900 lies inside the 19th century, so A and B agree; 1800 does not
    assert rows == {
        frozenset({"1801-01-01/7 per A", "1800-01-01/9 per C"}),
        frozenset({"1900-01-01/9 per B", "1800-01-01/9 per C"}),
    }
    # 1701 BCE, served as -1700, lies inside the 18th century BCE
    assert not [row for row in found if row.entity_qid == qids["event"]]


def test_a_year_bce_is_served_one_year_on(client, config, fixture_graph):
    # the one-year shift the report's span of a century BCE allows for: a
    # year before the common era is served in XSD 1.1's count, a century's
    # year as stored
    qids, _ = fixture_graph
    properties, _ = client.vocabulary()
    query = prefix_block(config.concept_uri) + (
        "SELECT ?value ?precision WHERE {\n"
        f"  wd:{properties['point_in_time']} wikibase:claim ?p ;\n"
        "    wikibase:statementValue ?psv .\n"
        f"  wd:{qids['event']} ?p ?statement .\n"
        "  ?statement ?psv ?node .\n"
        "  ?node wikibase:timeValue ?value ;\n"
        "    wikibase:timePrecision ?precision .\n"
        "}"
    )
    served = {
        (row["value"]["value"], row["precision"]["value"])
        for row in run_sparql(config, query)
    }
    assert served == {
        ("-1800-01-01T00:00:00Z", "7"),
        ("-1700-01-01T00:00:00Z", "9"),
    }
