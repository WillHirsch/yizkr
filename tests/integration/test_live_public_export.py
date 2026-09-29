"""Live validation that the public export keeps only the people the check
releases, and leaves out what states no type (#344; ADR-0020;
docs/MODELING.md, "Living persons and privacy").

A fictional fixture writes, under labels that carry a marker of the
fixture's content (test_live_privacy.py's write_fixture):

* one person typed human, a type a curator then deprecated;
* one typed human, a type a curator then outranked with a preferred
  `organization` type, so that the truthy type is that one;
* one with no type at all, but a birth in 1990;
* one typed as an organization, with a birth in 1990;
* one typed human, with a death: the check releases them;
* a migration, and two vessels that take part in it: one typed only as a
  vessel, the other with a `human` type too, which a curator deprecated;
* a person typed human, born in 1990, into whom a curator merges a
  duplicate, whose item becomes a redirect to them;
* a family with a religion, which is no person evidence: the export
  leaves every family out, as any of its people may be living.

The two whose type a curator deprecated or outranked carry a religion
beside it, which is no person evidence, so that their type is what marks
them. It also writes an item with a label and nothing else: no type, and
no statement at all.

None of the people but the released one is released by the check: the
truthy `human` type selected no one of the first four, and the export
dropped only the people the check had assessed, so each went into the
public export in full. A vessel's part in a migration does not make it a
person, but a `human` type at any rank does. An item that states no type
is left out, whatever else it holds, and a redirect goes with the item it
redirects to.

The test dumps the graph as `yizkr backup` does (dumpRdf.php), writes the
public export from it, and reads public.nt.gz: it mentions the released
person, the plain vessel and the migration by QID, and none of the other
nine, the label-only item, the family and the redirect included. The
export first waits for the query service to hold the dump's revision of
every item and property, a redirect's aside, so on a stack at rest, a
merge included, the export must run, not refuse. The site lists the
seven that have person evidence, and badges the six the export drops.

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

from yizkr.backup import FAMILY, NO_TYPE, dump_rdf, export_public
from yizkr.staticsite import PEOPLE_QUERY, SiteBuilder

from .conftest import register_fictional, wait_for_query_service
from .test_live_privacy import write_fixture

pytestmark = pytest.mark.live

DEPLOY = Path("deploy")
QID_TOKEN = re.compile(r"Q[1-9]\d*\b")
LABEL_ONLY = "Export Fixture Label Only"
MERGED_AWAY = "Export Fixture Merged Away"


def local(role: str, kind: str | None = "human") -> dict:
    ref = {
        "kind": "local",
        "local_id": f"export-{role.replace(' ', '-')}",
        "label": f"Export Fixture {role.title()}",
    }
    if kind:
        ref["instance_of"] = kind
    return ref


ENTITIES = {
    "deprecated type": local("deprecated type"),
    "outranked type": local("outranked type"),
    "no type": local("no type", kind=None),
    "organization born": local("organization born", kind="organization"),
    "released": local("released"),
    "migration": local("migration", kind="migration"),
    "vessel": local("vessel", kind="vessel"),
    "vessel typed human": local("vessel typed human", kind="vessel"),
    "merge target": local("merge target"),
    "family": local("family", kind="family"),
}
# the roles public.nt.gz mentions; every other one (the item with a label
# only, and the redirect a merge leaves, too) it leaves out
PRESENT = {"released", "migration", "vessel"}
# the roles with person evidence, which the site lists
PEOPLE = {
    "deprecated type",
    "outranked type",
    "no type",
    "organization born",
    "released",
    "vessel typed human",
    "merge target",
}


def claim(subject: str, prop: str, value: dict, artifact) -> dict:
    return {
        "subject": ENTITIES[subject],
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


def religion(role: str, artifact) -> dict:
    """A claim that is no person evidence, so that the person's type is
    all that marks them."""
    return claim(
        role,
        "religion",
        {
            "type": "monolingual",
            "value": {"text": "lutherisch", "language": "de"},
        },
        artifact,
    )


def born(role: str, artifact) -> dict:
    return claim(
        role, "date_of_birth", {"type": "time", "year": 1990}, artifact
    )


def participant(role: str, artifact) -> dict:
    """`role` takes part in the migration."""
    return claim(
        "migration",
        "participant",
        {"type": "item", "value": ENTITIES[role]},
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


def add_type(
    client, subject: str, kind: str, rank: WikibaseRank, artifact_qid: str
) -> None:
    """A curator's `kind` type at `rank` beside the item's own, stated in
    the artifact `artifact_qid` (rule 1). Idempotent."""
    from wikibaseintegrator.datatypes import Item
    from wikibaseintegrator.models import Reference, References
    from wikibaseintegrator.wbi_enums import ActionIfExists

    properties, items = client.vocabulary()
    entity = client.wbi.item.get(subject)
    if any(
        s.mainsnak.datavalue["value"]["id"] == items[kind]
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
            value=items[kind],
            references=references,
            rank=rank,
        ),
        action_if_exists=ActionIfExists.FORCE_APPEND,
    )
    entity.write(summary="integration test: a curator's type")


def redirects_to(client, target: str) -> set[str]:
    """The items that redirect to `target`, by their pages' titles
    (an item's page is its QID, in whatever namespace items live in)."""
    title = client.api_get(
        {"action": "wbgetentities", "ids": target, "props": "info"}
    )["entities"][target]["title"]
    pages = client.api_get(
        {"action": "query", "prop": "redirects", "titles": title}
    )["query"]["pages"]
    return {
        redirect["title"].rsplit(":", 1)[-1]
        for page in pages.values()
        for redirect in page.get("redirects", [])
    }


def merged_away(client, target: str, artifact_qid: str) -> str:
    """A duplicate of `target`, typed human (stated in `artifact_qid`,
    rule 1), which a curator merges into it: its item becomes a redirect
    to `target` (``WikiClient.merge_items``). Idempotent: a redirect to
    `target` made earlier is found again, not made anew. Returns the
    redirect's QID."""
    from wikibaseintegrator.datatypes import Item
    from wikibaseintegrator.models import Reference, References

    earlier = redirects_to(client, target)
    if earlier:
        return min(earlier)
    properties, items = client.vocabulary()
    reference = Reference()
    reference.add(Item(prop_nr=properties["stated_in"], value=artifact_qid))
    references = References()
    references.add(reference)
    duplicate = client.wbi.item.new()
    duplicate.labels.set("en", MERGED_AWAY)
    duplicate.claims.add(
        Item(
            prop_nr=properties["instance_of"],
            value=items["human"],
            references=references,
        )
    )
    source = duplicate.write(summary="integration test: a duplicate").id
    client.merge_items(
        source, target, summary="integration test: a curator's merge"
    )
    return source


def label_only(client) -> str:
    """An item with a label and nothing else: it states no type. Written
    through WikibaseIntegrator, as the writer writes no item without a
    claim; a label is no statement, so it needs no reference. Idempotent:
    re-runs find it by its label."""
    existing = client.find_item_by_label(LABEL_ONLY)
    if existing:
        return existing
    item = client.wbi.item.new()
    item.labels.set("en", LABEL_ONLY)
    return item.write(summary="integration test: an untyped item").id


@pytest.fixture(scope="module")
def fixture_graph(client, config):
    """Write the fixture under its marked labels (idempotent: re-runs
    reuse and skip everything), set the curators' types, merge the
    duplicate, write the item with a label only, and wait until the query
    service holds every fixture item's latest revision. Returns the QIDs
    by role."""
    source = register_fictional(client, "Export fixture source", "certificate")
    written = write_fixture(
        client,
        {source.ref.sha1: "A"},
        [
            religion("deprecated type", source.ref),
            religion("outranked type", source.ref),
            born("no type", source.ref),
            born("organization born", source.ref),
            claim(
                "released",
                "date_of_death",
                {"type": "time", "year": 1995},
                source.ref,
            ),
            participant("vessel", source.ref),
            participant("vessel typed human", source.ref),
            born("merge target", source.ref),
            religion("family", source.ref),
        ],
    )
    qids = {
        role: written.created_items[ref["local_id"]]
        for role, ref in ENTITIES.items()
    }
    deprecate_type(client, qids["deprecated type"])
    add_type(
        client,
        qids["outranked type"],
        "organization",
        WikibaseRank.PREFERRED,
        source.qid,
    )
    add_type(
        client,
        qids["vessel typed human"],
        "human",
        WikibaseRank.DEPRECATED,
        source.qid,
    )
    qids["label only"] = label_only(client)
    wait_for_query_service(config, client, list(qids.values()))
    qids["merged away"] = merged_away(client, qids["merge target"], source.qid)
    # a redirect has no entity data of its own to wait for: the merge's
    # edits of its target are waited for instead
    wait_for_query_service(config, client, [qids["merge target"]])
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


def exclusions_of(exported: Path) -> dict:
    return json.loads((exported / "public-exclusions.json").read_text())


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
    vessel = fixture_graph["vessel typed human"]
    assert typing(client, vessel, "vessel")[1].rank == WikibaseRank.NORMAL
    assert typing(client, vessel, "human")[1].rank == WikibaseRank.DEPRECATED
    for role in ("no type", "label only"):
        untyped = client.wbi.item.get(fixture_graph[role])
        assert untyped.claims.get(properties["instance_of"]) == []
    bare = client.wbi.item.get(fixture_graph["label only"])
    assert not bare.claims.get_json()
    # the merged-away duplicate is a redirect to its target
    assert fixture_graph["merged away"] in redirects_to(
        client, fixture_graph["merge target"]
    )


def test_only_released_people_are_in_the_public_export(
    fixture_graph, exported
):
    with gzip.open(exported / "public.nt.gz", "rt", encoding="utf-8") as dump:
        mentioned = {
            token for line in dump for token in QID_TOKEN.findall(line)
        }
    assert {
        role for role, qid in fixture_graph.items() if qid in mentioned
    } == PRESENT


def test_the_exclusions_name_whom_the_export_drops(fixture_graph, exported):
    exclusions = exclusions_of(exported)
    for role, qid in fixture_graph.items():
        assert (qid in exclusions["excluded"]) == (role not in PRESENT)
        assert (qid in exclusions["included_persons"]) == (role == "released")
    assert exclusions["excluded"][fixture_graph["label only"]] == NO_TYPE
    assert exclusions["excluded"][fixture_graph["family"]] == FAMILY
    target = fixture_graph["merge target"]
    assert exclusions["excluded"][fixture_graph["merged away"]] == (
        f"a redirect to {target}: {exclusions['excluded'][target]}"
    )


def test_the_site_badges_whom_the_export_drops(
    config, fixture_graph, exported, tmp_path
):
    exclusions = exclusions_of(exported)
    builder = SiteBuilder(config, tmp_path / "site")
    people = {
        row["person"]["value"].rsplit("/", 1)[-1]: row
        for row in builder.run(builder._instance_query(PEOPLE_QUERY))
    }
    assert {
        role for role, qid in fixture_graph.items() if qid in people
    } == PEOPLE
    for role in PEOPLE:
        qid = fixture_graph[role]
        badged = (
            "possibly living — never in public exports"
            in builder.person_page(people[qid], [])
        )
        assert badged == (qid in exclusions["excluded"])
