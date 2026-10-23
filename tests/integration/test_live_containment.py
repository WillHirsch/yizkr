"""Live containment as source claims (ADR-0038 §2, §5, §8; #515).

A containment is a source's claim, with the time its source gives. This
writes, through the running stack, a dated and an undated containment of
one hamlet in one district (`part_of` with a `point_in_time`, per a
letter that says the hamlet belonged to the district in 1941, and
without, per a certificate's district field), and the district in an
oblast, undated. Then it checks:

* the two containments of the hamlet coexist as two statements, the dated
  one with its point in time and range, the undated one with no time;
* the containment query lists each edge with its rank and whether a
  source states it;
* the inconsistency report reads the path through them at any time: a
  person born in the hamlet per one source and in the oblast per another
  differs only in resolution, and no row lists the pair;
* a containment a curator deprecated is not followed: a second person,
  born in the hamlet per one source and in a decoy district per another,
  is listed, though a deprecated claim puts the hamlet in the decoy;
* the impossibility filter's closure (ADR-0017 rule 2) reads the same
  edges.

The places are the universe's Koshnovka, Kaldovsky District and Chadsk
Oblast (docs/EXAMPLES.md), with the pattern index's worked example of a
dated containment, under labels that carry the session's marker
(conftest.fixture_mark). Like every live test it writes fictional data
to the one instance, so the session that runs it ends by clearing the
recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import pytest
from wikibaseintegrator.wbi_enums import WikibaseRank

from tests.fixtures.identity import by_label, write_mapped
from tests.integration.conftest import (
    fixture_mark,
    register_fictional,
    run_sparql,
    wait_for_query_service,
)
from yizkr import disagreement
from yizkr.claims import ClaimBatch
from yizkr.disagreement import (
    CONTAINMENT,
    DEPRECATED,
    disagreements,
    place_within,
    read_containment,
)
from yizkr.pipeline import reconcile, writer
from yizkr.pipeline.reconcile import fetch_place_containment
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "ADR-0038: a hamlet in a district at a point in time per a letter and "
    "undated per a certificate, the district in an oblast, a deprecated "
    "containment in a decoy district, and two people born in them"
)
MARK = fixture_mark(FIXTURE, disagreement, reconcile, writer)
RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-10-23T09:00:00Z",
}


def _local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": f"{label} {MARK}",
        "instance_of": kind,
    }


HAMLET = _local("hamlet", "Containment-Test Koshnovka", "place")
DISTRICT = _local("district", "Containment-Test Kaldovsky District", "place")
OBLAST = _local("oblast", "Containment-Test Chadsk Oblast", "place")
DECOY = _local("decoy", "Containment-Test Decoy District", "place")
REFINED = _local("refined", "Containment-Test Rudolf", "human")
LISTED = _local("listed", "Containment-Test Nina", "human")


def _time(year: int) -> dict:
    return {"type": "time", "year": year}


def _claim(subject, prop, value, source, excerpt, qualifiers=()) -> dict:
    return {
        "subject": subject,
        "property": prop,
        "value": {"type": "item", "value": value},
        "qualifiers": list(qualifiers),
        "source": {
            "artifact": source.model_dump(mode="json"),
            "page_number": "1",
            "transcription_excerpt": {"text": excerpt, "language": "en"},
        },
    }


def _batch(*claims: dict) -> ClaimBatch:
    return ClaimBatch.model_validate({"extraction_run": RUN, "claims": claims})


@pytest.fixture(scope="module")
def written(client, config):
    """Write the letter's and the certificate's claims with one identity
    map, deprecate the decoy containment as a curator would, and wait
    until the query service holds every item. Returns the QIDs by local
    id."""
    letter, certificate = (
        register_fictional(client, f"Containment-Test {kind} {MARK}", kind)
        for kind in ("letter", "certificate")
    )
    dated = [
        {"property": "point_in_time", "value": _time(1941)},
        {"property": "earliest_date", "value": _time(1940)},
        {"property": "latest_date", "value": _time(1941)},
    ]
    said_in_letter = _batch(
        _claim(
            HAMLET,
            "part_of",
            DISTRICT,
            letter.ref,
            "in 1941 Koshnovka belonged to Kaldovsky District",
            dated,
        ),
        _claim(HAMLET, "part_of", DECOY, letter.ref, "Koshnovka, Decoy"),
        _claim(REFINED, "place_of_birth", HAMLET, letter.ref, "Koshnovka"),
        _claim(LISTED, "place_of_birth", HAMLET, letter.ref, "Koshnovka"),
    )
    said_in_certificate = _batch(
        _claim(
            HAMLET,
            "part_of",
            DISTRICT,
            certificate.ref,
            "Koshnovka, Kaldovsky District",
        ),
        _claim(
            DISTRICT,
            "part_of",
            OBLAST,
            certificate.ref,
            "Kaldovsky District, Chadsk Oblast",
        ),
        _claim(
            REFINED, "place_of_birth", OBLAST, certificate.ref, "Chadsk Oblast"
        ),
        _claim(LISTED, "place_of_birth", DECOY, certificate.ref, "Decoy"),
    )
    identity = by_label(said_in_letter, said_in_certificate)
    qids: dict[str, str] = {}
    for batch in (said_in_letter, said_in_certificate):
        result = write_mapped(BatchWriter(client), batch, identity=identity)
        qids.update(result.created_items)
        # written again, the batch finds every item and statement
        again = write_mapped(BatchWriter(client), batch, identity=identity)
        assert (again.written, again.referenced) == (0, 0)
    properties, _ = client.vocabulary()
    hamlet = client.wbi.item.get(qids["hamlet"])
    (decoy,) = [
        statement
        for statement in hamlet.claims.get(properties["part_of"])
        if statement.mainsnak.datavalue["value"]["id"] == qids["decoy"]
    ]
    if decoy.rank != WikibaseRank.DEPRECATED:
        decoy.rank = WikibaseRank.DEPRECATED
        hamlet.write(summary="integration test: curator rank on a fixture")
    wait_for_query_service(
        config, client, [*qids.values(), letter.qid, certificate.qid]
    )
    return qids


def _part_of(client, qid: str) -> list[dict]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties["part_of"],
        }
    )
    return [c for cs in data["claims"].values() for c in cs]


def test_a_dated_and_an_undated_containment_coexist(client, written):
    properties, _ = client.vocabulary()
    keys = {pid: key for key, pid in properties.items()}
    in_district = [
        statement
        for statement in _part_of(client, written["hamlet"])
        if statement["mainsnak"]["datavalue"]["value"]["id"]
        == written["district"]
    ]
    times = sorted(
        (
            {
                keys[pid]: [
                    snak["datavalue"]["value"]["time"][:5] for snak in snaks
                ]
                for pid, snaks in statement.get("qualifiers", {}).items()
            }
            for statement in in_district
        ),
        key=len,
    )
    assert times == [
        {},
        {
            "point_in_time": ["+1941"],
            "earliest_date": ["+1940"],
            "latest_date": ["+1941"],
        },
    ]
    for statement in in_district:
        assert statement["rank"] == "normal"
        assert statement["references"], "a containment without its source"


def _containment_rows(client, config) -> list[dict]:
    properties, items = client.vocabulary()
    query = render_for_instance(
        load_templates()[CONTAINMENT], properties, items, config.concept_uri
    )
    return run_sparql(config, query)


def test_the_query_lists_each_edge_with_its_rank_and_source(
    client, config, written
):
    entity = f"{config.concept_uri}/entity/"
    hamlet, district, decoy = (
        entity + written[key] for key in ("hamlet", "district", "decoy")
    )
    edges = sorted(
        (
            row["target"]["value"],
            row["rank"]["value"].rsplit("#", 1)[-1],
            row["stated"]["value"],
        )
        for row in _containment_rows(client, config)
        if row["item"]["value"] == hamlet
    )
    assert edges == sorted(
        [
            (district, "NormalRank", "true"),
            (district, "NormalRank", "true"),
            (decoy, DEPRECATED.rsplit("#", 1)[-1], "true"),
        ]
    )


def test_the_report_reads_the_path_at_any_time(client, config, written):
    entity = f"{config.concept_uri}/entity/"
    containment = read_containment(_containment_rows(client, config))
    hamlet, oblast, decoy = (
        entity + written[key] for key in ("hamlet", "oblast", "decoy")
    )
    # through the dated claim and the undated one
    assert place_within(containment, hamlet, oblast)
    # never through a deprecated claim
    assert not place_within(containment, hamlet, decoy)
    properties, items = client.vocabulary()
    rows = {
        row.entity_qid: row
        for row in disagreements(
            lambda query: run_sparql(config, query),
            properties,
            items,
            config.concept_uri,
        )
        if row.entity_qid in (written["refined"], written["listed"])
    }
    assert written["refined"] not in rows
    listed = rows[written["listed"]]
    assert {listed.a.value.term.value, listed.b.value.term.value} == {
        hamlet,
        decoy,
    }


def test_the_impossibility_filter_reads_the_same_edges(
    client, config, written
):
    containment = fetch_place_containment(client, config)
    assert containment.contains(written["district"], written["hamlet"])
    assert containment.contains(written["oblast"], written["hamlet"])
    assert not containment.contains(written["decoy"], written["hamlet"])
