"""Live validation of how a century is dated (#241; docs/MODELING.md,
"Uncertainty and time").

Centuries are counted as Wikibase counts them: the 19th century runs from
1801 to 1900, and a claim gives it by its first year. A fictional fixture
writes one person's birth as three sources date it, in the 19th century,
in 1900 and in 1800, and reads them back through the API: the writer
stores the 19th century as 1801 at century precision, and Wikibase shows
it as "19. century".

The required lane checks the rest: the disagreement report's span for a
century (tests/fictional_integration/test_production_queries.py), and the
static site's name for the century the query service serves
(tests/fictional_integration/test_static_site.py).

A date before the common era is unsupported (ADR-0034, #338):
tests/unit/test_unsupported_dates.py checks that the writer refuses one.

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
from yizkr.claims import ArtifactRef, ClaimBatch, TimeValue
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter

from .conftest import fixture_mark, register_fictional

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
    its artifact's letter in `artifacts`. The writer finds a local item by
    its label, so a version of the fixture with other claims would
    otherwise write onto an earlier version's items and see their
    statements beside its own. With the marker each version writes items
    of its own, and a re-run of one version finds its own items again. The
    hash also covers each claim's time as TimeValue.to_wikibase gives it,
    the datavalue the writer's time conversion starts from, so a
    to_wikibase that gives another time (a mutant of the harness, say)
    writes items of its own too, and never leaves a second value on these.
    A change inside BatchWriter._to_wbi_claim is not hashed, but the
    writer's source is, through the module's marker (conftest.fixture_mark),
    with the session's random bytes: each session writes items of its own,
    so what an earlier session wrote, a live mutant's run included, never
    meets it. Returns the write's result and the marker."""
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
def fixture_graph(client):
    """Write the fixture under its marked labels (write_fixture;
    idempotent: a re-run within the session reuses and skips everything).
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
    ]
    written, _ = write_fixture(client, letters, claims)
    # written again, the fixture finds every item and statement
    again, _ = write_fixture(client, letters, claims)
    assert (again.written, again.referenced) == (0, 0)
    assert again.created_items == written.created_items
    qids = {"person": written.created_items[PERSON["local_id"]]}
    return qids, {letter: registered[letter].qid for letter in "ABC"}


# each subject's century: the property that dates it, its value as the
# writer stores it and as Wikibase shows it
CENTURIES = {
    "person": ("date_of_birth", "+1801-00-00T00:00:00Z", "19. century"),
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
    prop, stored, shown = CENTURIES[role]
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
