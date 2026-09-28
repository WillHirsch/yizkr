"""Live validation that the public export keeps only the people the check
releases (#344; ADR-0020; docs/MODELING.md, "Living persons and privacy").

A fictional fixture writes four people, under labels that carry a marker
of the fixture's content (test_live_privacy.py's write_fixture):

* one typed human, a type a curator then deprecated;
* one typed human, a type a curator then outranked with a preferred
  `organization` type, so that the truthy type is that one;
* one with no type at all, but a birth in 1990;
* one typed human, with a death: the check releases them.

The first two have no birth and no death, and the third's birth is
recent, so the check releases none of them. The truthy `human` type
selected none of the three, and the export dropped only the people the
check had assessed, so each went into the public export in full.

The test dumps the graph as `yizkr backup` does (dumpRdf.php), writes the
public export from it, and reads public.nt.gz: none of the three is
mentioned in any triple, by QID, and the released one is. The export
first waits for the query service to hold the dump's revision of every
item, so on a stack at rest the export must run, not refuse. The site
lists all four and badges the three the export drops.

Like every live test it writes fictional data to the one instance, so
the session that runs it ends by clearing the recorded instance-contents
key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

import pytest
from wikibaseintegrator.wbi_enums import WikibaseRank

from yizkr.backup import dump_rdf, export_public
from yizkr.staticsite import PEOPLE_QUERY, SiteBuilder

from .conftest import register_fictional, wait_for_query_service
from .test_live_privacy import write_fixture

pytestmark = pytest.mark.live

DEPLOY = Path("deploy")
QID_TOKEN = re.compile(r"Q[1-9]\d*\b")


def local(role: str, typed: bool = True) -> dict:
    ref = {
        "kind": "local",
        "local_id": f"export-{role.replace(' ', '-')}",
        "label": f"Export Fixture {role.title()}",
    }
    if typed:
        ref["instance_of"] = "human"
    return ref


PEOPLE = {
    "deprecated type": local("deprecated type"),
    "outranked type": local("outranked type"),
    "no type": local("no type", typed=False),
    "released": local("released"),
}
RELEASED = {"released"}


def claim(role: str, prop: str, value: dict, artifact) -> dict:
    return {
        "subject": PEOPLE[role],
        "property": prop,
        "value": value,
        "qualifiers": [],
        "source": {
            "artifact": artifact.model_dump(mode="json"),
            "page_number": "1",
            "transcription_excerpt": {
                "text": "fictional export fixture",
                "language": "en",
            },
        },
    }


def nickname(role: str, artifact) -> dict:
    """A claim that is no person evidence, so that the person's type is
    all that marks them."""
    return claim(
        role,
        "nickname",
        {
            "type": "monolingual",
            "value": {"text": "Fixture", "language": "en"},
        },
        artifact,
    )


def typing(client, subject: str, kind: str):
    """The `instance of` statement that types `subject` as `kind`, and
    the item it is on."""
    properties, items = client.vocabulary()
    entity = client.wbi.item.get(subject)
    (statement,) = [
        s
        for s in entity.claims.get(properties["instance_of"])
        if s.mainsnak.datavalue["value"]["id"] == items[kind]
    ]
    return entity, statement


def deprecate_type(client, subject: str) -> None:
    """A curator's deprecated rank on the item's `human` type: an optional
    overlay (ADR-0002). Idempotent."""
    entity, statement = typing(client, subject, "human")
    if statement.rank != WikibaseRank.DEPRECATED:
        statement.rank = WikibaseRank.DEPRECATED
        entity.write(summary="integration test: curator rank on a type")


def prefer_another_type(client, subject: str, artifact_qid: str) -> None:
    """A curator's `organization` type at preferred rank beside the
    item's `human` type, stated in the artifact `artifact_qid` (rule 1).
    Idempotent."""
    from wikibaseintegrator.datatypes import Item
    from wikibaseintegrator.models import Reference, References
    from wikibaseintegrator.wbi_enums import ActionIfExists

    properties, items = client.vocabulary()
    entity = client.wbi.item.get(subject)
    if any(
        s.mainsnak.datavalue["value"]["id"] == items["organization"]
        for s in entity.claims.get(properties["instance_of"])
    ):
        return
    reference = Reference()
    reference.add(Item(prop_nr=properties["stated_in"], value=artifact_qid))
    references = References()
    references.add(reference)
    entity.claims.add(
        Item(
            prop_nr=properties["instance_of"],
            value=items["organization"],
            references=references,
            rank=WikibaseRank.PREFERRED,
        ),
        action_if_exists=ActionIfExists.FORCE_APPEND,
    )
    entity.write(summary="integration test: a curator's preferred type")


@pytest.fixture(scope="module")
def fixture_graph(client, config):
    """Write the fixture under its marked labels (idempotent: re-runs
    reuse and skip everything), set the curators' types, and wait until
    the query service holds every fixture person's latest revision.
    Returns the QIDs by role."""
    source = register_fictional(client, "Export fixture source", "certificate")
    written = write_fixture(
        client,
        {source.ref.sha1: "A"},
        [
            nickname("deprecated type", source.ref),
            nickname("outranked type", source.ref),
            claim(
                "no type",
                "date_of_birth",
                {"type": "time", "year": 1990},
                source.ref,
            ),
            claim(
                "released",
                "date_of_death",
                {"type": "time", "year": 1995},
                source.ref,
            ),
        ],
    )
    qids = {
        role: written.created_items[ref["local_id"]]
        for role, ref in PEOPLE.items()
    }
    deprecate_type(client, qids["deprecated type"])
    prefer_another_type(client, qids["outranked type"], source.qid)
    wait_for_query_service(config, client, list(qids.values()))
    return qids


@pytest.fixture(scope="module")
def exported(config, fixture_graph, tmp_path_factory):
    """The dump and its public export, in a directory of their own."""
    if not (DEPLOY / "docker-compose.yml").is_file():
        pytest.skip("run from the repo root")
    out_dir = tmp_path_factory.mktemp("public-export")
    dump_rdf(DEPLOY, out_dir / "full.nt.gz")
    export_public(config, out_dir)
    return out_dir


def test_the_fixture_takes_the_shapes_the_truthy_type_missed(
    client, fixture_graph
):
    properties, _ = client.vocabulary()
    _, deprecated = typing(client, fixture_graph["deprecated type"], "human")
    assert deprecated.rank == WikibaseRank.DEPRECATED
    outranked = fixture_graph["outranked type"]
    assert typing(client, outranked, "human")[1].rank == WikibaseRank.NORMAL
    assert (
        typing(client, outranked, "organization")[1].rank
        == WikibaseRank.PREFERRED
    )
    untyped = client.wbi.item.get(fixture_graph["no type"])
    assert untyped.claims.get(properties["instance_of"]) == []


def test_only_released_people_are_in_the_public_export(
    fixture_graph, exported
):
    with gzip.open(exported / "public.nt.gz", "rt", encoding="utf-8") as dump:
        mentioned = {
            token for line in dump for token in QID_TOKEN.findall(line)
        }
    assert {
        role for role, qid in fixture_graph.items() if qid in mentioned
    } == RELEASED


def test_the_exclusions_name_whom_the_export_drops(fixture_graph, exported):
    exclusions = json.loads((exported / "public-exclusions.json").read_text())
    for role, qid in fixture_graph.items():
        assert (qid in exclusions["excluded"]) == (role not in RELEASED)
        assert (qid in exclusions["included_persons"]) == (role in RELEASED)


def test_the_site_badges_whom_the_export_drops(
    config, fixture_graph, exported, tmp_path
):
    exclusions = json.loads((exported / "public-exclusions.json").read_text())
    builder = SiteBuilder(config, tmp_path / "site")
    people = {
        row["person"]["value"].rsplit("/", 1)[-1]: row
        for row in builder.run(builder._instance_query(PEOPLE_QUERY))
    }
    for qid in fixture_graph.values():
        badged = (
            "possibly living — never in public exports"
            in builder.person_page(people[qid], [])
        )
        assert badged == (qid in exclusions["excluded"])
