"""Each new item is minted in one write, with its label, a source
descriptor as its description and its `instance of` with both its
references, so two people of one name that one source names both mint
(#86).

The fixture is two Eugen Löbigs, father and son, both human, each a
record of their own in the manifest of one batch citing the 2008 family
history (*A few pages of family history*): the master hatter of the
example universe, and a son of the same name, a homonym these tests make
up. Wikibase refuses two items with one label and one description, so
the son's description takes his place among the manifest's records
under the label, and a re-run finds each by it.
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
from tests.unit.test_writer_unknown_characters import MARKED, knows_the_mark
from yizkr.claims import ClaimBatch, EntityRecord
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.resolve import resolvable_labels, resolve_batch
from yizkr.pipeline.writer import (
    TERM_LIMIT,
    BatchWriter,
    homonym_ordinals,
    source_descriptor,
    source_title,
)

EUGEN = "Eugen Löbig"
TITLE = "A few pages of family history"
# the family history as the registrar labels a second copy of one title
TAGGED_TITLE = f"{TITLE} [0a1b2c3d]"
FATHER_DESCRIPTION = f"human named in “{TITLE}”"
SON_DESCRIPTION = f"human named in “{TITLE}” (2)"
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
    items it asks for."""

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


def _wiki(title: str | None = TITLE) -> FakeWiki:
    """A fake instance holding the family history, labelled `title`."""
    wiki = ResolvingWiki(detached=True)
    history = wiki.wbi.item.new()
    if title:
        history.labels.set("en", title)
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
    def test_kind_and_title(self):
        assert source_descriptor("human", TITLE) == FATHER_DESCRIPTION
        assert source_descriptor("human", TITLE, 2) == SON_DESCRIPTION
        assert (
            source_descriptor("occupation_class", TITLE)
            == f"occupation class named in “{TITLE}”"
        )

    def test_without_a_kind_or_a_title(self):
        assert source_descriptor(None, None) == "entity named in a source"
        assert source_descriptor("human", None, 3) == (
            "human named in a source (3)"
        )

    @pytest.mark.parametrize("ordinal", [1, 2, 12])
    def test_a_long_title_is_cut_and_the_ordinal_kept(self, ordinal):
        title = "Hutmacherei zum Blauen Hut, Flörburg: " * 10
        described = source_descriptor("human", title.strip(), ordinal)
        # cut at a character, a space before the ellipsis dropped
        assert TERM_LIMIT - 1 <= len(described) <= TERM_LIMIT
        tail = f"…”{f' ({ordinal})' if ordinal > 1 else ''}"
        assert described.endswith(tail)
        assert described.startswith("human named in “Hutmacherei zum")
        # no digest, as the cut of a long term has (_cut)
        assert "sha1" not in described

    def test_a_title_at_the_limit_is_whole(self):
        room = TERM_LIMIT - len("human named in “”")
        title = ("Flörburg " * 40)[:room].strip() + "x"
        title = title[:room]
        described = source_descriptor("human", title)
        assert described == f"human named in “{title}”"
        assert len(described) == TERM_LIMIT

    @knows_the_mark
    def test_a_title_python_does_not_know_is_left_out(self):
        # (here) Nina's 1958 letter, its title holding the combining
        # Cyrillic i Python's tables do not know: the instance may keep it
        # in another form, so the descriptor names no title
        assert source_title(f"Письмо Нины {MARKED}") is None

    def test_the_title_loses_its_content_tag(self):
        assert source_title(TAGGED_TITLE) == TITLE
        assert source_title(f"{TITLE}, page 2") == f"{TITLE}, page 2"
        assert source_title(None) is None
        assert source_title("") is None


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
        assert homonym_ordinals(records) == {
            "a": 1,
            "b": 1,
            "c": 2,
            "e": 3,
        }


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

    def test_the_title_is_read_without_its_content_tag(self):
        wiki = _wiki(TAGGED_TITLE)
        written = BatchWriter(wiki).write_batch(_eugens())
        assert _description(wiki, written.created_items["son"]) == (
            SON_DESCRIPTION
        )

    def test_an_untitled_source(self):
        wiki = _wiki(None)
        written = BatchWriter(wiki).write_batch(_eugens())
        assert _description(wiki, written.created_items["father"]) == (
            "human named in a source"
        )
        assert _description(wiki, written.created_items["son"]) == (
            "human named in a source (2)"
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
        other.descriptions.set("en", f"human named in “{TITLE}” (3)")
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
