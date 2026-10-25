"""Live validation that a death event is no person on the death it states
(#577; ADR-0020, as amended by #577; docs/MODELING.md, "Living persons
and privacy").

A fictional fixture writes Eugen Löbig's death as an event, typed `death`, with
Eugen as its principal, as a curator makes one in the wiki (ADR-0040 Q5: no
extraction or import makes a death event, and a curator-made one still releases
its principal; curator_event_writes): the event states his cause of death
("Wassersucht") and his age ("73 Jahre"), and its point in time, 1909. Beside
it, a death event that states a cause of death and a `date of birth` too, and
one whose only cause of death a curator deprecated. Read back through the
stack:

* the site's people roster holds no row for Eugen's death, so it gets no
  person page, and the check does not assess it; Eugen is in the roster,
  released by his death;
* Eugen's timeline still shows his death, dated by the event;
* a death event with a `date of birth` is still a person to the site and
  to the check: an event is set aside on its death evidence alone;
* a death event whose cause of death is deprecated, so that it has no
  truthy one, is still a person to the site and to the check, which
  excludes it: an event is set aside only on death evidence the check
  would read as such.

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
from yizkr.backup import exclusion_report
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core import privacy
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter
from yizkr.staticsite import PEOPLE_QUERY, SiteBuilder

from .conftest import (
    curator_event_writes,
    fixture_mark,
    register_fictional,
    wait_for_query_service,
)

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#577: Eugen Löbig's death as an event stating his cause of death and "
    "his age, with Eugen its principal; a death event stating a cause of "
    "death and a date of birth; a death event whose only cause of death a "
    "curator deprecated"
)
MARK = fixture_mark(FIXTURE, writer, privacy)


def local(key: str, kind: str, label: str) -> dict:
    return {
        "kind": "local",
        "local_id": f"event-people-{key}",
        "label": f"{label} ({MARK})",
        "instance_of": kind,
    }


ENTITIES = {
    "eugen": local("eugen", "human", "Eugen Löbig"),
    "death": local("death", "death", "Death of Eugen Löbig"),
    "born death": local("born-death", "death", "Death stating a birth"),
    "deprecated death": local(
        "deprecated-death", "death", "Death with a deprecated cause"
    ),
}


def claim(
    subject: str, prop: str, value: dict, artifact: ArtifactRef, qualifiers=()
) -> dict:
    return {
        "subject": ENTITIES[subject],
        "property": prop,
        "value": value,
        "qualifiers": list(qualifiers),
        "source": {
            "artifact": artifact.model_dump(mode="json"),
            "page_number": "1",
            "transcription_excerpt": {
                "text": "Todesursache: Wassersucht. Alter: 73 Jahre.",
                "language": "de",
            },
        },
    }


def cause(text: str) -> dict:
    return {"type": "monolingual", "value": {"text": text, "language": "de"}}


def deprecate_cause(client, subject: str) -> None:
    """A curator's deprecated rank on the cause of death of `subject`: an
    optional overlay (ADR-0002). Idempotent."""
    properties, _ = client.vocabulary()
    entity = client.wbi.item.get(subject)
    (statement,) = entity.claims.get(properties["cause_of_death"])
    if statement.rank != WikibaseRank.DEPRECATED:
        statement.rank = WikibaseRank.DEPRECATED
        entity.write(summary="integration test: curator rank on a fixture")


@pytest.fixture(scope="module")
def fixture_graph(client, config):
    """Write the fixture under its marked labels and wait until the query
    service holds every fixture item's latest revision. Returns the QIDs
    by key."""
    source = register_fictional(
        client, f"Event people fixture burial register {MARK}", "certificate"
    ).ref
    principal = {
        "property": "object_has_role",
        "value": {
            "type": "item",
            "value": {"kind": "vocab", "key": "principal"},
        },
    }
    claims = [
        claim(
            "death",
            "participant",
            {"type": "item", "value": ENTITIES["eugen"]},
            source,
            [principal],
        ),
        claim("death", "cause_of_death", cause("Wassersucht"), source),
        claim(
            "death",
            "age_at_death",
            {"type": "string", "value": "73 Jahre"},
            source,
        ),
        claim(
            "death", "point_in_time", {"type": "time", "year": 1909}, source
        ),
        claim("born death", "cause_of_death", cause("Wassersucht"), source),
        claim(
            "born death",
            "date_of_birth",
            {"type": "time", "year": 1836},
            source,
        ),
        claim(
            "deprecated death", "cause_of_death", cause("Wassersucht"), source
        ),
    ]
    with curator_event_writes():
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
    deprecate_cause(client, qids["deprecated death"])
    wait_for_query_service(config, client, list(qids.values()))
    return qids


def roster(config, tmp_path) -> tuple[SiteBuilder, dict[str, dict]]:
    builder = SiteBuilder(config, tmp_path / "site")
    people = {
        row["person"]["value"].rsplit("/", 1)[-1]: row
        for row in builder.run(builder._instance_query(PEOPLE_QUERY))
    }
    return builder, people


def test_a_death_event_gets_no_person_page(config, fixture_graph, tmp_path):
    qids = fixture_graph
    builder, people = roster(config, tmp_path)
    assert qids["death"] not in people
    assert qids["eugen"] in people
    # Eugen is released by his death, so his page carries no badge
    assert "possibly living" not in builder.person_page(
        people[qids["eugen"]], []
    )


def test_the_check_does_not_assess_a_death_event(config, fixture_graph):
    qids = fixture_graph
    report = exclusion_report(config)
    assert qids["death"] not in report.excluded | report.included
    assert qids["eugen"] in report.included
    assert report.reasons[qids["eugen"]] == "death recorded"


def test_the_principals_timeline_still_shows_the_death(
    config, fixture_graph, tmp_path
):
    qids = fixture_graph
    builder = SiteBuilder(config, tmp_path / "site")
    rows = builder.run(builder._library_query("person-timeline"))
    dates = {
        row["date"]["value"][:4]
        for row in rows
        if row["person"]["value"].endswith(f"/{qids['eugen']}")
    }
    assert "1909" in dates


def test_an_event_with_other_evidence_is_still_a_person(
    config, fixture_graph, tmp_path
):
    # fails closed: a `date of birth` selects the event whatever its type
    qids = fixture_graph
    _, people = roster(config, tmp_path)
    assert qids["born death"] in people
    report = exclusion_report(config)
    assert qids["born death"] in report.included | report.excluded


def test_an_event_with_no_truthy_death_is_still_a_person(
    config, fixture_graph, tmp_path
):
    # fails closed: a deprecated cause of death is no death evidence to the
    # check, so it sets nothing aside, and with no birth the check excludes
    # the event
    qids = fixture_graph
    _, people = roster(config, tmp_path)
    assert qids["deprecated death"] in people
    report = exclusion_report(config)
    assert qids["deprecated death"] in report.excluded
