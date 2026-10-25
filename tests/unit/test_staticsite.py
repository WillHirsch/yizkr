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
    CARD,
    CENTURY,
    INTERVIEW,
    MILLENNIUM,
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
from tests.unit.test_privacy import (
    BIRTHS,
    CASES,
    EVENT_BIRTHS,
    EVENT_CASES,
    EVENT_EXCLUDED,
    EVENT_PARTICIPANTS,
    EVENT_PEOPLE_BIRTHS,
    EXCLUDED,
    birth_rows,
)
from yizkr import disagreement
from yizkr.backup import exclusion_report
from yizkr.core import sparql
from yizkr.core.config import Config
from yizkr.core.dates import UnsupportedDate
from yizkr.core.privacy import (
    BIRTHS_QUERY,
    ended_by_death_query,
    family_type,
    participants_query,
    people_selection,
)
from yizkr.model.schema import read_manifest_data
from yizkr.queries.publish import load_templates, prefix_block
from yizkr.staticsite import SiteBuilder, format_time, ordinal


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


def qualifier_cells(*qualifiers: tuple[str, str, str]) -> str:
    """Qualifiers as STATEMENTS_QUERY gives them (staticsite.QUALIFIER_CELL):
    each (manifest key, value, precision) a cell of the property's id, its
    label and the value, percent-encoded as ENCODE_FOR_URI encodes them, and
    the precision ("" for a value that is not a time), "|"-separated."""
    labels = disagreement.property_labels()
    return "|".join(
        " ".join(
            (
                property_id(key),
                urllib.parse.quote(labels[key], safe=""),
                urllib.parse.quote(value, safe=""),
                precision,
            )
        )
        for key, value, precision in qualifiers
    )


STATEMENTS = [
    {
        "person": uri("Q7"),
        "prop": uri(property_id("date_of_death")),
        "propLabel": literal("date of death"),
        "st": uri("s1"),
        "value": time_literal("+1909-03-16T00:00:00Z"),
        "precision": literal("11"),
        "sources": literal("Q23~burial register"),
        "qualifierCells": literal(""),
    },
    {
        "person": uri("Q7"),
        "prop": uri(property_id("date_of_death")),
        "propLabel": literal("date of death"),
        "st": uri("s2"),
        "value": time_literal("+1909-01-01T00:00:00Z"),
        "precision": literal("9"),
        "sources": literal("Q29~gravestone photograph"),
        "qualifierCells": literal(""),
    },
    {
        "person": uri("Q7"),
        "prop": uri(property_id("spouse")),
        "propLabel": literal("spouse"),
        "st": uri("s3"),
        "value": uri("Q9"),
        "valueLabel": literal("Cäcilie Löbig"),
        "sources": literal("Q23~burial register|Q31~family tree"),
        "qualifierCells": literal(
            qualifier_cells(("start_time", "1859-01-01T00:00:00Z", "9"))
        ),
    },
    {
        "person": uri("Q7"),
        "prop": uri(property_id("instance_of")),
        "propLabel": literal("instance of"),
        "st": uri("s4"),
        "value": uri("Q1"),
        "valueLabel": literal("human"),
        "sources": literal("Q23~burial register"),
        "qualifierCells": literal(""),
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


def excerpt(text: str, language: str = "de") -> dict:
    """A transcription excerpt as the query service gives a monolingual
    text."""
    return {"type": "literal", "value": text, "xml:lang": language}


ASSERTIONS = [
    {
        "artifact": uri("Q23"),
        "source": uri("Q23"),
        "st": uri("s1"),
        "reference": uri("r1"),
        "subject": uri("Q7"),
        "subjectLabel": literal("Eugen Löbig"),
        "propLabel": literal("date of death"),
        "value": time_literal("+1909-03-16T00:00:00Z"),
        "precision": literal("11"),
        "page": literal("4"),
        "excerpt": excerpt("gestorben am 16. März 1909"),
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
    "GROUP_CONCAT(DISTINCT ?sourceLabel": STATEMENTS,
    'date of birth"@en': TIMELINE,  # person-timeline UNION arm marker
    "Special:FilePath": GALLERY,  # photo-gallery builds this URL
    "?kind wdt:": ARTIFACTS,  # artifacts query walks the subclass path
    # the assertions query, the one query that reads a printed page number
    "?printedPage": ASSERTIONS,
    # the recorded containment, and the disagreement report's statements:
    # both read a statement's rank, so the containment's marker comes first
    "?item ?link ?target": CONTAINED,
    "wikibase:rank ?rank": FETCHED,
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
            # no such property is in the manifest; one that bears the
            # label renders as any other
            {
                **STATEMENTS[0],
                "propLabel": literal("living person"),
            },
        ]
        site, _ = build(
            tmp_path,
            vocabulary,
            {**RESPONSES, "GROUP_CONCAT(DISTINCT ?sourceLabel": statements},
        )
        text = (site / "people/Q7.html").read_text()
        assert "ist ein" not in text
        assert "<dt>living person</dt>" in text
        assert ">human<" not in text
        assert "<dt>instance of</dt>" in text
        assert 'href="Q9.html"' in text

    def test_the_statements_query_selects_each_property_id(self):
        from yizkr.staticsite import STATEMENTS_QUERY

        assert "SELECT ?person ?prop ?propLabel" in STATEMENTS_QUERY
        assert "GROUP BY ?person ?prop ?propLabel" in STATEMENTS_QUERY

    def qualified(self, tmp_path, vocabulary, cells: str) -> str:
        """Q7's page when its first death statement carries `cells`; the
        statement's qualifier text, unescaped."""
        statements = [
            {**STATEMENTS[0], "qualifierCells": literal(cells)},
            *STATEMENTS[1:],
        ]
        site, _ = build(
            tmp_path,
            vocabulary,
            {**RESPONSES, "GROUP_CONCAT(DISTINCT ?sourceLabel": statements},
        )
        (entry,) = [
            dd
            for dd in re.findall(
                r"<dd>(.*?)</dd>", (site / "people/Q7.html").read_text()
            )
            if dd.startswith("1909-03-16")
        ]
        (shown,) = re.findall(
            r'<span class="qualifiers">\((.*?)\)</span>', entry
        )
        return html.unescape(shown)

    @pytest.mark.parametrize(
        ("value", "precision", "shown"),
        [
            # each at its own precision, never cut to its first year (#317)
            ("1890-01-01T00:00:00Z", "8", "1890s"),
            ("1801-01-01T00:00:00Z", "7", "19th century"),
            ("1900-01-01T00:00:00Z", "7", "19th century"),
            ("2001-01-01T00:00:00Z", "7", "21st century"),
            ("1001-01-01T00:00:00Z", "6", "2nd millennium"),
            ("1908-01-01T00:00:00Z", "9", "1908"),
            ("1909-03-01T00:00:00Z", "10", "1909-03"),
            ("1909-03-14T00:00:00Z", "11", "1909-03-14"),
        ],
    )
    def test_a_time_qualifier_shows_at_its_own_precision(
        self, tmp_path, vocabulary, value, precision, shown
    ):
        cells = qualifier_cells(("earliest_date", value, precision))
        assert self.qualified(tmp_path, vocabulary, cells) == (
            f"earliest date: {shown}"
        )

    def test_qualifiers_are_sorted_whatever_order_they_come_in(
        self, tmp_path, vocabulary
    ):
        # GROUP_CONCAT gives its cells in no set order: a period's start
        # and its bounds, then its end and its bounds, then the rest by
        # label (docs/MODELING.md, "Marriages, residences and other
        # periods")
        qualifiers = [
            ("start_time", "1938-01-01T00:00:00Z", "9"),
            ("earliest_date", "1937-01-01T00:00:00Z", "9"),
            ("latest_start_date", "1939-01-01T00:00:00Z", "9"),
            ("end_time", "1952-01-01T00:00:00Z", "9"),
            ("earliest_end_date", "1950-01-01T00:00:00Z", "9"),
            ("latest_date", "1950-01-01T00:00:00Z", "8"),
            ("sourcing_circumstances", "circa", ""),
            ("age_at_death", "73 Jahre", ""),
        ]
        expected = (
            "start time: 1938; earliest date: 1937; latest start date: 1939; "
            "end time: 1952; earliest end date: 1950; latest date: 1950s; "
            "age at death: 73 Jahre; sourcing circumstances: circa"
        )
        for order in (qualifiers, qualifiers[::-1]):
            cells = qualifier_cells(*order)
            assert self.qualified(tmp_path, vocabulary, cells) == expected

    def test_a_point_in_time_reads_before_its_bounds_and_each_value_in_order(
        self, tmp_path, vocabulary
    ):
        # a point in time before the bounds of its date, and two values of
        # one qualifier in one order, by value and then precision
        qualifiers = [
            ("latest_date", "1939-01-01T00:00:00Z", "9"),
            ("latest_date", "1939-01-01T00:00:00Z", "8"),
            ("earliest_date", "1938-01-01T00:00:00Z", "9"),
            ("point_in_time", "1938-01-01T00:00:00Z", "9"),
            ("object_has_role", "witness", ""),
            ("object_has_role", "informant", ""),
        ]
        expected = (
            "point in time: 1938; earliest date: 1938; latest date: 1930s; "
            "latest date: 1939; object has role: informant; "
            "object has role: witness"
        )
        for order in (qualifiers, qualifiers[::-1]):
            cells = qualifier_cells(*order)
            assert self.qualified(tmp_path, vocabulary, cells) == expected

    def test_a_qualifier_value_is_shown_whole_and_escaped(
        self, tmp_path, vocabulary
    ):
        # the cells are percent-encoded, so a value's spaces, "|" and
        # markup survive the GROUP_CONCAT whole, and are escaped on the page
        # and a value that is not a time is never read as one:
        # format_time would cut "Cäcilie Tarle" at its "T"
        cells = qualifier_cells(
            ("age_at_death", "73 Jahre | <alt> & mehr", ""),
            ("perspective", "Cäcilie Tarle", ""),
            ("sourcing_circumstances", "circa", ""),
        )
        assert self.qualified(tmp_path, vocabulary, cells) == (
            "age at death: 73 Jahre | <alt> & mehr; "
            "perspective: Cäcilie Tarle; sourcing circumstances: circa"
        )
        site = tmp_path / "site"
        assert (
            "&lt;alt&gt; &amp; mehr" in (site / "people/Q7.html").read_text()
        )

    def test_a_qualifier_before_the_common_era_is_refused(
        self, tmp_path, vocabulary
    ):
        # format_time's backstop (ADR-0034) names the statement
        cells = qualifier_cells(
            ("earliest_date", "-1909-01-01T00:00:00Z", "9")
        )
        with pytest.raises(UnsupportedDate, match="Q7 statement s1"):
            self.qualified(tmp_path, vocabulary, cells)

    def test_the_statements_query_formats_no_date_itself(self):
        from yizkr.staticsite import STATEMENTS_QUERY

        # the value and precision of each time qualifier, formatted in
        # Python; never a substring of the date (#317)
        assert "SUBSTR" not in STATEMENTS_QUERY
        assert "wikibase:timePrecision ?qualifierPrecision" in STATEMENTS_QUERY
        assert "AS ?qualifierCells" in STATEMENTS_QUERY

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
        properties, items = vocabulary
        assert calls == [
            prefix_block(config().concept_uri)
            + BIRTHS_QUERY.format(**properties),
            prefix_block(config().concept_uri)
            + participants_query(properties, items),
            prefix_block(config().concept_uri)
            + ended_by_death_query(properties),
        ]


class TestTheBadgeReadsEventsAsTheExportDoes:
    """A birth or death event is its principal's own (#345): the badge
    stays on an informant at someone else's death and on everyone in a
    death that states no role and has several participants, comes off
    the principal, and reads a dated birth event as a further birth of its
    principal, as the export decides each of them."""

    PEOPLE = [
        {
            "person": uri(qid),
            "personLabel": literal(description),
            "deceased": literal("false"),
            "aliases": literal(""),
        }
        for qid, (description, _, _) in EVENT_CASES.items()
    ]

    def responses(self, vocabulary) -> dict:
        properties, _ = vocabulary
        # the birth events' dates, asked before the births they read like
        dates = f"?person p:{properties['point_in_time']} ?statement"
        return {
            dates: EVENT_BIRTHS,
            "?eventKind": EVENT_PARTICIPANTS,
            **RESPONSES,
            "GROUP_CONCAT(DISTINCT ?alias": self.PEOPLE,
            'BIND("value" AS ?bound)': EVENT_PEOPLE_BIRTHS,
        }

    def badged(self, site) -> set[str]:
        return {
            qid
            for qid in EVENT_CASES
            if "possibly living — never in the non-privileged export"
            in (site / f"people/{qid}.html").read_text()
        }

    def test_the_badge_follows_whose_own_each_event_is(
        self, tmp_path, vocabulary
    ):
        site, _ = build(tmp_path, vocabulary, self.responses(vocabulary))
        assert self.badged(site) == EVENT_EXCLUDED
        assert "Q322" in self.badged(site)  # the informant
        assert "Q329" in self.badged(site)  # born 1850, and in 1990

    def test_the_site_and_the_export_agree(self, tmp_path, vocabulary):
        site, _ = build(tmp_path, vocabulary, self.responses(vocabulary))
        answers = self.responses(vocabulary)

        def run(query: str) -> list[dict]:
            if "SAMPLE(?deathDate)" in query:
                return [{"person": uri(qid)} for qid in EVENT_CASES]
            for marker, rows in answers.items():
                if marker in query:
                    return rows
            return []

        export = exclusion_report(config(), run=run, vocabulary=vocabulary)
        assert self.badged(site) == export.excluded == EVENT_EXCLUDED


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
            "GROUP_CONCAT(DISTINCT ?sourceLabel",  # their statements
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

    def test_the_roster_reads_no_death_event(self):
        # a death event is read apart, for its principal alone (#345): no
        # participant of any role is taken for the one who died
        from yizkr.staticsite import PEOPLE_QUERY

        assert "participant" not in PEOPLE_QUERY
        assert "{death}" not in PEOPLE_QUERY
        assert PEOPLE_QUERY.count("EXISTS {{ ?person wdt:") == 5

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


def asserted(row: dict, **cells: dict) -> dict:
    """An ASSERTIONS_QUERY row of the burial register (Q23), `row`'s cells
    with `cells` over them; a cell given as None is left out."""
    merged = {"artifact": uri("Q23"), "source": uri("Q23"), **row, **cells}
    return {name: value for name, value in merged.items() if value is not None}


# Gustav's residence in Australia as the family tree dates it (#316), a
# statement of its own beside the same residence as another reading dates
# it: the start "1938, by 1939 at the latest" (its earliest date and latest
# start date bound it), the end "after 1950, by the 1950s" (its earliest end
# date and latest date bound it), circa
RESIDENCE = {
    "st": uri("s10"),
    "reference": uri("r10"),
    "subject": uri("Q12"),
    "subjectLabel": literal("Gustav Löbig"),
    "propLabel": literal("residence"),
    "value": uri("Q40"),
    "valueLabel": literal("Australia"),
    "page": literal("2"),
    "qualifierCells": literal(
        qualifier_cells(
            ("sourcing_circumstances", "circa", ""),
            ("latest_date", "1950-01-01T00:00:00Z", "8"),
            ("end_time", "1952-01-01T00:00:00Z", "9"),
            ("earliest_end_date", "1950-01-01T00:00:00Z", "9"),
            ("latest_start_date", "1939-01-01T00:00:00Z", "9"),
            ("earliest_date", "1937-01-01T00:00:00Z", "9"),
            ("start_time", "1938-01-01T00:00:00Z", "9"),
        )
    ),
}


class TestArtifactPage:
    """The artifact page's table of what the artifact asserts: a row per
    statement, never per reference block (#351), each value with its
    qualifiers (#316) and each place the artifact asserts it."""

    def page_of(self, tmp_path, vocabulary, rows: list[dict]) -> str:
        site, _ = build(
            tmp_path, vocabulary, {**RESPONSES, "?printedPage": rows}
        )
        return (site / "artifacts/Q23.html").read_text()

    def rows_of(self, page: str) -> list[str]:
        """The table's rows, its header left out."""
        return re.findall(r"<tr><td>(.*?)</tr>", page)

    def test_two_blocks_citing_one_artifact_are_one_statement(
        self, tmp_path, vocabulary
    ):
        # one birth the certificate gives twice, on page 1 and on its back,
        # each block with its own excerpt: one statement, one row, both
        # places
        birth = {
            "st": uri("s20"),
            "subject": uri("Q12"),
            "subjectLabel": literal("Gustav Löbig"),
            "propLabel": literal("date of birth"),
            "value": time_literal("+1862-02-15T00:00:00Z"),
            "precision": literal("11"),
        }
        blocks = [
            asserted(
                birth,
                reference=uri("r20"),
                page=literal("1"),
                excerpt=excerpt("geboren am 15. Februar 1862 zu Flörburg"),
            ),
            asserted(
                birth,
                reference=uri("r21"),
                page=literal("Rückseite"),
                excerpt=excerpt("geb. 15.2.1862 in Flörburg"),
            ),
        ]
        # the query gives the blocks in no set order: the places are sorted
        for rows in (blocks, blocks[::-1]):
            page = self.page_of(tmp_path, vocabulary, rows)
            assert "What this artifact asserts (1)" in page
            (row,) = self.rows_of(page)
            assert "1862-02-15" in row
            assert row.endswith(
                '<td>page number: 1: <q lang="de">geboren am 15. Februar '
                "1862 zu Flörburg</q><br>"
                'page number: Rückseite: <q lang="de">geb. 15.2.1862 in '
                "Flörburg</q></td>"
            )

    def test_same_value_statements_that_differ_by_a_qualifier_stay_two(
        self, tmp_path, vocabulary
    ):
        # ADR-0002: two residences in Australia, dated differently, are two
        # assertions, each with its own range
        other = {
            **RESIDENCE,
            "st": uri("s11"),
            "reference": uri("r11"),
            "page": literal("3"),
            "qualifierCells": literal(
                qualifier_cells(("start_time", "1939-01-01T00:00:00Z", "9"))
            ),
        }
        page = self.page_of(
            tmp_path, vocabulary, [asserted(RESIDENCE), asserted(other)]
        )
        assert "What this artifact asserts (2)" in page
        first, second = self.rows_of(page)
        assert '<span class="qualifiers">(start time: 1939)</span>' in second
        assert "page number: 3" in second
        assert "start time: 1939" not in first

    def test_a_value_shows_its_stated_range_each_end_with_its_bounds(
        self, tmp_path, vocabulary
    ):
        # #316: the start with its earliest date and latest start date, the
        # end with its earliest end date and latest date, each at its own
        # precision, and circa — in the person page's `label: value` form
        (row,) = self.rows_of(
            self.page_of(tmp_path, vocabulary, [asserted(RESIDENCE)])
        )
        assert (
            "<td>Australia"
            ' <span class="qualifiers">(start time: 1938; '
            "earliest date: 1937; latest start date: 1939; end time: 1952; "
            "earliest end date: 1950; latest date: 1950s; "
            "sourcing circumstances: circa)</span></td>"
        ) in row

    def test_a_dated_value_shows_its_range_and_point_in_time(
        self, tmp_path, vocabulary
    ):
        # Rahel's death "after 1945" per the family tree: the bare year
        # never stands alone; and an event's point in time is shown
        death = {
            "st": uri("s30"),
            "reference": uri("r30"),
            "subject": uri("Q13"),
            "subjectLabel": literal("Rahel Löbig"),
            "propLabel": literal("date of death"),
            "value": time_literal("+1945-01-01T00:00:00Z"),
            "precision": literal("9"),
            "qualifierCells": literal(
                qualifier_cells(
                    ("earliest_date", "1945-01-01T00:00:00Z", "9"),
                )
            ),
        }
        event = {
            "st": uri("s31"),
            "reference": uri("r31"),
            "subject": uri("Q14"),
            "subjectLabel": literal("Emigration 1938"),
            "propLabel": literal("participant"),
            "value": uri("Q12"),
            "valueLabel": literal("Gustav Löbig"),
            "qualifierCells": literal(
                qualifier_cells(("point_in_time", "1938-01-01T00:00:00Z", "9"))
            ),
        }
        rows = self.rows_of(
            self.page_of(
                tmp_path, vocabulary, [asserted(death), asserted(event)]
            )
        )
        assert (
            '<td>1945 <span class="qualifiers">(earliest date: 1945)</span>'
            "</td>"
        ) in rows[0]
        assert (
            '<td>Gustav Löbig <span class="qualifiers">'
            "(point in time: 1938)</span></td>"
        ) in rows[1]

    def test_every_locator_of_a_block_is_shown(self, tmp_path, vocabulary):
        # a block's rows are its locators' values: one block, one place,
        # each locator once, under its label
        block = {
            **RESIDENCE,
            "printedPage": literal("iv"),
            "region": literal("10%,20%,30%,40%"),
            "excerpt": excerpt("Australien 1938", "de"),
        }
        page = self.page_of(
            tmp_path,
            vocabulary,
            [
                asserted(block),
                asserted(block, page=literal("5")),
                asserted(block, excerpt=excerpt("Australia 1938", "en")),
            ],
        )
        assert "What this artifact asserts (1)" in page
        (row,) = self.rows_of(page)
        assert row.endswith(
            "<td>page number: 2, 5; printed page number: iv; "
            "relative position within image: 10%,20%,30%,40%: "
            '<q lang="en">Australia 1938</q> '
            '<q lang="de">Australien 1938</q></td>'
        )

    def test_an_excerpt_is_kept_whole_and_escaped(self, tmp_path, vocabulary):
        # the writer turns a line break into " | ": the excerpt is its own
        # cell, never split on it, and is escaped; one with no language
        # has no lang attribute, and a block with no locator cites the
        # whole artifact
        (row,) = self.rows_of(
            self.page_of(
                tmp_path,
                vocabulary,
                [
                    asserted(
                        RESIDENCE,
                        page=None,
                        excerpt={
                            "type": "literal",
                            "value": "Australien | <seit> 1938 & später",
                        },
                    ),
                    asserted(RESIDENCE, reference=uri("r12"), page=None),
                ],
            )
        )
        assert row.endswith(
            "<td><q>Australien | &lt;seit&gt; 1938 &amp; später</q><br>"
            "the artifact as a whole</td>"
        )

    def test_a_fragment_is_named_on_its_wholes_page(
        self, tmp_path, vocabulary
    ):
        # the query lists a statement citing a fragment under its whole
        # (?artifact), with the fragment's label (?fragmentLabel)
        (row,) = self.rows_of(
            self.page_of(
                tmp_path,
                vocabulary,
                [
                    asserted(
                        RESIDENCE,
                        source=uri("Q24"),
                        fragmentLabel=literal(
                            "burial register <copy> & index, page 2"
                        ),
                        page=literal("2 & 3"),
                    )
                ],
            )
        )
        assert row.endswith(
            "<td>fragment: burial register &lt;copy&gt; &amp; index, page 2; "
            "page number: 2 &amp; 3</td>"
        )

    def test_a_fragment_with_no_english_label_is_named_by_its_id(
        self, tmp_path, vocabulary
    ):
        (row,) = self.rows_of(
            self.page_of(
                tmp_path,
                vocabulary,
                [asserted(RESIDENCE, source=uri("Q25"), page=None)],
            )
        )
        assert row.endswith("<td>fragment: Q25</td>")

    def test_places_are_in_page_order(self, tmp_path, vocabulary):
        # page 2 before page 10, in a statement's places and in a block's
        # values, whatever order the query gives them in
        rows = [
            asserted(RESIDENCE, reference=uri("r40"), page=literal("10")),
            asserted(RESIDENCE, reference=uri("r41"), page=literal("2")),
            asserted(RESIDENCE, reference=uri("r42"), page=literal("10")),
            asserted(RESIDENCE, reference=uri("r42"), page=literal("9")),
        ]
        for order in (rows, rows[::-1]):
            (row,) = self.rows_of(self.page_of(tmp_path, vocabulary, order))
            assert row.endswith(
                "<td>page number: 2<br>page number: 9, 10<br>"
                "page number: 10</td>"
            )

    def test_a_qualifier_before_the_common_era_is_refused(
        self, tmp_path, vocabulary
    ):
        bce = asserted(
            RESIDENCE,
            qualifierCells=literal(
                qualifier_cells(("start_time", "-1938-01-01T00:00:00Z", "9"))
            ),
        )
        with pytest.raises(UnsupportedDate, match="Q12 statement s10 as Q23"):
            self.page_of(tmp_path, vocabulary, [bce])

    def test_the_query_reads_each_statement_and_block(self):
        from yizkr.staticsite import ASSERTIONS_QUERY

        projection = ASSERTIONS_QUERY.split("WHERE", 1)[0]
        assert set(re.findall(r"\?(\w+)", projection)) >= {
            "artifact",
            "fragmentLabel",
            "st",
            "reference",
            "page",
            "printedPage",
            "region",
            "excerpt",
            "qualifierCells",
        }
        # the reference node is bound, never walked past as a path, and
        # each locator read from it
        assert "prov:wasDerivedFrom ?reference" in ASSERTIONS_QUERY
        for key, name in (
            ("page_number", "page"),
            ("printed_page_number", "printedPage"),
            ("relative_position_within_image", "region"),
            ("transcription_excerpt", "excerpt"),
        ):
            assert f"?reference pr:{{{key}}} ?{name} ." in ASSERTIONS_QUERY
        # only a fragment that is part of a whole folds into it, and the
        # fold is optional
        assert re.search(
            r"OPTIONAL \{\{\s+"
            r"\?source wdt:\{instance_of\} wd:\{artifact_fragment\} ;"
            r"\s+wdt:\{part_of\} \?whole \.",
            ASSERTIONS_QUERY,
        )
        assert "?reference pr:{transcription_excerpt} ?excerpt" in (
            ASSERTIONS_QUERY
        )
        assert "BIND(COALESCE(?whole, ?source) AS ?artifact)" in (
            ASSERTIONS_QUERY
        )
        # the only GROUP_CONCAT packs qualifier cells, never a locator
        assert ASSERTIONS_QUERY.count("GROUP_CONCAT") == 1
        assert "GROUP_CONCAT(DISTINCT ?qualifierCell;" in ASSERTIONS_QUERY


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
        # by property, as the site stage's record keeps them (#575)
        assert summary["inconsistencies_by_property"] == {
            "birth_name": 1,
            "date_of_birth": 1,
            "date_of_death": 1,
            "point_in_time": 1,
            "residence": 1,
        }
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
    what: str | None,
    date: u.Date,
    source: str,
    *,
    types: tuple[str, ...] = (),
    bounds: tuple[str, str] = DATE_BOUNDS,
    earliest: u.Date | None = None,
    latest: u.Date | None = None,
    precision: int | None = None,
) -> dict:
    """A person-timeline row: `source` dates `person`'s `what` as `date`
    (universe keys, but for `what`), and the qualifiers `bounds` names
    bound it. Its range is the one the date's own qualifier gives
    ("between", "after", "before"), as the GEDCOM adapter writes it, or
    `earliest` and `latest` when given; its precision is `date`'s, or
    `precision` when given. A circa date's statement carries circa as its
    sourcing circumstance (?circumstances, #315), as the GEDCOM adapter
    writes it. An event's row carries its `types` in ?eventTypes, each
    label URI-encoded and "|"-separated, and `what` names its start or
    end time, or is None for its point in time (#540)."""
    _, stated_precision, ranges = stated(date)
    precision = precision or stated_precision
    row = {
        "person": uri(QIDS[person]),
        "personLabel": literal(label(person)),
        "date": time_literal(stamp(date)),
        "precision": literal(str(precision)),
        "low": uri(PROPERTIES[bounds[0]]),
        "high": uri(PROPERTIES[bounds[1]]),
        "source": uri(QIDS[source]),
        "sourceLabel": literal(label(source)),
    }
    if what is not None:
        row["what"] = literal(what)
    if types:
        row["eventTypes"] = literal(
            "|".join(urllib.parse.quote(each, safe="") for each in types)
        )
    if date.qualifier == "circa":
        row["circumstances"] = literal("circa")
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
    "klara-loebig",
    None,
    u.Date(1938, 6, 22),
    POSTCARD,
    types=("migration",),
)
KLARA_LEFT_IN_38_OR_39 = on_timeline(
    "klara-loebig",
    None,
    u.Date(1938, qualifier="between", until=u.Date(1939)),
    INTERVIEW,
    types=("migration",),
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
    None,
    u.Date(1939, 9),
    INTERVIEW,
    types=("imprisonment",),
    earliest=u.Date(1939, 9, 3),
    latest=u.Date(1939, 10, 1),
)
# Cäcilie's birth "c. 1833" per the tree, and (here) as the 19th century
# per the interview, stored by its first year; and Leopold's imprisonment
# by its own start and end time per the registration card, each a row of
# its own (#318)
CAECILIE_BORN_CIRCA = on_timeline(
    "caecilie-loebig",
    "date of birth",
    u.Date(1833, qualifier="circa"),
    TREE,
)
CAECILIE_BORN_IN_THE_19TH_CENTURY = on_timeline(
    "caecilie-loebig",
    "date of birth",
    u.Date(1801),
    INTERVIEW,
    precision=CENTURY,
)
HELD_FROM = on_timeline(
    "leopold-loebig",
    "start time",
    u.Date(1939, 9, 3),
    CARD,
    types=("imprisonment",),
)
HELD_UNTIL = on_timeline(
    "leopold-loebig",
    "end time",
    u.Date(1939, 10, 1),
    CARD,
    types=("imprisonment",),
)
# (here) dates coarser than a year: the interview gives Eugen's birth as
# the 1830s and Klara's to within 100,000 years (stored as 1801), and the
# tree gives Rahel's as the 2nd millennium, stored by its first year
EUGEN_BORN_IN_THE_1830S = on_timeline(
    "eugen-loebig",
    "date of birth",
    u.Date(1830, decade=True),
    INTERVIEW,
)
RAHEL_BORN_IN_THE_2ND_MILLENNIUM = on_timeline(
    "rahel-loebig",
    "date of birth",
    u.Date(1001),
    TREE,
    precision=MILLENNIUM,
)
KLARA_BORN_TO_WITHIN_100000_YEARS = on_timeline(
    "klara-loebig",
    "date of birth",
    u.Date(1801),
    INTERVIEW,
    precision=4,
)
DATED = [
    RAHEL_BORN_IN_THE_2ND_MILLENNIUM,
    KLARA_BORN_TO_WITHIN_100000_YEARS,
    CAECILIE_BORN_IN_THE_19TH_CENTURY,
    EUGEN_BORN_IN_THE_1830S,
    CAECILIE_BORN_CIRCA,
    EUGEN_DIED,
    KLARA_LEFT,
    KLARA_LEFT_IN_38_OR_39,
    RAHEL_DIED_AFTER,
    RAHEL_DIED_BEFORE,
    MARRIED,
    DIVORCED,
    HELD,
    HELD_FROM,
    HELD_UNTIL,
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
            and f", {html.escape(SiteBuilder.timeline_what(row))}<" in found
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

    def test_a_circa_date_says_so(self, site):
        # circa is a sourcing circumstance, not a range (#315): shown as the
        # person page shows it, never read as an exact year
        assert self.dated(site, CAECILIE_BORN_CIRCA) == (
            '1833 <span class="qualifiers">'
            "(sourcing circumstances: circa)</span>"
        )

    def test_circa_follows_the_range_and_each_value_shows(
        self, tmp_path, vocabulary
    ):
        # the query folds a statement's circumstances into one cell, each
        # URI-encoded: each shows, sorted, after the range, and escaped
        row = {
            **KLARA_LEFT_IN_38_OR_39,
            "circumstances": literal("vague%20%3Cb%3E%7C%20%26|circa"),
        }
        site, _ = build(
            tmp_path, vocabulary, {**RESPONSES, 'date of birth"@en': [row]}
        )
        assert self.dated(site, row) == (
            '1938 <span class="qualifiers">(earliest date: 1938; '
            "latest date: 1939; sourcing circumstances: circa; "
            "sourcing circumstances: vague &lt;b&gt;| &amp;)</span>"
        )
        (event,) = json.loads((site / "timeline.json").read_text())["events"]
        assert event["start_date"]["display_date"] == (
            "1938 (earliest date: 1938; latest date: 1939; "
            "sourcing circumstances: circa; "
            "sourcing circumstances: vague &lt;b&gt;| &amp;)"
        )

    def test_a_coarse_date_is_headed_by_its_span(self, site):
        # a decade, century or millennium row is headed by its span, and a
        # coarser one by its stored year with the span its precision
        # allows, never by a bare stored year (#315); a finer date by its
        # year
        page = (site / "timeline.html").read_text()
        headers = re.findall(r'<div class="year">(.*?)</div>', page)
        assert headers[:6] == [
            "2nd millennium",
            "1801, to within 100,000 years",
            "19th century",
            "1830s",
            "1833",
            "1909",
        ]
        for stored in ("1001", "1801", "1830"):
            assert stored not in headers
        for row, shown in (
            (RAHEL_BORN_IN_THE_2ND_MILLENNIUM, "2nd millennium"),
            (
                KLARA_BORN_TO_WITHIN_100000_YEARS,
                "1801, to within 100,000 years",
            ),
            (CAECILIE_BORN_IN_THE_19TH_CENTURY, "19th century"),
            (EUGEN_BORN_IN_THE_1830S, "1830s"),
        ):
            assert self.dated(site, row) == shown

    def test_circa_shows_beside_a_periods_start_and_end_alike(
        self, tmp_path, vocabulary
    ):
        # circa qualifies the whole statement, so its start row and its end
        # row both carry it, after each end's own bounds (#315)
        married = {**MARRIED, "circumstances": literal("circa")}
        divorced = {**DIVORCED, "circumstances": literal("circa")}
        site, _ = build(
            tmp_path,
            vocabulary,
            {**RESPONSES, 'date of birth"@en': [married, divorced]},
        )
        assert self.dated(site, married) == (
            '1946 <span class="qualifiers">(earliest date: 1946; '
            "latest start date: 1947; sourcing circumstances: circa)</span>"
        )
        assert self.dated(site, divorced) == (
            '1956 <span class="qualifiers">(earliest end date: 1956; '
            "sourcing circumstances: circa)</span>"
        )
        events = json.loads((site / "timeline.json").read_text())["events"]
        shown = sorted(event["start_date"]["display_date"] for event in events)
        assert shown == [
            "1946 (earliest date: 1946; latest start date: 1947; "
            "sourcing circumstances: circa)",
            "1956 (earliest end date: 1956; sourcing circumstances: circa)",
        ]

    def test_an_event_dated_by_its_start_and_end_shows_both(self, site):
        # each its own row, with its own source (#318), and the page says
        # how such a date is bounded
        page = (site / "timeline.html").read_text()
        assert (
            "An event's own start or end time, like any other date, runs "
            "from its earliest date to its latest date."
        ) in page
        assert self.dated(site, HELD_FROM) == "1939-09-03"
        assert self.dated(site, HELD_UNTIL) == "1939-10-01"

    def test_an_events_types_name_one_row(self, tmp_path, vocabulary):
        # (here) the card types Leopold's imprisonment as an imprisonment
        # and an event: the query folds both into one cell, in no set
        # order, and the row names them sorted, once (#540)
        row = {**HELD_FROM, "eventTypes": literal("imprisonment|event")}
        site, _ = build(
            tmp_path, vocabulary, {**RESPONSES, 'date of birth"@en': [row]}
        )
        page = (site / "timeline.html").read_text()
        assert page.count('<div class="entry">') == 1
        assert ", event, imprisonment (start time)<" in page
        (event,) = json.loads((site / "timeline.json").read_text())["events"]
        assert event["text"]["headline"] == (
            f"{label('leopold-loebig')}: event, imprisonment (start time)"
        )

    def test_a_type_label_is_escaped_on_the_page(self, tmp_path, vocabulary):
        # a type's label comes from the graph, so the page escapes it
        row = {
            **HELD_FROM,
            "eventTypes": literal(
                urllib.parse.quote("prison <Peiltal> & yard", safe="")
            ),
        }
        site, _ = build(
            tmp_path, vocabulary, {**RESPONSES, 'date of birth"@en': [row]}
        )
        page = (site / "timeline.html").read_text()
        assert ", prison &lt;Peiltal&gt; &amp; yard (start time)<" in page
        assert "<Peiltal>" not in page

    def test_a_type_label_is_read_whole(self):
        # each label is URI-encoded, so a "|" or a "%" in one splits nothing
        # (#540); the labels are sorted ignoring case, and a point in time
        # names the types alone
        row = on_timeline(
            "leopold-loebig",
            None,
            u.Date(1939, 9),
            INTERVIEW,
            types=("imprisonment", "Peiltal court prison | 100% held"),
        )
        assert SiteBuilder.timeline_what(row) == (
            "imprisonment, Peiltal court prison | 100% held"
        )
        assert SiteBuilder.timeline_what(EUGEN_DIED) == "date of death"

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
            person = row["personLabel"]["value"]
            what = SiteBuilder.timeline_what(row)
            source = row["sourceLabel"]["value"]
            return starts[(f"{person}: {what}", f"per {source}")]

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
        # circa beside its date (#315)
        assert start(CAECILIE_BORN_CIRCA) == {
            "year": "1833",
            "display_date": "1833 (sourcing circumstances: circa)",
        }
        # a decade, century or millennium shows as its span, and a coarser
        # date with the span its precision allows, never as its stored year
        assert start(CAECILIE_BORN_IN_THE_19TH_CENTURY) == {
            "year": "1801",
            "display_date": "19th century",
        }
        assert start(EUGEN_BORN_IN_THE_1830S) == {
            "year": "1830",
            "display_date": "1830s",
        }
        assert start(RAHEL_BORN_IN_THE_2ND_MILLENNIUM) == {
            "year": "1001",
            "display_date": "2nd millennium",
        }
        assert start(KLARA_BORN_TO_WITHIN_100000_YEARS) == {
            "year": "1801",
            "display_date": "1801, to within 100,000 years",
        }
        # an event's start and end, each at its day (#318)
        assert start(HELD_FROM) == {"year": "1939", "month": "09", "day": "03"}
        assert start(HELD_UNTIL) == {
            "year": "1939",
            "month": "10",
            "day": "01",
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
            "eventTypes",
            "date",
            "precision",
            "low",
            "earliest",
            "earliestPrecision",
            "high",
            "latest",
            "latestPrecision",
            "circumstances",
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
        ],
    )
    def test_a_century_is_the_one_wikibase_counts(self, stored, shown):
        assert format_time(stored, "7") == shown

    @pytest.mark.parametrize(
        ("stored", "shown"),
        [
            # English ordinals (#319): 1st, 2nd and 3rd, and 11th, 12th
            # and 13th, which are the exceptions to them
            ("0001-01-01T00:00:00Z", "1st century"),
            ("0200-01-01T00:00:00Z", "2nd century"),
            ("0201-01-01T00:00:00Z", "3rd century"),
            ("0400-01-01T00:00:00Z", "4th century"),
            ("1100-01-01T00:00:00Z", "11th century"),
            ("1101-01-01T00:00:00Z", "12th century"),
            ("1300-01-01T00:00:00Z", "13th century"),
            ("2001-01-01T00:00:00Z", "21st century"),
            ("2200-01-01T00:00:00Z", "22nd century"),
            ("2201-01-01T00:00:00Z", "23rd century"),
            ("+10001-01-01T00:00:00Z", "101st century"),
            ("+11100-01-01T00:00:00Z", "111th century"),
        ],
    )
    def test_a_century_takes_its_english_ordinal(self, stored, shown):
        assert format_time(stored, "7") == shown

    @pytest.mark.parametrize(
        ("number", "shown"),
        [
            (1, "1st"),
            (2, "2nd"),
            (3, "3rd"),
            (4, "4th"),
            (10, "10th"),
            (11, "11th"),
            (12, "12th"),
            (13, "13th"),
            (21, "21st"),
            (22, "22nd"),
            (23, "23rd"),
            (101, "101st"),
            (102, "102nd"),
            (103, "103rd"),
            (111, "111th"),
            (112, "112th"),
            (113, "113th"),
        ],
    )
    def test_ordinals(self, number, shown):
        assert ordinal(number) == shown

    @pytest.mark.parametrize(
        ("stored", "shown"),
        [
            # a millennium is counted from year 1, as Wikibase counts it
            # and as docs/MODELING.md and core/privacy.py do: 1001 to 2000
            # is the 2nd millennium, whichever year in it is stored (#319)
            ("0001-01-01T00:00:00Z", "1st millennium"),
            ("1000-01-01T00:00:00Z", "1st millennium"),
            ("1001-01-01T00:00:00Z", "2nd millennium"),
            ("1919-01-07T00:00:00Z", "2nd millennium"),
            ("2000-01-01T00:00:00Z", "2nd millennium"),
            ("2001-01-01T00:00:00Z", "3rd millennium"),
            ("+11000-01-01T00:00:00Z", "11th millennium"),
            ("+21000-01-01T00:00:00Z", "21st millennium"),
        ],
    )
    def test_a_millennium_is_shown_as_a_millennium(self, stored, shown):
        assert format_time(stored, "6") == shown

    @pytest.mark.parametrize(
        ("precision", "shown"),
        [
            ("5", "1919, to within 10,000 years"),
            ("4", "1919, to within 100,000 years"),
            ("3", "1919, to within 1,000,000 years"),
            ("2", "1919, to within 10,000,000 years"),
            ("1", "1919, to within 100,000,000 years"),
            ("0", "1919, to within 1,000,000,000 years"),
        ],
    )
    def test_a_precision_coarser_than_a_millennium_is_never_narrowed(
        self, precision, shown
    ):
        # no century or millennium: the stored year with the span its
        # precision allows, so nothing reads narrower than the source
        assert format_time("1919-01-07T00:00:00Z", precision) == shown


# What sources say about the letter's image in *Our Family from Germany*
# and about the studio portrait (#550, ADR-0039 §7), as SOURCES_SAY_QUERY
# gives it: the booklet's caption over the image on its page 6 (printed 4),
# the image's own dateline, heading and greeting, and the portrait's own
# studio and the names on its reverse. The second `inception`, July 1939
# stated in the booklet, is a curator's promotion of the caption's fact
# (ADR-0039 §4): two sources, two dates, both shown. Rahel's studio
# portrait dates itself circa 1895 (#586).
LETTER, BOOKLET, PORTRAIT, RAHELS_PORTRAIT = "Q50", "Q51", "Q52", "Q53"
KLARA, ADDRESS, STUDIO = "Q12", "Q60", "Q61"
TITLES = {
    LETTER: u.by_key()["letter-excerpt-1939"].title,
    BOOKLET: u.by_key()["family-booklet-2011"].title,
    PORTRAIT: u.by_key()["studio-photo-1890s"].title,
    RAHELS_PORTRAIT: u.by_key()["studio-photo-1895"].title,
}


def said_row(
    artifact: str,
    key: str,
    value: dict,
    source: str,
    st: str,
    *,
    reference: str | None = None,
    page: str | None = None,
    printed: str | None = None,
    value_label: str | None = None,
    precision: str | None = None,
    qualifiers: str = "",
) -> dict:
    """A SOURCES_SAY_QUERY row: `artifact`'s statement `st` of the
    property `key`, stated in `source` in the block `reference` (one of
    its own when None), at `page` and printed page `printed`."""
    row = {
        "artifact": uri(artifact),
        "artifactLabel": literal(TITLES[artifact]),
        "prop": uri(property_id(key)),
        "propLabel": literal(disagreement.property_labels()[key]),
        "st": uri(st),
        "value": value,
        "reference": uri(reference or f"r-{st}"),
        "source": uri(source),
        "sourceLabel": literal(TITLES[source]),
        "qualifierCells": literal(qualifiers),
    }
    for name, given in (
        ("valueLabel", value_label),
        ("precision", precision),
        ("page", page),
        ("printedNumber", printed),
    ):
        if given is not None:
            row[name] = literal(given)
    return row


CAPTION = "Wastl to Klara, July 1939."
# the rows come in no order the pages show: the pages sort what they list
# by its text (staticsite.reading_order)
SOURCES_SAY = [
    said_row(
        LETTER,
        "caption",
        excerpt(CAPTION, "en"),
        BOOKLET,
        "c1",
        page="6",
        printed="4",
    ),
    said_row(
        LETTER,
        "inception",
        time_literal("+1939-07-01T00:00:00Z"),
        BOOKLET,
        "c3",
        precision="10",
        page="6",
        printed="4",
    ),
    said_row(
        LETTER,
        "inception",
        time_literal("+1939-07-28T00:00:00Z"),
        LETTER,
        "c2",
        precision="11",
    ),
    # the letter's heading, and the booklet's translation of it, give the
    # place it was written: one statement, cited in two blocks
    said_row(
        LETTER,
        "location_of_creation",
        uri(ADDRESS),
        LETTER,
        "c4",
        value_label="Brunnenring 35",
    ),
    said_row(
        LETTER,
        "location_of_creation",
        uri(ADDRESS),
        BOOKLET,
        "c4",
        reference="r-c4-booklet",
        value_label="Brunnenring 35",
        page="6",
        printed="4",
    ),
    said_row(
        LETTER,
        "addressee",
        uri(KLARA),
        LETTER,
        "c5",
        value_label="Klara Löbig",
    ),
    said_row(
        PORTRAIT,
        "author",
        uri(STUDIO),
        PORTRAIT,
        "c6",
        value_label="Photograph Brüsrich",
    ),
    said_row(
        PORTRAIT,
        "shows",
        uri("Q8"),
        PORTRAIT,
        "c8",
        value_label="Living Person",
    ),
    said_row(
        PORTRAIT,
        "shows",
        uri("Q7"),
        PORTRAIT,
        "c7",
        value_label='Eugen "Muckel" & Co <Löbig>',
        qualifiers=qualifier_cells(("referred_to_as", "Muckel", "")),
    ),
]


def said_of(st: str) -> dict:
    """SOURCES_SAY's first row of the statement `st`."""
    return next(row for row in SOURCES_SAY if row["st"] == uri(st))


KLARAS_ROW = {
    "person": uri(KLARA),
    "personLabel": literal("Klara Löbig"),
    "deceased": literal("true"),
    "aliases": literal("Pünktchen"),
}
SAID_ARTIFACTS = [
    {
        "artifact": uri(qid),
        "artifactLabel": literal(title),
        "kindLabel": literal("photograph"),
    }
    for qid, title in TITLES.items()
]


class TestWhatSourcesSay:
    """An artifact page's "What sources say about this artifact" and a
    person page's "Named in artifacts" (#550, ADR-0039 §7)."""

    @pytest.fixture
    def site(self, tmp_path, vocabulary):
        site, _ = build(
            tmp_path,
            vocabulary,
            {
                # the query's own marker comes first
                "?printedNumber": SOURCES_SAY,
                **RESPONSES,
                "GROUP_CONCAT(DISTINCT ?alias": [*PEOPLE, KLARAS_ROW],
                "?kind wdt:": SAID_ARTIFACTS,
            },
        )
        return site

    @staticmethod
    def section(page: str, heading: str) -> str:
        """The part of `page` from the h2 `heading` to the next heading or
        the page's last paragraph."""
        start = page.index(f"<h2>{heading}</h2>")
        rest = page[start + len(heading) + 9 :]
        end = min(
            (
                i
                for i in (rest.find("<h2>"), rest.find('<p class="sub"><a'))
                if i >= 0
            ),
            default=len(rest),
        )
        return page[start : start + len(heading) + 9 + end]

    def entries(self, site, qid: str) -> dict[str, list[str]]:
        """The section's values on `qid`'s artifact page, by property."""
        page = (site / f"artifacts/{qid}.html").read_text()
        section = self.section(page, "What sources say about this artifact")
        grouped: dict[str, list[str]] = {}
        for prop, values in re.findall(
            r"<dt>(.*?)</dt>((?:<dd>.*?</dd>)*)", section
        ):
            grouped[prop] = re.findall(r"<dd>(.*?)</dd>", values)
        return grouped

    def test_a_caption_shows_with_its_container_and_page(self, site):
        caption = self.entries(site, LETTER)["caption"]
        assert caption == [
            f'<q lang="en">{CAPTION}</q><span class="source">'
            f'<a href="../artifacts/{BOOKLET}.html">{TITLES[BOOKLET]}</a>'
            ", page 6 (printed 4)</span>"
        ]

    def test_captions_come_first_then_the_artifacts_own_statements(self, site):
        assert list(self.entries(site, LETTER)) == [
            "caption",
            "addressee",
            "inception",
            "location of creation",
        ]

    def test_two_sources_with_two_dates_both_show_at_their_precision(
        self, site
    ):
        # the letter's own day and the caption's month promoted by a
        # curator: neither is picked, each with its own source
        assert self.entries(site, LETTER)["inception"] == [
            '1939-07<span class="source">'
            f'<a href="../artifacts/{BOOKLET}.html">{TITLES[BOOKLET]}</a>'
            ", page 6 (printed 4)</span>",
            '1939-07-28<span class="source">this artifact</span>',
        ]

    def test_an_items_value_is_its_label_linked_only_for_a_person(self, site):
        entries = self.entries(site, LETTER)
        # each block's source by its own label, sorted by what a reader
        # sees, whatever order the blocks come in
        assert entries["location of creation"] == [
            'Brunnenring 35<span class="source">'
            f'<a href="../artifacts/{BOOKLET}.html">{TITLES[BOOKLET]}</a>'
            ", page 6 (printed 4); this artifact</span>"
        ]
        assert entries["addressee"] == [
            f'<a href="../people/{KLARA}.html">Klara Löbig</a>'
            '<span class="source">this artifact</span>'
        ]

    def test_values_are_escaped_and_a_possibly_living_person_badged(
        self, site
    ):
        entries = self.entries(site, PORTRAIT)
        assert entries["shows"] == [
            '<a href="../people/Q7.html">'
            "Eugen &quot;Muckel&quot; &amp; Co &lt;Löbig&gt;</a>"
            ' <span class="qualifiers">(referred to as: Muckel)</span>'
            '<span class="source">this artifact</span>',
            '<a href="../people/Q8.html">Living Person</a>'
            ' <span class="badge">possibly living</span>'
            '<span class="source">this artifact</span>',
        ]
        assert entries["author"] == [
            'Photograph Brüsrich<span class="source">this artifact</span>'
        ]

    def test_an_artifact_no_source_speaks_of_has_no_section(self, site):
        # the booklet's caption is the image's, never the booklet's
        page = (site / f"artifacts/{BOOKLET}.html").read_text()
        assert "What sources say" not in page

    def test_a_person_page_lists_the_artifacts_that_name_them(self, site):
        page = (site / f"people/{KLARA}.html").read_text()
        section = self.section(page, "Named in artifacts")
        assert re.findall(r"<li>(.*?)</li>", section) == [
            f'addressee of <a href="../artifacts/{LETTER}.html">'
            f'{TITLES[LETTER]}</a><span class="source">the artifact '
            "itself</span>"
        ]
        page = (site / "people/Q7.html").read_text()
        section = self.section(page, "Named in artifacts")
        assert re.findall(r"<li>(.*?)</li>", section) == [
            f'shown in <a href="../artifacts/{PORTRAIT}.html">'
            f"{TITLES[PORTRAIT]}</a>"
            ' <span class="qualifiers">(referred to as: Muckel)</span>'
            '<span class="source">the artifact itself</span>'
        ]

    def test_a_person_no_artifact_names_has_no_section(
        self, tmp_path, vocabulary
    ):
        site, _ = build(tmp_path, vocabulary, RESPONSES)
        assert (
            "Named in artifacts" not in (site / "people/Q7.html").read_text()
        )

    def test_statements_are_counted_not_rows(self, tmp_path, vocabulary):
        # a statement cited in two blocks, at two pages, is one statement
        # the second block's rows come first; the citations read in order
        two_blocks = [
            {
                **SOURCES_SAY[0],
                "reference": uri("r-other"),
                "page": literal("8"),
                "printedNumber": literal("6"),
            },
            SOURCES_SAY[0],
        ]
        site, summary = build(
            tmp_path,
            vocabulary,
            {
                "?printedNumber": two_blocks,
                **RESPONSES,
                "?kind wdt:": SAID_ARTIFACTS,
            },
        )
        assert summary["sources_say"] == 1
        page = (site / f"artifacts/{LETTER}.html").read_text()
        source = f'<a href="../artifacts/{BOOKLET}.html">{TITLES[BOOKLET]}</a>'
        assert (
            f'<dd><q lang="en">{CAPTION}</q><span class="source">'
            f"{source}, page 6 (printed 4); {source}, page 8 (printed 6)"
            "</span></dd>"
        ) in page

    def test_the_query_reads_every_placed_and_document_subject_property(
        self, tmp_path, vocabulary
    ):
        sent = []

        def run(query: str):
            sent.append(query)
            return []

        SiteBuilder(
            config(), tmp_path / "site", run=run, vocabulary=vocabulary
        ).build()
        (query,) = [q for q in sent if "?printedNumber" in q]
        keys = [
            "addressee",
            "author",
            "caption",
            "inception",
            "issued_by",
            "location_of_creation",
            "main_subject",
            "shows",
        ]
        wanted = " ".join(f"wd:{property_id(key)}" for key in keys)
        # the statements, and the qualifiers gathered for them
        assert query.count(f"VALUES ?prop {{ {wanted} }}") == 2
        assert f"wd:{property_id('depicts')} " not in query

    def page_of(self, tmp_path, vocabulary, rows: list[dict], qid: str):
        site, _ = build(
            tmp_path,
            vocabulary,
            {
                "?printedNumber": rows,
                **RESPONSES,
                "GROUP_CONCAT(DISTINCT ?alias": [*PEOPLE, KLARAS_ROW],
                "?kind wdt:": SAID_ARTIFACTS,
            },
        )
        return (site / qid).read_text()

    def test_a_date_its_source_gives_as_circa_shows_with_its_circa(
        self, tmp_path, vocabulary
    ):
        # Rahel's portrait dates its own making "um 1895" on its mount: its
        # `inception` at year precision with circa as its sourcing
        # circumstance, both shown, labelled by its source, the portrait
        # itself (ADR-0039 §2.2, §7; #586)
        (made,) = u.about("studio-photo-1895", "inception")
        assert made.source == "studio-photo-1895"
        assert made.value.qualifier == "circa"
        _, precision, ranges = stated(made.value)
        assert ranges == {}  # circa is no range
        row = said_row(
            RAHELS_PORTRAIT,
            "inception",
            time_literal("+" + stamp(made.value)),
            RAHELS_PORTRAIT,
            "c10",
            precision=str(precision),
            qualifiers=qualifier_cells(
                ("sourcing_circumstances", made.value.qualifier, "")
            ),
        )
        page = self.page_of(
            tmp_path, vocabulary, [row], f"artifacts/{RAHELS_PORTRAIT}.html"
        )
        (section,) = re.findall(
            r'<dl class="statements">(.*?)</dl>', page, flags=re.S
        )
        assert section == (
            "<dt>inception</dt>"
            '<dd>1895 <span class="qualifiers">(sourcing circumstances: '
            'circa)</span><span class="source">this artifact</span></dd>'
        )

    def test_a_blocks_pages_are_listed_once_each_in_order(
        self, tmp_path, vocabulary
    ):
        # one block whose rows give two pages, 7 before 6, and the one
        # printed page twice
        rows = [
            {**SOURCES_SAY[0], "page": literal(page), "reference": uri("rA")}
            for page in ("7", "6")
        ]
        page = self.page_of(
            tmp_path, vocabulary, rows, f"artifacts/{LETTER}.html"
        )
        assert ", pages 6, 7 (printed 4)</span>" in page
        assert "printed 4, 4" not in page

    def test_a_caption_its_property_and_its_page_are_escaped(
        self, tmp_path, vocabulary
    ):
        hostile = {
            **SOURCES_SAY[0],
            "value": excerpt('Wastl & Klara <"July"> 1939.', "en"),
            "propLabel": literal("caption <&>"),
            "page": literal("6 & <7>"),
        }
        page = self.page_of(
            tmp_path, vocabulary, [hostile], f"artifacts/{LETTER}.html"
        )
        assert "<dt>caption &lt;&amp;&gt;</dt>" in page
        assert (
            '<q lang="en">Wastl &amp; Klara &lt;&quot;July&quot;&gt; 1939.</q>'
        ) in page
        assert ", page 6 &amp; &lt;7&gt; (printed 4)" in page

    def test_an_unlabelled_uncited_value_shows_its_id_and_says_so(
        self, tmp_path, vocabulary
    ):
        bare = {
            name: value
            for name, value in said_of("c6").items()  # the studio, author
            if name not in ("valueLabel", "reference", "source", "sourceLabel")
        }
        assert qid(bare, "prop") == property_id("author")
        page = self.page_of(
            tmp_path, vocabulary, [bare], f"artifacts/{PORTRAIT}.html"
        )
        assert (
            f'<dd>{STUDIO}<span class="qualifiers"> — no recorded '
            "source</span></dd>"
        ) in page
        assert "— per no recorded source" not in page
        assert '<span class="source">no recorded' not in page

    def test_named_in_entries_read_in_order(self, tmp_path, vocabulary):
        # the placeholder person is shown in the portrait and, here, the
        # letter's addressee: the portrait's row comes first
        rows = [
            said_of("c8"),
            said_row(
                LETTER,
                "addressee",
                uri("Q8"),
                LETTER,
                "c9",
                value_label="Living Person",
            ),
        ]
        page = self.page_of(tmp_path, vocabulary, rows, "people/Q8.html")
        section = self.section(page, "Named in artifacts")
        assert [
            entry.split(" <a", 1)[0]
            for entry in re.findall(r"<li>(.*?)</li>", section)
        ] == ["addressee of", "shown in"]

    def test_the_evidence_table_links_a_caption_to_its_artifact(
        self, tmp_path, vocabulary
    ):
        # the booklet's page lists its caption, about the letter's image:
        # the image links to its artifact page, never to a person page,
        # and an item that has no page is not linked
        about = {
            "artifact": uri(BOOKLET),
            "source": uri(BOOKLET),
            "reference": uri("r1"),
            "propLabel": literal("caption"),
            "page": literal("6"),
        }
        assertions = [
            {
                **about,
                "st": uri("e1"),
                "subject": uri(LETTER),
                "subjectLabel": literal(TITLES[LETTER]),
                "value": excerpt(CAPTION, "en"),
            },
            {
                **about,
                "st": uri("e2"),
                "subject": uri(ADDRESS),
                "subjectLabel": literal("Brunnenring 35"),
                "propLabel": literal("part of"),
                "value": uri("Q62"),
            },
        ]
        site, _ = build(
            tmp_path,
            vocabulary,
            {
                **RESPONSES,
                "?printedPage": assertions,
                "?kind wdt:": SAID_ARTIFACTS,
            },
        )
        page = (site / f"artifacts/{BOOKLET}.html").read_text()
        assert (
            f'<td><a href="../artifacts/{LETTER}.html">{TITLES[LETTER]}</a>'
            "</td>"
        ) in page
        assert "<td>Brunnenring 35</td>" in page
        assert f"people/{LETTER}.html" not in page

    def test_people_sort_by_their_names_not_their_links(
        self, tmp_path, vocabulary
    ):
        # Klara (Q12) and the placeholder person (Q8), both given here as
        # the letter's addressee: by their links Q8 would come first
        rows = [
            said_of("c5"),
            said_row(
                LETTER,
                "addressee",
                uri("Q8"),
                LETTER,
                "c9",
                value_label="Living Person",
            ),
        ]
        page = self.page_of(
            tmp_path, vocabulary, rows, f"artifacts/{LETTER}.html"
        )
        assert re.findall(r'<dd><a href="../people/(Q\d+).html">', page) == [
            KLARA,
            "Q8",
        ]


# -- a death no source dates (#354) -----------------------------------------

WIKIBASE_RANK = "http://wikiba.se/ontology#"


def death_rows(event: str, parts, kind_sources=()) -> list[dict]:
    """UNDATED_DEATHS_QUERY's rows for one undated death event: `parts`
    holds each participant statement as (its value's QID, "" for none or
    "?" for an unknown one; its roles; its rank; the (QID, label) of each
    source it is stated in, the label "" for none), and `kind_sources`
    the sources its `death` type is stated in."""
    rows = []
    for n, (value, roles, rank, sources) in enumerate(parts):
        base = {
            "event": uri(event),
            "part": {
                "type": "uri",
                "value": f"http://wikibase.example/entity/statement/{event}-{n}",
            },
            "partRank": {"type": "uri", "value": f"{WIKIBASE_RANK}{rank}"},
        }
        if value == "?":
            base["value"] = {"type": "bnode", "value": f"b{event}{n}"}
        elif value:
            base["value"] = uri(value)
        for role in roles or [None]:
            with_role = dict(base)
            if role is not None:
                with_role["role"] = uri(f"role-{role}")
                with_role["principal"] = literal(
                    "true" if role == "principal" else "false"
                )
            for source, source_label in sources or [(None, None)]:
                for kind, kind_label in kind_sources or [(None, None)]:
                    row = dict(with_role)
                    for name, qid, named in (
                        ("source", source, source_label),
                        ("kindSource", kind, kind_label),
                    ):
                        if qid:
                            row[name] = uri(qid)
                            if named:
                                row[f"{name}Label"] = literal(named)
                    rows.append(row)
    return rows


# The universe's Rudolf, whose death Nina's 1958 letter states with no
# date ("the widow of your late brother Rudolf"), with Nina beside him as
# the one who reports it: content-marked by QIDs in the Q70x range
UNIVERSE = u.by_key()
RUDOLF, NINA, LEOPOLD = "Q701", "Q702", "Q703"
NINAS_LETTER, CERTIFICATE = "Q704", "Q705"
RUDOLFS_DEATH = "Q710"
LETTER_TITLE = UNIVERSE["letter-to-leopold-1958"].title
CERTIFICATE_TITLE = UNIVERSE["death-certificate-1941"].title
ROSTER = [
    {
        "person": uri(qid),
        "personLabel": literal(UNIVERSE[key].label),
        "deceased": literal("false"),
        "aliases": literal(""),
    }
    for qid, key in (
        (RUDOLF, "rudolf-loebig"),
        (NINA, "nina-sartina"),
        (LEOPOLD, "leopold-loebig"),
    )
]
UNDATED = death_rows(
    RUDOLFS_DEATH,
    [
        (RUDOLF, ["principal"], "NormalRank", [(NINAS_LETTER, LETTER_TITLE)]),
        # the informant's statement cites the certificate, which is no
        # source of the death the letter states
        (
            NINA,
            ["informant"],
            "NormalRank",
            [(CERTIFICATE, CERTIFICATE_TITLE)],
        ),
    ],
    kind_sources=[(NINAS_LETTER, LETTER_TITLE)],
)
DIED = (
    "died (date not stated)"
    f'<span class="source"><a href="../artifacts/{NINAS_LETTER}.html">'
    f"{html.escape(LETTER_TITLE)}</a></span>"
)


class TestAnUndatedDeath:
    """A death event that states no date shows on its principal's page,
    and at the foot of the timeline, as "died (date not stated)" with its
    source (CLAUDE.md rule 6, #354); never as the death of an informant or
    a witness who takes part in it."""

    def responses(self, rows) -> dict:
        return {
            **RESPONSES,
            "GROUP_CONCAT(DISTINCT ?alias": ROSTER,
            "?kindSource": rows,
        }

    def site(self, tmp_path, vocabulary, rows=UNDATED):
        site, summary = build(tmp_path, vocabulary, self.responses(rows))
        return site, summary

    def page(self, site, qid: str) -> str:
        return (site / f"people/{qid}.html").read_text()

    def test_the_principal_is_shown_to_have_died_with_its_source(
        self, tmp_path, vocabulary
    ):
        site, summary = self.site(tmp_path, vocabulary)
        assert f"<dt>death</dt><dd>{DIED}</dd>" in self.page(site, RUDOLF)
        assert summary["undated_deaths"] == 1

    def test_an_informant_is_never_shown_to_have_died(
        self, tmp_path, vocabulary
    ):
        site, _ = self.site(tmp_path, vocabulary)
        for qid in (NINA, LEOPOLD):
            assert "died (date not stated)" not in self.page(site, qid)
        # and the informant statement's source is no source of the death
        assert CERTIFICATE not in self.page(site, RUDOLF)

    def test_only_the_principals_own_statements_cite_it(
        self, tmp_path, vocabulary
    ):
        # beside the principal, a participant who states no role, and a
        # second statement of the principal's own that states another
        # role: neither one's source is a source of the death, and the
        # role-less participant is not shown to have died
        rows = death_rows(
            "Q720",
            [
                (RUDOLF, ["principal"], "NormalRank", []),
                (NINA, [], "NormalRank", [(CERTIFICATE, CERTIFICATE_TITLE)]),
            ],
            kind_sources=[(NINAS_LETTER, LETTER_TITLE)],
        ) + death_rows(
            "Q721",
            [
                (
                    RUDOLF,
                    ["principal"],
                    "NormalRank",
                    [(NINAS_LETTER, LETTER_TITLE)],
                ),
                (
                    RUDOLF,
                    ["informant"],
                    "NormalRank",
                    [(CERTIFICATE, CERTIFICATE_TITLE)],
                ),
            ],
        )
        site, summary = self.site(tmp_path, vocabulary, rows)
        text = self.page(site, RUDOLF)
        assert text.count(f"<dd>{DIED}</dd>") == 2
        assert CERTIFICATE not in text
        assert summary["undated_deaths"] == 2
        assert "died (date not stated)" not in self.page(site, NINA)

    def test_the_timeline_lists_it_by_whom_it_happened_to(
        self, tmp_path, vocabulary
    ):
        site, _ = self.site(tmp_path, vocabulary)
        timeline = (site / "timeline.html").read_text()
        assert (
            "<h2>No date stated</h2>" in timeline
            and '<div class="entry">'
            f'<a href="people/{RUDOLF}.html">Rudolf Löbig</a>, '
            "died (date not stated)"
            f'<span class="source"><a href="artifacts/{NINAS_LETTER}.html">'
            f"{html.escape(LETTER_TITLE)}</a></span></div>"
            in timeline
        )
        assert f"people/{NINA}.html" not in timeline

    def test_with_none_the_timeline_has_no_undated_section(
        self, tmp_path, vocabulary
    ):
        site, _ = self.site(tmp_path, vocabulary, rows=[])
        assert "No date stated" not in (site / "timeline.html").read_text()
        assert "died (date not stated)" not in self.page(site, RUDOLF)

    def test_a_death_that_is_nobodys_own_is_shown_on_no_page(
        self, tmp_path, vocabulary
    ):
        # no role stated; two principals; a principal beside an unknown one
        rows = (
            death_rows("Q711", [(RUDOLF, [], "NormalRank", [])])
            + death_rows(
                "Q712",
                [
                    (RUDOLF, ["principal"], "NormalRank", []),
                    (NINA, ["principal"], "NormalRank", []),
                ],
            )
            + death_rows(
                "Q713",
                [
                    (RUDOLF, ["principal"], "NormalRank", []),
                    ("?", ["principal"], "NormalRank", []),
                ],
            )
        )
        site, summary = self.site(tmp_path, vocabulary, rows)
        for qid in (RUDOLF, NINA):
            assert "died (date not stated)" not in self.page(site, qid)
        assert summary["undated_deaths"] == 0

    def test_its_principal_is_read_at_any_rank_but_deprecated(
        self, tmp_path, vocabulary
    ):
        # the principal's statement at normal rank beside a preferred one
        # of the informant's still names whose death it is; a deprecated
        # one names no one
        outranked = death_rows(
            "Q714",
            [
                (RUDOLF, ["principal"], "NormalRank", []),
                (NINA, ["informant"], "PreferredRank", []),
            ],
        )
        deprecated = death_rows(
            "Q715", [(NINA, ["principal"], "DeprecatedRank", [])]
        )
        site, _ = self.site(tmp_path, vocabulary, outranked + deprecated)
        assert "died (date not stated)" in self.page(site, RUDOLF)
        assert "died (date not stated)" not in self.page(site, NINA)

    def test_a_deprecated_second_principal_hides_no_death(
        self, tmp_path, vocabulary
    ):
        # a deprecated statement naming Nina as a second principal is set
        # aside: the death is still Rudolf's, by the letter, and never hers
        rows = death_rows(
            "Q722",
            [
                (
                    RUDOLF,
                    ["principal"],
                    "NormalRank",
                    [(NINAS_LETTER, LETTER_TITLE)],
                ),
                (
                    NINA,
                    ["principal"],
                    "DeprecatedRank",
                    [(CERTIFICATE, CERTIFICATE_TITLE)],
                ),
            ],
        )
        site, _ = self.site(tmp_path, vocabulary, rows)
        assert f"<dd>{DIED}</dd>" in self.page(site, RUDOLF)
        assert "died (date not stated)" not in self.page(site, NINA)

    def test_a_deprecated_statement_of_the_principal_cites_nothing(
        self, tmp_path, vocabulary
    ):
        rows = death_rows(
            "Q716",
            [
                (RUDOLF, ["principal"], "NormalRank", []),
                (
                    RUDOLF,
                    ["principal"],
                    "DeprecatedRank",
                    [(CERTIFICATE, CERTIFICATE_TITLE)],
                ),
            ],
            kind_sources=[(NINAS_LETTER, LETTER_TITLE)],
        )
        site, _ = self.site(tmp_path, vocabulary, rows)
        assert f"<dd>{DIED}</dd>" in self.page(site, RUDOLF)

    def test_each_undated_death_is_shown_with_its_own_sources(
        self, tmp_path, vocabulary
    ):
        # a second event, the certificate's: its type cites it, and its
        # principal's statement cites a source with no English label,
        # shown by its id
        second = death_rows(
            "Q717",
            [(RUDOLF, ["principal"], "NormalRank", [("Q706", "")])],
            kind_sources=[(CERTIFICATE, CERTIFICATE_TITLE)],
        )
        site, summary = self.site(tmp_path, vocabulary, UNDATED + second)
        text = self.page(site, RUDOLF)
        assert f"<dd>{DIED}</dd>" in text
        assert (
            "<dd>died (date not stated)"
            f'<span class="source"><a href="../artifacts/{CERTIFICATE}.html">'
            f"{html.escape(CERTIFICATE_TITLE)}</a>; Q706</span></dd>" in text
        )
        assert summary["undated_deaths"] == 2
        # the timeline lists both, each with its own sources
        timeline = (site / "timeline.html").read_text()
        person = f'<a href="people/{RUDOLF}.html">Rudolf Löbig</a>, '
        assert sorted(
            re.findall(
                rf'<div class="entry">{re.escape(person)}(.*?)</div>',
                timeline,
            )
        ) == sorted(
            [
                "died (date not stated)"
                f'<span class="source"><a href="artifacts/{NINAS_LETTER}'
                f'.html">{html.escape(LETTER_TITLE)}</a></span>',
                "died (date not stated)"
                f'<span class="source"><a href="artifacts/{CERTIFICATE}'
                f'.html">{html.escape(CERTIFICATE_TITLE)}</a>; Q706</span>',
            ]
        )

    def test_it_stands_beside_a_dated_death_of_the_same_person(
        self, tmp_path, vocabulary
    ):
        # Q7's date of death and an undated death event, both shown under
        # their own headings
        rows = death_rows(
            "Q718",
            [
                (
                    "Q7",
                    ["principal"],
                    "NormalRank",
                    [("Q23", "burial register")],
                )
            ],
        )
        site, _ = build(
            tmp_path, vocabulary, {**RESPONSES, "?kindSource": rows}
        )
        text = self.page(site, "Q7")
        assert "<dt>date of death</dt>" in text
        assert (
            "<dt>death</dt><dd>died (date not stated)"
            '<span class="source"><a href="../artifacts/Q23.html">'
            "burial register</a></span></dd>" in text
        )
        # the timeline names them as the person page does, escaped
        timeline = (site / "timeline.html").read_text()
        assert (
            '<a href="people/Q7.html">'
            "Eugen &quot;Muckel&quot; &amp; Co &lt;Löbig&gt;</a>, died"
            in timeline
        )

    def test_no_page_and_no_entry_for_anyone_the_site_does_not_list(
        self, tmp_path, vocabulary
    ):
        # the privacy module's selection decides who has a page: a death
        # of someone it does not list adds no page and no timeline entry
        rows = death_rows("Q719", [("Q799", ["principal"], "NormalRank", [])])
        site, summary = self.site(tmp_path, vocabulary, UNDATED + rows)
        pages = {path.stem for path in (site / "people").iterdir()}
        assert pages == {RUDOLF, NINA, LEOPOLD}
        assert "Q799" not in (site / "timeline.html").read_text()
        assert summary["undated_deaths"] == 1

    def test_the_badge_is_decided_as_before(self, tmp_path, vocabulary):
        # the undated deaths are shown, never weighed: the badge reads the
        # privacy module's evidence alone
        site, _ = self.site(tmp_path, vocabulary)
        assert "possibly living — never in the non-privileged export" in (
            self.page(site, RUDOLF)
        )

    def test_the_query_reads_statements_at_any_rank_but_deprecated(
        self, tmp_path, vocabulary
    ):
        from yizkr.staticsite import UNDATED_DEATHS_QUERY

        calls = []

        def run(query: str) -> list[dict]:
            calls.append(query)
            return []

        SiteBuilder(
            config(), tmp_path / "site", run=run, vocabulary=vocabulary
        ).build()
        properties, items = vocabulary
        (query,) = [q for q in calls if "?kindSource" in q]
        assert "wdt:" not in UNDATED_DEATHS_QUERY
        typing = properties["instance_of"]
        compact = " ".join(query.split())
        assert (
            f"?event p:{typing} ?typing . ?typing ps:{typing}/"
            f"<http://www.w3.org/2002/07/owl#sameAs>* wd:{items['death']} ; "
            "wikibase:rank ?typingRank . "
            "FILTER(?typingRank != wikibase:DeprecatedRank)" in compact
        )
        dated = " ".join(
            f"wd:{properties[key]}"
            for key in ("point_in_time", "start_time", "end_time")
        )
        assert f"VALUES ?dated {{ {dated} }}" in compact
        assert "FILTER(?datingRank != wikibase:DeprecatedRank)" in compact
        # the participants are read as the privacy module reads them
        assert (
            f"?event p:{properties['participant']} ?part . "
            "?part wikibase:rank ?partRank ." in compact
        )
        assert (
            f"BIND(EXISTS {{ ?role <http://www.w3.org/2002/07/owl#sameAs>* "
            f"wd:{items['principal']} . }} AS ?principal)" in compact
        )
        # the sources: the participant statement's, and the type
        # statement's, never the event's own
        stated_in = properties["stated_in"]
        assert f"?part prov:wasDerivedFrom/pr:{stated_in} ?source ." in compact
        assert (
            f"?typing prov:wasDerivedFrom/pr:{stated_in} ?kindSource ."
            in compact
        )

    def test_no_property_path_holds_a_variable(self):
        # SPARQL 1.1 allows no variable in a property path: the query
        # service refuses the query, and the whole build with it
        from yizkr.staticsite import UNDATED_DEATHS_QUERY

        paths = re.findall(r"\S+/\S+", UNDATED_DEATHS_QUERY)
        assert paths
        assert [path for path in paths if "?" in path] == []


# an un-held media item a GEDCOM export makes (ADR-0021), the export, and
# a source's mention of a photograph it only writes about (ADR-0039
# §10.1(c)), each with a QID of its own
UNHELD, EXPORT_FILE, ENCLOSED = "Q54", "Q55", "Q56"
OTHER_TITLES = {
    UNHELD: "Studio portrait of the Löbig family",
    EXPORT_FILE: "Löbig family tree",
    ENCLOSED: "photograph",
}


def other_row(
    artifact: str, key: str, value: dict, source: str, st: str, **more
):
    """said_row for an artifact or a source TITLES does not hold."""
    row = said_row(LETTER, key, value, LETTER, st, **more)
    return row | {
        "artifact": uri(artifact),
        "artifactLabel": literal(
            OTHER_TITLES.get(artifact) or TITLES[artifact]
        ),
        "source": uri(source),
        "sourceLabel": literal(OTHER_TITLES.get(source) or TITLES[source]),
    }


class TestWhatOtherSourcesSay:
    """What a source states of another artifact, beside what the artifact
    states of itself: a container's claim about an image it places, an
    export's about the un-held media item its record makes, a letter's
    about an enclosure it mentions (#646, ADR-0039 §10.2)."""

    ROWS = [
        *SOURCES_SAY,
        # the booklet's caption conveys the letter's addressee
        said_row(
            LETTER,
            "addressee",
            uri(KLARA),
            BOOKLET,
            "c20",
            value_label="Klara Löbig",
            page="6",
            printed="4",
        ),
        # the export dates the object its record makes
        other_row(
            UNHELD,
            "inception",
            time_literal("+1890-01-01T00:00:00Z"),
            EXPORT_FILE,
            "c21",
            precision="8",
        ),
        # (here) a letter says whom the photograph it encloses shows
        other_row(
            ENCLOSED,
            "shows",
            uri(KLARA),
            LETTER,
            "c22",
            value_label="Klara Löbig",
        ),
    ]
    ARTIFACTS = [
        *SAID_ARTIFACTS,
        {
            "artifact": uri(UNHELD),
            "artifactLabel": literal(OTHER_TITLES[UNHELD]),
            "kindLabel": literal("photograph"),
        },
    ]

    @pytest.fixture
    def site(self, tmp_path, vocabulary):
        site, _ = build(
            tmp_path,
            vocabulary,
            {
                "?printedNumber": self.ROWS,
                **RESPONSES,
                "GROUP_CONCAT(DISTINCT ?alias": [*PEOPLE, KLARAS_ROW],
                "?kind wdt:": self.ARTIFACTS,
            },
        )
        return site

    def test_the_sub_heading_names_other_sources(self, site):
        page = (site / f"artifacts/{LETTER}.html").read_text()
        assert (
            "what the artifact states of itself, and what other sources "
            "state of it, each with its page."
        ) in page

    def test_another_source_s_value_stands_beside_the_artifact_s_own(
        self, site
    ):
        entries = TestWhatSourcesSay().entries(site, LETTER)
        assert entries["addressee"] == [
            f'<a href="../people/{KLARA}.html">Klara Löbig</a>'
            '<span class="source">'
            f'<a href="../artifacts/{BOOKLET}.html">{TITLES[BOOKLET]}</a>'
            ", page 6 (printed 4)</span>",
            f'<a href="../people/{KLARA}.html">Klara Löbig</a>'
            '<span class="source">this artifact</span>',
        ]

    def test_an_unheld_media_item_s_page_shows_its_inception(self, site):
        entries = TestWhatSourcesSay().entries(site, UNHELD)
        assert entries == {
            "inception": [
                '1890s<span class="source">'
                f'<a href="../artifacts/{EXPORT_FILE}.html">'
                f"{OTHER_TITLES[EXPORT_FILE]}</a></span>"
            ]
        }
        page = (site / f"artifacts/{UNHELD}.html").read_text()
        assert "download the original" not in page

    def test_named_in_labels_each_value_by_its_source(self, site):
        page = (site / f"people/{KLARA}.html").read_text()
        section = TestWhatSourcesSay.section(page, "Named in artifacts")
        assert (
            "each labelled by the source that says it, with its page"
        ) in section
        assert re.findall(r"<li>(.*?)</li>", section) == [
            f'addressee of <a href="../artifacts/{LETTER}.html">'
            f'{TITLES[LETTER]}</a><span class="source">'
            f'<a href="../artifacts/{BOOKLET}.html">{TITLES[BOOKLET]}</a>'
            ", page 6 (printed 4)</span>",
            f'addressee of <a href="../artifacts/{LETTER}.html">'
            f'{TITLES[LETTER]}</a><span class="source">the artifact '
            "itself</span>",
            # a mention item has no artifact page, so it is named alone
            'shown in photograph<span class="source">'
            f'<a href="../artifacts/{LETTER}.html">{TITLES[LETTER]}</a>'
            "</span>",
        ]
        assert not (site / f"artifacts/{ENCLOSED}.html").exists()

    def test_a_mention_s_label_is_escaped(self, tmp_path, vocabulary):
        hostile = {
            **other_row(
                ENCLOSED,
                "shows",
                uri(KLARA),
                LETTER,
                "c23",
                value_label="Klara Löbig",
            ),
            "artifactLabel": literal('<img src=x onerror=alert(1)>"&'),
        }
        site, _ = build(
            tmp_path,
            vocabulary,
            {
                "?printedNumber": [hostile],
                **RESPONSES,
                "GROUP_CONCAT(DISTINCT ?alias": [*PEOPLE, KLARAS_ROW],
                "?kind wdt:": self.ARTIFACTS,
            },
        )
        page = (site / f"people/{KLARA}.html").read_text()
        assert "<img src=x" not in page
        assert "shown in &lt;img src=x onerror=alert(1)&gt;&quot;&amp;" in page
