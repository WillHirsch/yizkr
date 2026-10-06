"""Each new item is minted in one write, with its label, a source
descriptor as its description and its `instance of` with both its
references, so two people of one name that one source names both mint
(#86).

The fixture is two Eugen Löbigs, father and son, both human, each a
record of their own in the manifest of one batch citing the 2008 family
history (*A few pages of family history*): the master hatter of the
example universe, and a son of the same name, a homonym these tests make
up. The description names the kind of the source, a genealogical
report, never its title, which may name a living person. Wikibase
refuses two items with one label and one description, so the son's
description takes his place among the manifest's records under the
label, and a re-run finds each by it.
"""

from __future__ import annotations

import pytest

from tests.unit.test_pipeline import (
    ITEMS,
    MEDIA_SHA1,
    PROPERTIES,
    RUN,
    FakeWiki,
)
from yizkr.claims import ClaimBatch, EntityRecord
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.resolve import resolvable_labels, resolve_batch
from yizkr.pipeline.writer import (
    BatchWriter,
    entity_terms,
    homonym_ordinals,
    source_descriptor,
)

EUGEN = "Eugen Löbig"
TITLE = "A few pages of family history"
FATHER_DESCRIPTION = "human named in a genealogical report"
SON_DESCRIPTION = "human named in a genealogical report (2)"
# the artifact kinds the fake vocabulary holds besides test_pipeline's
SOURCE_KINDS = {
    "genealogical_report": "Q90",
    "letter": "Q91",
    "official_document": "Q92",
}
ARTIFACT = {"kind": "artifact", "sha1": MEDIA_SHA1}


def _ref(local_id: str) -> dict:
    return {"kind": "local", "local_id": local_id}


def _born(local_id: str, year: int) -> dict:
    return {
        "subject": _ref(local_id),
        "property": "date_of_birth",
        "value": {"type": "time", "year": year},
        "source": {"artifact": ARTIFACT},
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
        },
        "son": {
            "id": "son",
            "label": f" {EUGEN}",
            "kind": "human",
            "summary": "son of the master hatter, born 1870",
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
                    "source": {"artifact": ARTIFACT},
                },
                _born("father", 1836),
            ],
            "entities": [records[key] for key in order],
        }
    )


class ResolvingWiki(FakeWiki):
    """A fake instance the resolve step reads too: the statements of the
    items it asks for, and a vocabulary with artifact kinds."""

    def vocabulary(self):
        properties, items = super().vocabulary()
        return properties, {**items, **SOURCE_KINDS}

    def label_index(self):
        """(property labels, item labels) as the wiki client reads them:
        a label two items hold names the earliest, and keeps both
        (ItemLabels)."""
        return WikiClient.label_index(
            self,
            {
                qid: {"label": str(item.labels.get("en") or "") or None}
                for qid, item in self.wbi.item.store.items()
            },
        )

    def fetch_claims(self, ids: list[str]) -> dict[str, dict]:
        return {
            qid: self.wbi.item.get(qid).get_json().get("claims") or {}
            for qid in ids
            if qid in self.wbi.item.store
        }


def _wiki(*kinds: tuple[str, str]) -> FakeWiki:
    """A fake instance holding the family history, under its title, an
    `instance of` each of `kinds` (QID, rank), a genealogical report
    without any."""
    from wikibaseintegrator.datatypes import Item
    from wikibaseintegrator.wbi_enums import ActionIfExists, WikibaseRank

    wiki = ResolvingWiki(detached=True)
    history = wiki.wbi.item.new()
    history.labels.set("en", TITLE)
    for qid, rank in kinds or ((SOURCE_KINDS["genealogical_report"], ""),):
        history.claims.add(
            Item(
                prop_nr=PROPERTIES["instance_of"],
                value=qid,
                rank=WikibaseRank(rank) if rank else WikibaseRank.NORMAL,
            ),
            action_if_exists=ActionIfExists.FORCE_APPEND,
        )
    wiki.media[MEDIA_SHA1] = history.write().id
    return wiki


def _first_sent(wiki: FakeWiki, qid: str) -> dict:
    return next(item for item in wiki.wbi.item.sent if item.get("id") == qid)


def _description(wiki: FakeWiki, qid: str) -> str | None:
    found = wiki.wbi.item.get(qid).descriptions.get("en")
    return str(found) if found else None


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


class TestTheDescriptor:
    def test_kind_and_source_kind(self):
        assert (
            source_descriptor("human", "genealogical_report")
            == FATHER_DESCRIPTION
        )
        assert (
            source_descriptor("human", "genealogical_report", 2)
            == SON_DESCRIPTION
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


class TestTheSourceKind:
    def test_never_the_title(self):
        wiki = _wiki()
        written = BatchWriter(wiki).write_batch(_eugens())
        for local_id in ("father", "son"):
            described = _description(wiki, written.created_items[local_id])
            assert TITLE not in described
            assert "family history" not in described

    def test_a_deprecated_kind_is_passed_over(self):
        wiki = _wiki(
            (SOURCE_KINDS["letter"], "deprecated"),
            (SOURCE_KINDS["official_document"], ""),
        )
        written = BatchWriter(wiki).write_batch(_eugens())
        assert _description(wiki, written.created_items["father"]) == (
            "human named in an official document"
        )

    def test_a_kind_the_vocabulary_lacks_is_passed_over(self):
        wiki = _wiki(("Q999", ""), (SOURCE_KINDS["letter"], ""))
        written = BatchWriter(wiki).write_batch(_eugens())
        assert _description(wiki, written.created_items["son"]) == (
            "human named in a letter (2)"
        )

    def test_a_source_of_no_known_kind(self):
        wiki = _wiki(("Q999", ""))
        written = BatchWriter(wiki).write_batch(_eugens())
        assert _description(wiki, written.created_items["father"]) == (
            "human named in a source"
        )
        assert _description(wiki, written.created_items["son"]) == (
            "human named in a source (2)"
        )

    def test_the_source_is_read_not_written(self):
        wiki = _wiki()
        history = wiki.media[MEDIA_SHA1]
        before = wiki.wbi.item.get(history).get_json()
        BatchWriter(wiki).write_batch(_eugens())
        assert wiki.wbi.item.get(history).get_json() == before


class TestTheOrdinals:
    def test_manifest_order_by_the_label_as_sent(self):
        records = {
            record.id: record
            for record in (
                EntityRecord(id="a", label=EUGEN, kind="human"),
                EntityRecord(id="b", label="Sebastian Löbig", kind="human"),
                # the label as the writer sends it: trimmed
                EntityRecord(id="c", label=f"{EUGEN} ", kind="human"),
                # on the artifact side: never counted
                EntityRecord(id="d", label=EUGEN, kind="photograph"),
                EntityRecord(id="e", label=EUGEN, kind="place"),
            )
        }
        assert homonym_ordinals(records, set(records)) == {
            "a": 1,
            "b": 1,
            "c": 2,
            "e": 3,
        }

    def test_an_audit_record_no_claim_names_takes_no_place(self):
        records = {
            record.id: record
            for record in (
                EntityRecord(id="a", label=EUGEN, kind="human", extract=False),
                EntityRecord(id="b", label=EUGEN, kind="human"),
                EntityRecord(id="c", label=EUGEN, kind="human", extract=False),
            )
        }
        assert homonym_ordinals(records, {"b"}) == {"b": 1}
        # named by a claim, an audit record takes its place
        assert homonym_ordinals(records, {"b", "c"}) == {"b": 1, "c": 2}

    def test_a_resolved_record_keeps_its_place(self):
        # resolve names the father's item by QID, so no local ref names
        # him: the extraction asserts about him, so he is still first
        records = {
            record.id: record
            for record in (
                EntityRecord(id="father", label=EUGEN, kind="human"),
                EntityRecord(id="son", label=EUGEN, kind="human"),
            )
        }
        assert homonym_ordinals(records, {"son"}) == {"father": 1, "son": 2}


class TestTwoEugens:
    @pytest.mark.parametrize("dry_run", [False, True], ids=["write", "plan"])
    def test_both_are_minted_each_in_one_write(self, dry_run):
        wiki = _wiki()
        writes = wiki.wbi.item.writes

        written = BatchWriter(wiki, dry_run=dry_run).write_batch(_eugens())

        father = written.created_items["father"]
        son = written.created_items["son"]
        assert father != son
        assert written.warnings == []
        if dry_run:
            assert (father, son) == ("(new:father)", "(new:son)")
            assert wiki.wbi.item.writes == writes
            return
        assert sorted(_labelled(wiki, EUGEN)) == sorted([father, son])
        # the first record under the label is minted first, though the
        # first claim names the son: the father's is the earliest item
        # under it, which a re-run and the resolve step find under it
        assert int(father[1:]) < int(son[1:])
        assert _description(wiki, father) == FATHER_DESCRIPTION
        assert _description(wiki, son) == SON_DESCRIPTION
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
        written = BatchWriter(wiki).write_batch(_eugens())
        for local_id in ("father", "son"):
            described = _description(wiki, written.created_items[local_id])
            assert "18" not in described
            assert "hatter" not in described

    def test_a_rerun_mints_nothing_and_sends_no_edit(self):
        wiki = _wiki()
        first = BatchWriter(wiki).write_batch(_eugens())
        writes, items = wiki.wbi.item.writes, len(wiki.wbi.item.store)

        again = BatchWriter(wiki).write_batch(_eugens())

        assert again.created_items == first.created_items
        assert (again.written, again.skipped) == (0, 3)
        assert (wiki.wbi.item.writes, len(wiki.wbi.item.store)) == (
            writes,
            items,
        )
        # and a dry run plans no new item either
        planned = BatchWriter(wiki, dry_run=True).write_batch(_eugens())
        assert planned.created_items == first.created_items

    def test_the_manifest_order_decides_who_is_second(self):
        wiki = _wiki()
        written = BatchWriter(wiki).write_batch(_eugens(("son", "father")))
        assert _description(wiki, written.created_items["son"]) == (
            FATHER_DESCRIPTION
        )
        assert _description(wiki, written.created_items["father"]) == (
            SON_DESCRIPTION
        )

    def test_the_first_joins_an_earlier_eugen_and_the_second_mints(self):
        # a Eugen Löbig a curator made, with a description of their own:
        # the father joins him, as the earliest item under the label
        # (ItemLabels), and his description is never overwritten
        wiki = _wiki()
        earlier = wiki.wbi.item.new()
        earlier.labels.set("en", EUGEN)
        earlier.descriptions.set("en", "a curator's Eugen")
        earlier = earlier.write().id

        written = BatchWriter(wiki).write_batch(_eugens())

        assert written.created_items["father"] == earlier
        son = written.created_items["son"]
        assert son != earlier
        assert _description(wiki, earlier) == "a curator's Eugen"
        assert _description(wiki, son) == SON_DESCRIPTION
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(_eugens())
        assert again.created_items == written.created_items
        assert wiki.wbi.item.writes == writes

    def test_an_item_whose_description_differs_is_not_the_sons(self):
        # a second Eugen under the label with another description is not
        # the son's: the son is found only by his own description
        wiki = _wiki()
        written = BatchWriter(wiki).write_batch(_eugens())
        other = wiki.wbi.item.new()
        other.labels.set("en", EUGEN)
        other.descriptions.set("en", f"{FATHER_DESCRIPTION} (3)")
        other.write()

        again = BatchWriter(wiki).write_batch(_eugens())

        assert again.created_items == written.created_items

    def test_a_failed_write_leaves_no_item(self, monkeypatch):
        # the son's one write fails: the father's item is whole, the son
        # has none, nothing under the label lacks its description or kind,
        # and a re-run mints the son whole
        wiki = _wiki()
        items = wiki.wbi.item
        new = items.new

        def failing():
            item = new()
            write = item.write

            def refused(summary=None, **kwargs):
                if str(item.descriptions.get("en") or "") == SON_DESCRIPTION:
                    raise ConnectionError("the instance went away")
                return write(summary=summary, **kwargs)

            item.write = refused
            return item

        monkeypatch.setattr(items, "new", failing)
        with pytest.raises(ConnectionError):
            BatchWriter(wiki).write_batch(_eugens())
        (father,) = _labelled(wiki, EUGEN)
        assert _description(wiki, father) == FATHER_DESCRIPTION
        assert wiki.wbi.item.get(father).claims.get(PROPERTIES["instance_of"])

        monkeypatch.setattr(items, "new", new)
        written = BatchWriter(wiki).write_batch(_eugens())
        assert len(_labelled(wiki, EUGEN)) == 2
        assert _description(wiki, written.created_items["son"]) == (
            SON_DESCRIPTION
        )

    def test_records_without_a_manifest_still_join(self):
        # two refs of one label, no manifest: one entity's refs, as a
        # face batch's are, so one item
        refs = [
            {
                "subject": {
                    "kind": "local",
                    "local_id": local_id,
                    "label": EUGEN,
                    "instance_of": "human",
                },
                "property": "date_of_birth",
                "value": {"type": "time", "year": 1836},
                "source": {"artifact": ARTIFACT},
            }
            for local_id in ("face-person-0", "face-person-1")
        ]
        batch = ClaimBatch.model_validate(
            {"extraction_run": RUN, "claims": refs}
        )
        wiki = _wiki()
        written = BatchWriter(wiki).write_batch(batch)
        assert (
            written.created_items["face-person-0"]
            == written.created_items["face-person-1"]
        )


class TestResolve:
    def test_the_second_stays_local_and_the_writer_finds_him(self):
        # the run's order: resolve, then write, then both again
        wiki = _wiki()
        first = BatchWriter(wiki).write_batch(
            resolve_batch(_eugens(), resolvable_labels(wiki, _eugens()))
        )

        held = resolvable_labels(wiki, _eugens())
        resolved = resolve_batch(_eugens(), held)
        again = BatchWriter(wiki).write_batch(resolved)

        # the father resolves to his item by label; the son, second under
        # it, stays local, and the writer finds him by his description and
        # mints nothing (the resolved batch is another batch, with a run
        # item of its own)
        father, son = first.created_items["father"], first.created_items["son"]
        assert held == {EUGEN: father}
        subjects = [
            claim.subject.model_dump()
            for claim in resolved.claims
            if claim.property == "date_of_birth"
        ]
        assert subjects == [
            {
                "kind": "local",
                **_ref("son"),
                "label": None,
                "description": None,
                "instance_of": None,
            },
            {"kind": "qid", "qid": father},
        ]
        assert again.created_items == {"son": son}
        assert sorted(_labelled(wiki, EUGEN)) == sorted([father, son])
        assert (again.written, again.skipped) == (0, 3)


class TestARecordNoClaimNames:
    """The manifest lists a Eugen Löbig no claim names (extract=false),
    then the one its claim names, and the instance holds a curator's
    Eugen: the named record is the first under the label, so it joins the
    curator's Eugen, as resolve finds it, and nothing is minted."""

    @staticmethod
    def batch() -> ClaimBatch:
        return ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [_born("named", 1836)],
                "entities": [
                    {
                        "id": "unnamed",
                        "label": EUGEN,
                        "kind": "human",
                        "extract": False,
                    },
                    {"id": "named", "label": EUGEN, "kind": "human"},
                ],
            }
        )

    @staticmethod
    def wiki():
        wiki = _wiki()
        earlier = wiki.wbi.item.new()
        earlier.labels.set("en", EUGEN)
        earlier.descriptions.set("en", "a curator's Eugen")
        return wiki, earlier.write().id

    def test_the_named_record_joins_the_earlier_eugen(self):
        wiki, earlier = self.wiki()
        items = len(wiki.wbi.item.store)

        written = BatchWriter(wiki).write_batch(self.batch())

        assert written.created_items == {"named": earlier}
        assert len(wiki.wbi.item.store) == items + 1  # the run item alone
        assert _description(wiki, earlier) == "a curator's Eugen"

    def test_resolve_and_the_plan_agree(self):
        wiki, earlier = self.wiki()
        batch = self.batch()
        records = {record.id: record for record in batch.entities}
        assert homonym_ordinals(records, entity_terms(batch, records)) == {
            "named": 1
        }

        held = resolvable_labels(wiki, batch)
        planned = BatchWriter(wiki, dry_run=True).write_batch(batch)

        assert held == {EUGEN: earlier}
        assert planned.created_items == {"named": earlier}
        resolved = resolve_batch(batch, held)
        assert resolved.claims[0].subject.model_dump() == {
            "kind": "qid",
            "qid": earlier,
        }
