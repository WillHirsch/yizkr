"""The writer refuses a statement whose item value is its own subject's
item, after the steps that put two refs on one item (#491; ADR-0035
§3.5).

Validation refuses a claim whose value names its own subject (#461), but
it reads the extraction's batch, before the identity map, an approved
equivalence or a curator's correction can put a claim's subject and its
value on one item. The writer checks again before its first write: such
a claim, or one whose qualifier's value is its subject's item, is a
counted refusal with its reason, never a written self-loop, and the rest
of the batch is written, in a dry run as in a write.

The fixtures are fictional (docs/EXAMPLES.md): Eugen Löbig the master
hatter, "Muckel", and a son of the same name, a homonym the identity-map
tests make up, named by the 2008 family history, with a `father` claim
between them and the family's pet name for the father from the son's
side; the 1984 family tree's Eugen; and Flörburg.
"""

from __future__ import annotations

import csv

import pytest

from tests.fixtures.identity import joined, mapped
from tests.unit.test_identity_map_writes import (
    EUGEN,
    SourcedWiki,
    _born,
    _ref,
    _wiki,
)
from tests.unit.test_pipeline import ITEMS, MEDIA_SHA1, PROPERTIES, RUN
from yizkr.claims import ClaimBatch
from yizkr.pipeline.reconcile import (
    EQUIVALENCE_FIELDS,
    apply_equivalences,
    read_approved_equivalences,
)
from yizkr.pipeline.resolve import resolve_batch
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import BatchWriter, self_references

# the fake vocabulary's properties besides test_pipeline's
FATHER = "P40"
PERSPECTIVE = "P41"
ISSUED_BY = "P42"
SOURCE = {"artifact": {"kind": "artifact", "sha1": MEDIA_SHA1}}


class FamilyWiki(SourcedWiki):
    """A fake instance whose vocabulary has `father` and `perspective`."""

    def vocabulary(self):
        properties, items = super().vocabulary()
        return {
            **properties,
            "father": FATHER,
            "perspective": PERSPECTIVE,
            "issued_by": ISSUED_BY,
        }, items


def _family_wiki() -> FamilyWiki:
    """The identity-map tests' instance, with the family history and the
    family tree registered, and `father` and `perspective`."""
    wiki = _wiki()
    wiki.__class__ = FamilyWiki
    return wiki


def _history() -> ClaimBatch:
    """The family history's batch: the son, born 1870, whose father is the
    master hatter, born 1836, whom the son's side calls "Muckel"."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                _born("son", 1870),
                {
                    "subject": _ref("son"),
                    "property": "father",
                    "value": {"type": "item", "value": _ref("father")},
                    "source": SOURCE,
                },
                _born("father", 1836),
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
                    "source": SOURCE,
                },
            ],
            "entities": [
                {
                    "id": "father",
                    "label": EUGEN,
                    "kind": "human",
                    "summary": "master hatter of Flörburg, 1836-1909",
                    "mention_lines": [3],
                },
                {
                    "id": "son",
                    "label": EUGEN,
                    "kind": "human",
                    "summary": "son of the master hatter, born 1870",
                    "mention_lines": [7],
                },
            ],
        }
    )


FATHER_CLAIM, MUCKEL_CLAIM = 1, 3


def _ids(batch: ClaimBatch, *indexes: int) -> list[str]:
    return [claim_id(batch.claims[index]) for index in indexes]


def _statements(wiki, qid: str, pid: str) -> list[dict]:
    return wiki.wbi.item.get(qid).get_json()["claims"].get(pid, [])


def _value(statement: dict) -> str:
    return statement["mainsnak"]["datavalue"]["value"]["id"]


def _self_loops(wiki) -> list[tuple[str, str]]:
    """Every (item, property) of the instance with a statement or a
    qualifier whose item value is the item itself."""
    loops = []
    for qid in list(wiki.wbi.item.store):
        claims = wiki.wbi.item.get(qid).get_json()["claims"]
        for pid, statements in claims.items():
            for statement in statements:
                snaks = [statement["mainsnak"]] + [
                    snak
                    for found in statement.get("qualifiers", {}).values()
                    for snak in found
                ]
                for snak in snaks:
                    value = (snak.get("datavalue") or {}).get("value")
                    if isinstance(value, dict) and value.get("id") == qid:
                        loops.append((qid, pid))
    return loops


def _merged():
    """The family history's map, with clustering's merge of the two
    Eugens into one cluster: one item, the father his own son's father."""
    return joined(mapped(_history()), EUGEN)


class TestOneClusterOfTheMap:
    def test_the_loops_are_refused_and_the_rest_written(self):
        wiki = _family_wiki()
        batch = _history()

        written = BatchWriter(wiki).write_batch(batch, identity=_merged())

        loops = _ids(batch, FATHER_CLAIM, MUCKEL_CLAIM)
        assert written.self_references == loops
        assert written.refused == loops
        # both births are written, on the one item
        assert written.written == 2
        (eugen,) = set(written.created_items.values())
        births = _statements(wiki, eugen, PROPERTIES["date_of_birth"])
        assert len(births) == 2
        assert _statements(wiki, eugen, FATHER) == []
        assert _statements(wiki, eugen, PROPERTIES["referred_to_as"]) == []
        assert _self_loops(wiki) == []
        # a warning names each, by its claim id, and says which value
        father, muckel = loops
        assert any(
            f"claim {father} (father)" in w
            and "its value names its subject's item" in w
            for w in written.warnings
        )
        assert any(
            f"claim {muckel} (referred_to_as)" in w
            and "its perspective qualifier names" in w
            for w in written.warnings
        )

    def test_a_dry_run_reports_the_same(self):
        wiki = _family_wiki()
        batch = _history()
        before = dict(wiki.wbi.item.store)

        planned = BatchWriter(wiki, dry_run=True).write_batch(
            batch, identity=_merged()
        )

        assert planned.self_references == _ids(
            batch, FATHER_CLAIM, MUCKEL_CLAIM
        )
        assert planned.refused == planned.self_references
        assert planned.written == 2
        assert dict(wiki.wbi.item.store) == before

    def test_two_clusters_write_every_claim(self):
        # the map keeps father and son apart: no loop, nothing refused
        wiki = _family_wiki()
        written = BatchWriter(wiki).write_batch(
            _history(), identity=mapped(_history())
        )
        assert written.self_references == []
        assert written.refused == []
        assert written.written == 4
        son, father = (written.created_items[i] for i in ("son", "father"))
        (statement,) = _statements(wiki, son, FATHER)
        assert _value(statement) == father

    def test_resolve_leaves_the_refs_to_the_map(self):
        # resolve matches nothing and replaces no ref: the map it checks
        # the batch against is the map that puts the two on one item
        wiki = _family_wiki()
        identity = _merged()
        resolved = resolve_batch(_history(), identity)

        written = BatchWriter(wiki).write_batch(resolved, identity=identity)

        assert written.self_references == _ids(
            resolved, FATHER_CLAIM, MUCKEL_CLAIM
        )
        assert written.written == 2
        assert _self_loops(wiki) == []


class TestAnApprovedEquivalence:
    def test_both_entities_approved_to_one_item(self):
        wiki = _family_wiki()
        existing = wiki.wbi.item.new()
        existing.labels.set("en", EUGEN)
        eugen = existing.write().id
        approved = apply_equivalences(
            _history(), {"father": eugen, "son": eugen}
        )

        written = BatchWriter(wiki).write_batch(approved)

        assert written.self_references == _ids(
            approved, FATHER_CLAIM, MUCKEL_CLAIM
        )
        assert written.written == 2
        assert len(_statements(wiki, eugen, PROPERTIES["date_of_birth"])) == 2
        assert _statements(wiki, eugen, FATHER) == []
        assert _self_loops(wiki) == []
        assert any(f"({eugen})" in w for w in written.warnings)

    def test_one_approved_to_the_item_the_map_finds_for_the_other(self):
        # the first write made the father's and the son's items; a later
        # equivalence approves the son to the father's item: the son's
        # refs name it by QID, the father's reach it through his cluster,
        # which the plan finds it for
        wiki = _family_wiki()
        identity = mapped(_history())
        first = BatchWriter(wiki).write_batch(_history(), identity=identity)
        father = first.created_items["father"]
        approved = apply_equivalences(_history(), {"son": father})

        again = BatchWriter(wiki).write_batch(
            approved, identity=identity, mapped_from=_history()
        )

        assert again.self_references == _ids(
            approved, FATHER_CLAIM, MUCKEL_CLAIM
        )
        assert again.minted == []
        assert _self_loops(wiki) == []

    def test_records_given_one_items_qid_with_their_refs_local(self):
        # a batch built beforehand whose two records carry one item's QID,
        # their refs left local: each record names the item
        wiki = _family_wiki()
        existing = wiki.wbi.item.new()
        existing.labels.set("en", EUGEN)
        eugen = existing.write().id
        given = _history().model_dump(mode="json")
        for record in given["entities"]:
            record["qid"] = eugen
        batch = ClaimBatch.model_validate(given)

        written = BatchWriter(wiki).write_batch(batch)

        assert written.self_references == _ids(
            batch, FATHER_CLAIM, MUCKEL_CLAIM
        )
        assert written.written == 2
        assert _self_loops(wiki) == []


class TestACuratorsCorrection:
    def test_a_corrected_qid_on_the_equivalence_sheet(self, tmp_path):
        # the curator approves the son's row and corrects the father's
        # proposed item to the son's: a misreading the writer refuses
        wiki = _family_wiki()
        existing = wiki.wbi.item.new()
        existing.labels.set("en", EUGEN)
        eugen = existing.write().id
        sheet = tmp_path / "equivalences.csv"
        with open(sheet, "w", newline="") as handle:
            rows = csv.DictWriter(handle, fieldnames=EQUIVALENCE_FIELDS)
            rows.writeheader()
            for entity_id, qid in (("son", eugen), ("father", eugen)):
                rows.writerow(
                    {
                        "entity_id": entity_id,
                        "entity_label": EUGEN,
                        "qid": qid,
                        "verdict": "same",
                        "decision": "approve",
                    }
                )
        approved = apply_equivalences(
            _history(), read_approved_equivalences(sheet)
        )

        written = BatchWriter(wiki).write_batch(approved)

        assert written.self_references == _ids(
            approved, FATHER_CLAIM, MUCKEL_CLAIM
        )
        assert written.written == 2
        assert _self_loops(wiki) == []

    def test_a_curated_claim_naming_its_subject_by_qid(self):
        # a curated claim (a curator's own write) whose subject and value
        # name one item is refused too, and a curated one that does not
        # is written
        wiki = _family_wiki()
        existing = wiki.wbi.item.new()
        existing.labels.set("en", EUGEN)
        eugen = existing.write().id
        other = wiki.wbi.item.new()
        other.labels.set("en", "Sebastian Löbig")
        sebastian = other.write().id
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [
                    {
                        "subject": {"kind": "qid", "qid": sebastian},
                        "property": "father",
                        "value": {
                            "type": "item",
                            "value": {"kind": "qid", "qid": sebastian},
                        },
                        "source": SOURCE,
                        "curated": True,
                    },
                    {
                        "subject": {"kind": "qid", "qid": sebastian},
                        "property": "father",
                        "value": {
                            "type": "item",
                            "value": {"kind": "qid", "qid": eugen},
                        },
                        "source": SOURCE,
                        "curated": True,
                    },
                ],
            }
        )

        written = BatchWriter(wiki).write_batch(batch)

        assert written.self_references == _ids(batch, 0)
        assert written.written == 1
        (statement,) = _statements(wiki, sebastian, FATHER)
        assert _value(statement) == eugen


class TestTheCheck:
    def _refs(self, **refs):
        return ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [
                    {
                        "subject": refs["subject"],
                        "property": "location",
                        "value": {"type": "item", "value": refs["value"]},
                        "source": SOURCE,
                    }
                ],
            }
        )

    @pytest.mark.parametrize(
        ("subject", "value", "loop"),
        [
            ({"kind": "qid", "qid": "Q71"}, {"kind": "qid", "qid": "Q71"}, 1),
            ({"kind": "qid", "qid": "Q71"}, {"kind": "qid", "qid": "Q72"}, 0),
            (
                {"kind": "vocab", "key": "place"},
                {"kind": "qid", "qid": "Q5"},
                1,
            ),
        ],
    )
    def test_refs_naming_one_item(self, subject, value, loop):
        batch = self._refs(subject=subject, value=value)
        writer = BatchWriter(FamilyWiki())
        assert ITEMS["place"] == "Q5"
        found = self_references(batch, writer._item_of)
        assert len(found) == loop

    def test_a_document_issued_by_its_own_item(self):
        # the document subject is its own artifact's item, which a QID
        # value names
        wiki = _family_wiki()
        history = wiki.media[MEDIA_SHA1]
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [
                    {
                        "subject": {"kind": "artifact", "sha1": MEDIA_SHA1},
                        "property": "issued_by",
                        "value": {
                            "type": "item",
                            "value": {"kind": "qid", "qid": history},
                        },
                        "source": SOURCE,
                    }
                ],
            }
        )

        written = BatchWriter(wiki).write_batch(batch)

        assert written.self_references == _ids(batch, 0)
        assert written.written == 0
        assert _statements(wiki, history, ISSUED_BY) == []

    def test_a_claim_refused_already_is_not_read(self):
        batch = self._refs(
            subject={"kind": "qid", "qid": "Q71"},
            value={"kind": "qid", "qid": "Q71"},
        )
        writer = BatchWriter(FamilyWiki())
        assert self_references(batch, writer._item_of, skip={0}) == {}

    def test_a_ref_of_no_known_item_matches_nothing(self):
        batch = self._refs(
            subject={"kind": "qid", "qid": "Q71"},
            value={"kind": "qid", "qid": "Q71"},
        )
        assert self_references(batch, lambda ref: None) == {}

    def test_a_claim_refused_for_its_text_is_counted_there(self):
        # one reason a claim: a loop whose text the instance cannot hold
        # is refused for its text, and not counted again as a loop
        wiki = _family_wiki()
        given = _history().model_dump(mode="json")
        given["claims"][MUCKEL_CLAIM]["value"]["value"]["text"] = "Muckel\n"
        batch = ClaimBatch.model_validate(given)

        written = BatchWriter(wiki).write_batch(batch, identity=_merged())

        assert written.self_references == _ids(batch, FATHER_CLAIM)
        assert written.refused == _ids(batch, FATHER_CLAIM, MUCKEL_CLAIM)
