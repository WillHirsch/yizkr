"""Offline tests for the content-identity dedup key (ADR-0010).

The registrar's full hash-keyed flow touches WikibaseIntegrator item
objects and is verified live (tests/integration/test_live_pipeline.py);
here we cover the lag-free lookup primitive it relies on.
"""

from __future__ import annotations

from yizkr.core.wiki import WikiClient


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
                "Q5": {"claims": {"P42": [{"mainsnak": {"datavalue": {"value": 3}}}]}},
                "Q6": {
                    "claims": {"P99": [{"mainsnak": {"datavalue": {"value": "x"}}}]}
                },
            }
        )
        assert client.string_statement_index("P42") == {}

    def test_empty_instance(self):
        assert FakeIndexClient({}).string_statement_index("P42") == {}
