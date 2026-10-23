"""Live write path for the containment a GEDCOM PLAC states (ADR-0035 §6,
ADR-0038 §8.5; #571).

A birth placed at "Street, Town, Shire" must land as three place items,
each holding its own name (the street's, the town's) as an alias beside
its comma-joined label, with a `part_of` statement from the street to
the town and from the town to the shire, each stated in the export and
carrying no qualifier, as a PLAC states no time. A residence placed at
"Town, Shire", a PLAC that is the birth's tail, must point to the same
town item. The place stage's read of the graph (places.fetch_places)
must then name each container of the birth's chain by its name, so that
every link the export states is claimed and settled, none left out
unnamed and none asked of."""

from __future__ import annotations

import pytest

from tests.fixtures.identity import write_mapped
from yizkr.claims import ArtifactRef
from yizkr.pipeline import gedcom, places, writer
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.writer import BatchWriter

from .conftest import fixture_mark, wait_for_query_service

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#571: a GEDCOM export's birth at a street in a town in a shire, and a "
    "residence in the town"
)
MARK = fixture_mark(FIXTURE, writer, gedcom, places)

STREET = f"Integration Street {MARK}"
TOWN = f"Integration Town {MARK}"
SHIRE = f"Integration Shire {MARK}"

GEDCOM = f"""\
0 HEAD
1 CHAR UTF-8
0 @I1@ INDI
1 NAME Gedcom Placed /Tester {MARK}/
1 BIRT
2 DATE 1950
2 PLAC {STREET}, {TOWN}, {SHIRE}
1 RESI
2 DATE 1960
2 PLAC {TOWN}, {SHIRE}
0 TRLR
"""


@pytest.fixture(scope="module")
def written(client, tmp_path_factory):
    """(the export's item, the export's key, the batch, the write)."""
    path = tmp_path_factory.mktemp("gedcom") / f"integration-plac-{MARK}.ged"
    path.write_text(GEDCOM)
    registered = register_artifact(
        client,
        media_path=path,
        artifact_type="genealogical_report",
        title=f"Integration GEDCOM places {MARK}",
        description="written by tests/integration; safe to delete",
    )
    export = ArtifactRef.from_path(path)
    batch = adapt_gedcom(GEDCOM, artifact=export).batch
    write = write_mapped(BatchWriter(client), batch)
    return registered.artifact_qid, export, batch, write


def _qids(batch, write) -> dict[str, str]:
    """Each place's item, by its label."""
    return {
        entity.label: write.created_items[entity.id]
        for entity in batch.entities
        if entity.kind == "place"
    }


def _statements(client, qid: str, prop: str) -> list[dict]:
    properties, _ = client.vocabulary()
    found = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties[prop],
        }
    )
    return [
        statement
        for statements in found["claims"].values()
        for statement in statements
    ]


def test_each_link_of_a_plac_is_an_undated_part_of(client, written):
    export_qid, _, batch, write = written
    qid = _qids(batch, write)
    street = qid[f"{STREET}, {TOWN}, {SHIRE}"]
    town = qid[f"{TOWN}, {SHIRE}"]
    shire = qid[SHIRE]
    properties, _ = client.vocabulary()
    for inner, outer in ((street, town), (town, shire)):
        (statement,) = _statements(client, inner, "part_of")
        assert statement["mainsnak"]["datavalue"]["value"]["id"] == outer
        assert statement["rank"] == "normal"
        # a PLAC states no time (ADR-0038 §2)
        assert not statement.get("qualifiers")
        stated_in = {
            snak["datavalue"]["value"]["id"]
            for reference in statement.get("references", [])
            for snak in reference["snaks"].get(properties["stated_in"], [])
        }
        assert stated_in == {export_qid}
    assert _statements(client, shire, "part_of") == []
    # the residence's PLAC is the birth's tail: the one town item
    (person,) = [
        write.created_items[e.id] for e in batch.entities if e.id == "i1"
    ]
    (residence,) = _statements(client, person, "residence")
    assert residence["mainsnak"]["datavalue"]["value"]["id"] == town


def test_each_place_holds_its_own_name(client, written):
    _, _, batch, write = written
    qid = _qids(batch, write)
    found = client.api_get(
        {
            "action": "wbgetentities",
            "ids": "|".join(qid.values()),
            "props": "labels|aliases",
        }
    )["entities"]
    for label, own in (
        (f"{STREET}, {TOWN}, {SHIRE}", STREET),
        (f"{TOWN}, {SHIRE}", TOWN),
        (SHIRE, SHIRE),
    ):
        entity = found[qid[label]]
        names = {entity["labels"]["en"]["value"]} | {
            alias["value"] for alias in entity.get("aliases", {}).get("en", [])
        }
        assert own in names, label


def test_the_place_stage_names_and_settles_every_link(client, config, written):
    _, export, batch, write = written
    wait_for_query_service(config, client, list(_qids(batch, write).values()))
    graph = places.fetch_places(client, config)
    links = places.stated_links(batch, write.created_items, export.sha1)
    assert links and all(link.claimed for link in links)
    work = places.build_worklist(graph, links)
    assert (work.unnamed, work.unresolved) == (0, 0)
    assert work.questions == []
    qid = _qids(batch, write)
    street = qid[f"{STREET}, {TOWN}, {SHIRE}"]
    chain = (TOWN, SHIRE)
    assert work.settled[(street, chain, 0)] == qid[f"{TOWN}, {SHIRE}"]
    assert work.settled[(street, chain, 1)] == qid[SHIRE]
