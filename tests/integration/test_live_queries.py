"""Every packaged query must execute against live WDQS, and the rule-1
invariant must hold: zero unreferenced statements, ever."""

import pytest

from yizkr.queries.publish import load_templates, render_for_instance

from .conftest import run_sparql

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def rendered(client, config):
    properties, items = client.vocabulary()
    return {
        name: render_for_instance(template, properties, items, config.concept_uri)
        for name, template in load_templates().items()
    }


def test_all_queries_execute(config, rendered):
    for name, query in rendered.items():
        rows = run_sparql(config, query)  # raises on non-200
        assert isinstance(rows, list), name


def test_no_unreferenced_statements_anywhere(config, rendered):
    """The whole-project rule-1 invariant (ADR-0003 §3)."""
    rows = run_sparql(config, rendered["unreferenced-statements"])
    offenders = [row.get("entityLabel", {}).get("value") for row in rows]
    assert rows == [], f"unreferenced statements found on: {offenders}"
