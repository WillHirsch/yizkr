"""Live validation of the public export's possibly-living exclusions
(#334; ADR-0020; docs/MODELING.md, "Living persons and privacy").

A fictional fixture writes people whose births take each shape the check
reads, with a curator's rank on three of the birth statements and a
curator's birth of unknown value beside an 1850 one, and reads the
export's exclusions back through the stack, with the cutoff pinned at
1903:

* a birth releases a person only at the latest year it allows: a birth
  in the 20th century not before 2000, in the decade 1900-1909 not before
  1909, and one in the 1890s by 1899; a year releases in the cutoff year,
  not the year after;
* every birth statement counts, never one sampled from the truthy
  values: births in 1850 and 1990 keep a person possibly living, even
  when a curator prefers the 1850 one, and so does a birth of unknown
  value beside an 1850 one;
* a statement a curator deprecated counts for nothing, either way, its
  latest date included: a deprecated birth "before 1850" releases no one,
  and a deprecated 1990 birth keeps no one;
* a range its source states bounds the birth: "before 1850" releases, a
  20th-century birth no later than 1902 releases, "after 1850" is open at
  its late end and releases no one, and "between 1850 and 1990" not
  before 1990;
* a birth before the common era is not read, and a person with no birth
  is excluded: unknown means excluded;
* death evidence releases a person whatever their birth.

The static site's badge marks exactly the fixture people the export
drops, at the default cutoff both use.

Each source is registered with a fictional PNG of its own
(register_fictional). Like every live test it writes fictional data to
the one instance, so the session that runs it ends by clearing the
recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from wikibaseintegrator.wbi_enums import WikibaseRank

from yizkr.backup import exclusion_report
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import prefix_block
from yizkr.staticsite import PEOPLE_QUERY, SiteBuilder

from .conftest import register_fictional, wait_for_query_service

pytestmark = pytest.mark.live

CUTOFF = 1903


def local(role: str) -> dict:
    words = role.title()
    return {
        "kind": "local",
        "local_id": f"privacy-{role.replace(' ', '-')}",
        "label": f"Privacy Fixture {words}",
        "instance_of": "human",
    }


# Each fixture person, and whether the export must exclude them at the
# pinned cutoff.
EXCLUDED = {
    "century": True,
    "late decade": True,
    "early decade": False,
    "cutoff year": False,
    "year after": True,
    "two births": True,
    "preferred early": True,
    "deprecated early": True,
    "deprecated late": False,
    "unknown beside": True,
    "bounded century": False,
    "before": False,
    "after": True,
    "between": True,
    "before the common era": True,
    "no birth": True,
    "died": False,
}
PEOPLE = {role: local(role) for role in EXCLUDED}


def date(year: int, precision: str | None = None) -> dict:
    value = {"type": "time", "year": year}
    if precision:
        value["precision"] = precision
    return value


def claim(
    role: str, prop: str, value: dict, artifact: ArtifactRef, qualifiers=()
) -> dict:
    return {
        "subject": PEOPLE[role],
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
                "text": "fictional privacy fixture",
                "language": "en",
            },
        },
    }


def born(role: str, value: dict, artifact: ArtifactRef, *qualifiers) -> dict:
    return claim(role, "date_of_birth", value, artifact, qualifiers)


def write_fixture(client, artifacts: dict[str, str], claims: list[dict]):
    """Write the fixture's claims under item labels that carry a marker:
    six hex digits of a hash over the claims, each cited SHA-1 replaced by
    its artifact's letter in `artifacts`, as test_live_period_dates.py's
    write_fixture marks its own. The writer finds a local item by its
    label, so a version of the fixture with other claims would otherwise
    write onto an earlier version's items and see their statements beside
    its own. With the marker each version writes items of its own, and a
    re-run of one version finds its own items again. The curator ranks set
    afterwards are not in the marker. Returns the write's result."""
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
        json.dumps(content, sort_keys=True).encode()
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

    return BatchWriter(client).write_batch(
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


def set_rank(client, subject: str, year: int, rank: WikibaseRank) -> None:
    """A curator's rank on the birth statement of `year`: an optional
    overlay (ADR-0002). Idempotent."""
    properties, _ = client.vocabulary()
    entity = client.wbi.item.get(subject)
    (statement,) = [
        s
        for s in entity.claims.get(properties["date_of_birth"])
        if s.mainsnak.datavalue["value"]["time"].startswith(f"+{year}-")
    ]
    if statement.rank != rank:
        statement.rank = rank
        entity.write(summary="integration test: curator rank on a fixture")


def add_unknown_birth(client, subject: str, artifact_qid: str) -> None:
    """A curator's birth of unknown value, stated in the artifact
    `artifact_qid`: the writer writes no unknown value, so it is added
    here, with its reference (rule 1). Idempotent."""
    from wikibaseintegrator.datatypes import Item, Time
    from wikibaseintegrator.models import Reference, References
    from wikibaseintegrator.wbi_enums import ActionIfExists, WikibaseSnakType

    properties, _ = client.vocabulary()
    entity = client.wbi.item.get(subject)
    unknown = WikibaseSnakType.UNKNOWN_VALUE
    if any(
        statement.mainsnak.snaktype == unknown
        for statement in entity.claims.get(properties["date_of_birth"])
    ):
        return
    reference = Reference()
    reference.add(Item(prop_nr=properties["stated_in"], value=artifact_qid))
    references = References()
    references.add(reference)
    entity.claims.add(
        Time(
            prop_nr=properties["date_of_birth"],
            snaktype=unknown,
            references=references,
        ),
        action_if_exists=ActionIfExists.FORCE_APPEND,
    )
    entity.write(summary="integration test: a birth of unknown value")


@pytest.fixture(scope="module")
def fixture_graph(client, config):
    """Write the fixture under its marked labels (write_fixture;
    idempotent: re-runs reuse and skip everything), set the curator ranks,
    add the birth of unknown value, and wait until the query service holds
    every fixture person's latest revision. Returns the QIDs by role."""
    registered = {
        letter: register_fictional(
            client, f"Privacy fixture source {letter}", "certificate"
        )
        for letter in "AB"
    }
    a, b = (registered[letter].ref for letter in "AB")
    written = write_fixture(
        client,
        {a.sha1: "A", b.sha1: "B"},
        [
            born("century", date(1901, "century"), a),
            born("late decade", date(1900, "decade"), a),
            born("early decade", date(1890, "decade"), a),
            born("cutoff year", date(CUTOFF), a),
            born("year after", date(CUTOFF + 1), a),
            born("two births", date(1850), a),
            born("two births", date(1990), b),
            # the curator prefers the 1850 birth, below
            born("preferred early", date(1850), a),
            born("preferred early", date(1990), b),
            # deprecated below: the only birth, "before 1850"
            born(
                "deprecated early",
                date(1850),
                a,
                ("latest_date", date(1850)),
            ),
            # the 1990 birth is deprecated below
            born("deprecated late", date(1850), a),
            born("deprecated late", date(1990), b),
            # a curator adds a birth of unknown value beside it, below
            born("unknown beside", date(1850), a),
            born(
                "bounded century",
                date(1901, "century"),
                a,
                ("latest_date", date(1902)),
            ),
            born("before", date(1850), a, ("latest_date", date(1850))),
            born("after", date(1850), a, ("earliest_date", date(1850))),
            born(
                "between",
                date(1850),
                a,
                ("earliest_date", date(1850)),
                ("latest_date", date(1990)),
            ),
            born("before the common era", date(-1850), a),
            claim(
                "no birth",
                "birth_name",
                {
                    "type": "monolingual",
                    "value": {"text": "Privacy Fixture", "language": "en"},
                },
                a,
            ),
            born("died", date(1990), a),
            claim("died", "date_of_death", date(1995), a),
        ],
    )
    qids = {
        role: written.created_items[ref["local_id"]]
        for role, ref in PEOPLE.items()
    }
    set_rank(client, qids["preferred early"], 1850, WikibaseRank.PREFERRED)
    set_rank(client, qids["deprecated early"], 1850, WikibaseRank.DEPRECATED)
    set_rank(client, qids["deprecated late"], 1990, WikibaseRank.DEPRECATED)
    add_unknown_birth(client, qids["unknown beside"], registered["B"].qid)
    wait_for_query_service(config, client, list(qids.values()))
    return qids


def test_the_export_excludes_whom_no_birth_releases(config, fixture_graph):
    qids = fixture_graph
    report = exclusion_report(
        config, cutoff_years=datetime.now(UTC).year - CUTOFF
    )
    assert set(qids.values()) <= report.excluded | report.included
    assert {role: qid in report.excluded for role, qid in qids.items()} == (
        EXCLUDED
    )


def test_the_site_badges_whom_the_export_drops(
    client, config, fixture_graph, tmp_path
):
    qids = fixture_graph
    fixture = set(qids.values())
    report = exclusion_report(config)
    properties, items = client.vocabulary()
    builder = SiteBuilder(config, tmp_path / "site")
    people = {
        row["person"]["value"].rsplit("/", 1)[-1]: row
        for row in builder.run(
            prefix_block(config.concept_uri)
            + PEOPLE_QUERY.format(**{**items, **properties})
        )
    }
    badged = {
        qid
        for qid in fixture
        if "possibly living — never in public exports"
        in builder.person_page(people[qid], [])
    }
    assert badged == report.excluded & fixture
