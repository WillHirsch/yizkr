"""The writer finds or mints the item of every local entity through the
identity map alone (#109; ADR-0035, ADR-0010).

Each cluster of the map is one item, minted in one write (#86) with its
label, a description made from the map alone (its kind, the kind of the
source that names it or how many do, and its place among the map's
clusters of its label and kind), its aliases (the labels of all its
mentions and the batch's names for it) and its `instance of` with both
references. A later write of a batch with the same map finds it by that
label and description; no item is ever found by its label alone, a
curator's 'distinct' is never undone, and a batch with a mention the map
lacks is refused before anything is written.

The fixtures are fictional (docs/EXAMPLES.md): Eugen Löbig the master
hatter and a son of the same name, a homonym these tests make up, named
by the 2008 family history (*A few pages of family history*) and by the
1984 family tree, two sources of one kind; and the Hutmacherei zum Blauen
Hut, the firm, under the same name as a place a test makes up.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.fixtures.identity import corpus, mapped
from tests.unit.test_pipeline import (
    ITEMS,
    MEDIA_SHA1,
    PROPERTIES,
    RUN,
    FakeWiki,
)
from yizkr.claims import ClaimBatch
from yizkr.pipeline.clustering import (
    ClusteringInputError,
    IdentityMap,
    Manifest,
    UnmappedMention,
)
from yizkr.pipeline.reconcile import apply_equivalences
from yizkr.pipeline.resolve import resolve_batch
from yizkr.pipeline.writer import (
    BatchWriter,
    cluster_terms,
    mapped_clusters,
    source_descriptor,
)

EUGEN = "Eugen Löbig"
TITLE = "A few pages of family history"
REPORT = "human named in a genealogical report"
# the 1984 family tree, a second genealogical report
TREE_SHA1 = hashlib.sha1(b"fictional scan: the 1984 family tree").hexdigest()
TREE_RUN = dict(RUN, started_at="2026-07-15T09:00:00Z")
# the artifact kinds the fake vocabulary holds besides test_pipeline's
SOURCE_KINDS = {
    "genealogical_report": "Q90",
    "letter": "Q91",
    "official_document": "Q92",
}


def _ref(local_id: str) -> dict:
    return {"kind": "local", "local_id": local_id}


def _born(local_id: str, year: int, sha1: str = MEDIA_SHA1) -> dict:
    return {
        "subject": _ref(local_id),
        "property": "date_of_birth",
        "value": {"type": "time", "year": year},
        "source": {"artifact": {"kind": "artifact", "sha1": sha1}},
    }


def _eugens(order: tuple[str, str] = ("father", "son")) -> ClaimBatch:
    """The family history's batch: both Eugens, the father born 1836 and
    his son, born 1870, whose relative he is; the manifest lists them in
    `order`, and the claims always name the son first."""
    records = {
        "father": {
            "id": "father",
            "label": EUGEN,
            "kind": "human",
            "summary": "master hatter of Flörburg, 1836-1909",
            "aliases": ["Muckel"],
            "mention_lines": [3],
        },
        "son": {
            "id": "son",
            "label": f" {EUGEN}",
            "kind": "human",
            "summary": "son of the master hatter, born 1870",
            "mention_lines": [7],
        },
    }
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                _born("son", 1870),
                {
                    "subject": _ref("son"),
                    "property": "relative",
                    "value": {"type": "item", "value": _ref("father")},
                    "source": {
                        "artifact": {"kind": "artifact", "sha1": MEDIA_SHA1}
                    },
                },
                _born("father", 1836),
            ],
            "entities": [records[key] for key in order],
        }
    )


def _tree(label: str = EUGEN, kind: str = "human", **record) -> ClaimBatch:
    """The family tree's batch: a Eugen Löbig born 1836, of `kind`."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": TREE_RUN,
            "claims": [_born("e1", 1836, TREE_SHA1)],
            "entities": [{"id": "e1", "label": label, "kind": kind, **record}],
        }
    )


class SourcedWiki(FakeWiki):
    """A fake instance whose vocabulary has artifact kinds."""

    def vocabulary(self):
        properties, items = super().vocabulary()
        return properties, {**items, **SOURCE_KINDS}


def _wiki(*kinds: tuple[str, str], tree: bool = True) -> FakeWiki:
    """A fake instance holding the family history, under its title, an
    `instance of` each of `kinds` (QID, rank), a genealogical report
    without any, and the family tree, a genealogical report."""
    from wikibaseintegrator.datatypes import Item
    from wikibaseintegrator.wbi_enums import ActionIfExists, WikibaseRank

    wiki = SourcedWiki(detached=True)
    sources = [(MEDIA_SHA1, TITLE, kinds)]
    if tree:
        sources.append((TREE_SHA1, "The family tree", ()))
    for sha1, title, given in sources:
        source = wiki.wbi.item.new()
        source.labels.set("en", title)
        for qid, rank in given or ((SOURCE_KINDS["genealogical_report"], ""),):
            source.claims.add(
                Item(
                    prop_nr=PROPERTIES["instance_of"],
                    value=qid,
                    rank=WikibaseRank(rank) if rank else WikibaseRank.NORMAL,
                ),
                action_if_exists=ActionIfExists.FORCE_APPEND,
            )
        wiki.media[sha1] = source.write().id
    return wiki


def _first_sent(wiki: FakeWiki, qid: str) -> dict:
    return next(item for item in wiki.wbi.item.sent if item.get("id") == qid)


def _description(wiki: FakeWiki, qid: str) -> str | None:
    found = wiki.wbi.item.get(qid).descriptions.get("en")
    return str(found) if found else None


def _aliases(wiki: FakeWiki, qid: str) -> list[str]:
    return [str(a) for a in wiki.wbi.item.get(qid).aliases.get("en") or []]


def _labelled(wiki: FakeWiki, label: str) -> list[str]:
    return [
        qid
        for qid, item in wiki.wbi.item.store.items()
        if str(item.labels.get("en") or "") == label
    ]


def _cited(statement: dict, key: str) -> list[str]:
    return [
        snak["datavalue"]["value"]["id"]
        for block in statement["references"]
        for snak in block["snaks"].get(PROPERTIES[key], [])
    ]


def _kinds(wiki: FakeWiki, qid: str) -> dict[str, list[str]]:
    """Each kind the item states -> the artifacts each statement of it is
    stated in."""
    item = wiki.wbi.item.get(qid).get_json()
    found: dict[str, list[str]] = {}
    for statement in item["claims"].get(PROPERTIES["instance_of"], []):
        value = statement["mainsnak"]["datavalue"]["value"]["id"]
        found.setdefault(value, []).extend(_cited(statement, "stated_in"))
    return found


def _two_eugens_wiki_items(written) -> tuple[str, str]:
    return written.created_items["father"], written.created_items["son"]


# -- the description ----------------------------------------------------------


class TestTheDescriptor:
    def test_kind_and_source_kind(self):
        assert source_descriptor("human", "genealogical_report") == REPORT
        assert (
            source_descriptor("human", "genealogical_report", 2)
            == f"{REPORT} (2)"
        )
        assert (
            source_descriptor("occupation_class", "letter")
            == "occupation class named in a letter"
        )
        assert (
            source_descriptor("place", "official_document", 12)
            == "place named in an official document (12)"
        )

    def test_without_a_kind_or_a_source_kind(self):
        assert source_descriptor(None, None) == "entity named in a source"
        assert source_descriptor("human", None, 3) == (
            "human named in a source (3)"
        )

    def test_a_cluster_of_several_documents_counts_them(self):
        # whatever their kinds, which the descriptor never reads
        assert source_descriptor("human", "letter", 1, 3) == (
            "human named in 3 sources"
        )
        assert source_descriptor("place", None, 2, 2) == (
            "place named in 2 sources (2)"
        )


class TestTheClusterTerms:
    def test_one_cluster_a_record(self):
        identity = mapped(_eugens())
        terms = cluster_terms(identity)
        assert len(terms) == 2
        assert {t.label for t in terms.values()} == {EUGEN}
        # the two clusters of one label and one kind are placed by their
        # cluster ids, from the map alone
        by_id = sorted(terms)
        assert [terms[cid].ordinal for cid in by_id] == [1, 2]
        assert {t.sources for t in terms.values()} == {1}

    def test_the_label_most_mentions_give_then_the_longest(self):
        tree = _tree(f"{EUGEN} der Ältere")
        identity = corpus(
            _eugens(),
            tree,
            entities=((EUGEN, f"{EUGEN} der Ältere"),),
        )
        (only,) = cluster_terms(identity).values()
        # two mentions give the short form, one the long
        assert only.label == EUGEN
        assert only.aliases == (f"{EUGEN} der Ältere",)
        assert only.sources == 2
        identity = corpus(
            _tree(f"{EUGEN} der Ältere"),
            _eugens(("father", "son")).model_copy(
                update={
                    "entities": _eugens().entities[:1],
                    "claims": _eugens().claims[2:],
                }
            ),
            entities=((EUGEN, f"{EUGEN} der Ältere"),),
        )
        (only,) = cluster_terms(identity).values()
        # one each: the longer, the fuller form
        assert only.label == f"{EUGEN} der Ältere"

    def test_another_kind_has_places_of_its_own(self):
        identity = mapped(_eugens(), _tree(kind="place"))
        terms = cluster_terms(identity)
        assert sorted(
            t.ordinal for t in terms.values() if t.kind == "human"
        ) == [1, 2]
        assert [t.ordinal for t in terms.values() if t.kind == "place"] == [1]

    def test_made_once_a_map(self):
        identity = mapped(_eugens())
        assert cluster_terms(identity) is cluster_terms(identity)


# -- the identity map's read side ---------------------------------------------


class TestTheMapsReadSide:
    def test_the_batches_document_and_clusters(self):
        identity = mapped(_eugens(), _tree())
        document, clusters = identity.clusters_of(
            _eugens(), [TREE_SHA1, MEDIA_SHA1]
        )
        assert document == MEDIA_SHA1
        assert set(clusters) == {"father", "son"}
        assert clusters["father"] != clusters["son"]
        assert identity.cluster(clusters["father"]).documents == (MEDIA_SHA1,)

    def test_a_mention_the_map_lacks_is_named(self):
        with pytest.raises(UnmappedMention, match="'e1'"):
            mapped(_eugens()).clusters_of(_tree(), [TREE_SHA1])

    def test_a_map_is_read_from_its_file(self, tmp_path):
        identity = mapped(_eugens())
        path = tmp_path / "identity-map.json"
        path.write_text(json.dumps(identity.data))
        assert IdentityMap.read(path).hash == identity.hash

    def test_a_map_whose_decisions_do_not_hash_to_its_hash_is_refused(
        self, tmp_path
    ):
        data = dict(mapped(_eugens()).data, hash="0" * 64)
        path = tmp_path / "identity-map.json"
        path.write_text(json.dumps(data))
        with pytest.raises(ClusteringInputError, match="identity map"):
            IdentityMap.read(path)
        with pytest.raises(ClusteringInputError):
            IdentityMap.read(tmp_path / "none.json")

    def test_of_clusters_the_manifests(self):
        identity = IdentityMap.of([Manifest(MEDIA_SHA1, _eugens())])
        assert identity.hash == mapped(_eugens()).hash


# -- the source kind ----------------------------------------------------------


class TestTheSourceKind:
    def test_never_the_title(self):
        wiki = _wiki()
        written = BatchWriter(wiki).write_batch(
            _eugens(), identity=mapped(_eugens())
        )
        for qid in _two_eugens_wiki_items(written):
            described = _description(wiki, qid)
            assert TITLE not in described
            assert "family history" not in described

    def test_a_deprecated_kind_is_passed_over(self):
        wiki = _wiki(
            (SOURCE_KINDS["letter"], "deprecated"),
            (SOURCE_KINDS["official_document"], ""),
        )
        written = BatchWriter(wiki).write_batch(
            _eugens(), identity=mapped(_eugens())
        )
        assert {
            _description(wiki, qid) for qid in _two_eugens_wiki_items(written)
        } == {
            "human named in an official document",
            "human named in an official document (2)",
        }

    def test_a_source_of_no_known_kind(self):
        wiki = _wiki(("Q999", ""))
        written = BatchWriter(wiki).write_batch(
            _eugens(), identity=mapped(_eugens())
        )
        assert {
            _description(wiki, qid) for qid in _two_eugens_wiki_items(written)
        } == {"human named in a source", "human named in a source (2)"}

    def test_the_kind_of_the_clusters_document_not_another_cited(self):
        # the family history's batch also cites a letter, its last claim
        # the father's birth as the letter gives it: the clusters' one
        # document is the family history, whose kind names the source
        wiki = _wiki()
        letter = "f" * 40
        source = wiki.wbi.item.new()
        source.labels.set("en", "a letter")
        from wikibaseintegrator.datatypes import Item

        source.claims.add(
            Item(
                prop_nr=PROPERTIES["instance_of"], value=SOURCE_KINDS["letter"]
            )
        )
        wiki.media[letter] = source.write().id
        given = _eugens().model_dump(mode="json")
        given["claims"].append(_born("father", 1837, letter))
        batch = ClaimBatch.model_validate(given)
        written = BatchWriter(wiki).write_batch(batch, identity=mapped(batch))
        assert {
            _description(wiki, qid) for qid in _two_eugens_wiki_items(written)
        } == {REPORT, f"{REPORT} (2)"}

    def test_the_source_is_read_not_written(self):
        wiki = _wiki()
        history = wiki.media[MEDIA_SHA1]
        before = wiki.wbi.item.get(history).get_json()
        BatchWriter(wiki).write_batch(_eugens(), identity=mapped(_eugens()))
        assert wiki.wbi.item.get(history).get_json() == before


# -- two people of one name in one document -----------------------------------


class TestTwoEugensOfOneDocument:
    @pytest.mark.parametrize("dry_run", [False, True], ids=["write", "plan"])
    def test_both_are_minted_each_in_one_write(self, dry_run):
        wiki = _wiki()
        writes = wiki.wbi.item.writes

        written = BatchWriter(wiki, dry_run=dry_run).write_batch(
            _eugens(), identity=mapped(_eugens())
        )

        father, son = _two_eugens_wiki_items(written)
        assert father != son
        assert written.warnings == []
        if dry_run:
            assert {father, son} == {"(new:father)", "(new:son)"}
            assert wiki.wbi.item.writes == writes
            return
        assert sorted(_labelled(wiki, EUGEN)) == sorted([father, son])
        assert {_description(wiki, father), _description(wiki, son)} == {
            REPORT,
            f"{REPORT} (2)",
        }
        for qid in (father, son):
            # the first write of each holds its whole item: label,
            # description, and its kind with both references
            sent = _first_sent(wiki, qid)
            assert sent["labels"]["en"]["value"] == EUGEN
            assert sent["descriptions"]["en"]["value"] == _description(
                wiki, qid
            )
            (kind,) = sent["claims"][PROPERTIES["instance_of"]]
            assert (
                kind["mainsnak"]["datavalue"]["value"]["id"]
                == (ITEMS["human"])
            )
            assert _cited(kind, "stated_in") == [wiki.media[MEDIA_SHA1]]
            assert _cited(kind, "inferred_from_extraction_run") == [
                written.run_qid
            ]
        assert [
            a["value"] for a in _first_sent(wiki, father)["aliases"]["en"]
        ] == ["Muckel"]
        # the son's relative is the father, not himself
        (stated,) = wiki.wbi.item.get(son).claims.get(PROPERTIES["relative"])
        assert stated.mainsnak.datavalue["value"]["id"] == father

    def test_a_summary_is_never_the_description(self):
        wiki = _wiki()
        written = BatchWriter(wiki).write_batch(
            _eugens(), identity=mapped(_eugens())
        )
        for qid in _two_eugens_wiki_items(written):
            described = _description(wiki, qid)
            assert "18" not in described
            assert "hatter" not in described

    def test_the_manifests_order_never_decides_who_is_second(self):
        # the places come from the cluster ids, which the manifest's order
        # does not change
        wiki = _wiki()
        first = BatchWriter(wiki).write_batch(
            _eugens(), identity=mapped(_eugens())
        )
        other = _wiki()
        reordered = _eugens(("son", "father"))
        second = BatchWriter(other).write_batch(
            reordered, identity=mapped(reordered)
        )
        for local_id in ("father", "son"):
            assert _description(
                wiki, first.created_items[local_id]
            ) == _description(other, second.created_items[local_id])

    def test_a_failed_write_leaves_no_item(self, monkeypatch):
        # the second Eugen's one write fails: the first's item is whole,
        # the second has none, nothing under the label lacks its
        # description or kind, and a re-run mints the second whole
        wiki = _wiki()
        identity = mapped(_eugens())
        items = wiki.wbi.item
        new = items.new
        minted: list[str] = []

        def failing():
            item = new()
            write = item.write

            def refused(summary=None, **kwargs):
                if str(item.labels.get("en") or "") == EUGEN:
                    if minted:
                        raise ConnectionError("the instance went away")
                    minted.append(EUGEN)
                return write(summary=summary, **kwargs)

            item.write = refused
            return item

        monkeypatch.setattr(items, "new", failing)
        with pytest.raises(ConnectionError):
            BatchWriter(wiki).write_batch(_eugens(), identity=identity)
        (first,) = _labelled(wiki, EUGEN)
        assert _description(wiki, first) in (REPORT, f"{REPORT} (2)")
        assert wiki.wbi.item.get(first).claims.get(PROPERTIES["instance_of"])

        monkeypatch.setattr(items, "new", new)
        written = BatchWriter(wiki).write_batch(_eugens(), identity=identity)
        assert len(_labelled(wiki, EUGEN)) == 2
        assert first in written.created_items.values()


# -- the acceptance of #109 ---------------------------------------------------


class TestTheMapIsTheOnlyWay:
    def test_a_curators_eugen_under_the_label_is_never_joined(self):
        # a Eugen Löbig a curator made, with a description of their own:
        # no Eugen of the batch is found by the label, and the curator's
        # item is left as it is
        wiki = _wiki()
        earlier = wiki.wbi.item.new()
        earlier.labels.set("en", EUGEN)
        earlier.descriptions.set("en", "a curator's Eugen")
        earlier = earlier.write().id
        before = wiki.wbi.item.get(earlier).get_json()

        written = BatchWriter(wiki).write_batch(
            _eugens(), identity=mapped(_eugens())
        )

        assert earlier not in written.created_items.values()
        assert wiki.wbi.item.get(earlier).get_json() == before
        assert len(_labelled(wiki, EUGEN)) == 3

    def test_an_item_of_the_label_and_description_from_elsewhere_is_found(
        self,
    ):
        # the key is the label and the description: an item that holds
        # both is the cluster's, as the write of the same map made it
        wiki = _wiki()
        identity = mapped(_eugens())
        first = BatchWriter(wiki).write_batch(_eugens(), identity=identity)
        other = wiki.wbi.item.new()
        other.labels.set("en", EUGEN)
        other.descriptions.set("en", f"{REPORT} (3)")
        other.write()

        again = BatchWriter(wiki).write_batch(_eugens(), identity=identity)

        assert again.created_items == first.created_items

    def test_a_distinct_decision_is_never_overridden(self):
        # the reviewer said the tree's Eugen is not the item a write of the
        # family history made: no equivalence is applied, the map keeps
        # them apart, and the tree's write never joins him by his label,
        # whatever kind or source he shares with that item
        wiki = _wiki()
        tree = _tree()
        identity = mapped(_eugens(), tree)
        history = BatchWriter(wiki).write_batch(_eugens(), identity=identity)

        rejected = apply_equivalences(tree, {})
        written = BatchWriter(wiki).write_batch(
            resolve_batch(rejected, identity), identity=identity
        )

        assert (
            written.created_items["e1"] not in history.created_items.values()
        )
        assert len(_labelled(wiki, EUGEN)) == 3

    def test_the_same_label_of_another_kind_is_another_item(self):
        # the tree's Eugen Löbig is a place, as an extraction may read a
        # name: his cluster is of its own kind, and so is his item
        wiki = _wiki()
        tree = _tree(kind="place")
        identity = mapped(_eugens(), tree)
        history = BatchWriter(wiki).write_batch(_eugens(), identity=identity)

        written = BatchWriter(wiki).write_batch(tree, identity=identity)

        place = written.created_items["e1"]
        assert place not in history.created_items.values()
        assert _description(wiki, place) == (
            "place named in a genealogical report"
        )
        assert _kinds(wiki, place) == {ITEMS["place"]: [wiki.media[TREE_SHA1]]}

    def test_two_people_of_one_name_and_one_descriptor_coexist(self):
        # the family history and the tree, both genealogical reports, each
        # name a Eugen Löbig the map keeps apart: three clusters of one
        # label, kind and source descriptor, three items
        wiki = _wiki()
        tree = _tree()
        identity = mapped(_eugens(), tree)
        history = BatchWriter(wiki).write_batch(_eugens(), identity=identity)
        written = BatchWriter(wiki).write_batch(tree, identity=identity)

        eugens = {*history.created_items.values(), written.created_items["e1"]}
        assert len(eugens) == 3
        assert sorted(_description(wiki, qid) for qid in eugens) == [
            REPORT,
            f"{REPORT} (2)",
            f"{REPORT} (3)",
        ]
        # and each finds its own again
        again = BatchWriter(wiki).write_batch(tree, identity=identity)
        assert again.created_items == written.created_items

    def test_the_same_batch_after_a_reset_gives_the_same_items(self):
        # a reset instance, the same registrations, the same batches and
        # the same map: the same items, under the same terms, and a second
        # write of each mints nothing
        tree = _tree()
        identity = mapped(_eugens(), tree)

        def run():
            wiki = _wiki()
            results = [
                BatchWriter(wiki).write_batch(batch, identity=identity)
                for batch in (_eugens(), tree)
            ]
            items = {
                qid: (
                    str(wiki.wbi.item.get(qid).labels.get("en")),
                    _description(wiki, qid),
                )
                for result in results
                for qid in result.created_items.values()
            }
            return wiki, results, items

        wiki, first, items = run()
        _, second, again = run()
        assert [r.created_items for r in first] == [
            r.created_items for r in second
        ]
        assert items == again
        store = len(wiki.wbi.item.store)
        for batch, result in zip((_eugens(), tree), first, strict=True):
            rewritten = BatchWriter(wiki).write_batch(batch, identity=identity)
            assert rewritten.created_items == result.created_items
            assert rewritten.written == 0
        assert len(wiki.wbi.item.store) == store

    def test_a_cluster_of_two_documents_is_one_item(self):
        # the map puts the tree's Eugen in the father's cluster: the tree
        # finds the father's item, adds the name it gives him and its own
        # witness of his kind, and both describe him by his two sources
        wiki = _wiki()
        tree = _tree(aliases=["der alte Hutmacher"], mention_lines=[2])
        father_only = _eugens().model_copy(
            update={
                "entities": _eugens().entities[:1],
                "claims": _eugens().claims[2:],
            }
        )
        identity = corpus(father_only, tree, entities=(EUGEN,))
        history = BatchWriter(wiki).write_batch(father_only, identity=identity)
        father = history.created_items["father"]

        written = BatchWriter(wiki).write_batch(tree, identity=identity)

        assert written.created_items == {"e1": father}
        assert _description(wiki, father) == "human named in 2 sources"
        assert _aliases(wiki, father) == ["Muckel", "der alte Hutmacher"]
        assert _kinds(wiki, father) == {
            ITEMS["human"]: [wiki.media[MEDIA_SHA1], wiki.media[TREE_SHA1]]
        }
        assert written.type_statements_referenced == 1

    def test_a_minted_item_takes_every_mentions_label_as_an_alias(self):
        # the map puts the tree's "Eugen Löbig der Ältere" in the father's
        # cluster: the family history's write mints him under that fuller
        # form, one mention giving each, with his own label as an alias,
        # before the tree is written
        wiki = _wiki()
        tree = _tree(f"{EUGEN} der Ältere")
        father_only = _eugens().model_copy(
            update={
                "entities": _eugens().entities[:1],
                "claims": _eugens().claims[2:],
            }
        )
        identity = corpus(
            father_only, tree, entities=((EUGEN, f"{EUGEN} der Ältere"),)
        )
        father = (
            BatchWriter(wiki)
            .write_batch(father_only, identity=identity)
            .created_items["father"]
        )
        assert _labelled(wiki, f"{EUGEN} der Ältere") == [father]
        assert _aliases(wiki, father) == [EUGEN, "Muckel"]

    def test_a_later_kind_reaches_a_found_item_cited_to_its_artifact(self):
        # the tree types the father's cluster as an unclassified entity
        wiki = _wiki()
        tree = _tree(kind="unclassified_entity", type_hint="hatter")
        father_only = _eugens().model_copy(
            update={
                "entities": _eugens().entities[:1],
                "claims": _eugens().claims[2:],
            }
        )
        identity = corpus(father_only, tree, entities=(EUGEN,))
        father = (
            BatchWriter(wiki)
            .write_batch(father_only, identity=identity)
            .created_items["father"]
        )

        written = BatchWriter(wiki).write_batch(tree, identity=identity)

        assert written.created_items == {"e1": father}
        assert _kinds(wiki, father) == {
            ITEMS["human"]: [wiki.media[MEDIA_SHA1]],
            ITEMS["unclassified_entity"]: [wiki.media[TREE_SHA1]],
        }
        (hint,) = wiki.wbi.item.get(father).claims.get(PROPERTIES["type_hint"])
        assert hint.mainsnak.datavalue["value"] == "hatter"
        again = BatchWriter(wiki).write_batch(tree, identity=identity)
        assert (
            again.type_statements_written,
            again.type_statements_referenced,
        ) == (0, 0)


class TestAnUnmappedMention:
    @pytest.mark.parametrize("dry_run", [False, True], ids=["write", "plan"])
    def test_no_map_refuses_the_batch_before_any_read(self, dry_run):
        class Unread(SourcedWiki):
            def label_index(self):
                raise AssertionError("nothing is read")

            def string_statement_index(self, pid):
                raise AssertionError("nothing is read")

        wiki = Unread(detached=True)
        writes = wiki.wbi.item.writes
        with pytest.raises(UnmappedMention, match="no identity map"):
            BatchWriter(wiki, dry_run=dry_run).write_batch(_eugens())
        assert wiki.wbi.item.writes == writes

    def test_a_map_without_the_batch_refuses_it(self):
        wiki = _wiki()
        writes = wiki.wbi.item.writes
        with pytest.raises(UnmappedMention, match="'e1'"):
            BatchWriter(wiki).write_batch(_tree(), identity=mapped(_eugens()))
        assert wiki.wbi.item.writes == writes

    def test_a_ref_without_its_record_is_no_mention(self):
        given = _tree().model_dump(mode="json")
        given["claims"].append(
            dict(given["claims"][0], subject=dict(_ref("e2"), label=EUGEN))
        )
        batch = ClaimBatch.model_validate(given)
        with pytest.raises(UnmappedMention, match="'e2'.*no record"):
            mapped_clusters(batch, mapped(batch))

    def test_a_batch_naming_no_local_entity_needs_no_map(self):
        approved = apply_equivalences(_tree(), {"e1": "Q500"})
        assert mapped_clusters(approved, None) == ({}, None, {})


class TestTheRunItem:
    def test_it_records_the_maps_hash(self):
        wiki = _wiki()
        identity = mapped(_eugens())
        written = BatchWriter(wiki).write_batch(_eugens(), identity=identity)
        described = _description(wiki, written.run_qid)
        assert described.endswith(f", identity map {identity.hash}")

    def test_another_map_has_a_run_item_of_its_own(self):
        wiki = _wiki()
        first = BatchWriter(wiki).write_batch(
            _eugens(), identity=mapped(_eugens())
        )
        other = BatchWriter(wiki).write_batch(
            _eugens(), identity=mapped(_eugens(), _tree())
        )
        assert other.run_qid != first.run_qid
        again = BatchWriter(wiki).write_batch(
            _eugens(), identity=mapped(_eugens())
        )
        assert again.run_qid == first.run_qid
