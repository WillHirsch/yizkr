"""Offline tests for the static publish tier (frontend ladder rung 2).

The SPARQL runner and vocabulary are injected, so rendering is tested
without an instance; fixtures are fictional.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

import pytest
import yaml

from tests.fixtures import example_universe as u
from tests.unit.test_disagreement import (
    QIDS,
    at,
    containment_rows,
    iri,
    rows_of,
    said,
    text,
)
from yizkr.core import sparql
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


class TestTheQueryClient:
    """The site runs its queries through the shared client
    (yizkr.core.sparql), which sends each one in a POST body."""

    def test_each_query_travels_in_the_request_body(
        self, monkeypatch, tmp_path, vocabulary
    ):
        # the query service refuses a request line longer than about 8 KB
        sent = []

        def urlopen(request, timeout):
            sent.append(request)
            return io.BytesIO(b'{"results": {"bindings": []}}')

        monkeypatch.setattr(sparql.urllib.request, "urlopen", urlopen)
        builder = SiteBuilder(config(), tmp_path, vocabulary=vocabulary)
        query = "SELECT ?x WHERE {}\n#" + "x" * 10_000
        assert builder.run(query) == []
        (request,) = sent
        assert request.get_method() == "POST"
        assert request.full_url == "http://query.example/sparql"
        assert urllib.parse.parse_qs(request.data.decode())["query"] == [query]

    def test_a_refused_query_reports_the_engines_answer(self, monkeypatch):
        def urlopen(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "Bad Request",
                {},
                io.BytesIO(b"MalformedQueryException: line 1"),
            )

        monkeypatch.setattr(sparql.urllib.request, "urlopen", urlopen)
        with pytest.raises(sparql.SparqlError, match="MalformedQuery"):
            sparql.runner(config())("SELECT")

    def test_a_missing_endpoint_is_named(self):
        with pytest.raises(sparql.SparqlError, match="WDQS_PUBLIC_HOST"):
            sparql.runner(
                Config(
                    wikibase_url="http://wikibase.example",
                    concept_uri="http://wikibase.example",
                    wdqs_url=None,
                    sparql_url=None,
                    admin_name="x",
                    admin_pass="x",
                    env_file=None,
                )
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

# What the disagreement report's two queries return (#266): the universe's
# contradiction (Gustav's birth day and month transposed between the civil
# register extract and the naturalisation), and variations with the
# universe's dates: one residence two sources date differently, a second
# birth name, an event date one source gives as a range ("between 1938
# and 1939") that another lies outside, and a date two sources store alike,
# set apart only by their stated ranges ("after 1945" and "before 1945").
# Gustav's two birthplaces are a difference of resolution: the recorded
# containment sets them aside.
DISAGREEING = [
    said(
        "gustav-loebig",
        "date_of_birth",
        u.Date(1919, 1, 7),
        "birth-extract-1938",
    ),
    said(
        "gustav-loebig",
        "date_of_birth",
        u.Date(1919, 7, 1),
        "naturalisation-1952",
    ),
    said(
        "gustav-loebig",
        "place_of_birth",
        "brunnenring-35",
        "birth-extract-1938",
    ),
    said(
        "gustav-loebig", "place_of_birth", "floerburg", "naturalisation-1952"
    ),
    # a residence statement's own range may date either end of it, so the
    # start-time row does not show it
    said(
        "gustav-loebig",
        "residence",
        "australia",
        "family-tree-1984",
        start=at(u.Date(1938)),
        latest=at(u.Date(1952)),
    ),
    said(
        "gustav-loebig",
        "residence",
        "australia",
        "naturalisation-1952",
        start=at(u.Date(1939)),
    ),
    said(
        "caecilie-loebig",
        "birth_name",
        text("Cäcilie Tarle", "de"),
        "burial-register-1909",
    ),
    said(
        "caecilie-loebig",
        "birth_name",
        text("Cäcilie Löbig", "en"),
        "family-tree-1984",
    ),
    said(
        "emigration-1938",
        "point_in_time",
        u.Date(1938, qualifier="between", until=u.Date(1939)),
        "interview-2002",
    ),
    said(
        "emigration-1938",
        "point_in_time",
        u.Date(1942, 8, 21),
        "family-tree-1984",
    ),
    said(
        "rahel-loebig",
        "date_of_death",
        u.Date(1945, qualifier="after"),
        "family-tree-1984",
    ),
    said(
        "rahel-loebig",
        "date_of_death",
        u.Date(1945, qualifier="before"),
        "interview-2002",
    ),
]
FETCHED = [row for statement in DISAGREEING for row in rows_of(statement)]
CONTAINED = containment_rows(
    edges=[
        (iri("brunnenring-35"), iri("brunnenring")),
        (iri("brunnenring"), iri("floerburg")),
    ]
)


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
    # assertions query (not the disagreement report's ?statement)
    "?st prov:wasDerivedFrom/pr:": ASSERTIONS,
    # the disagreement report's statements and the recorded containment
    "wikibase:rank ?rank": FETCHED,
    "?item ?link ?target": CONTAINED,
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


class TestInconsistencies:
    """The disagreement report as a page: the site fetches the attributed
    values and the recorded containment, compares them
    (yizkr.disagreement), and shows both values of every row, each with its
    sources and at its stated precision (ADR-0002, rule 6)."""

    def page(self, tmp_path, vocabulary) -> str:
        site, summary = build(tmp_path, vocabulary, RESPONSES)
        # Gustav's birth, the residence, the birth name, the emigration and
        # Rahel's death; the two birthplaces differ only in resolution
        assert summary["inconsistencies"] == 5
        return (site / "inconsistencies.html").read_text()

    def test_both_values_render_with_their_sources(self, tmp_path, vocabulary):
        text = self.page(tmp_path, vocabulary)
        assert "1919-01-07" in text and "1919-07-01" in text
        assert (
            f'<a href="artifacts/{QIDS["birth-extract-1938"]}.html">'
            "Birth register extract for Gustav Löbig</a>"
        ) in text
        assert (
            f'<a href="artifacts/{QIDS["naturalisation-1952"]}.html">'
            "Certificate of naturalisation for Gustave Lobig</a>"
        ) in text

    def test_recorded_containment_leaves_the_birthplaces_out(
        self, tmp_path, vocabulary
    ):
        text = self.page(tmp_path, vocabulary)
        assert "<td>place of birth</td>" not in text

    def test_dated_residence_names_the_place_and_the_date_compared(
        self, tmp_path, vocabulary
    ):
        text = self.page(tmp_path, vocabulary)
        assert "<td>residence (Australia), start time</td>" in text
        # year precision, never padded to 1 January
        assert "<td>1938</td>" in text and "<td>1939</td>" in text
        assert "1938-01-01" not in text

    def test_a_stated_range_is_shown_beside_its_value(
        self, tmp_path, vocabulary
    ):
        # rule 5: the range a source gives is never silently dropped, and
        # it is why the pair is listed (21 August 1942 lies outside
        # 1938-1939)
        text = self.page(tmp_path, vocabulary)
        assert (
            '<td>1938 <span class="qualifiers">(earliest date: 1938; '
            "latest date: 1939)</span></td>"
        ) in text
        assert "<td>1942-08-21</td>" in text
        # a residence or spouse row compares a start or end time, which
        # the statement's range need not bound: it is not shown there
        assert "latest date: 1952" not in text

    def test_dates_stored_alike_show_the_ranges_that_set_them_apart(
        self, tmp_path, vocabulary
    ):
        # "before 1945" and "after 1945" are both stored as 1945: without
        # their ranges the row would show the same value twice
        text = self.page(tmp_path, vocabulary)
        assert (
            '<td>1945 <span class="qualifiers">(latest date: 1945)</span></td>'
        ) in text
        assert (
            '<td>1945 <span class="qualifiers">(earliest date: 1945)</span>'
            "</td>"
        ) in text

    def test_single_valued_rows_show_the_property_alone(
        self, tmp_path, vocabulary
    ):
        text = self.page(tmp_path, vocabulary)
        assert "<td>date of birth</td>" in text
        assert "<td>birth name</td>" in text
        assert "<td>Cäcilie Tarle</td>" in text
        assert "<td>Cäcilie Löbig</td>" in text


class TestTimeFormatting:
    def test_precisions(self):
        assert format_time("+1909-03-16T00:00:00Z", "11") == "1909-03-16"
        assert format_time("+1909-03-16T00:00:00Z", "10") == "1909-03"
        assert format_time("+1909-00-00T00:00:00Z", "9") == "1909"
        assert format_time("+1920-00-00T00:00:00Z", "8") == "1920s"
        assert format_time("+1800-00-00T00:00:00Z", "7") == "19th century"
