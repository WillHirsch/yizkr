"""Live check that the writer finds or mints the item of every local
entity through the identity map alone (#109; ADR-0035, ADR-0010).

The fixture is three Eugen Löbigs and a place of his name: the master
hatter (1836-1909) of the example universe and a son of the same name,
each a manifest record of their own in one batch citing the 2008 family
history, and a third Eugen Löbig with a place called Eugen Löbig in a
batch citing the 1984 family tree, both sources genealogical reports.
The fictional people, and the place, are homonyms this test makes up.
Both batches are written with one identity map of the two, in which
every mention is a cluster of its own. On the instance:

* the same label with a different kind gives a new item: the place is
  an item of its own beside the people, described as a place;
* two same-kind, same-label people with identical source descriptions
  coexist: the three Eugens, every one a human named in a genealogical
  report, are three items, told apart by their places among the map's
  clusters of their label and kind ("(2)", "(3)");
* each item was made in one `wbeditentity` write carrying its label, its
  description and its `instance of` with both references, and none of
  them is found by its label alone;
* the same batches written again, by a client of their own, find the same
  items and mint nothing. The same batches written after a reset of the
  instance give the same items: an opt-in test of
  test_live_scratch_reset.py (`the_same_items_after_a_reset`) resets the
  instance, as it must run in a session of its own;
* the run item records the map's hash, and its label the map's tag;
* a write of the history with another map, its own alone, is refused
  before anything is written: the instance holds the first map's writes,
  whose items that map alone finds again.

Every label and title carries a marker, eight hex digits of a hash over
the fixture, over the writer's, the resolve step's and the identity
map's source and over eight random bytes drawn once a session
(conftest.fixture_mark), so each session meets the fixture as a first
write. Like every live test it writes fictional data to the one
instance, so the session that runs it ends by clearing the recorded
instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from wikibaseintegrator.entities.item import ItemEntity

from tests.fixtures.identity import mapped, run_label
from yizkr.claims import ClaimBatch
from yizkr.core.wiki import WikiClient
from yizkr.pipeline import clustering, resolve, writer
from yizkr.pipeline.clustering import IdentityMap
from yizkr.pipeline.resolve import resolve_batch
from yizkr.pipeline.writer import (
    MAP_TAG,
    BatchWriter,
    OtherIdentityMap,
    source_descriptor,
)

from .conftest import Registered, fixture_mark, register_fictional

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#109: three Eugen Löbigs and a place of his name, from the 2008 "
    "family history and the 1984 family tree, written with one identity "
    "map in which each is an entity of its own"
)
MARK = fixture_mark(FIXTURE, writer, resolve, clustering)
EUGEN = f"Eugen Löbig (identity test {MARK})"
HISTORY = f"A few pages of family history (identity test {MARK})"
TREE = f"The family tree, 1984 (identity test {MARK})"
RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-10-06T18:00:00+00:00",
}
REPORT = source_descriptor("human", "genealogical_report")


def _cites(source: Registered) -> dict:
    return {"artifact": source.ref.model_dump(mode="json")}


def _ref(local_id: str) -> dict:
    return {"kind": "local", "local_id": local_id}


def history_batch(source: Registered) -> ClaimBatch:
    """The family history's batch: the father, then the son, in its
    manifest, each with a summary that gives his life dates; its first
    claim names the son."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                {
                    "subject": _ref("son"),
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1870},
                    "source": _cites(source),
                },
                {
                    "subject": _ref("son"),
                    "property": "father",
                    "value": {"type": "item", "value": _ref("father")},
                    "source": _cites(source),
                },
                {
                    "subject": _ref("father"),
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1836},
                    "source": _cites(source),
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


def tree_batch(source: Registered) -> ClaimBatch:
    """The family tree's batch: a third Eugen Löbig, born 1902, who lived
    at a place of the same name."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                {
                    "subject": _ref("e1"),
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1902},
                    "source": _cites(source),
                },
                {
                    "subject": _ref("e1"),
                    "property": "residence",
                    "value": {"type": "item", "value": _ref("e2")},
                    "source": _cites(source),
                },
            ],
            "entities": [
                {
                    "id": "e1",
                    "label": EUGEN,
                    "kind": "human",
                    "mention_lines": [1],
                },
                {
                    "id": "e2",
                    "label": EUGEN,
                    "kind": "place",
                    "mention_lines": [1],
                },
            ],
        }
    )


@dataclass(frozen=True)
class Written:
    history: Registered
    tree: Registered
    identity: IdentityMap
    runs: dict[str, str]
    # (batch, local id) -> QID
    items: dict[tuple[str, str], str]
    # each write that made an item: the item as the write sent it
    made: list[dict]


def record_mints(patch, made: list[dict]) -> None:
    """While `patch` holds, each item write that makes an item appends the
    item as it was sent, with its new id, to `made`."""
    write = ItemEntity.write

    def recording(self, *args, **kwargs):
        new = self.id is None
        sent = self.get_json()
        written = write(self, *args, **kwargs)
        if new:
            made.append(dict(sent, id=written.id))
        return written

    patch.setattr(ItemEntity, "write", recording)


def write_both(
    client: WikiClient, history, tree, identity, made, one_map=False
):
    """The two batches written, each by a writer of its own, with the one
    map: (run QIDs, (batch, local id) -> QID). The one instance every live
    session writes to holds the writes of many maps, their labels kept
    apart by their session marks, so the writer's refusal of another map
    is off (`one_map`) but on a reset instance."""
    runs, items = {}, {}
    with pytest.MonkeyPatch.context() as patch:
        record_mints(patch, made)
        for name, batch in (
            ("history", history_batch(history)),
            ("tree", tree_batch(tree)),
        ):
            result = BatchWriter(client).write_batch(
                resolve_batch(batch, identity),
                identity=identity,
                one_map=one_map,
            )
            runs[name] = result.run_qid
            items.update(
                ((name, local_id), qid)
                for local_id, qid in result.created_items.items()
            )
    return runs, items


@pytest.fixture(scope="module")
def written(client: WikiClient) -> Written:
    history = register_fictional(client, HISTORY, "genealogical_report")
    tree = register_fictional(client, TREE, "genealogical_report")
    identity = mapped(history_batch(history), tree_batch(tree))
    made: list[dict] = []
    runs, items = write_both(client, history, tree, identity, made)
    return Written(history, tree, identity, runs, items, made)


def labelled(made: list[dict]) -> list[dict]:
    return [item for item in made if item["labels"]["en"]["value"] == EUGEN]


def terms(client: WikiClient, qids) -> dict[str, tuple[str, str]]:
    """Each item's English label and description, as the instance holds
    them."""
    found = client.api_get(
        {
            "action": "wbgetentities",
            "ids": "|".join(sorted(set(qids))),
            "props": "labels|descriptions",
            "languages": "en",
        }
    )["entities"]
    return {
        qid: (
            entity["labels"]["en"]["value"],
            entity["descriptions"]["en"]["value"],
        )
        for qid, entity in found.items()
    }


def test_each_mention_is_an_item_of_its_own(client, written):
    assert len(written.items) == 4
    assert len(set(written.items.values())) == 4
    held = terms(client, written.items.values())
    assert {label for label, _ in held.values()} == {EUGEN}
    people = [
        written.items[key]
        for key in (("history", "father"), ("history", "son"), ("tree", "e1"))
    ]
    # two same-kind, same-label people of one source descriptor coexist:
    # their places among the map's clusters tell them apart
    assert sorted(held[qid][1] for qid in people) == [
        REPORT,
        f"{REPORT} (2)",
        f"{REPORT} (3)",
    ]
    # the same label of another kind is another item, described as such
    place = written.items[("tree", "e2")]
    assert held[place][1] == "place named in a genealogical report"
    for _, described in held.values():
        assert "hatter" not in described
        assert MARK not in described
    _, labels = WikiClient(client.config).label_index()
    assert sorted(labels.holders(EUGEN)) == sorted(written.items.values())


def test_each_item_was_made_whole_in_one_write(client, written):
    properties, items = client.vocabulary()
    mints = labelled(written.made)
    assert sorted(item["id"] for item in mints) == sorted(
        written.items.values()
    )
    sources = {"history": written.history.qid, "tree": written.tree.qid}
    for (batch, local_id), qid in written.items.items():
        (item,) = [item for item in mints if item["id"] == qid]
        (kind,) = item["claims"][properties["instance_of"]]
        expected = "place" if local_id == "e2" else "human"
        assert kind["mainsnak"]["datavalue"]["value"]["id"] == items[expected]
        # the source's reference and the run's, each a block of its own
        # (MODELING.md, "Reference pattern")
        assert len(kind["references"]) == 2
        cited = {
            key: [
                snak["datavalue"]["value"]["id"]
                for block in kind["references"]
                for snak in block["snaks"].get(properties[key], [])
            ]
            for key in ("stated_in", "inferred_from_extraction_run")
        }
        assert cited == {
            "stated_in": [sources[batch]],
            "inferred_from_extraction_run": [written.runs[batch]],
        }


def test_the_same_batches_written_again_mint_nothing(client, written):
    made: list[dict] = []
    fresh = WikiClient(client.config)
    runs, items = write_both(
        fresh, written.history, written.tree, written.identity, made
    )
    assert items == written.items
    assert runs == written.runs
    assert labelled(made) == []


def test_the_run_item_records_the_maps_hash(client, written):
    held = terms(client, written.runs.values())
    for _, described in held.values():
        assert described.endswith(f", identity map {written.identity.hash}")
    tag = f" map {written.identity.hash[:MAP_TAG]} "
    for label, _ in held.values():
        assert tag in label


def test_a_write_with_another_map_is_refused(client, written):
    history = history_batch(written.history)
    # the map of the history alone: one of its clusters is described by
    # another source descriptor than in the map of the two
    alone = mapped(history)
    assert alone.hash != written.identity.hash
    made: list[dict] = []
    with pytest.MonkeyPatch.context() as patch:
        record_mints(patch, made)
        with pytest.raises(OtherIdentityMap) as refused:
            BatchWriter(client).write_batch(
                resolve_batch(history, alone), identity=alone
            )
    assert written.identity.hash[:MAP_TAG] in refused.value.tags
    assert alone.hash[:MAP_TAG] not in refused.value.tags
    # nothing was written: no item, and no run item for the other map
    assert made == []
    assert client.find_item_by_label(run_label(history, alone)) is None
