"""Live check that the writer refuses a statement whose item value is its
own subject's item, and writes the rest of the batch (#491; ADR-0035
§3.5).

The fixture is the master hatter Eugen Löbig of the example universe and
a son of the same name, a homonym this test makes up, each a manifest
record of their own in one batch citing the 2008 family history: the
son's birth, his `father` claim naming the master hatter, the master
hatter's birth, and the family's pet name for him, "Muckel", with a
`perspective` qualifier naming the son. The batch is written with an
identity map in which clustering has merged the two Eugens into one
cluster, so the map puts the `father` claim's subject and value, and the
pet name's subject and its qualifier's value, on one item. On the
instance:

* both births are written, on the one item, with both references;
* neither the `father` statement nor the pet name is written, and no
  statement of the item, nor a qualifier of one, has the item as its
  value;
* the writer's result counts the two claims as refused, and as refused
  for naming their own subject's item, apart from its other refusals;
  a dry run counts the same before the write, and the same batch written
  again by a client of its own refuses them again and adds nothing.

Every label and title carries a marker, eight hex digits of a hash over
the fixture, over the writer's and the identity map's source and over
eight random bytes drawn once a session (conftest.fixture_mark), so each
session meets the fixture as a first write. Like every live test it
writes fictional data to the one instance, so the session that runs it
ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tests.fixtures.identity import joined, mapped
from yizkr.claims import ClaimBatch
from yizkr.core.wiki import WikiClient
from yizkr.pipeline import clustering, writer
from yizkr.pipeline.clustering import IdentityMap
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import BatchWriter, WriteResult

from .conftest import Registered, fixture_mark, register_fictional

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#491: the master hatter Eugen Löbig and his son of the same name, "
    "merged into one cluster, with a father claim and a pet name whose "
    "perspective is the son between them"
)
MARK = fixture_mark(FIXTURE, writer, clustering)
EUGEN = f"Eugen Löbig (self-reference test {MARK})"
HISTORY = f"A few pages of family history (self-reference test {MARK})"
RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-10-16T18:00:00+00:00",
}
# the places in the batch of the claims that name their subject's item
LOOPS = (1, 3)


def _ref(local_id: str) -> dict:
    return {"kind": "local", "local_id": local_id}


def history_batch(source: Registered) -> ClaimBatch:
    cites = {"artifact": source.ref.model_dump(mode="json")}
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                {
                    "subject": _ref("son"),
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1870},
                    "source": cites,
                },
                {
                    "subject": _ref("son"),
                    "property": "father",
                    "value": {"type": "item", "value": _ref("father")},
                    "source": cites,
                },
                {
                    "subject": _ref("father"),
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1836},
                    "source": cites,
                },
                {
                    "subject": _ref("father"),
                    "property": "referred_to_as",
                    "value": {
                        "type": "monolingual",
                        "value": {"text": "Muckel", "language": "de"},
                    },
                    "qualifiers": [
                        {
                            "property": "perspective",
                            "value": {"type": "item", "value": _ref("son")},
                        }
                    ],
                    "source": cites,
                },
            ],
            "entities": [
                {
                    "id": "father",
                    "label": EUGEN,
                    "kind": "human",
                    "summary": "master hatter of Flörburg, 1836-1909",
                    "mention_lines": [2],
                },
                {
                    "id": "son",
                    "label": EUGEN,
                    "kind": "human",
                    "summary": "son of the master hatter, born 1870",
                    "mention_lines": [5],
                },
            ],
        }
    )


@dataclass(frozen=True)
class Written:
    history: Registered
    batch: ClaimBatch
    identity: IdentityMap
    planned: WriteResult
    result: WriteResult


def write(client: WikiClient, written: Written | None = None, **options):
    """The batch written with the merged map. The one instance every live
    session writes to holds the writes of many maps, their labels kept
    apart by their session marks, so the writer's refusal of another map
    is off."""
    return BatchWriter(client, **options).write_batch(
        written.batch, identity=written.identity, one_map=False
    )


@pytest.fixture(scope="module")
def written(client: WikiClient) -> Written:
    history = register_fictional(client, HISTORY, "genealogical_report")
    batch = history_batch(history)
    # clustering's merge of the two Eugens: one cluster, one item
    identity = joined(mapped(batch), EUGEN)
    given = Written(history, batch, identity, None, None)
    planned = write(client, given, dry_run=True)
    result = write(client, given)
    return Written(history, batch, identity, planned, result)


def loops(written: Written) -> list[str]:
    return [claim_id(written.batch.claims[index]) for index in LOOPS]


def statements(client: WikiClient, qid: str) -> dict[str, list[dict]]:
    found = client.api_get(
        {"action": "wbgetentities", "ids": qid, "props": "claims"}
    )
    return found["entities"][qid].get("claims", {})


def test_the_two_claims_are_refused_and_counted(written):
    assert written.result.self_references == loops(written)
    assert written.result.refused == loops(written)
    assert written.result.written == 2
    for index in LOOPS:
        claim = written.batch.claims[index]
        assert any(
            f"claim {claim_id(claim)} ({claim.property}) is refused" in w
            for w in written.result.warnings
        )


def test_a_dry_run_counts_the_same(written):
    assert written.planned.self_references == loops(written)
    assert written.planned.refused == loops(written)
    assert written.planned.written == 2


def test_the_rest_is_written_and_no_self_loop(client, written):
    properties, _ = client.vocabulary()
    (eugen,) = set(written.result.created_items.values())
    claims = statements(client, eugen)
    births = claims.get(properties["date_of_birth"], [])
    assert len(births) == 2
    for birth in births:
        assert birth.get("rank", "normal") == "normal"
        cited = {
            key: [
                snak["datavalue"]["value"]["id"]
                for block in birth["references"]
                for snak in block["snaks"].get(properties[key], [])
            ]
            for key in ("stated_in", "inferred_from_extraction_run")
        }
        assert cited == {
            "stated_in": [written.history.qid],
            "inferred_from_extraction_run": [written.result.run_qid],
        }
    assert properties["father"] not in claims
    assert properties["referred_to_as"] not in claims
    for found in claims.values():
        for statement in found:
            snaks = [statement["mainsnak"]] + [
                snak
                for qualifier in statement.get("qualifiers", {}).values()
                for snak in qualifier
            ]
            for snak in snaks:
                value = (snak.get("datavalue") or {}).get("value")
                assert not (
                    isinstance(value, dict) and value.get("id") == eugen
                )


def test_written_again_it_is_refused_again_and_adds_nothing(client, written):
    again = write(WikiClient(client.config), written)
    assert again.self_references == loops(written)
    assert again.written == 0
    assert again.skipped == 2
    assert again.minted == []
    assert set(again.created_items.values()) == set(
        written.result.created_items.values()
    )
