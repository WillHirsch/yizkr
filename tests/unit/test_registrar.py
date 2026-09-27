"""Offline tests for the content-identity dedup key (ADR-0010).

The registrar's full hash-keyed flow touches WikibaseIntegrator item
objects and is verified live (tests/integration/test_live_pipeline.py);
here we cover the lag-free lookup primitive it relies on, and the
additive statement helper derived-media registration uses (#50).
"""

from __future__ import annotations

from yizkr.core.wiki import WikiClient
from yizkr.pipeline.registrar import (
    _cites,
    _ensure_referenced,
    _item_claim,
    _string_claim,
)


class FakeIndexClient(WikiClient):
    """A WikiClient whose entity enumeration is scripted, so
    string_statement_index can be exercised without an instance."""

    def __init__(self, entities: dict[str, dict]) -> None:
        self._entities = entities

    def list_entity_ids(self):
        return list(self._entities)

    def api_get(self, params):
        ids = params["ids"].split("|")
        return {"entities": {qid: self._entities[qid] for qid in ids}}


def _with_hash(pid: str, value: str) -> dict:
    return {"claims": {pid: [{"mainsnak": {"datavalue": {"value": value}}}]}}


class TestStringStatementIndex:
    def test_maps_value_to_entity(self):
        client = FakeIndexClient(
            {
                "Q5": _with_hash("P42", "aaa"),
                "Q6": _with_hash("P42", "bbb"),
                "Q7": {"claims": {}},  # no hash: ignored
            }
        )
        index = client.string_statement_index("P42")
        assert index == {"aaa": "Q5", "bbb": "Q6"}

    def test_duplicate_value_resolves_to_numerically_earliest(self):
        # the very leak the hash key prevents may exist in legacy data;
        # it must resolve deterministically, not raise (unlike labels)
        client = FakeIndexClient(
            {
                "Q30": _with_hash("P42", "dup"),
                "Q4": _with_hash("P42", "dup"),
                "Q100": _with_hash("P42", "dup"),
            }
        )
        assert client.string_statement_index("P42") == {"dup": "Q4"}

    def test_non_string_and_missing_property_ignored(self):
        client = FakeIndexClient(
            {
                "Q5": {
                    "claims": {
                        "P42": [{"mainsnak": {"datavalue": {"value": 3}}}]
                    }
                },
                "Q6": {
                    "claims": {
                        "P99": [{"mainsnak": {"datavalue": {"value": "x"}}}]
                    }
                },
            }
        )
        assert client.string_statement_index("P42") == {}

    def test_empty_instance(self):
        assert FakeIndexClient({}).string_statement_index("P42") == {}


# -- additive statements (ADR-0002, ADR-0008; #50) ---------------------------

PART_OF, STATED_IN, PAGE, MEDIA_FILE = "P3", "P2", "P6", "P57"


def _entity():
    from wikibaseintegrator import WikibaseIntegrator

    return WikibaseIntegrator().item.new()


def _part_of(deck: str, slide: int):
    from wikibaseintegrator.datatypes import String

    claim = _item_claim(PART_OF, deck, STATED_IN, deck)
    claim.qualifiers.add(String(prop_nr=PAGE, value=str(slide)))
    return claim


def _media_file(value: str, source: str):
    return _string_claim(MEDIA_FILE, value, STATED_IN, source)


def _ensure(entity, claim, source: str) -> str:
    return _ensure_referenced(entity, claim, STATED_IN, source)


def _statements(entity, pid: str) -> list[dict]:
    return [claim.get_json() for claim in entity.claims.get(pid)]


class TestEnsureReferenced:
    def test_new_deck_appends_its_own_part_of_statement(self):
        entity = _entity()
        assert _ensure(entity, _part_of("Q10", 1), "Q10") == "added"
        before = _statements(entity, PART_OF)

        assert _ensure(entity, _part_of("Q20", 3), "Q20") == "added"

        first, second = entity.claims.get(PART_OF)
        # the first deck's statement is untouched: same value, qualifier
        # and references (never overwritten or re-sourced)
        assert first.get_json() == before[0]
        assert _cites(first, STATED_IN, "Q10")
        assert not _cites(first, STATED_IN, "Q20")
        assert second.mainsnak.datavalue["value"]["id"] == "Q20"
        assert second.qualifiers.get(PAGE)[0].datavalue["value"] == "3"
        assert _cites(second, STATED_IN, "Q20")

    def test_same_deck_other_slide_is_a_distinct_statement(self):
        entity = _entity()
        _ensure(entity, _part_of("Q10", 1), "Q10")
        assert _ensure(entity, _part_of("Q10", 2), "Q10") == "added"
        assert len(entity.claims.get(PART_OF)) == 2

    def test_rerun_is_a_no_op(self):
        entity = _entity()
        _ensure(entity, _part_of("Q10", 1), "Q10")
        before = _statements(entity, PART_OF)
        assert _ensure(entity, _part_of("Q10", 1), "Q10") == "present"
        assert _statements(entity, PART_OF) == before

    def test_agreeing_source_gains_a_reference_not_a_duplicate(self):
        entity = _entity()
        _ensure(entity, _media_file("File:Img.png", "Q10"), "Q10")

        outcome = _ensure(entity, _media_file("File:Img.png", "Q20"), "Q20")

        assert outcome == "referenced"
        (statement,) = entity.claims.get(MEDIA_FILE)
        # both sources cite the one statement, the first one still first
        first_ref = statement.references.references[0]
        assert first_ref.snaks.get(STATED_IN)[0].datavalue["value"]["id"] == (
            "Q10"
        )
        assert _cites(statement, STATED_IN, "Q20")
        # re-asserting from either source changes nothing
        for source in ("Q10", "Q20"):
            claim = _media_file("File:Img.png", source)
            assert _ensure(entity, claim, source) == "present"
        assert len(statement.references.references) == 2

    def test_different_value_coexists(self):
        entity = _entity()
        _ensure(entity, _media_file("File:A.png", "Q10"), "Q10")
        assert _ensure(entity, _media_file("File:B.png", "Q10"), "Q10") == (
            "added"
        )
        assert {
            claim.mainsnak.datavalue["value"]
            for claim in entity.claims.get(MEDIA_FILE)
        } == {"File:A.png", "File:B.png"}
