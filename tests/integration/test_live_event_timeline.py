"""Live validation that an event's rows on the person timeline are read
at any rank but deprecated, once per date whatever its types (#540;
docs/MODELING.md, "Marriages, residences and other periods").

A fictional fixture writes Leopold Löbig's imprisonment, dated by its
point in time (September 1939) and its own start and end time (3
September and 1 October 1939), and (here) typed three times, as an
`imprisonment`, an `event` and a `military service`, with three
participants: Leopold, Ida and Felix. A curator then prefers the `event`
type and Leopold's participant statement, and deprecates the `military
service` type and Felix's participant statement: optional overlays
(ADR-0002). Read back through the stack:

* Leopold and Ida each get the event's three rows, once each: the
  preferred participant hides no normal-rank one, and a second type adds
  no row;
* each row names the event by both of its types that are not
  deprecated, sorted, the preferred one beside the normal one, as the
  static site shows it ("event, imprisonment (start time)");
* Felix, deprecated, gets none of them.

The source is registered with a fictional PNG of its own
(register_fictional), and every item's label carries the session's marker
(fixture_mark). Like every live test it writes fictional data to the one
instance, so the session that runs it ends by clearing the recorded
instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wikibaseintegrator.wbi_enums import WikibaseRank

from tests.fixtures.identity import write_mapped
from yizkr import staticsite
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter
from yizkr.staticsite import SiteBuilder

from .conftest import fixture_mark, register_fictional, wait_for_query_service

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#540: Leopold Löbig's imprisonment, dated by its point in time, start "
    "time and end time, typed an imprisonment, an event (preferred) and a "
    "military service (deprecated), with Leopold (preferred), Ida and "
    "Felix (deprecated) its participants"
)
MARK = fixture_mark(FIXTURE, writer, staticsite)


def local(key: str, kind: str, label: str) -> dict:
    return {
        "kind": "local",
        "local_id": f"event-timeline-{key}",
        "label": f"{label} ({MARK})",
        "instance_of": kind,
    }


ENTITIES = {
    "leopold": local("leopold", "human", "Leopold Löbig"),
    "ida": local("ida", "human", "Ida Löbig"),
    "felix": local("felix", "human", "Felix Löbig"),
    "held": local("held", "imprisonment", "Imprisonment of Leopold Löbig"),
}


def claim(subject: str, prop: str, value: dict, artifact: ArtifactRef):
    return {
        "subject": ENTITIES[subject],
        "property": prop,
        "value": value,
        "source": {
            "artifact": artifact.model_dump(mode="json"),
            "page_number": "1",
            "transcription_excerpt": {
                "text": "Leopold LÖBIG, arrested 3.9.1939",
                "language": "de",
            },
        },
    }


def item(value: dict) -> dict:
    return {"type": "item", "value": value}


def curate(client, subject: str, prop: str, ranks: dict[str, WikibaseRank]):
    """A curator's rank on each of `subject`'s `prop` statements whose
    value is a key of `ranks`: optional overlays (ADR-0002). Idempotent."""
    properties, _ = client.vocabulary()
    entity = client.wbi.item.get(subject)
    changed = False
    for statement in entity.claims.get(properties[prop]):
        rank = ranks.get(statement.mainsnak.datavalue["value"]["id"])
        if rank is not None and statement.rank != rank:
            statement.rank = rank
            changed = True
    if changed:
        entity.write(summary="integration test: curator ranks on a fixture")


@pytest.fixture(scope="module")
def fixture_graph(client, config):
    """Write the fixture under its marked labels, set the curator's ranks
    and wait until the query service holds every fixture item's latest
    revision. Returns the QIDs by key, the source's under "card"."""
    _, items = client.vocabulary()
    registered = register_fictional(
        client,
        f"Event timeline fixture registration card {MARK}",
        "certificate",
    )
    source = registered.ref
    claims = [
        claim(
            "held", "instance_of", item({"kind": "vocab", "key": key}), source
        )
        for key in ("event", "military_service")
    ]
    claims += [
        claim("held", "participant", item(ENTITIES[who]), source)
        for who in ("leopold", "ida", "felix")
    ]
    claims += [
        claim("held", prop, {"type": "time", **when}, source)
        for prop, when in (
            ("point_in_time", {"year": 1939, "month": 9}),
            ("start_time", {"year": 1939, "month": 9, "day": 3}),
            ("end_time", {"year": 1939, "month": 10, "day": 1}),
        )
    ]
    written = write_mapped(
        BatchWriter(client),
        ClaimBatch.model_validate(
            {
                "extraction_run": {
                    "model_identifier": "integration-test",
                    "pipeline_version": "0.0.0",
                    "started_at": datetime.now(UTC).isoformat(),
                },
                "claims": claims,
            }
        ),
    )
    qids = {
        key: written.created_items[ref["local_id"]]
        for key, ref in ENTITIES.items()
    }
    curate(
        client,
        qids["held"],
        "instance_of",
        {
            items["event"]: WikibaseRank.PREFERRED,
            items["military_service"]: WikibaseRank.DEPRECATED,
        },
    )
    curate(
        client,
        qids["held"],
        "participant",
        {
            qids["leopold"]: WikibaseRank.PREFERRED,
            qids["felix"]: WikibaseRank.DEPRECATED,
        },
    )
    wait_for_query_service(config, client, list(qids.values()))
    return {**qids, "card": registered.qid}


@pytest.fixture(scope="module")
def timeline(config, fixture_graph, tmp_path_factory) -> list[dict]:
    """The person timeline's rows for the fixture's three people."""
    builder = SiteBuilder(config, tmp_path_factory.mktemp("site"))
    people = {fixture_graph[key] for key in ("leopold", "ida", "felix")}
    return [
        row
        for row in builder.run(builder._library_query("person-timeline"))
        if row["person"]["value"].rsplit("/", 1)[-1] in people
    ]


def test_each_participant_but_a_deprecated_one_gets_each_row_once(
    fixture_graph, timeline
):
    qids = fixture_graph
    who = {qids[key]: key for key in ("leopold", "ida", "felix")}
    rows = [
        (
            who[row["person"]["value"].rsplit("/", 1)[-1]],
            SiteBuilder.timeline_what(row),
            row["date"]["value"].lstrip("+")[:10],
            row["precision"]["value"],
        )
        for row in timeline
    ]
    # a list, not a set: a second type repeating a row would show here
    assert sorted(rows) == sorted(
        (person, what, date, precision)
        for person in ("leopold", "ida")
        for what, date, precision in (
            ("event, imprisonment", "1939-09-01", "10"),
            ("event, imprisonment (start time)", "1939-09-03", "11"),
            ("event, imprisonment (end time)", "1939-10-01", "11"),
        )
    )


def test_each_row_cites_the_source_of_its_date(fixture_graph, timeline):
    sources = {row["source"]["value"].rsplit("/", 1)[-1] for row in timeline}
    assert sources == {fixture_graph["card"]}
