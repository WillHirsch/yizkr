"""Live check of the kinship query (ADR-0014, ADR-0018; #229, #276).

The fixture is a slice of the universe's family (docs/EXAMPLES.md), each
relation stated on one side only, and some statements carrying a
curator's rank:

* Sebastian's and Felix's father is Eugen, so the two are brothers
  through a shared parent, and Felix is Leopold's father;
* Sebastian's children Klara and Gustav are stated on him, as `child`,
  so the parent step is read from the parent's side; Klara's statement is
  preferred, and must hide no other;
* Sebastian's spouse Hildegard is stated on him only, and Ursula's
  sibling Klara on Ursula only;
* the interview's "Aunt Pünktchen": ``Klara —relative→ Nicholas ;
  kinship_type = "aunt"``, with the person the term names as the
  statement's subject and the ego as its value;
* ADR-0018's "our great grandmother", with a side as well:
  ``Cäcilie —relative→ Nicholas ; kinship_type = "paternal great
  grandmother"`` (in the universe she is his father Gustav's
  grandmother; the fixture states none of her other relations), so her
  inverse row drops the side and keeps the degree;
* two statements a curator deprecated, so neither gives a row: Felix
  misread as Klara's father, and the courtesy title "Nana Maisie" misread
  as kinship (it is a `referred_to_as`, never a `relative`).

The query reads each row as "?relative is ?subject's ?relationship", so
the fixture gives the 44 rows of RELATIVES: the stated steps from both
sides, the siblings through a shared parent, the second-order relations
through them (Leopold's uncle Sebastian and his aunt by marriage
Hildegard, whose nephew he is), the in-laws, the asserted aunt and
great-grandmother, and their inverse rows, with Nicholas as Klara's
nephew and Cäcilie's great-grandson.

The fixture writes the people under labels of its own, each carrying the
session's marker (conftest.fixture_mark), so each session meets the fixture
as a first write, a re-run within the session reuses its entities, and what
an earlier session wrote, a live mutant's run included, never reaches the
rows. The query is scoped to them with a VALUES line in its main WHERE, as
its header describes for one subject; its named sets are still computed
over the whole graph, so this also runs the query at the instance's size.
Like every live test it writes fictional data to the one instance, so the
session that runs it ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wikibaseintegrator.wbi_enums import WikibaseRank

from tests.fixtures import example_universe as u
from tests.fixtures.identity import write_mapped
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance

from .conftest import (
    fixture_mark,
    legacy_spouse_writes,
    register_fictional,
    run_sparql,
    wait_for_query_service,
)

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "ADR-0014, ADR-0018: a slice of the Löbig family, each relation stated "
    "on one side only, some with a curator's rank"
)
MARK = fixture_mark(FIXTURE, writer)

EUGEN = "eugen-loebig"
SEBASTIAN = "sebastian-loebig"
FELIX = "felix-loebig"
HILDEGARD = "hildegard-loebig"
URSULA = "ursula-loebig"
KLARA = "klara-loebig"
GUSTAV = "gustav-loebig"
LEOPOLD = "leopold-loebig"
NICHOLAS = "nicholas-lobig"
MAISIE = "maisie-cutmore"
CAECILIE = "caecilie-loebig"
PEOPLE = (
    EUGEN,
    SEBASTIAN,
    FELIX,
    HILDEGARD,
    URSULA,
    KLARA,
    GUSTAV,
    LEOPOLD,
    NICHOLAS,
    MAISIE,
    CAECILIE,
)

# (subject, property, value, kinship term)
STATEMENTS = [
    (SEBASTIAN, "father", EUGEN, None),
    (FELIX, "father", EUGEN, None),
    (SEBASTIAN, "child", KLARA, None),
    (SEBASTIAN, "child", GUSTAV, None),
    (LEOPOLD, "father", FELIX, None),
    (SEBASTIAN, "spouse", HILDEGARD, None),
    (URSULA, "sibling", KLARA, None),
    (KLARA, "relative", NICHOLAS, "aunt"),
    (CAECILIE, "relative", NICHOLAS, "paternal great grandmother"),
    (MAISIE, "relative", NICHOLAS, "Nana"),
    (KLARA, "father", FELIX, None),
]
# the curator's ranks: (subject, property, value) -> rank
RANKS = {
    (SEBASTIAN, "child", KLARA): WikibaseRank.PREFERRED,
    (MAISIE, "relative", NICHOLAS): WikibaseRank.DEPRECATED,
    (KLARA, "father", FELIX): WikibaseRank.DEPRECATED,
}
EXCERPTS = {"aunt": "Aunt Pünktchen", "Nana": "Nana Maisie"}

# (subject, relative, relationship): the relative is the subject's
RELATIVES = {
    (EUGEN, SEBASTIAN, "son"),
    (EUGEN, FELIX, "son"),
    (EUGEN, KLARA, "granddaughter"),
    (EUGEN, GUSTAV, "grandson"),
    (EUGEN, LEOPOLD, "grandson"),
    (EUGEN, HILDEGARD, "daughter-in-law"),
    (SEBASTIAN, EUGEN, "father"),
    (SEBASTIAN, KLARA, "daughter"),
    (SEBASTIAN, GUSTAV, "son"),
    (SEBASTIAN, FELIX, "brother"),
    (SEBASTIAN, HILDEGARD, "wife"),
    (SEBASTIAN, LEOPOLD, "nephew"),
    (FELIX, EUGEN, "father"),
    (FELIX, LEOPOLD, "son"),
    (FELIX, SEBASTIAN, "brother"),
    (FELIX, KLARA, "niece"),
    (FELIX, GUSTAV, "nephew"),
    (FELIX, HILDEGARD, "sister-in-law"),
    (HILDEGARD, SEBASTIAN, "husband"),
    (HILDEGARD, EUGEN, "father-in-law"),
    (HILDEGARD, FELIX, "brother-in-law"),
    # her husband's brother's son
    (HILDEGARD, LEOPOLD, "nephew"),
    (KLARA, SEBASTIAN, "father"),
    (KLARA, GUSTAV, "brother"),
    (KLARA, URSULA, "sister"),
    (KLARA, EUGEN, "grandfather"),
    (KLARA, FELIX, "uncle"),
    (KLARA, LEOPOLD, "cousin"),
    # the inverse of the aunt her nephew's interview names
    (KLARA, NICHOLAS, "nephew"),
    (GUSTAV, SEBASTIAN, "father"),
    (GUSTAV, KLARA, "sister"),
    (GUSTAV, EUGEN, "grandfather"),
    (GUSTAV, FELIX, "uncle"),
    (GUSTAV, LEOPOLD, "cousin"),
    (URSULA, KLARA, "sister"),
    (LEOPOLD, FELIX, "father"),
    (LEOPOLD, EUGEN, "grandfather"),
    (LEOPOLD, SEBASTIAN, "uncle"),
    # his uncle's wife
    (LEOPOLD, HILDEGARD, "aunt"),
    (LEOPOLD, KLARA, "cousin"),
    (LEOPOLD, GUSTAV, "cousin"),
    (NICHOLAS, KLARA, "aunt"),
    # a term a source asserts is shown as the source gives it
    (NICHOLAS, CAECILIE, "paternal great grandmother"),
    # its inverse drops the side, which names Nicholas's parent, and
    # keeps the degree
    (CAECILIE, NICHOLAS, "great-grandson"),
}


def person(key: str) -> dict:
    """A local ref to one of the universe's people, under this fixture's
    own label, marked with the session's marker, so that no other test's
    item, and no earlier session's, is reused."""
    return {
        "kind": "local",
        "local_id": f"kinship-{key}",
        "label": f"{u.by_key()[key].label} (kinship fixture {MARK})",
        "instance_of": "human",
    }


def claim(
    source: ArtifactRef, subject: str, prop: str, value: dict, excerpt=None
):
    reference = {
        "artifact": source.model_dump(mode="json"),
        "page_number": "1",
    }
    if excerpt:
        reference["transcription_excerpt"] = {
            "text": excerpt,
            "language": "en",
        }
    return {
        "subject": person(subject),
        "property": prop,
        "value": value,
        "source": reference,
    }


def statements(client, qid: str, prop_key: str) -> list[dict]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties[prop_key],
        }
    )
    return [c for cs in data["claims"].values() for c in cs]


def set_rank(client, subject: str, prop_key: str, value: str, rank) -> None:
    """A curator's rank change on one item-valued statement: an optional
    overlay (ADR-0002). Idempotent."""
    properties, _ = client.vocabulary()
    entity = client.wbi.item.get(subject)
    (statement,) = [
        s
        for s in entity.claims.get(properties[prop_key])
        if s.mainsnak.datavalue["value"]["id"] == value
    ]
    if statement.rank != rank:
        statement.rank = rank
        entity.write(summary="integration test: curator rank on a fixture")


@pytest.fixture(scope="module")
def fixture_graph(client, config) -> dict[str, str]:
    """Write the fixture (idempotent: a re-run within the session reuses
    and skips everything), apply the curator's ranks, and wait until the
    query service holds every person's latest revision. Its spouse claim
    goes through legacy_spouse_writes, as a statement an instance holds
    from before ADR-0036, which the kinship query reads until #520.
    Returns their QIDs by universe key."""
    source = register_fictional(
        client, f"Kinship fixture source {MARK}", "artifact"
    ).ref
    claims = [
        claim(
            source,
            key,
            "sex_or_gender",
            {
                "type": "item",
                "value": {"kind": "vocab", "key": u.by_key()[key].sex},
            },
        )
        for key in PEOPLE
    ]
    for subject, prop, value, term in STATEMENTS:
        written = claim(
            source,
            subject,
            prop,
            {"type": "item", "value": person(value)},
            EXCERPTS.get(term),
        )
        if term:
            written["qualifiers"] = [
                {
                    "property": "kinship_type",
                    "value": {"type": "string", "value": term},
                }
            ]
        claims.append(written)
    batch = ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": claims,
        }
    )
    # the fixture's spouse statement is one an instance holds from before
    # ADR-0036, which the kinship query reads until #520
    with legacy_spouse_writes():
        written = write_mapped(BatchWriter(client), batch)
        # written again, the batch finds every person and statement
        again = write_mapped(BatchWriter(client), batch)
    assert (again.written, again.referenced) == (0, 0)
    assert again.created_items == written.created_items
    qids = {key: written.created_items[f"kinship-{key}"] for key in PEOPLE}
    for (subject, prop, value), rank in RANKS.items():
        set_rank(client, qids[subject], prop, qids[value], rank)
    wait_for_query_service(config, client, list(qids.values()))
    return qids


def kinship_rows(client, config, qids: dict[str, str]) -> set[tuple]:
    """The kinship query's rows whose subject is one of the fixture's
    people, as (subject, relative, relationship), each person given by
    universe key. The main WHERE is scoped to them, and the rows are
    filtered to them as well, so no row about anyone else reaches an
    assertion."""
    properties, items = client.vocabulary()
    template = load_templates()["kinship"]
    # the main WHERE, after the named sets, is the only one at the start
    # of a line
    assert template.count("\nWHERE {\n") == 1
    scope = " ".join(f"wd:{qid}" for qid in qids.values())
    query = render_for_instance(
        template.replace(
            "\nWHERE {\n", f"\nWHERE {{\n  VALUES ?subject {{ {scope} }}\n"
        ),
        properties,
        items,
        config.concept_uri,
    )
    key_of = {qid: key for key, qid in qids.items()}

    def key(row: dict, var: str) -> str:
        qid = row[var]["value"].rsplit("/", 1)[-1]
        return key_of.get(qid, qid)

    return {
        (
            key(row, "subject"),
            key(row, "relative"),
            row["relationship"]["value"],
        )
        for row in run_sparql(config, query)
        if key(row, "subject") in qids
    }


def test_the_fixture_is_stored_as_adr_0018_records_it(client, fixture_graph):
    """The aunt is the statement's subject and the ego its value, so the
    query below reads what ADR-0018 writes."""
    properties, _ = client.vocabulary()
    (statement,) = statements(client, fixture_graph[KLARA], "relative")
    value = statement["mainsnak"]["datavalue"]["value"]["id"]
    assert value == fixture_graph[NICHOLAS]
    (term,) = statement["qualifiers"][properties["kinship_type"]]
    assert term["datavalue"]["value"] == "aunt"
    assert statements(client, fixture_graph[NICHOLAS], "relative") == []


def test_the_fixture_carries_the_curators_ranks(client, fixture_graph):
    """Every other statement keeps the normal rank, so the rows below
    show that a preferred value hides none and a deprecated one gives
    nothing."""
    by_key = {qid: key for key, qid in fixture_graph.items()}
    for subject, prop, value, _ in STATEMENTS:
        ranks = {
            by_key.get(s["mainsnak"]["datavalue"]["value"]["id"]): s["rank"]
            for s in statements(client, fixture_graph[subject], prop)
        }
        rank = RANKS.get((subject, prop, value), WikibaseRank.NORMAL)
        assert ranks[value] == rank.value


def test_each_person_has_the_relatives_the_fixture_implies(
    client, config, fixture_graph
):
    assert kinship_rows(client, config, fixture_graph) == RELATIVES
