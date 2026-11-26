"""Live validation that a death event is no person on the death it states
(#577; ADR-0020, as amended by #577; docs/MODELING.md, "Living persons
and privacy").

A fictional fixture writes Eugen Löbig's death as an event, typed `death`, with
Eugen as its principal, as a curator makes one in the wiki (ADR-0040 Q5: no
extraction or import makes a death event, and a curator-made one still releases
its principal; curator_event_writes): the event states his cause of death
("Wassersucht") and his age ("73 Jahre"), and its point in time, 1909. Read
back through the stack, the check does not assess Eugen's death, and
releases Eugen by it.

The required lane checks the rest: that the site's people roster holds no
row for such an event, and that one with a `date of birth` or whose cause
of death is deprecated is still a person to the site and to the check
(tests/fictional_integration/test_static_site.py), and that the person
timeline dates Eugen's death by such an event
(tests/fictional_integration/test_production_queries.py).

The source is registered with a fictional PNG of its own
(register_fictional), and every item's label carries the session's marker
(fixture_mark). Like every live test it writes fictional data to the one
instance, so the session that runs it ends by clearing the recorded
instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import write_mapped
from yizkr.backup import exclusion_report
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core import privacy
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter

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
    "his age, with Eugen its principal"
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
    wait_for_query_service(config, client, list(qids.values()))
    return qids


def test_the_check_does_not_assess_a_death_event(config, fixture_graph):
    qids = fixture_graph
    report = exclusion_report(config)
    assert qids["death"] not in report.excluded | report.included
    assert qids["eugen"] in report.included
    assert report.reasons[qids["eugen"]] == "death recorded"
