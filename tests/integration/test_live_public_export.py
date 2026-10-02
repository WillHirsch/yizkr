"""Live validation that the non-privileged export (public.nt.gz) keeps
only the people the check releases, and leaves out what states no type
(#344), the events of those it leaves out (#363) and the statements that
name them (#364), while every text about what it keeps stays (#363;
ADR-0020; docs/MODELING.md, "Living persons and privacy").

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
  leaves every family out, as any of its people may be living;
* a person typed human and born in 1990, "Named In Text", whom texts on
  other items name: a birth event they take part in, labelled "Birth of"
  and their label, and dated 1990; the family's description; the
  released person's description; and a letter's title, its page's label
  and description, and its file's name, which is made from the title.
  The letter is the source of their birth, and the excerpt of the
  released person's death names them too;
* a birth, labelled for "Named In Text" and dated 1990, that names no
  participant, as a claim refused or cut short leaves one;
* two statements of the released person that name "Named In Text": a
  spouse statement whose value is them, dated by a 1938 start time (a
  property ADR-0036 retired, which a graph may still hold), and an
  appellation ("referred to as") whose `perspective` qualifier is them,
  each with an excerpt of its own;
* a talk, an event of no person kind dated 1938, whose one participant
  statement is of unknown value, with a `said to be the same as`
  qualifier naming "Named In Text" (added through WikibaseIntegrator, as
  the writer writes no unknown value);
* two children of the released person, of unknown value, added so too:
  one with a `said to be the same as` qualifier naming "Named In Text",
  one with no qualifier.

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
redirects to. An event that one of those left out takes part in is left
out too: the migration, for its vessel typed human, and the birth; so is
a person event that names no participant, the unrecorded birth, and an
event whose one participant left out is named only in a qualifier, the
talk. A statement of the released person whose value, or a qualifier's
value, is an item left out goes whole: its node, its rank, its
qualifiers, its dates among them, the reference that holds its excerpt,
and the truthy triple it gave; the run's reference, which the released
person's death cites too, stays. Of the two children of unknown value,
the one said to be "Named In Text" goes whole, and the truthy triple it
gave goes with it; which truthy triple of unknown value a statement gave
cannot be read from the dump, so the other's goes too, and its statement
stays. Every text about what the export keeps
stays, one naming a possibly living person included (owner,
2026-09-29).

The test dumps the graph as `yizkr backup` does (dumpRdf.php), writes the
export from it, and reads public.nt.gz: it mentions the released person,
the plain vessel, the letter and its page by QID, and none of the others,
the label-only item, the family, the redirect, the migration, both births
and the talk included. Of the released person's two statements that name
"Named In Text", no triple of their nodes leaves, nor the truthy triple
either gave, nor their excerpts, nor a reference or value node that only
they used. Of the two children of unknown value, the one said to be
"Named In Text" leaves no triple of its node, the other's node leaves,
and no truthy child of unknown value does. The released person's
description and death excerpt,
the letter's title and its page's label and description leave as
written, "Named In Text" and all; the labels of the person named and of
both births, and the family's description, do not; and the vocabulary
keeps its terms. The export first waits for the query service to hold
the dump's revision of every item and property, a redirect's aside, so
on a stack at rest, a merge included, the export must run, not refuse.
The site lists the eight that have person evidence, and badges the
seven the export drops.

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

from yizkr.backup import (
    FAMILY,
    NO_TYPE,
    PARTICIPANT_LEFT_OUT,
    UNATTENDED,
    dump_rdf,
    export_public,
)
from yizkr.staticsite import PEOPLE_QUERY, SiteBuilder

from .conftest import register_fictional, wait_for_query_service
from .test_live_privacy import write_fixture

pytestmark = pytest.mark.live

DEPLOY = Path("deploy")
QID_TOKEN = re.compile(r"Q[1-9]\d*\b")
# a reference or value node, as the dump names one
PROVENANCE = re.compile(r"<[^<>]*/(?:reference|value)/[0-9a-f]+>")
LABEL_ONLY = "Export Fixture Label Only"
MERGED_AWAY = "Export Fixture Merged Away"
# the person whom texts on other items name, the export leaves out (born
# in 1990)
NAMED = "Export Fixture Named In Text"
LETTER = f"Export fixture letter to {NAMED}"
# the predicates of an entity's terms: its label, under each of the three
# names dumpRdf.php gives one, its description and its aliases
TERMS = (
    "<http://www.w3.org/2000/01/rdf-schema#label>",
    "<http://www.w3.org/2004/02/skos/core#prefLabel>",
    "<http://schema.org/name>",
    "<http://schema.org/description>",
    "<http://www.w3.org/2004/02/skos/core#altLabel>",
)


def local(
    role: str,
    kind: str | None = "human",
    description: str | None = None,
    label: str | None = None,
) -> dict:
    ref = {
        "kind": "local",
        "local_id": f"export-{role.replace(' ', '-')}",
        "label": label or f"Export Fixture {role.title()}",
    }
    if kind:
        ref["instance_of"] = kind
    if description:
        ref["description"] = description
    return ref


ENTITIES = {
    "deprecated type": local("deprecated type"),
    "outranked type": local("outranked type"),
    "no type": local("no type", kind=None),
    "organization born": local("organization born", kind="organization"),
    "released": local("released", description=f"father of {NAMED}, born 1990"),
    "migration": local("migration", kind="migration"),
    "vessel": local("vessel", kind="vessel"),
    "vessel typed human": local("vessel typed human", kind="vessel"),
    "merge target": local("merge target"),
    "family": local(
        "family", kind="family", description=f"the family of {NAMED}"
    ),
    "named in text": local("named in text"),
    # labelled after its participant, as an event's label is: "Birth of"
    # and the person's label, verbatim
    "birth of named in text": local(
        "birth of named in text", kind="birth", label=f"Birth of {NAMED}"
    ),
    # a birth that names no participant, as a claim refused or cut short
    # leaves one: whom it happened to is unknown
    "unrecorded birth": local(
        "unrecorded birth",
        kind="birth",
        label=f"Birth of {NAMED}, participant unrecorded",
    ),
    # an event of no person kind whose one participant, of unknown value,
    # a qualifier says is "Named In Text"
    "talk": local("talk", kind="event"),
}
# the excerpts and the appellation of the released person's statements
# that name "Named In Text", which go whole with them
WEDDING = "the export fixture's father wed in 1938"
APPELLATION = "Export Fixture Papa"
CALLED = "the export fixture's father, as his child called him"
# the roles public.nt.gz mentions; every other one (the item with a label
# only, and the redirect a merge leaves, too) it leaves out
PRESENT = {"released", "vessel", "letter", "letter page"}
# the roles with person evidence, which the site lists
PEOPLE = {
    "deprecated type",
    "outranked type",
    "no type",
    "organization born",
    "released",
    "vessel typed human",
    "merge target",
    "named in text",
}


def claim(
    subject: str,
    prop: str,
    value: dict,
    artifact,
    excerpt: str = "fictional export fixture",
    qualifiers=(),
) -> dict:
    return {
        "subject": ENTITIES[subject],
        "property": prop,
        "value": value,
        "qualifiers": [
            {"property": key, "value": qualifier}
            for key, qualifier in qualifiers
        ],
        "source": {
            "artifact": artifact.model_dump(mode="json"),
            "page_number": "1",
            "transcription_excerpt": {"text": excerpt, "language": "en"},
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


def add_unknown(
    client,
    subject: str,
    key: str,
    artifact_qid: str,
    same_as: str | None = None,
) -> None:
    """A statement of `key` of unknown value on `subject`, with a `said to
    be the same as` qualifier naming `same_as` when one is given, stated
    in the artifact `artifact_qid` (rule 1): the writer writes no unknown
    value, so it is added here. Idempotent: it is not added again when
    `subject` holds one of unknown value of `key`, so qualified or not."""
    from wikibaseintegrator.datatypes import Item
    from wikibaseintegrator.models import Qualifiers, Reference, References
    from wikibaseintegrator.wbi_enums import ActionIfExists, WikibaseSnakType

    properties, _ = client.vocabulary()
    said = properties["said_to_be_the_same_as"]
    entity = client.wbi.item.get(subject)
    unknown = WikibaseSnakType.UNKNOWN_VALUE
    if any(
        statement.mainsnak.snaktype == unknown
        and bool(statement.qualifiers.get(said)) == (same_as is not None)
        for statement in entity.claims.get(properties[key])
    ):
        return
    qualifiers = Qualifiers()
    if same_as is not None:
        qualifiers.add(Item(prop_nr=said, value=same_as))
    reference = Reference()
    reference.add(Item(prop_nr=properties["stated_in"], value=artifact_qid))
    references = References()
    references.add(reference)
    entity.claims.add(
        Item(
            prop_nr=properties[key],
            snaktype=unknown,
            qualifiers=qualifiers,
            references=references,
        ),
        action_if_exists=ActionIfExists.FORCE_APPEND,
    )
    entity.write(summary="integration test: a statement of unknown value")


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
    letter = register_fictional(client, LETTER, "letter", pages=1)
    written = write_fixture(
        client,
        {source.ref.sha1: "A", letter.ref.sha1: "B"},
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
                excerpt=f"the father of {NAMED} died in 1995",
            ),
            participant("vessel", source.ref),
            participant("vessel typed human", source.ref),
            born("merge target", source.ref),
            religion("family", source.ref),
            born("named in text", letter.ref),
            claim(
                "birth of named in text",
                "participant",
                {"type": "item", "value": ENTITIES["named in text"]},
                source.ref,
            ),
            claim(
                "birth of named in text",
                "point_in_time",
                {"type": "time", "year": 1990},
                source.ref,
            ),
            claim(
                "unrecorded birth",
                "point_in_time",
                {"type": "time", "year": 1990},
                source.ref,
            ),
            claim(
                "released",
                "spouse",
                {"type": "item", "value": ENTITIES["named in text"]},
                source.ref,
                excerpt=WEDDING,
                qualifiers=[("start_time", {"type": "time", "year": 1938})],
            ),
            claim(
                "released",
                "referred_to_as",
                {
                    "type": "monolingual",
                    "value": {"text": APPELLATION, "language": "en"},
                },
                source.ref,
                excerpt=CALLED,
                qualifiers=[
                    (
                        "perspective",
                        {"type": "item", "value": ENTITIES["named in text"]},
                    )
                ],
            ),
            claim(
                "talk",
                "point_in_time",
                {"type": "time", "year": 1938},
                source.ref,
            ),
        ],
    )
    qids = {
        role: written.created_items[ref["local_id"]]
        for role, ref in ENTITIES.items()
    }
    qids["letter"] = letter.qid
    qids["letter page"] = letter.fragment_qids[1]
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
    named = qids["named in text"]
    add_unknown(client, qids["talk"], "participant", source.qid, named)
    add_unknown(client, qids["released"], "child", source.qid, named)
    add_unknown(client, qids["released"], "child", source.qid)
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
    # the unrecorded birth names no participant
    unrecorded = client.wbi.item.get(fixture_graph["unrecorded birth"])
    assert unrecorded.claims.get(properties["participant"]) == []
    # the talk's one participant is of unknown value, and a qualifier
    # names the person the export leaves out
    talk = client.wbi.item.get(fixture_graph["talk"])
    (taking_part,) = talk.claims.get(properties["participant"])
    assert taking_part.mainsnak.snaktype.value == "somevalue"
    (same_as,) = taking_part.qualifiers.get(
        properties["said_to_be_the_same_as"]
    )
    assert same_as.datavalue["value"]["id"] == fixture_graph["named in text"]
    # the texts name the person the export leaves out: an event's label, a
    # family's and a released person's description, a letter's title and
    # its page's label and description
    for role, terms in (
        ("birth of named in text", "labels"),
        ("family", "descriptions"),
        ("released", "descriptions"),
        ("letter", "labels"),
        ("letter page", "labels"),
        ("letter page", "descriptions"),
    ):
        entity = client.wbi.item.get(fixture_graph[role])
        assert NAMED in str(getattr(entity, terms).get("en"))


def dump_lines(exported: Path, name: str) -> list[str]:
    with gzip.open(exported / name, "rt", encoding="utf-8") as dump:
        return dump.read().splitlines()


def public_lines(exported: Path) -> list[str]:
    return dump_lines(exported, "public.nt.gz")


def test_only_released_people_are_in_the_public_export(
    fixture_graph, exported
):
    mentioned = {
        token
        for line in public_lines(exported)
        for token in QID_TOKEN.findall(line)
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
    # an event goes with a participant the export leaves out, one named
    # only in a qualifier of a participant of unknown value included
    for event, part in (
        ("birth of named in text", "named in text"),
        ("migration", "vessel typed human"),
        ("talk", "named in text"),
    ):
        assert exclusions["excluded"][fixture_graph[event]] == (
            PARTICIPANT_LEFT_OUT.format(fixture_graph[part])
        )
    # and a person event that names no participant goes: unknown means
    # excluded
    assert exclusions["excluded"][fixture_graph["unrecorded birth"]] == (
        UNATTENDED
    )


def test_what_is_kept_leaves_with_its_text(
    config, client, fixture_graph, exported
):
    lines = public_lines(exported)
    text = "\n".join(lines)

    def term(role: str, terms: str) -> str:
        entity = client.wbi.item.get(fixture_graph[role])
        return f'"{getattr(entity, terms).get("en")}"@en'

    # every text about what the export keeps stays, as written, one that
    # names the person it leaves out included (owner, 2026-09-29): the
    # released person's description and their death's excerpt, the
    # letter's title and its page's label and description
    for role, terms in (
        ("released", "descriptions"),
        ("letter", "labels"),
        ("letter page", "labels"),
        ("letter page", "descriptions"),
    ):
        assert NAMED in term(role, terms)
        assert term(role, terms) in text
    assert f'"the father of {NAMED} died in 1995"@en' in text
    # what is recorded about what it leaves out goes with it: the named
    # person's label, their birth's, and the family's description
    for role, terms in (
        ("named in text", "labels"),
        ("birth of named in text", "labels"),
        ("unrecorded birth", "labels"),
        ("family", "descriptions"),
    ):
        assert term(role, terms) not in text
    # and the vocabulary keeps its terms
    _, items = client.vocabulary()
    human = f"<{config.concept_uri}/entity/{items['human']}>"
    assert f'{human} {TERMS[0]} "human"@en .' in lines


def test_a_statement_naming_one_left_out_goes_whole(
    config, client, fixture_graph, exported
):
    # the released person's spouse statement, whose value is "Named In
    # Text", and their appellation, whose perspective qualifier is, go
    # whole, though each node carries the released person's QID: the
    # node's triples (its rank, its qualifiers, the 1938 start time
    # among them), the truthy triple it gave, its excerpt's reference,
    # and every reference or value node only it used. The run's
    # reference, which the released person's death cites too, stays,
    # linked from what is kept alone (#364)
    properties, _ = client.vocabulary()
    full = dump_lines(exported, "full.nt.gz")
    lines = public_lines(exported)
    text = "\n".join(lines)
    concept = config.concept_uri
    released = client.wbi.item.get(fixture_graph["released"])
    subject = f"<{concept}/entity/{fixture_graph['released']}>"
    derived = "<http://www.w3.org/ns/prov#wasDerivedFrom>"
    excerpt = properties["transcription_excerpt"]

    def node_of(statement) -> str:
        return f"<{concept}/entity/statement/{statement.id.replace('$', '-')}>"

    (death,) = released.claims.get(properties["date_of_death"])
    death_cites = {reference.hash for reference in death.references}
    for key, qualifier in (
        ("spouse", "start_time"),
        ("referred_to_as", "perspective"),
    ):
        (statement,) = released.claims.get(properties[key])
        node = node_of(statement)
        held = [line for line in full if line.startswith(f"{node} ")]
        assert [
            line
            for line in held
            if f"/prop/qualifier/{properties[qualifier]}> " in line
        ]
        assert not [line for line in lines if node in line]
        truthy = f"{subject} <{concept}/prop/direct/{properties[key]}> "
        assert [line for line in full if line.startswith(truthy)]
        assert not [line for line in lines if line.startswith(truthy)]
        # its references (ADR-0003): the source's, with the excerpt, which
        # it alone cites, goes; the run's, which the released person's
        # death cites too when one write made both, stays with the death
        assert [r for r in statement.references if r.snaks.get(excerpt)]
        for reference in statement.references:
            cited = f"<{concept}/reference/{reference.hash}>"
            assert f"{node} {derived} {cited} ." in held
            if reference.snaks.get(excerpt):
                assert cited not in text
            elif reference.hash in death_cites:
                assert f"{node_of(death)} {derived} {cited} ." in lines
        for used in {
            found for line in held for found in PROVENANCE.findall(line)
        }:
            if any(line.startswith(f"{used} ") for line in lines):
                assert any(line.endswith(f" {used} .") for line in lines)
    (spouse,) = released.claims.get(properties["spouse"])
    named = fixture_graph["named in text"]
    assert spouse.mainsnak.datavalue["value"]["id"] == named
    for written in (WEDDING, CALLED, APPELLATION):
        assert any(written in line for line in full)
        assert written not in text
    # the released person stays, with the rest of what is recorded
    assert f'"the father of {NAMED} died in 1995"@en' in text


def test_a_truthy_triple_of_unknown_value_goes_with_every_one(
    config, client, fixture_graph, exported
):
    # the released person's two children of unknown value: the one said
    # to be "Named In Text" goes whole, with the truthy triple it gave.
    # Each unknown value is a blank node of its own, and which truthy one
    # a statement gave cannot be read from the dump, so the other child's
    # truthy triple goes too, while its statement stays (#364)
    properties, _ = client.vocabulary()
    full = dump_lines(exported, "full.nt.gz")
    lines = public_lines(exported)
    concept = config.concept_uri
    child = properties["child"]
    said = properties["said_to_be_the_same_as"]
    released = client.wbi.item.get(fixture_graph["released"])
    truthy = (
        f"<{concept}/entity/{fixture_graph['released']}> "
        f"<{concept}/prop/direct/{child}> _:"
    )
    unknown = [
        statement
        for statement in released.claims.get(child)
        if statement.mainsnak.snaktype.value == "somevalue"
    ]
    assert sorted(bool(s.qualifiers.get(said)) for s in unknown) == [
        False,
        True,
    ]
    for statement in unknown:
        goes = bool(statement.qualifiers.get(said))
        node = f"<{concept}/entity/statement/{statement.id.replace('$', '-')}>"
        assert any(line.startswith(f"{node} ") for line in full)
        assert any(line.startswith(f"{node} ") for line in lines) is not goes
        assert any(line.endswith(f" {node} .") for line in lines) is not goes
    assert len([line for line in full if line.startswith(truthy)]) == 2
    assert not [line for line in lines if line.startswith(truthy)]


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
