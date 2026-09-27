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


# ?deceased / ?birth are the runtime possibly-living signals (ADR-0020):
# Q7 has death evidence (?deceased true) → not living; Q8 has neither
# death evidence nor a birth → possibly living, so it earns the badge.
PEOPLE = [
    {
        "person": uri("Q7"),
        "personLabel": literal('Eugen "Muckel" & Co <Löbig>'),
        "personDescription": literal("master hatter"),
        "deceased": literal("true"),
        "birth": time_literal("+1836-01-01T00:00:00Z"),
        "aliases": literal("Eugen Löbig|Muckel"),
    },
    {
        "person": uri("Q8"),
        "personLabel": literal("Living Person"),
        "deceased": literal("false"),
        "aliases": literal(""),
    },
]

STATEMENTS = [
    {
        "person": uri("Q7"),
        "propLabel": literal("date of death"),
        "st": uri("s1"),
        "value": time_literal("+1909-03-16T00:00:00Z"),
        "precision": literal("11"),
        "sources": literal("Q23~burial register"),
        "qualifierTexts": literal(""),
    },
    {
        "person": uri("Q7"),
        "propLabel": literal("date of death"),
        "st": uri("s2"),
        "value": time_literal("+1909-01-01T00:00:00Z"),
        "precision": literal("9"),
        "sources": literal("Q29~gravestone photograph"),
        "qualifierTexts": literal(""),
    },
    {
        "person": uri("Q7"),
        "propLabel": literal("spouse"),
        "st": uri("s3"),
        "value": uri("Q9"),
        "valueLabel": literal("Cäcilie Löbig"),
        "sources": literal("Q23~burial register|Q31~family tree"),
        "qualifierTexts": literal("start time: 1859"),
    },
    {
        "person": uri("Q7"),
        "propLabel": literal("instance of"),
        "st": uri("s4"),
        "value": uri("Q1"),
        "valueLabel": literal("human"),
        "sources": literal("Q23~burial register"),
        "qualifierTexts": literal(""),
    },
]

TIMELINE = [
    {
        "person": uri("Q7"),
        "personLabel": literal("Eugen Löbig"),
        "what": literal("date of death"),
        "date": time_literal("+1909-03-16T00:00:00Z"),
        "source": uri("Q23"),
        "sourceLabel": literal("burial register"),
    }
]

ARTIFACTS = [
    {
        "artifact": uri("Q23"),
        "artifactLabel": literal("burial register"),
        "artifactDescription": literal("Friedhof Flörburg, 1909"),
        "kindLabel": literal("artifact"),
        "file": literal("File:Burial-register-1909.pdf"),
    }
]

ASSERTIONS = [
    {
        "artifact": uri("Q23"),
        "subject": uri("Q7"),
        "subjectLabel": literal("Eugen Löbig"),
        "propLabel": literal("date of death"),
        "value": time_literal("+1909-03-16T00:00:00Z"),
        "precision": literal("11"),
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

    builder = SiteBuilder(
        config(), tmp_path / "site", run=run, vocabulary=vocabulary
    )
    summary = builder.build()
    return tmp_path / "site", summary


RESPONSES = {
    "GROUP_CONCAT(DISTINCT ?alias": PEOPLE,
    "GROUP_CONCAT(DISTINCT ?qualifierText": STATEMENTS,
    'date of birth"@en': TIMELINE,  # person-timeline UNION arm marker
    "Special:FilePath": GALLERY,  # photo-gallery builds this URL
    "?kind wdt:": ARTIFACTS,  # artifacts query walks the subclass path
    # assertions query (not ?stA/?stB)
    "?st prov:wasDerivedFrom/pr:": ASSERTIONS,
}


class TestPersonPage:
    def test_coexisting_values_render_side_by_side_with_sources(
        self, tmp_path, vocabulary
    ):
        site, _ = build(tmp_path, vocabulary, RESPONSES)
        text = (site / "people/Q7.html").read_text()
        # both attributed death values, each labeled — nothing picked
        assert "1909-03-16" in text and "burial register" in text
        assert ">1909<" in text  # year precision, not padded
        assert "1909-01-01" not in text  # precision respected
        assert "gravestone photograph" in text
        # multi-source statement links BOTH sources to artifact pages
        assert '<a href="../artifacts/Q23.html">burial register</a>' in text
        assert '<a href="../artifacts/Q31.html">family tree</a>' in text
        # qualifiers rendered
        assert "start time: 1859" in text
        # item values link to the person page of the target
        assert 'href="Q9.html"' in text

    def test_labels_are_escaped(self, tmp_path, vocabulary):
        site, _ = build(tmp_path, vocabulary, RESPONSES)
        text = (site / "people/Q7.html").read_text()
        assert "&lt;Löbig&gt;" in text
        assert "<Löbig>" not in text

    def test_possibly_living_badge(self, tmp_path, vocabulary):
        # runtime determination (ADR-0020): Q8 has no death evidence, so
        # the private site marks it; Q7 (deceased) is not marked.
        site, _ = build(tmp_path, vocabulary, RESPONSES)
        assert (
            "possibly living — never in public exports"
            in (site / "people/Q8.html").read_text()
        )
        assert "possibly living" not in (site / "people/Q7.html").read_text()
        index = (site / "index.html").read_text()
        assert (
            index.count('class="badge"') == 1
        )  # only the possibly-living one


class TestSections:
    def test_index_links_every_person(self, tmp_path, vocabulary):
        site, summary = build(tmp_path, vocabulary, RESPONSES)
        index = (site / "index.html").read_text()
        assert 'href="people/Q7.html"' in index
        assert 'href="people/Q8.html"' in index
        assert summary["people"] == 2

    def test_timeline_page_and_feed(self, tmp_path, vocabulary):
        site, _ = build(tmp_path, vocabulary, RESPONSES)
        assert "Eugen Löbig" in (site / "timeline.html").read_text()
        feed = json.loads((site / "timeline.json").read_text())
        assert feed["events"][0]["start_date"]["year"] == "1909"
        assert "per burial register" in feed["events"][0]["text"]["text"]

    def test_artifact_page_has_download_and_assertions(
        self, tmp_path, vocabulary
    ):
        site, summary = build(tmp_path, vocabulary, RESPONSES)
        assert summary["artifacts"] == 1
        text = (site / "artifacts/Q23.html").read_text()
        # raw-content download link
        assert "Special:FilePath/Burial-register-1909.pdf" in text
        assert "download the original" in text
        # assertions table with precision-correct value and person link
        assert '<a href="../people/Q7.html">Eugen Löbig</a>' in text
        assert "1909-03-16" in text
        # index links the artifact page
        index = (site / "index.html").read_text()
        assert 'href="artifacts/Q23.html"' in index

    def test_inconsistencies_page_renamed(self, tmp_path, vocabulary):
        site, _ = build(tmp_path, vocabulary, RESPONSES)
        assert (site / "inconsistencies.html").exists()
        assert not (site / "disagreements.html").exists()
        assert "Inconsistencies" in (site / "index.html").read_text()

    def test_gallery_renders_images_with_provenance(
        self, tmp_path, vocabulary
    ):
        site, _ = build(tmp_path, vocabulary, RESPONSES)
        text = (site / "gallery.html").read_text()
        assert (
            '<img src="http://wikibase.example/wiki/Special:FilePath/x.png"'
            in text
        )
        assert "Deck" in text

    def test_empty_graph_still_builds(self, tmp_path, vocabulary):
        site, summary = build(tmp_path, vocabulary, {})
        assert (site / "index.html").exists()
        assert "no place carries" in (site / "places.html").read_text()
        assert summary["people"] == 0


class TestTimeFormatting:
    def test_precisions(self):
        assert format_time("+1909-03-16T00:00:00Z", "11") == "1909-03-16"
        assert format_time("+1909-03-16T00:00:00Z", "10") == "1909-03"
        assert format_time("+1909-00-00T00:00:00Z", "9") == "1909"
        assert format_time("+1920-00-00T00:00:00Z", "8") == "1920s"
        assert format_time("+1800-00-00T00:00:00Z", "7") == "19th century"
