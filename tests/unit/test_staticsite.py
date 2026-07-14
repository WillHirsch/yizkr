"""Offline tests for the static publish tier (frontend ladder rung 2).

The SPARQL runner and vocabulary are injected, so rendering is tested
without an instance; fixtures are fictional.
"""

from __future__ import annotations

import json

import pytest
import yaml

from yizkr.core.config import Config
from yizkr.model.schema import read_manifest_data
from yizkr.staticsite import SiteBuilder, format_time


@pytest.fixture(scope="module")
def vocabulary():
    raw = yaml.safe_load(read_manifest_data())
    properties = {key: f"P{n}" for n, key in enumerate(raw["properties"], 1)}
    items = {key: f"Q{n}" for n, key in enumerate(raw["items"], 1)}
    return properties, items


def config() -> Config:
    return Config(
        wikibase_url="http://wikibase.example",
        concept_uri="http://wikibase.example",
        wdqs_url="http://query.example",
        sparql_url="http://query.example/sparql",
        admin_name="x",
        admin_pass="x",
        env_file=None,
    )


def uri(qid: str) -> dict:
    return {"type": "uri", "value": f"http://wikibase.example/entity/{qid}"}


def literal(text: str) -> dict:
    return {"type": "literal", "value": text}


def time_literal(value: str) -> dict:
    return {
        "type": "literal",
        "datatype": "http://www.w3.org/2001/XMLSchema#dateTime",
        "value": value,
    }


PEOPLE = [
    {
        "person": uri("Q7"),
        "personLabel": literal('Rosa "Ro" & Co <Genserowski>'),
        "personDescription": literal("matriarch"),
        "aliases": literal("Rosa|our great grandmother"),
    },
    {
        "person": uri("Q8"),
        "personLabel": literal("Living Person"),
        "living": literal("yes"),
        "aliases": literal(""),
    },
]

STATEMENTS = [
    {
        "person": uri("Q7"),
        "propLabel": literal("date of birth"),
        "st": uri("s1"),
        "value": time_literal("+1873-05-10T00:00:00Z"),
        "precision": literal("11"),
        "sources": literal("family tree PDF"),
        "qualifierTexts": literal(""),
    },
    {
        "person": uri("Q7"),
        "propLabel": literal("date of birth"),
        "st": uri("s2"),
        "value": time_literal("+1874-01-01T00:00:00Z"),
        "precision": literal("9"),
        "sources": literal("travel notes"),
        "qualifierTexts": literal("sourcing circumstances: circa"),
    },
    {
        "person": uri("Q7"),
        "propLabel": literal("spouse"),
        "st": uri("s3"),
        "value": uri("Q9"),
        "valueLabel": literal("Salomon Rothholz"),
        "sources": literal("family tree PDF|marriage record"),
        "qualifierTexts": literal("start time: 1901"),
    },
    {
        "person": uri("Q7"),
        "propLabel": literal("instance of"),
        "st": uri("s4"),
        "value": uri("Q1"),
        "valueLabel": literal("human"),
        "sources": literal("family tree PDF"),
        "qualifierTexts": literal(""),
    },
]

TIMELINE = [
    {
        "person": uri("Q7"),
        "personLabel": literal("Rosa Genserowski"),
        "what": literal("date of birth"),
        "date": time_literal("+1873-05-10T00:00:00Z"),
        "sourceLabel": literal("family tree PDF"),
    }
]

GALLERY = [
    {
        "photo": uri("Q30"),
        "photoLabel": literal("Deck — image1.png"),
        "image": {
            "type": "uri",
            "value": "http://wikibase.example/wiki/Special:FilePath/x.png",
        },
        "containerLabel": literal("Deck"),
    }
]


def build(tmp_path, vocabulary, responses):
    calls = []

    def run(query: str):
        calls.append(query)
        for marker, rows in responses.items():
            if marker in query:
                return rows
        return []

    builder = SiteBuilder(config(), tmp_path / "site", run=run, vocabulary=vocabulary)
    summary = builder.build()
    return tmp_path / "site", summary


RESPONSES = {
    "GROUP_CONCAT(DISTINCT ?alias": PEOPLE,
    "wikibase:statementProperty": STATEMENTS,
    "date of birth": TIMELINE,  # person-timeline template contains this literal
    "Special:FilePath": GALLERY,
}


class TestPersonPage:
    def test_coexisting_values_render_side_by_side_with_sources(
        self, tmp_path, vocabulary
    ):
        site, _ = build(tmp_path, vocabulary, RESPONSES)
        text = (site / "people/Q7.html").read_text()
        # both attributed birth values, each labeled — nothing picked
        assert "1873-05-10" in text and "family tree PDF" in text
        assert ">1874<" in text or "1874" in text  # year precision, not padded
        assert "1874-01-01" not in text  # precision respected
        assert "travel notes" in text
        # multi-source statement lists both sources
        assert "family tree PDF; marriage record" in text
        # qualifiers rendered
        assert "start time: 1901" in text
        # item values link to the person page of the target
        assert 'href="Q9.html"' in text

    def test_labels_are_escaped(self, tmp_path, vocabulary):
        site, _ = build(tmp_path, vocabulary, RESPONSES)
        text = (site / "people/Q7.html").read_text()
        assert "&lt;Genserowski&gt;" in text
        assert "<Genserowski>" not in text

    def test_living_badge(self, tmp_path, vocabulary):
        site, _ = build(tmp_path, vocabulary, RESPONSES)
        assert (
            "living — never in public exports" in (site / "people/Q8.html").read_text()
        )
        index = (site / "index.html").read_text()
        assert index.count('class="badge"') == 1  # only the living person


class TestSections:
    def test_index_links_every_person(self, tmp_path, vocabulary):
        site, summary = build(tmp_path, vocabulary, RESPONSES)
        index = (site / "index.html").read_text()
        assert 'href="people/Q7.html"' in index
        assert 'href="people/Q8.html"' in index
        assert summary["people"] == 2

    def test_timeline_page_and_feed(self, tmp_path, vocabulary):
        site, _ = build(tmp_path, vocabulary, RESPONSES)
        assert "Rosa Genserowski" in (site / "timeline.html").read_text()
        feed = json.loads((site / "timeline.json").read_text())
        assert feed["events"][0]["start_date"]["year"] == "1873"
        assert "per family tree PDF" in feed["events"][0]["text"]["text"]

    def test_gallery_renders_images_with_provenance(self, tmp_path, vocabulary):
        site, _ = build(tmp_path, vocabulary, RESPONSES)
        text = (site / "gallery.html").read_text()
        assert '<img src="http://wikibase.example/wiki/Special:FilePath/x.png"' in text
        assert "Deck" in text

    def test_empty_graph_still_builds(self, tmp_path, vocabulary):
        site, summary = build(tmp_path, vocabulary, {})
        assert (site / "index.html").exists()
        assert "no place carries" in (site / "places.html").read_text()
        assert summary["people"] == 0


class TestTimeFormatting:
    def test_precisions(self):
        assert format_time("+1873-05-10T00:00:00Z", "11") == "1873-05-10"
        assert format_time("+1873-05-10T00:00:00Z", "10") == "1873-05"
        assert format_time("+1873-00-00T00:00:00Z", "9") == "1873"
        assert format_time("+1920-00-00T00:00:00Z", "8") == "1920s"
        assert format_time("+1800-00-00T00:00:00Z", "7") == "19th century"
