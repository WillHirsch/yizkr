"""Live validation that a birth or death event is its principal's own
(#345; ADR-0020, ADR-0035 §3.2; docs/MODELING.md, "Living persons and
privacy").

A fictional fixture writes people born in 1990, or in 1850, and the
birth and death events they take part in, and reads the non-privileged
export's exclusions back through the stack, with the cutoff pinned at
1903:

* a death releases its principal, and never an informant or a witness at
  it: every one of them was born in 1990;
* a death that states no role releases its one participant, and neither
  of two;
* a birth event in 1990 keeps its principal, born 1850 by their own
  `date of birth`, possibly living, and is no birth of an informant at it,
  born 1850 too, who is released.

The static site's badge marks exactly the fixture people the export
drops, at the same cutoff.

Each source is registered with a fictional PNG of its own
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
from yizkr.staticsite import PEOPLE_QUERY, SiteBuilder

from .conftest import fixture_mark, register_fictional, wait_for_query_service

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#345: people born in 1990 or 1850 who take part in birth and death "
    "events, as principal, informant or witness, or with no role"
)
MARK = fixture_mark(FIXTURE, writer, privacy)

CUTOFF = 1903

# Each fixture person, the year their own `date of birth` gives, and
# whether the export must exclude them at the pinned cutoff.
PEOPLE = {
    "principal": (1990, False),
    "informant": (1990, True),
    "witness": (1990, True),
    "sole": (1990, False),
    "pair one": (1990, True),
    "pair two": (1990, True),
    "born twice": (1850, True),
    "birth informant": (1850, False),
}
EXCLUDED = {role: excluded for role, (_, excluded) in PEOPLE.items()}


def local(key: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": f"event-privacy-{key.replace(' ', '-')}",
        "label": f"Event Privacy Fixture {key.title()} ({MARK})",
        "instance_of": kind,
    }


ENTITIES = {
    **{role: local(role, "human") for role in PEOPLE},
    # a death with a principal, an informant and a witness
    "death": local("death", "death"),
    # a death that states no role, with one participant
    "lone death": local("lone death", "death"),
    # a death that states no role, with two
    "shared death": local("shared death", "death"),
    # a birth in 1990, with a principal and an informant
    "birth": local("birth", "birth"),
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
                "text": "fictional event privacy fixture",
                "language": "en",
            },
        },
    }


def year(value: int) -> dict:
    return {"type": "time", "year": value}


def takes_part(event: str, role: str, artifact, as_role: str | None):
    """`role` takes part in `event`, in the role `as_role`, or in none."""
    qualifiers = (
        [
            {
                "property": "object_has_role",
                "value": {
                    "type": "item",
                    "value": {"kind": "vocab", "key": as_role},
                },
            }
        ]
        if as_role
        else []
    )
    return claim(
        event,
        "participant",
        {"type": "item", "value": ENTITIES[role]},
        artifact,
        qualifiers,
    )


@pytest.fixture(scope="module")
def fixture_graph(client, config):
    """Write the fixture under its marked labels and wait until the query
    service holds every fixture item's latest revision. Returns the QIDs
    of the people by role."""
    source = register_fictional(
        client, f"Event privacy fixture source {MARK}", "certificate"
    ).ref
    claims = [
        claim(role, "date_of_birth", year(born), source)
        for role, (born, _) in PEOPLE.items()
    ]
    claims += [
        takes_part("death", "principal", source, "principal"),
        takes_part("death", "informant", source, "informant"),
        takes_part("death", "witness", source, "witness"),
        takes_part("lone death", "sole", source, None),
        takes_part("shared death", "pair one", source, None),
        takes_part("shared death", "pair two", source, None),
        takes_part("birth", "born twice", source, "principal"),
        takes_part("birth", "birth informant", source, "informant"),
        claim("birth", "point_in_time", year(1990), source),
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
    wait_for_query_service(config, client, list(qids.values()))
    return {role: qids[role] for role in PEOPLE}


def test_the_export_releases_only_each_events_principal(config, fixture_graph):
    qids = fixture_graph
    report = exclusion_report(
        config, cutoff_years=datetime.now(UTC).year - CUTOFF
    )
    assert set(qids.values()) <= report.excluded | report.included
    assert {role: qid in report.excluded for role, qid in qids.items()} == (
        EXCLUDED
    )
    assert report.reasons[qids["principal"]] == "death recorded"
    assert report.reasons[qids["born twice"]] == (
        "born as late as 1990, with no death evidence"
    )


def test_the_site_badges_whom_the_export_drops(
    config, fixture_graph, tmp_path
):
    qids = fixture_graph
    fixture = set(qids.values())
    cutoff_years = datetime.now(UTC).year - CUTOFF
    report = exclusion_report(config, cutoff_years=cutoff_years)
    builder = SiteBuilder(config, tmp_path / "site")
    builder._cutoff_year = CUTOFF
    people = {
        row["person"]["value"].rsplit("/", 1)[-1]: row
        for row in builder.run(builder._instance_query(PEOPLE_QUERY))
    }
    badged = {
        qid
        for qid in fixture
        if "possibly living — never in the non-privileged export"
        in builder.person_page(people[qid], [])
    }
    assert badged == report.excluded & fixture
    assert {role for role, qid in qids.items() if qid in badged} == {
        role for role, excluded in EXCLUDED.items() if excluded
    }
