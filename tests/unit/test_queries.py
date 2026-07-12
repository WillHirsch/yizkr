"""Offline tests for the query library (pytest; no instance needed)."""

import pytest
import yaml

from yizkr.model.schema import read_manifest_data
from yizkr.queries.publish import (
    description_of,
    index_wikitext,
    load_templates,
    page_wikitext,
    placeholder_keys,
    render,
    render_for_instance,
)

EXPECTED_QUERIES = {
    "disagreement-report",
    "person-timeline",
    "places-map",
    "unreferenced-statements",
    "artifact-assertions",
}


@pytest.fixture(scope="module")
def templates() -> dict[str, str]:
    return load_templates()


@pytest.fixture(scope="module")
def vocabulary() -> tuple[dict, dict]:
    raw = yaml.safe_load(read_manifest_data())
    properties = {key: f"P{n}" for n, key in enumerate(raw["properties"], 1)}
    items = {key: f"Q{n}" for n, key in enumerate(raw["items"], 1)}
    return properties, items


class TestLibrary:
    def test_roadmap_queries_present(self, templates):
        assert set(templates) >= EXPECTED_QUERIES

    def test_every_template_has_description(self, templates):
        for name, template in templates.items():
            assert description_of(template), f"{name} has no leading comment"

    def test_placeholders_reference_real_manifest_keys(self, templates):
        """Templates and model/manifest.yaml must not drift apart."""
        raw = yaml.safe_load(read_manifest_data())
        for name, template in templates.items():
            properties, items = placeholder_keys(template)
            unknown_p = properties - set(raw["properties"])
            unknown_q = items - set(raw["items"])
            assert not unknown_p, f"{name}: unknown property keys {unknown_p}"
            assert not unknown_q, f"{name}: unknown item keys {unknown_q}"

    def test_disagreement_report_uses_provenance(self, templates):
        assert "prov:wasDerivedFrom" in templates["disagreement-report"]
        assert "{P:stated_in}" in templates["disagreement-report"]


class TestRendering:
    def test_render_leaves_no_placeholders(self, templates, vocabulary):
        properties, items = vocabulary
        for name, template in templates.items():
            rendered = render(template, properties, items)
            assert "{P:" not in rendered, name
            assert "{Q:" not in rendered, name

    def test_render_substitutes_ids(self, vocabulary):
        properties, items = vocabulary
        rendered = render("?x wdt:{P:instance_of} wd:{Q:human} .", properties, items)
        assert rendered == (f"?x wdt:{properties['instance_of']} wd:{items['human']} .")

    def test_unknown_key_fails_loudly(self, vocabulary):
        properties, items = vocabulary
        with pytest.raises(KeyError, match="shoe_size"):
            render("wdt:{P:shoe_size}", properties, items)

    def test_instance_render_declares_local_prefixes(self, templates, vocabulary):
        """WDQS ships wikidata.org prefixes; queries must redeclare them
        for the local concept URI or silently match nothing."""
        properties, items = vocabulary
        rendered = render_for_instance(
            templates["person-timeline"],
            properties,
            items,
            "http://wikibase.example",
        )
        for prefix in ("wd:", "wdt:", "p:", "ps:", "pq:", "pr:"):
            assert f"PREFIX {prefix} <http://wikibase.example/" in rendered
        assert "PREFIX prov: <http://www.w3.org/ns/prov#>" in rendered


class TestWikitext:
    def test_page_has_run_link_and_highlight(self, templates, vocabulary):
        properties, items = vocabulary
        name = "unreferenced-statements"
        rendered = render(templates[name], properties, items)
        text = page_wikitext(name, templates[name], rendered, "http://query.example")
        assert "http://query.example/#" in text
        assert "embed.html#" in text
        assert '<syntaxhighlight lang="sparql">' in text
        assert "do not edit by hand" in text

    def test_index_lists_every_query(self, templates):
        text = index_wikitext(templates)
        for name in EXPECTED_QUERIES:
            assert f"Project:Queries/{name}|{name}" in text
