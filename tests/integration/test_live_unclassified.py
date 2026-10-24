"""Live check of the unclassified-entities query (ADR-0013 §A, #91).

A fictional fixture of entities held under the holding class, each with
the type hint its source gives, the 2002 interview's word for the
family firm, "hat shop", among them:

* three entities with one hint in two spellings, two from one source
  and one from another, are one row: three entities, two artifacts (the
  hint is compared in lower case, ADR-0035);
* an entity whose hint two artifacts state, one by a curator's
  reference, is one entity and two artifacts;
* an entity whose hint a curator deprecated is not counted under it;
* an entity whose unclassified kind a curator deprecated is not listed;
* an entity held without a hint is counted under the empty hint;
* the rows are ordered by how many entities carry the hint, the shop's
  two before the airship's one, which comes first alphabetically.

Every hint carries the session's marker (conftest.fixture_mark), so the
fixture's rows are its own whatever else the instance holds, and a
session meets the fixture as a first write. The empty hint's row is
shared with whatever else the instance holds unhinted, so only its
presence is asserted.
"""

from __future__ import annotations

import pytest
from wikibaseintegrator.datatypes import Item
from wikibaseintegrator.models import Reference
from wikibaseintegrator.wbi_enums import WikibaseRank

from tests.fixtures import example_universe as u
from tests.fixtures.identity import write_mapped
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance

from .conftest import (
    fixture_mark,
    register_fictional,
    run_sparql,
    wait_for_query_service,
)

pytestmark = pytest.mark.live

UNIVERSE = u.by_key()
# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#91: unclassified entities with type hints from the 2002 interview "
    "and the 1952 naturalisation certificate, one hint and one kind "
    "deprecated by a curator, and one hint cited by a curator in a second "
    "artifact"
)
MARK = fixture_mark(FIXTURE, writer)
INTERVIEW = "interview-2002"
NATURALISATION = "naturalisation-1952"
RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-10-23T12:00:00+00:00",
}
# the hints, each with the session's marker; the first two spellings are
# one hint in lower case
SHOP = f"Hat Shop {MARK}"
SHOP_AGAIN = f"hat shop {MARK}"
CRAFT = f"aircraft {MARK}"
# a hint of one entity, before the shop's alphabetically
AIRSHIP = f"Airship {MARK}"
LINER = f"ocean liner {MARK}"
# local id -> (label, hint, source)
ENTITIES = {
    "blue-hat": (f"Unclassified Blue Hat {MARK}", SHOP, INTERVIEW),
    "parlour": (f"Unclassified Hat Parlour {MARK}", SHOP, INTERVIEW),
    "stall": (f"Unclassified Hat Stall {MARK}", SHOP_AGAIN, NATURALISATION),
    "craft": (f"Unclassified Craft {MARK}", CRAFT, INTERVIEW),
    "liner": (f"Unclassified Liner {MARK}", LINER, NATURALISATION),
    "airship": (f"Unclassified Airship {MARK}", AIRSHIP, NATURALISATION),
    "unhinted": (f"Unclassified Unhinted {MARK}", None, INTERVIEW),
}


def _batch(source: str, artifact: ArtifactRef) -> ClaimBatch:
    """The source's entities, each named by its label in a referred_to_as
    claim, with its record in the entity manifest."""
    local = [key for key, (_, _, said) in ENTITIES.items() if said == source]
    claims, entities = [], []
    for key in local:
        label, hint, _ = ENTITIES[key]
        claims.append(
            {
                "subject": {
                    "kind": "local",
                    "local_id": key,
                    "label": label,
                    "instance_of": "unclassified_entity",
                },
                "property": "referred_to_as",
                "value": {
                    "type": "monolingual",
                    "value": {"text": label, "language": "en"},
                },
                "source": {
                    "artifact": artifact.model_dump(mode="json"),
                    "transcription_excerpt": {
                        "text": "fictional unclassified fixture",
                        "language": "en",
                    },
                },
            }
        )
        record = {"id": key, "label": label, "kind": "unclassified_entity"}
        if hint:
            record["type_hint"] = hint
        entities.append(record)
    return ClaimBatch.model_validate(
        {"extraction_run": RUN, "claims": claims, "entities": entities}
    )


def _cite_hint(client, qid: str, artifact: str) -> None:
    """Add a reference block stated in `artifact` to the item's type hint,
    as a curator would who reads the same hint in a second source."""
    properties, _ = client.vocabulary()
    item = client.wbi.item.get(qid)
    (hint,) = item.claims.get(properties["type_hint"])
    cited = {
        snak.datavalue["value"]["id"]
        for reference in hint.references
        for snak in reference.snaks.get(properties["stated_in"])
    }
    if artifact in cited:
        return
    block = Reference()
    block.add(Item(prop_nr=properties["stated_in"], value=artifact))
    hint.references.add(block)
    item.write(summary="integration test: curator reference on a fixture")


def _deprecate(client, qid: str, prop: str, value=None) -> None:
    """Give the item's `prop` statement (the one whose item value is
    `value`, when given) deprecated rank, as a curator would."""
    item = client.wbi.item.get(qid)
    for statement in item.claims.get(prop):
        datavalue = statement.mainsnak.datavalue["value"]
        if value is not None and datavalue.get("id") != value:
            continue
        if statement.rank != WikibaseRank.DEPRECATED:
            statement.rank = WikibaseRank.DEPRECATED
            item.write(summary="integration test: curator rank on a fixture")
        return
    raise AssertionError(f"{qid} has no {prop} statement to deprecate")


@pytest.fixture(scope="module")
def written(client, config) -> dict[str, str]:
    """Write each source's batch, cite the airship's hint in the
    interview too, deprecate the craft's hint and the liner's kind, and
    wait until the query service holds every item. Returns the QIDs by
    local id."""
    qids: dict[str, str] = {}
    artifacts: dict[str, str] = {}
    for source in (INTERVIEW, NATURALISATION):
        registered = register_fictional(
            client,
            f"{UNIVERSE[source].title} {MARK}",
            UNIVERSE[source].artifact_class or "artifact",
        )
        result = write_mapped(
            BatchWriter(client), _batch(source, registered.ref)
        )
        qids.update(result.created_items)
        artifacts[source] = registered.qid
    properties, items = client.vocabulary()
    _cite_hint(client, qids["airship"], artifacts[INTERVIEW])
    _deprecate(client, qids["craft"], properties["type_hint"])
    _deprecate(
        client,
        qids["liner"],
        properties["instance_of"],
        items["unclassified_entity"],
    )
    wait_for_query_service(config, client, sorted(qids.values()))
    return qids


@pytest.fixture(scope="module")
def rows(client, config, written) -> dict[str, dict]:
    """The query's rows, by type hint."""
    properties, items = client.vocabulary()
    query = render_for_instance(
        load_templates()["unclassified-entities"],
        properties,
        items,
        config.concept_uri,
    )
    found: dict[str, dict] = {}
    for row in run_sparql(config, query):
        hint = row.get("typeHint", {}).get("value")
        assert hint is not None, f"a row without its hint: {row}"
        assert hint not in found, f"the hint {hint!r} in two rows"
        found[hint] = {key: value["value"] for key, value in row.items()}
    return found


def test_one_hint_in_two_spellings_is_one_row(rows, written, config):
    # three entities, two of them from one artifact
    row = rows[SHOP.lower()]
    assert (row["entities"], row["artifacts"]) == ("3", "2")
    shops = ("blue-hat", "parlour", "stall")
    entity = f"{config.concept_uri}/entity/"
    examples = {entity + written[key]: ENTITIES[key][0] for key in shops}
    # the example's label is the example's own
    assert row["exampleLabel"] == examples[row["example"]]


def test_a_hint_of_one_entity_is_its_row(rows, written, config):
    # its hint is stated in the certificate and, by a curator's
    # reference, in the interview: one entity, two artifacts
    row = rows[AIRSHIP.lower()]
    assert (row["entities"], row["artifacts"]) == ("1", "2")
    assert (
        row["example"] == f"{config.concept_uri}/entity/{written['airship']}"
    )
    assert row["exampleLabel"] == ENTITIES["airship"][0]


def test_a_deprecated_hint_is_not_counted(rows):
    assert CRAFT.lower() not in rows


def test_an_entity_whose_kind_is_deprecated_is_not_listed(rows):
    assert LINER.lower() not in rows


def test_an_unhinted_entity_is_under_the_empty_hint(rows):
    assert int(rows[""]["entities"]) >= 2  # the unhinted and the craft


def test_rows_are_ordered_by_entities(client, config, written):
    properties, items = client.vocabulary()
    query = render_for_instance(
        load_templates()["unclassified-entities"],
        properties,
        items,
        config.concept_uri,
    )
    counts = [
        int(row["entities"]["value"]) for row in run_sparql(config, query)
    ]
    assert counts == sorted(counts, reverse=True)
