"""Offline tests for the static publish tier (frontend ladder rung 2).

The SPARQL runner and vocabulary are injected, so rendering is tested
without an instance; fixtures are fictional.
"""

from __future__ import annotations

import html
import io
import json
import re
import urllib.error
import urllib.parse

import pytest
import yaml

from tests.fixtures import example_universe as u
from tests.unit.test_backup import FACTS
from tests.unit.test_disagreement import (
    INTERVIEW,
    POSTCARD,
    PROPERTIES,
    QIDS,
    REGISTER,
    TREE,
    at,
    containment_rows,
    iri,
    label,
    rows_of,
    said,
    stamp,
    stated,
    text,
)
from tests.unit.test_privacy import BIRTHS, CASES, EXCLUDED, birth_rows
from yizkr import disagreement
from yizkr.backup import exclusion_report
from yizkr.core import sparql
from yizkr.core.config import Config
from yizkr.core.privacy import BIRTHS_QUERY, family_type, people_selection
from yizkr.model.schema import read_manifest_data
from yizkr.queries.publish import load_templates, prefix_block
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

    @staticmethod
    def refused(monkeypatch, body: bytes, code: int = 400) -> None:
        def urlopen(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, code, "Refused", {}, io.BytesIO(body)
            )

        monkeypatch.setattr(sparql.urllib.request, "urlopen", urlopen)

    def test_a_refused_query_reports_the_engines_answer(self, monkeypatch):
        # the query service echoes the whole query before it explains, with
        # each character outside ASCII echoed as "?"; a query longer than
        # the message keeps is still explained
        query = (
            "# Disagreement report — the values sources attribute\n"
            + "".join(f"# comment line {n}\n" for n in range(200))
            + "SELECT ?x WHERE { ?x ?p ?o }\n}"
        )
        assert len(query.encode()) > sparql.DETAIL_LIMIT
        echoed = query.encode("ascii", errors="replace")
        self.refused(
            monkeypatch,
            b"SPARQL-QUERY: queryStr="
            + echoed
            + b"\njava.util.concurrent.ExecutionException: "
            b"org.openrdf.query.MalformedQueryException: Encountered "
            b'" "}" "} "" at line 203, column 1.\n'
            b"\tat java.util.concurrent.FutureTask.report(FutureTask.java:122)"
            b"\n",
        )
        with pytest.raises(sparql.SparqlError) as refused:
            sparql.runner(config())(query)
        message = str(refused.value)
        assert "HTTP 400" in message
        assert "MalformedQueryException: Encountered" in message
        assert "at line 203, column 1." in message
        assert "comment line" not in message

    def test_an_answer_that_names_no_exception_is_kept_from_its_start(
        self, monkeypatch
    ):
        # a proxy in front of the engine refuses with a page of its own
        self.refused(
            monkeypatch,
            b"<html><head><title>502 Bad Gateway</title></head></html>\n",
            code=502,
        )
        with pytest.raises(sparql.SparqlError, match="HTTP 502: <html>"):
            sparql.runner(config())("SELECT ?x WHERE {}")

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


# ?deceased and the births query are the runtime possibly-living signals
# (ADR-0020): Q7 has death evidence (?deceased true) → not living; Q8 has
# neither death evidence nor a birth → possibly living, so it earns the
# badge.
PEOPLE = [
    {
        "person": uri("Q7"),
        "personLabel": literal('Eugen "Muckel" & Co <Löbig>'),
        "personDescription": literal("master hatter"),
        "deceased": literal("true"),
        "aliases": literal("Eugen Löbig|Muckel"),
    },
    {
        "person": uri("Q8"),
        "personLabel": literal("Living Person"),
        "deceased": literal("false"),
        "aliases": literal(""),
    },
]


def property_id(key: str) -> str:
    """A manifest property's id as the ``vocabulary`` fixture numbers it."""
    raw = yaml.safe_load(read_manifest_data())
    return f"P{list(raw['properties']).index(key) + 1}"


STATEMENTS = [
    {
        "person": uri("Q7"),
        "prop": uri(property_id("date_of_death")),
        "propLabel": literal("date of death"),
        "st": uri("s1"),
        "value": time_literal("+1909-03-16T00:00:00Z"),
        "precision": literal("11"),
        "sources": literal("Q23~burial register"),
        "qualifierTexts": literal(""),
    },
    {
        "person": uri("Q7"),
        "prop": uri(property_id("date_of_death")),
        "propLabel": literal("date of death"),
        "st": uri("s2"),
        "value": time_literal("+1909-01-01T00:00:00Z"),
        "precision": literal("9"),
        "sources": literal("Q29~gravestone photograph"),
        "qualifierTexts": literal(""),
    },
    {
        "person": uri("Q7"),
        "prop": uri(property_id("spouse")),
        "propLabel": literal("spouse"),
        "st": uri("s3"),
        "value": uri("Q9"),
        "valueLabel": literal("Cäcilie Löbig"),
        "sources": literal("Q23~burial register|Q31~family tree"),
        "qualifierTexts": literal("start time: 1859"),
    },
    {
        "person": uri("Q7"),
        "prop": uri(property_id("instance_of")),
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
    # the tree's latest date bounds the residence's end, not its start, so
    # the start-time row does not show it
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


@pytest.fixture
def hostile_label(monkeypatch) -> str:
    """The earliest date's label, holding characters HTML must escape, as
    both pages read a bound's label (disagreement.property_labels); returns
    it escaped."""
    labels = disagreement.property_labels()
    monkeypatch.setattr(
        disagreement,
        "property_labels",
        lambda: {**labels, "earliest_date": "earliest <date> & co"},
    )
    return "earliest &lt;date&gt; &amp; co"


# Eugen's birth in 1836, as the births query gives it
EUGENS_BIRTH = birth_rows("Q7", [(("1836-01-01T00:00:00Z", 9), [], [])])

RESPONSES = {
    "GROUP_CONCAT(DISTINCT ?alias": PEOPLE,
    'BIND("value" AS ?bound)': EUGENS_BIRTH,  # the privacy module's births
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

    def test_instance_of_is_skipped_by_its_id_not_its_label(
        self, tmp_path, vocabulary
    ):
        """`instance of` is the subtitle, found by the property's id: a
        relabelled `instance of` is still skipped, and another property
        that carries its English label is shown."""
        statements = [
            {
                **STATEMENTS[3],
                "propLabel": literal("ist ein"),
            },
            {
                **STATEMENTS[2],
                "propLabel": literal("instance of"),
            },
        ]
        site, _ = build(
            tmp_path,
            vocabulary,
            {**RESPONSES, "GROUP_CONCAT(DISTINCT ?qualifierText": statements},
        )
        text = (site / "people/Q7.html").read_text()
        assert "ist ein" not in text
        assert ">human<" not in text
        assert "<dt>instance of</dt>" in text
        assert 'href="Q9.html"' in text

    def test_the_statements_query_selects_each_property_id(self):
        from yizkr.staticsite import STATEMENTS_QUERY

        assert "SELECT ?person ?prop ?propLabel" in STATEMENTS_QUERY
        assert "GROUP BY ?person ?prop ?propLabel" in STATEMENTS_QUERY

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
            "possibly living — never in the non-privileged export"
            in (site / "people/Q8.html").read_text()
        )
        assert "possibly living" not in (site / "people/Q7.html").read_text()
        index = (site / "index.html").read_text()
        assert (
            index.count('class="badge"') == 1
        )  # only the possibly-living one


# The people of the privacy module's shared cases, as PEOPLE_QUERY gives
# them: ?deceased is true for those with death evidence.
PRIVACY_PEOPLE = [
    {
        "person": uri(qid),
        "personLabel": literal(description),
        "deceased": literal("true" if has_death else "false"),
        "aliases": literal(""),
    }
    for qid, (description, _, has_death, _) in CASES.items()
]
PRIVACY_RESPONSES = {
    **RESPONSES,
    "GROUP_CONCAT(DISTINCT ?alias": PRIVACY_PEOPLE,
    'BIND("value" AS ?bound)': BIRTHS,
}


class TestTheBadgeReadsEveryBirth:
    """The badge marks whom the non-privileged export drops, with the
    export's own reading of births: every birth statement a curator has not
    deprecated, each at the latest year it allows (#334)."""

    def badged(self, site) -> set[str]:
        return {
            qid
            for qid in CASES
            if "possibly living — never in the non-privileged export"
            in (site / f"people/{qid}.html").read_text()
        }

    def test_the_badge_follows_every_birth(self, tmp_path, vocabulary):
        site, _ = build(tmp_path, vocabulary, PRIVACY_RESPONSES)
        assert self.badged(site) == EXCLUDED

    def test_the_index_badges_the_same_people(self, tmp_path, vocabulary):
        site, _ = build(tmp_path, vocabulary, PRIVACY_RESPONSES)
        index = (site / "index.html").read_text()
        marked = set(
            re.findall(
                r'<li><a href="people/(Q\d+)\.html">[^<]*</a>'
                r' <span class="badge">possibly living</span></li>',
                index,
            )
        )
        assert marked == EXCLUDED

    def test_the_site_and_the_export_agree(self, tmp_path, vocabulary):
        site, _ = build(tmp_path, vocabulary, PRIVACY_RESPONSES)

        def run(query: str) -> list[dict]:
            if "SAMPLE(?deathDate)" in query:
                return FACTS
            return BIRTHS if 'BIND("value" AS ?bound)' in query else []

        export = exclusion_report(config(), run=run, vocabulary=vocabulary)
        assert self.badged(site) == export.excluded

    def test_births_are_read_once_with_the_exports_query(
        self, tmp_path, vocabulary
    ):
        calls = []

        def run(query: str) -> list[dict]:
            calls.append(query)
            return []

        builder = SiteBuilder(
            config(), tmp_path / "site", run=run, vocabulary=vocabulary
        )
        assert builder._possibly_living(PRIVACY_PEOPLE[0])
        assert builder._possibly_living(PRIVACY_PEOPLE[1])
        properties, _ = vocabulary
        assert calls == [
            prefix_block(config().concept_uri)
            + BIRTHS_QUERY.format(**properties)
        ]


class TestThePeopleAreTheExports:
    """The site lists, and badges, the people the non-privileged export's check
    assesses: every item with person evidence, at every rank, so that a
    person whose `human` type a curator deprecated or outranked, or who
    has none, is listed and badged too (#344)."""

    def test_people_and_their_statements_are_selected_as_the_check_selects(
        self, tmp_path, vocabulary
    ):
        calls = []

        def run(query: str) -> list[dict]:
            calls.append(query)
            return []

        SiteBuilder(
            config(), tmp_path / "site", run=run, vocabulary=vocabulary
        ).build()
        properties, items = vocabulary
        selection = people_selection(properties, items)
        # no truthy type, human or other, beside the selection
        truthy = f"?person wdt:{properties['instance_of']} "
        for marker in (
            "GROUP_CONCAT(DISTINCT ?alias",  # the roster
            "GROUP_CONCAT(DISTINCT ?qualifierText",  # their statements
        ):
            (query,) = [query for query in calls if marker in query]
            assert selection in query
            assert truthy not in query

    def test_the_roster_says_which_people_are_families(
        self, tmp_path, vocabulary
    ):
        calls = []

        def run(query: str) -> list[dict]:
            calls.append(query)
            return []

        SiteBuilder(
            config(), tmp_path / "site", run=run, vocabulary=vocabulary
        ).build()
        properties, items = vocabulary
        (roster,) = [q for q in calls if "GROUP_CONCAT(DISTINCT ?alias" in q]
        assert f"BIND({family_type(properties, items)} AS ?family)" in roster
        assert "?personDescription ?deceased ?family\n" in roster

    def test_a_family_is_badged_whatever_the_check_says(
        self, tmp_path, vocabulary
    ):
        # the export leaves every family out, released or not (#344): a
        # death releases the one, not the other
        builder = SiteBuilder(
            config(),
            tmp_path / "site",
            run=lambda query: [],
            vocabulary=vocabulary,
        )
        died = {"person": uri("Q9"), "deceased": literal("true")}
        assert builder._possibly_living({**died, "family": literal("true")})
        assert not builder._possibly_living(
            {**died, "family": literal("false")}
        )

    def test_the_family_test_reads_the_type_at_normal_or_preferred_rank(
        self, vocabulary
    ):
        properties, items = vocabulary
        typing = properties["instance_of"]
        assert " ".join(family_type(properties, items).split()) == (
            "EXISTS { "
            f"?person p:{typing} ?familyTyping . "
            f"?familyTyping ps:{typing}/"
            "<http://www.w3.org/2002/07/owl#sameAs>* "
            f"wd:{items['family']} ; "
            "wikibase:rank ?familyRank . "
            "FILTER(?familyRank != wikibase:DeprecatedRank) }"
        )


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
        # the residence's latest date bounds its end, which no source
        # dates, so the start-time row does not show it
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

    def test_a_stated_range_is_escaped(
        self, tmp_path, vocabulary, hostile_label
    ):
        text = self.page(tmp_path, vocabulary)
        assert (
            f'<td>1938 <span class="qualifiers">({hostile_label}: 1938; '
            "latest date: 1939)</span></td>"
        ) in text

    def test_a_marriage_row_shows_the_range_of_the_end_it_compares(
        self, tmp_path, vocabulary
    ):
        # (here) the tree has Leopold and Agnes married "between 1948 and
        # 1949" and divorced "between 1958 and 1959", beside the
        # judgment's 8 February 1947 and 2 January 1957: each row shows the
        # bounds of its own end (MODELING.md, "Marriages, residences and
        # other periods")
        statements = [
            said(
                "leopold-loebig",
                "spouse",
                "agnes-loebig",
                "divorce-judgment-1957",
                start=at(u.Date(1947, 2, 8)),
                end=at(u.Date(1957, 1, 2)),
            ),
            said(
                "leopold-loebig",
                "spouse",
                "agnes-loebig",
                "family-tree-1984",
                start=at(u.Date(1948)),
                earliest=at(u.Date(1948)),
                latest_start=at(u.Date(1949)),
                end=at(u.Date(1958)),
                earliest_end=at(u.Date(1958)),
                latest=at(u.Date(1959)),
            ),
        ]
        fetched = [
            row for statement in statements for row in rows_of(statement)
        ]
        site, summary = build(
            tmp_path,
            vocabulary,
            {**RESPONSES, "wikibase:rank ?rank": fetched},
        )
        assert summary["inconsistencies"] == 2
        text = (site / "inconsistencies.html").read_text()
        assert "<td>spouse (Agnes Löbig), start time</td>" in text
        assert (
            '<td>1948 <span class="qualifiers">(earliest date: 1948; '
            "latest start date: 1949)</span></td>"
        ) in text
        assert "<td>spouse (Agnes Löbig), end time</td>" in text
        assert (
            '<td>1958 <span class="qualifiers">(earliest end date: 1958; '
            "latest date: 1959)</span></td>"
        ) in text


# The person timeline's rows as person-timeline.rq returns them (#309):
# each date a source gives, the range it states for that date (?earliest,
# ?latest), and the two qualifiers that bound the date (?low, ?high).
DATE_BOUNDS = ("earliest_date", "latest_date")
START_BOUNDS = ("earliest_date", "latest_start_date")
END_BOUNDS = ("earliest_end_date", "latest_date")


def on_timeline(
    person: str,
    what: str,
    date: u.Date,
    source: str,
    *,
    bounds: tuple[str, str] = DATE_BOUNDS,
    earliest: u.Date | None = None,
    latest: u.Date | None = None,
) -> dict:
    """A person-timeline row: `source` dates `person`'s `what` as `date`
    (universe keys, but for `what`), and the qualifiers `bounds` names
    bound it. Its range is the one the date's own qualifier gives
    ("between", "after", "before"), as the GEDCOM adapter writes it, or
    `earliest` and `latest` when given."""
    _, precision, ranges = stated(date)
    row = {
        "person": uri(QIDS[person]),
        "personLabel": literal(label(person)),
        "what": literal(what),
        "date": time_literal(stamp(date)),
        "precision": literal(str(precision)),
        "low": uri(PROPERTIES[bounds[0]]),
        "high": uri(PROPERTIES[bounds[1]]),
        "source": uri(QIDS[source]),
        "sourceLabel": literal(label(source)),
    }
    for name, given in (("earliest", earliest), ("latest", latest)):
        bound = at(given) if given else min(ranges.get(name, ()), default=None)
        if bound is not None:
            row[name] = time_literal(bound.value)
            row[f"{name}Precision"] = literal(str(bound.precision))
    return row


# Eugen's death, dated to the day by the burial register; Klara's
# emigration as two sources date it, 22 June 1938 per the postcard and
# "between 1938 and 1939" per the interview; Rahel's death "after 1945"
# per the tree and (here) "before 1945" per the interview; (here) the tree
# has Leopold and Agnes married "between 1946 and 1947" and divorced
# "after 1956"; and (here) the interview dates Leopold's imprisonment to
# September 1939, between his arrest on 3 September and his release on 1
# October 1939, the days the registration card gives.
EUGEN_DIED = on_timeline(
    "eugen-loebig", "date of death", u.Date(1909, 3, 16), REGISTER
)
KLARA_LEFT = on_timeline(
    "klara-loebig", "migration", u.Date(1938, 6, 22), POSTCARD
)
KLARA_LEFT_IN_38_OR_39 = on_timeline(
    "klara-loebig",
    "migration",
    u.Date(1938, qualifier="between", until=u.Date(1939)),
    INTERVIEW,
)
RAHEL_DIED_AFTER = on_timeline(
    "rahel-loebig", "date of death", u.Date(1945, qualifier="after"), TREE
)
RAHEL_DIED_BEFORE = on_timeline(
    "rahel-loebig",
    "date of death",
    u.Date(1945, qualifier="before"),
    INTERVIEW,
)
MARRIED = on_timeline(
    "leopold-loebig",
    f"spouse: {label('agnes-loebig')} (start time)",
    u.Date(1946, qualifier="between", until=u.Date(1947)),
    TREE,
    bounds=START_BOUNDS,
)
DIVORCED = on_timeline(
    "leopold-loebig",
    f"spouse: {label('agnes-loebig')} (end time)",
    u.Date(1956, qualifier="after"),
    TREE,
    bounds=END_BOUNDS,
)
HELD = on_timeline(
    "leopold-loebig",
    "imprisonment",
    u.Date(1939, 9),
    INTERVIEW,
    earliest=u.Date(1939, 9, 3),
    latest=u.Date(1939, 10, 1),
)
DATED = [
    EUGEN_DIED,
    KLARA_LEFT,
    KLARA_LEFT_IN_38_OR_39,
    RAHEL_DIED_AFTER,
    RAHEL_DIED_BEFORE,
    MARRIED,
    DIVORCED,
    HELD,
]


def qid(row: dict, name: str) -> str:
    return row[name]["value"].rsplit("/", 1)[-1]


class TestTimeline:
    """The timeline page and its feed show beside each date the range its
    source states, as person-timeline.rq returns it: each bound under the
    name of the qualifier that states it and at its own precision, and
    each value with its source's label (CLAUDE.md rules 5 and 6)."""

    @pytest.fixture
    def site(self, tmp_path, vocabulary):
        site, _ = build(
            tmp_path, vocabulary, {**RESPONSES, 'date of birth"@en': DATED}
        )
        return site

    @staticmethod
    def dated(site, row: dict) -> str:
        """What the timeline page shows for `row` before its person: the
        date, and the range beside it."""
        page = (site / "timeline.html").read_text()
        (entry,) = [
            found
            for found in re.findall(r'<div class="entry">(.*?)</div>', page)
            if f'<a href="people/{qid(row, "person")}.html">' in found
            and f", {html.escape(row['what']['value'])}<" in found
            and f'<a href="artifacts/{qid(row, "source")}.html">' in found
        ]
        return entry.split(" — ", 1)[0]

    def test_a_date_its_source_gives_as_a_range_shows_the_range(self, site):
        # one emigration, two sources, each under its own label: the
        # postcard's day as it is, and the interview's range beside its year
        assert self.dated(site, KLARA_LEFT) == "1938-06-22"
        assert self.dated(site, KLARA_LEFT_IN_38_OR_39) == (
            '1938 <span class="qualifiers">'
            "(earliest date: 1938; latest date: 1939)</span>"
        )

    def test_a_range_open_at_one_end_names_only_its_bound(self, site):
        # "after 1945" and "before 1945" are both stored as 1945: each
        # reads as open, never as a closed range or as the bare date
        assert self.dated(site, RAHEL_DIED_AFTER) == (
            '1945 <span class="qualifiers">(earliest date: 1945)</span>'
        )
        assert self.dated(site, RAHEL_DIED_BEFORE) == (
            '1945 <span class="qualifiers">(latest date: 1945)</span>'
        )

    def test_each_end_of_a_period_shows_its_own_bounds(self, site):
        # a start runs from its earliest date to its latest start date, and
        # an end from its earliest end date to its latest date (MODELING.md,
        # "Marriages, residences and other periods")
        assert self.dated(site, MARRIED) == (
            '1946 <span class="qualifiers">'
            "(earliest date: 1946; latest start date: 1947)</span>"
        )
        assert self.dated(site, DIVORCED) == (
            '1956 <span class="qualifiers">(earliest end date: 1956)</span>'
        )

    def test_each_bound_shows_at_its_own_precision(self, site):
        # a month bounded by two days: the date keeps its month and the
        # bounds their days, none cut or padded to another's precision
        assert self.dated(site, HELD) == (
            '1939-09 <span class="qualifiers">'
            "(earliest date: 1939-09-03; latest date: 1939-10-01)</span>"
        )

    def test_a_date_with_no_stated_range_shows_as_the_date_alone(self, site):
        page = (site / "timeline.html").read_text()
        assert (
            '<div class="entry">1909-03-16 — '
            f'<a href="people/{QIDS["eugen-loebig"]}.html">Eugen Löbig</a>, '
            "date of death"
            f'<span class="source"><a href="artifacts/{QIDS[REGISTER]}.html">'
            "Burial register of Friedhof Flörburg: Eugen Löbig</a></span>"
            "</div>"
        ) in page

    def test_the_feed_gives_each_range_beside_its_date(self, site):
        # TimelineJS places an event by its year, month and day, and shows
        # a date's display_date in place of the date
        events = json.loads((site / "timeline.json").read_text())["events"]
        starts = {
            (event["text"]["headline"], event["text"]["text"]): event[
                "start_date"
            ]
            for event in events
        }
        assert len(starts) == len(events) == len(DATED)

        def start(row: dict) -> dict:
            person, what = row["personLabel"], row["what"]
            source = row["sourceLabel"]["value"]
            return starts[
                (f"{person['value']}: {what['value']}", f"per {source}")
            ]

        # a date with no stated range, which TimelineJS shows by default
        assert start(EUGEN_DIED) == {
            "year": "1909",
            "month": "03",
            "day": "16",
        }
        assert start(KLARA_LEFT) == {
            "year": "1938",
            "month": "06",
            "day": "22",
        }
        assert start(KLARA_LEFT_IN_38_OR_39) == {
            "year": "1938",
            "display_date": "1938 (earliest date: 1938; latest date: 1939)",
        }
        assert start(RAHEL_DIED_AFTER) == {
            "year": "1945",
            "display_date": "1945 (earliest date: 1945)",
        }
        assert start(RAHEL_DIED_BEFORE) == {
            "year": "1945",
            "display_date": "1945 (latest date: 1945)",
        }
        assert start(MARRIED) == {
            "year": "1946",
            "display_date": (
                "1946 (earliest date: 1946; latest start date: 1947)"
            ),
        }
        assert start(DIVORCED) == {
            "year": "1956",
            "display_date": "1956 (earliest end date: 1956)",
        }
        # a ranged date finer than a year keeps its month in start_date,
        # and its display_date shows it at its own precision
        assert start(HELD) == {
            "year": "1939",
            "month": "09",
            "display_date": (
                "1939-09 (earliest date: 1939-09-03; latest date: 1939-10-01)"
            ),
        }

    def test_the_feed_escapes_what_timelinejs_shows_as_html(
        self, tmp_path, vocabulary
    ):
        row = {
            **EUGEN_DIED,
            "personLabel": literal('Eugen "Muckel" & Co <Löbig>'),
            "sourceLabel": literal("burial register <p. 1>"),
        }
        site, _ = build(
            tmp_path, vocabulary, {**RESPONSES, 'date of birth"@en': [row]}
        )
        (event,) = json.loads((site / "timeline.json").read_text())["events"]
        assert event["text"] == {
            "headline": (
                "Eugen &quot;Muckel&quot; &amp; Co &lt;Löbig&gt;: "
                "date of death"
            ),
            "text": "per burial register &lt;p. 1&gt;",
        }

    def test_a_range_is_escaped_on_the_page(
        self, tmp_path, vocabulary, hostile_label
    ):
        site, _ = build(
            tmp_path, vocabulary, {**RESPONSES, 'date of birth"@en': DATED}
        )
        assert self.dated(site, KLARA_LEFT_IN_38_OR_39) == (
            f'1938 <span class="qualifiers">({hostile_label}: 1938; '
            "latest date: 1939)</span>"
        )

    def test_a_range_is_escaped_in_the_feed(
        self, tmp_path, vocabulary, hostile_label
    ):
        # TimelineJS writes a display_date into its page as HTML
        site, _ = build(
            tmp_path, vocabulary, {**RESPONSES, 'date of birth"@en': DATED}
        )
        events = json.loads((site / "timeline.json").read_text())["events"]
        (shown,) = [
            event["start_date"]["display_date"]
            for event in events
            if event["text"]["text"] == f"per {label(INTERVIEW)}"
            and event["text"]["headline"].endswith(": migration")
        ]
        assert shown == f"1938 ({hostile_label}: 1938; latest date: 1939)"

    def test_a_bound_the_query_does_not_name_stops_the_build(
        self, tmp_path, vocabulary
    ):
        # shown unnamed, "1945" beside a date of death could be its earliest
        # or its latest date: the build stops and names what is missing
        row = {k: v for k, v in RAHEL_DIED_AFTER.items() if k != "low"}
        with pytest.raises(
            RuntimeError,
            match=re.escape(
                "the person-timeline query gave a row's ?earliest without "
                "the qualifier that states it (?low is missing); it must "
                "project ?low and ?high"
            ),
        ):
            build(
                tmp_path,
                vocabulary,
                {**RESPONSES, 'date of birth"@en': [row]},
            )

    def test_the_query_returns_every_column_the_timeline_reads(self):
        # each bound is named by the qualifier the query read it from
        query = load_templates()["person-timeline"]
        (select,) = re.findall(r"^SELECT(.*?)^WHERE", query, flags=re.S | re.M)
        assert {
            "person",
            "personLabel",
            "what",
            "date",
            "precision",
            "low",
            "earliest",
            "earliestPrecision",
            "high",
            "latest",
            "latestPrecision",
            "source",
            "sourceLabel",
        } <= set(re.findall(r"\?(\w+)", select))


class TestTimeFormatting:
    def test_precisions(self):
        assert format_time("+1909-03-16T00:00:00Z", "11") == "1909-03-16"
        assert format_time("+1909-03-16T00:00:00Z", "10") == "1909-03"
        assert format_time("+1909-00-00T00:00:00Z", "9") == "1909"
        assert format_time("+1890-00-00T00:00:00Z", "8") == "1890s"
        assert format_time("+1801-00-00T00:00:00Z", "7") == "19th century"

    @pytest.mark.parametrize(
        ("stored", "shown"),
        [
            # the 19th century runs from 1801 to 1900, as Wikibase counts
            # it, whichever year in it is stored (#241)
            ("1801-01-01T00:00:00Z", "19th century"),
            ("1850-01-01T00:00:00Z", "19th century"),
            ("1900-01-01T00:00:00Z", "19th century"),
            # 1800 is the 18th century's last year, 1901 the 20th's first
            ("1800-01-01T00:00:00Z", "18th century"),
            ("1901-01-01T00:00:00Z", "20th century"),
            # before the common era the query service gives a century's
            # year as stored: 1800 to 1701 BCE is the 18th century BCE
            ("-1800-01-01T00:00:00Z", "−18th century"),
            ("-1701-01-01T00:00:00Z", "−18th century"),
            ("-1700-01-01T00:00:00Z", "−17th century"),
        ],
    )
    def test_a_century_is_the_one_wikibase_counts(self, stored, shown):
        assert format_time(stored, "7") == shown
