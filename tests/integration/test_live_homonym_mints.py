"""Live check that two people of one name that one source names are both
minted, each in one write with its source descriptor (#86).

The fixture is two Eugen Löbigs, father and son, both human: the master
hatter (1836-1909) of the example universe, and a son of the same name,
a homonym this test makes up. One batch citing the 2008 family history
gives each a manifest record of their own under one label, the son's
birth (1870), his father, and the father's birth; its first claim names
the son. The writer mints both:

* each in one `wbeditentity` write that carries its label, its
  description and its `instance of human` with both references (stated
  in the family history, inferred from the run), so no write leaves an
  item half made;
* with descriptions that differ, the writer's source descriptor: the
  father's "human named in a genealogical report", the kind of the
  source, never its title, the son's the same with "(2)",
  his place among the manifest's records under the label, as Wikibase
  refuses two items with one label and one description; neither holds a
  life date of the batch's summaries;
* the father first, though the first claim names the son, so his is the
  earliest item under the label, which the label index names.

Writing the batch again mints nothing and finds each by its own item; so
does resolving it first, as a run does: the father resolves by his label,
and the son stays local and is found by his description.

Every label and title carries a marker, eight hex digits of a hash over
the fixture, over the writer's and the resolve step's source and over
eight random bytes drawn once a session (conftest.fixture_mark), so each
session meets the pair as a first write. Like every live test it writes
fictional data to the one instance, so the session that runs it ends by
clearing the recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from wikibaseintegrator.entities.item import ItemEntity

from yizkr.claims import ClaimBatch
from yizkr.core.wiki import WikiClient
from yizkr.pipeline import resolve, writer
from yizkr.pipeline.resolve import resolvable_labels, resolve_batch
from yizkr.pipeline.writer import BatchWriter, source_descriptor

from .conftest import Registered, fixture_mark, register_fictional

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#86: two Eugen Löbigs, father and son, each a manifest record of "
    "their own in one batch citing the 2008 family history, both minted"
)
MARK = fixture_mark(FIXTURE, writer, resolve)
EUGEN = f"Eugen Löbig (mint test {MARK})"
TITLE = f"A few pages of family history (mint test {MARK})"
RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-10-06T15:00:00+00:00",
}


def eugens(source: Registered) -> ClaimBatch:
    """The family history's batch: the father, then the son, in its
    manifest, each with a summary that gives his life dates; its first
    claim names the son."""
    cites = {"artifact": source.ref.model_dump(mode="json")}

    def ref(local_id: str) -> dict:
        return {"kind": "local", "local_id": local_id}

    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                {
                    "subject": ref("son"),
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1870},
                    "source": cites,
                },
                {
                    "subject": ref("son"),
                    "property": "father",
                    "value": {"type": "item", "value": ref("father")},
                    "source": cites,
                },
                {
                    "subject": ref("father"),
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1836},
                    "source": cites,
                },
            ],
            "entities": [
                {
                    "id": "father",
                    "label": EUGEN,
                    "kind": "human",
                    "summary": "master hatter of Flörburg, 1836-1909",
                },
                {
                    "id": "son",
                    "label": EUGEN,
                    "kind": "human",
                    "summary": "son of the master hatter, born 1870",
                },
            ],
        }
    )


@dataclass(frozen=True)
class Minted:
    source: Registered
    run: str
    first: dict[str, str]
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


@pytest.fixture(scope="module")
def minted(client: WikiClient) -> Minted:
    source = register_fictional(client, TITLE, "genealogical_report")
    made: list[dict] = []
    with pytest.MonkeyPatch.context() as patch:
        record_mints(patch, made)
        written = BatchWriter(client).write_batch(eugens(source))
    # the session's first write meets the pair as a first mint
    assert written.written == 3
    return Minted(source, written.run_qid, dict(written.created_items), made)


def labelled(made: list[dict]) -> list[dict]:
    return [item for item in made if item["labels"]["en"]["value"] == EUGEN]


def test_both_are_minted_each_in_one_write(client, minted):
    father, son = minted.first["father"], minted.first["son"]
    assert father != son
    properties, items = client.vocabulary()
    # the source is registered as a genealogical report: its kind, never
    # its title, goes in the description
    expected = {
        father: "human named in a genealogical report",
        son: "human named in a genealogical report (2)",
    }
    assert expected == {
        father: source_descriptor("human", "genealogical_report"),
        son: source_descriptor("human", "genealogical_report", 2),
    }
    # one write made each, and it carried the whole item
    mints = labelled(minted.made)
    assert sorted(item["id"] for item in mints) == sorted([father, son])
    for item in mints:
        assert item["descriptions"]["en"]["value"] == expected[item["id"]]
        (kind,) = item["claims"][properties["instance_of"]]
        assert kind["mainsnak"]["datavalue"]["value"]["id"] == items["human"]
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
            "stated_in": [minted.source.qid],
            "inferred_from_extraction_run": [minted.run],
        }
    # and the instance holds them so, the father's the earliest item
    found = client.api_get(
        {
            "action": "wbgetentities",
            "ids": f"{father}|{son}",
            "props": "labels|descriptions",
            "languages": "en",
        }
    )["entities"]
    for qid in (father, son):
        assert found[qid]["labels"]["en"]["value"] == EUGEN
        described = found[qid]["descriptions"]["en"]["value"]
        assert described == expected[qid]
        assert "hatter" not in described
        assert MARK not in described
    assert int(father[1:]) < int(son[1:])
    _, labels = WikiClient(client.config).label_index()
    assert labels.holders(EUGEN) == (father, son)


def test_a_rewrite_mints_nothing(client, minted):
    made: list[dict] = []
    with pytest.MonkeyPatch.context() as patch:
        record_mints(patch, made)
        again = BatchWriter(client).write_batch(eugens(minted.source))
    assert again.created_items == minted.first
    assert (again.written, again.skipped) == (0, 3)
    assert labelled(made) == []


def test_a_resolved_rewrite_mints_nothing(client, minted):
    # as a run does: resolve, then write
    batch = eugens(minted.source)
    held = resolvable_labels(client, batch)
    assert held == {EUGEN: minted.first["father"]}
    made: list[dict] = []
    with pytest.MonkeyPatch.context() as patch:
        record_mints(patch, made)
        again = BatchWriter(client).write_batch(resolve_batch(batch, held))
    assert again.created_items == {"son": minted.first["son"]}
    assert labelled(made) == []
