"""Offline tests: a date before the common era or in a calendar other than
the proleptic Gregorian is unsupported, and every layer that meets one
fails explicitly, naming where, never converting or guessing (ADR-0034,
#338).

One refusal per layer: the shared check (yizkr.core.dates), the claim
schema as extraction meets it, the GEDCOM adapter, the review sheet's
correction path, the writer, and each reader of the graph: the
disagreement report, the site (format_time and its build), the
possibly-living check and reconcile's roster. Each case beside a refusal
shows that the same input with a supported date passes, so no refusal is
vacuous. tests/integration/test_live_unsupported_dates.py checks the
writer's refusal on the running stack.

The examples are Eugen Löbig's birth, 1836 in the example universe, given
(here) as a Julian date or moved before the common era.
"""

from __future__ import annotations

import urllib.request
from types import SimpleNamespace

import pytest
import yaml

from tests.unit.test_pipeline import (
    ARTIFACT,
    MEDIA_REF,
    MEDIA_SHA1,
    RUN,
    FakeWiki,
)
from yizkr import disagreement
from yizkr.backup import exclusion_report
from yizkr.claims import Claim, ClaimBatch, TimeValue
from yizkr.core import dates, privacy
from yizkr.core.dates import (
    GREGORIAN,
    UnsupportedDate,
    check_time,
    time_problem,
    unsupported_in_graph,
)
from yizkr.model.schema import read_manifest_data
from yizkr.pipeline import reconcile
from yizkr.pipeline.extract import salvage_valid_claims
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.review import _parse_correction, claim_id
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates
from yizkr.staticsite import SiteBuilder, format_time

JULIAN = "http://www.wikidata.org/entity/Q1985786"
# the manifest's property keys, for a vocabulary of made-up ids
PROPERTY_KEYS = yaml.safe_load(read_manifest_data())["properties"]
CONCEPT = "http://wikibase.example"
XSD = "http://www.w3.org/2001/XMLSchema#dateTime"
NORMAL = "http://wikiba.se/ontology#NormalRank"
EUGEN = f"{CONCEPT}/entity/Q1001"
STATEMENT = f"{CONCEPT}/entity/statement/Q1001-born"


def birth(value: dict, qualifiers: tuple = ()) -> dict:
    """Eugen Löbig's birth as a claim citing the fictional scan."""
    return {
        "subject": {
            "kind": "local",
            "local_id": "eugen",
            "label": "Eugen Löbig",
            "instance_of": "human",
        },
        "property": "date_of_birth",
        "value": value,
        "qualifiers": list(qualifiers),
        "source": {"artifact": ARTIFACT},
    }


def bce(year: int = -1836) -> TimeValue:
    """A time value no claim could carry, built around validation."""
    return TimeValue.model_construct(
        type="time",
        year=year,
        month=None,
        day=None,
        precision="year",
        calendar="gregorian",
    )


class TestTheCheck:
    """yizkr.core.dates: the one reading every reader shares."""

    @pytest.mark.parametrize(
        ("value", "calendar"),
        [
            # a year with a minus, as the API and the query service give it
            ("-1836-01-07T00:00:00Z", None),
            ("-1836-00-00T00:00:00Z", GREGORIAN),
            # xsd:dateTime's year 0 is 1 BCE
            ("0000-01-01T00:00:00Z", None),
            ("+0000-00-00T00:00:00Z", None),
            # a supported year in the Julian calendar
            ("1836-01-01T00:00:00Z", JULIAN),
            ("+1836-00-00T00:00:00Z", "julian"),
        ],
    )
    def test_an_unsupported_time_is_named(self, value, calendar):
        assert time_problem(value, calendar).startswith("unsupported: ")
        with pytest.raises(UnsupportedDate, match=r"^Q1001: unsupported: "):
            check_time(value, calendar, "Q1001")

    @pytest.mark.parametrize(
        ("value", "calendar"),
        [
            ("1836-01-01T00:00:00Z", GREGORIAN),
            ("+1836-00-00T00:00:00Z", None),
            ("0001-01-01T00:00:00Z", GREGORIAN),
            ("12026-01-01T00:00:00Z", None),
        ],
    )
    def test_a_supported_time_passes(self, value, calendar):
        assert time_problem(value, calendar) is None
        check_time(value, calendar, "Q1001")

    def test_the_graph_check_names_each_item_and_statement(self):
        sent = []

        def run(query: str) -> list[dict]:
            sent.append(query)
            return [
                {
                    "entity": {"value": EUGEN},
                    "statement": {"value": STATEMENT},
                    "time": {"value": "1836-01-01T00:00:00Z"},
                    "calendar": {"value": JULIAN},
                }
            ]

        with pytest.raises(UnsupportedDate) as refusal:
            unsupported_in_graph(run, "PREFIX x: <y>\n")
        assert "Q1001 statement Q1001-born holds" in str(refusal.value)
        assert "(calendar Q1985786)" in str(refusal.value)
        (query,) = sent
        assert query.startswith("PREFIX x: <y>\n")
        # every calendar model but the Gregorian, and every year before 1
        assert f"?calendar != <{GREGORIAN}>" in query
        assert 'STRSTARTS(STR(?time), "-")' in query
        assert 'STRSTARTS(STR(?time), "0000-")' in query

    def test_the_graph_check_passes_a_graph_without_one(self):
        unsupported_in_graph(lambda query: [], "")


class TestTheClaimSchema:
    """An extraction claim with an unsupported date is a reported reject."""

    @pytest.mark.parametrize(
        ("value", "reason"),
        [
            ({"type": "time", "year": -1836}, "unsupported: year -1836"),
            ({"type": "time", "year": 0}, "unsupported: year 0"),
            (
                {"type": "time", "year": 1836, "calendar": "julian"},
                "unsupported: calendar 'julian'",
            ),
        ],
    )
    def test_a_claim_with_one_is_rejected_with_its_reason(self, value, reason):
        batch, rejects = salvage_valid_claims([birth(value)], RUN)
        assert batch.claims == []
        (reject,) = rejects
        assert any(reason in problem for problem in reject["problems"])

    def test_a_qualifier_with_one_is_rejected_too(self):
        earliest = {
            "property": "earliest_date",
            "value": {"type": "time", "year": -1836},
        }
        batch, rejects = salvage_valid_claims(
            [birth({"type": "time", "year": 1836}, (earliest,))], RUN
        )
        assert batch.claims == []
        (reject,) = rejects
        assert any("unsupported: year" in p for p in reject["problems"])

    def test_the_same_claim_with_a_supported_date_passes(self):
        batch, rejects = salvage_valid_claims(
            [birth({"type": "time", "year": 1836, "calendar": "gregorian"})],
            RUN,
        )
        assert (len(batch.claims), rejects) == (1, [])


GEDCOM = """\
0 HEAD
1 CHAR UTF-8
0 @I1@ INDI
1 NAME Eugen /Löbig/
1 BIRT
2 DATE {date}
0 TRLR
"""


class TestTheGedcomAdapter:
    """A date before the common era or in any calendar but the Gregorian
    fails the whole file, naming its lines."""

    @pytest.mark.parametrize(
        "date",
        [
            # the Julian calendar, as GEDCOM 5.5.1 and GEDCOM 7 write it
            "@#DJULIAN@ 7 JAN 1836",
            "JULIAN 7 JAN 1836",
            # every other calendar but the Gregorian
            "@#DHEBREW@ 5596",
            "@#DFRENCH R@ 1 VEND 45",
            "@#DROMAN@ 1836",
            "@#DUNKNOWN@ 1836",
            "HEBREW 5596",
            "FRENCH_R 1 VEND 45",
            "_LOEBIG 1836",
            # an epoch before the common era, spaced or not, any case
            "1836 B.C.",
            "1836 B. C.",
            "1836 B C",
            "1836 b.c.",
            "1836 BC",
            "ABT 1836 BCE",
            # a year the grammar reads below 1, either end of a range
            "-1836",
            "0",
            "BET 0 AND 1836",
            "BET 1836 AND 0",
            "FROM 1836 TO 0",
        ],
    )
    def test_a_file_stating_one_fails_naming_the_line(self, date):
        with pytest.raises(UnsupportedDate, match=r"DATE line\(s\) 6,"):
            adapt_gedcom(GEDCOM.format(date=date), MEDIA_REF)

    def test_every_line_stating_one_is_named(self):
        # a BC birth on line 6 and a Julian death on line 8
        text = GEDCOM.format(date="1836 BC").replace(
            "0 TRLR", "1 DEAT\n2 DATE @#DJULIAN@ 16 MAR 1909\n0 TRLR"
        )
        with pytest.raises(UnsupportedDate, match=r"DATE line\(s\) 6, 8,"):
            adapt_gedcom(text, MEDIA_REF)

    def test_the_error_names_lines_never_dates(self):
        with pytest.raises(UnsupportedDate) as refusal:
            adapt_gedcom(
                GEDCOM.format(date="@#DJULIAN@ 7 JAN 1836"), MEDIA_REF
            )
        assert "1836" not in str(refusal.value)

    @pytest.mark.parametrize(
        "date",
        [
            "7 JAN 1836",
            "@#DGREGORIAN@ 7 JAN 1836",
            "ABT 1836",
            "DEC 1836",
            "BET 1836 AND 1840",
            # a date phrase is free text, never read for the markers
            "INT 1836 (Julian the clerk's note)",
            "INT 1836 (BC register of Peiltal)",
            "INT 1836 (per the Brüsrich ledger, page -2)",
            "INT 1836 (Abc)",
        ],
    )
    def test_the_same_file_with_a_supported_date_is_read(self, date):
        result = adapt_gedcom(GEDCOM.format(date=date), MEDIA_REF)
        assert result.batch.entities


class TestTheReviewCorrection:
    """A curator's correction to a year before the common era is refused."""

    CLAIM = Claim.model_validate(birth({"type": "time", "year": 1836}))

    @pytest.mark.parametrize("text", ["0", "0000", "-1836", "-1836-01-07"])
    def test_a_correction_before_the_common_era_is_refused(self, text):
        with pytest.raises(UnsupportedDate, match="unsupported: year"):
            _parse_correction(self.CLAIM, text)

    def test_a_supported_correction_is_taken(self):
        corrected = _parse_correction(self.CLAIM, "1836-01-07")
        assert corrected.value == TimeValue(year=1836, month=1, day=7)


class TestTheWriter:
    """The writer refuses a batch holding one, and writes nothing."""

    @staticmethod
    def batch(value=None, qualifier=None) -> ClaimBatch:
        # a time qualifier the writer's fake vocabulary holds
        earliest = {
            "property": "point_in_time",
            "value": {"type": "time", "year": 1836},
        }
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [birth({"type": "time", "year": 1836}, (earliest,))],
            }
        )
        (claim,) = batch.claims
        if value is not None:
            claim = claim.model_copy(update={"value": value})
        if qualifier is not None:
            (q,) = claim.qualifiers
            claim = claim.model_copy(
                update={
                    "qualifiers": [q.model_copy(update={"value": qualifier})]
                }
            )
        return batch.model_copy(update={"claims": [claim]})

    @pytest.mark.parametrize(
        ("value", "qualifier"),
        [
            (bce(), None),
            (bce(0), None),
            (
                TimeValue.model_construct(
                    type="time",
                    year=1836,
                    month=None,
                    day=None,
                    precision="year",
                    calendar="julian",
                ),
                None,
            ),
            (None, bce()),
        ],
        ids=["value BCE", "value year 0", "value Julian", "qualifier BCE"],
    )
    @pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry"])
    @pytest.mark.filterwarnings("ignore::UserWarning")
    def test_a_batch_with_one_is_refused_before_anything(
        self, value, qualifier, dry_run
    ):
        wiki = FakeWiki({MEDIA_SHA1: "Q23"})
        writer = BatchWriter(wiki, dry_run=dry_run)
        batch = self.batch(value, qualifier)
        with pytest.raises(UnsupportedDate, match="nothing was written") as e:
            writer.write_batch(batch)
        assert wiki.wbi.item.store == {}
        assert wiki.index_reads == 0
        # the claim, by its id and property, and which part holds the date
        (claim,) = batch.claims
        part = (
            "its value" if value is not None else "its point_in_time qualifier"
        )
        assert (
            f"claim {claim_id(claim)} (date_of_birth), {part}: unsupported: "
            in str(e.value)
        )

    @pytest.mark.filterwarnings("ignore::UserWarning")
    def test_each_unsupported_part_of_a_claim_is_named(self):
        message = str(
            pytest.raises(
                UnsupportedDate,
                BatchWriter(FakeWiki({MEDIA_SHA1: "Q23"})).write_batch,
                self.batch(bce(), bce()),
            ).value
        )
        assert message.count("unsupported: year -1836") == 2
        assert "its value: " in message
        assert "its point_in_time qualifier: " in message

    def test_the_same_batch_with_a_supported_date_is_written(self):
        wiki = FakeWiki({MEDIA_SHA1: "Q23"})
        result = BatchWriter(wiki).write_batch(self.batch())
        assert result.written == 1


def report_row(time: str, calendar: str | None, **qualifier) -> dict:
    """One row of the disagreement fetch: Eugen's birth, with its time,
    its calendar model and the qualifier columns given."""
    row = {
        "entity": {"value": EUGEN},
        "statement": {"value": STATEMENT},
        "property": {"value": f"{CONCEPT}/entity/P1"},
        "rank": {"value": NORMAL},
        "value": {"type": "literal", "value": time, "datatype": XSD},
        "precision": {"value": "9"},
    }
    if calendar:
        row["calendar"] = {"value": calendar}
    for column, cell in qualifier.items():
        row[column] = {"value": cell}
    return row


class TestTheDisagreementReport:
    """The report reads each time's calendar and stops at one it does not
    support, naming the entity and the statement."""

    PROPERTIES = {"date_of_birth": "P1"}

    @pytest.mark.parametrize(
        "row",
        [
            report_row("-1836-01-01T00:00:00Z", GREGORIAN),
            report_row("0000-01-01T00:00:00Z", GREGORIAN),
            report_row("1836-01-01T00:00:00Z", JULIAN),
            report_row(
                "1836-01-01T00:00:00Z",
                GREGORIAN,
                earliest="1836-01-01T00:00:00Z",
                earliestPrecision="9",
                earliestCalendar=JULIAN,
            ),
            report_row(
                "1836-01-01T00:00:00Z",
                GREGORIAN,
                latest="-1836-01-01T00:00:00Z",
                latestPrecision="9",
                latestCalendar=GREGORIAN,
            ),
        ],
        ids=[
            "value BCE",
            "value 1 BCE",
            "value Julian",
            "earliest Julian",
            "latest BCE",
        ],
    )
    def test_a_statement_with_one_stops_the_report(self, row):
        with pytest.raises(
            UnsupportedDate, match=r"^Q1001 statement Q1001-born: unsupported"
        ):
            disagreement.read_statements([row], self.PROPERTIES)

    def test_the_same_statement_with_supported_dates_is_read(self):
        row = report_row(
            "1836-01-01T00:00:00Z",
            GREGORIAN,
            earliest="1836-01-01T00:00:00Z",
            earliestPrecision="9",
            earliestCalendar=GREGORIAN,
        )
        (statement,) = disagreement.read_statements([row], self.PROPERTIES)
        assert statement.precision == 9

    def test_a_span_is_never_given_to_one(self):
        with pytest.raises(UnsupportedDate):
            disagreement.period(disagreement.Time("-1836-01-01T00:00:00Z", 9))
        assert disagreement.period(
            disagreement.Time("1836-01-01T00:00:00Z", 9)
        ) == (18360000, 18369999)


class TestTheSite:
    """The site never shows one: format_time refuses it, and the build
    stops, before writing anything, when the graph holds one."""

    @pytest.mark.parametrize("precision", ["7", "8", "9", "10", "11"])
    def test_format_time_refuses_a_date_before_the_common_era(self, precision):
        with pytest.raises(UnsupportedDate):
            format_time("-1836-01-01T00:00:00Z", precision)
        assert format_time("1836-01-01T00:00:00Z", precision)

    @staticmethod
    def builder(tmp_path, found: list[dict]) -> SiteBuilder:
        def run(query: str) -> list[dict]:
            return (
                found
                if "wikibase:timeCalendarModel ?calendar" in query
                else []
            )

        return SiteBuilder(
            SimpleNamespace(concept_uri=CONCEPT),
            tmp_path / "site",
            run=run,
            vocabulary=({}, {}),
        )

    def test_the_build_stops_at_one_in_the_graph(self, tmp_path):
        found = [
            {
                "entity": {"value": EUGEN},
                "statement": {"value": STATEMENT},
                "time": {"value": "1836-01-01T00:00:00Z"},
                "calendar": {"value": JULIAN},
            }
        ]
        with pytest.raises(
            UnsupportedDate, match="Q1001 statement Q1001-born"
        ):
            self.builder(tmp_path, found).build()
        assert not (tmp_path / "site").exists()

    def test_the_build_asks_before_anything_else(self, tmp_path, monkeypatch):
        asked = []

        def check(run, prefixes):
            asked.append(prefixes)
            raise UnsupportedDate("stop")

        monkeypatch.setattr("yizkr.staticsite.unsupported_in_graph", check)
        with pytest.raises(UnsupportedDate, match="stop"):
            self.builder(tmp_path, []).build()
        (prefixes,) = asked
        assert f"PREFIX wd: <{CONCEPT}/entity/>" in prefixes


def birth_row(time: str, calendar: str | None, bound: str = "value") -> dict:
    """A row of the possibly-living check's birth query."""
    row = {
        "person": {"value": EUGEN},
        "statement": {"value": STATEMENT},
        "bound": {"value": bound},
        "time": {"value": time},
        "precision": {"value": "9"},
    }
    if calendar:
        row["calendar"] = {"value": calendar}
    return row


class TestThePrivacyCheck:
    """The possibly-living check stops, failing closed, at a birth it does
    not support: the export and the site stop with it."""

    @pytest.mark.parametrize(
        "rows",
        [
            [birth_row("-1836-01-01T00:00:00Z", GREGORIAN)],
            [birth_row("0000-01-01T00:00:00Z", None)],
            [birth_row("1836-01-01T00:00:00Z", JULIAN)],
            [
                birth_row("1836-01-01T00:00:00Z", GREGORIAN),
                birth_row("1836-01-01T00:00:00Z", JULIAN, "latest"),
            ],
        ],
        ids=["BCE", "1 BCE", "Julian", "latest date Julian"],
    )
    def test_a_birth_with_one_stops_the_check(self, rows):
        with pytest.raises(
            UnsupportedDate,
            match=r"^Q1001 birth statement Q1001-born: unsupported",
        ):
            privacy.latest_births(rows)

    def test_the_same_birth_with_a_supported_date_is_read(self):
        assert privacy.latest_births(
            [birth_row("1836-01-01T00:00:00Z", GREGORIAN)]
        ) == {"Q1001": [1836]}

    def test_the_query_reads_each_calendar(self):
        # one calendar per part of the union: the value and both bounds
        assert privacy.BIRTHS_QUERY.count("wikibase:timeCalendarModel") == 3

    def test_a_year_before_the_common_era_is_never_read(self):
        with pytest.raises(UnsupportedDate):
            privacy.year_of("-1836-01-07T00:00:00Z")
        with pytest.raises(UnsupportedDate):
            privacy.years_allowed("0000-01-01T00:00:00Z", 9)


class TestTheRoster:
    """Reconcile's roster, whose dates are adjudication facts, is never
    read from a graph holding one."""

    def test_the_roster_is_not_read(self, monkeypatch):
        sent = []

        def runner(config):
            def run(query: str) -> list[dict]:
                sent.append(query)
                return [
                    {
                        "entity": {"value": EUGEN},
                        "statement": {"value": STATEMENT},
                        "time": {"value": "-1836-01-01T00:00:00Z"},
                        "calendar": {"value": GREGORIAN},
                    }
                ]

            return run

        def never(*args, **kwargs):
            raise AssertionError("the roster query was sent")

        monkeypatch.setattr(reconcile.sparql, "runner", runner)
        monkeypatch.setattr(urllib.request, "urlopen", never)
        client = SimpleNamespace(vocabulary=never)
        config = SimpleNamespace(
            concept_uri=CONCEPT, sparql_url="http://wdqs.example/sparql"
        )
        with pytest.raises(UnsupportedDate, match="Q1001 statement"):
            reconcile.fetch_roster(client, config)
        (query,) = sent
        assert dates.UNSUPPORTED_QUERY.split("\n")[0] in query


def flat(text: str) -> str:
    """`text` with its whitespace as single spaces."""
    return " ".join(text.split())


class TestTheReaderQueries:
    """What each reader's query fetches: an unprojected calendar reads as
    no calendar, which the check passes, so each is pinned here."""

    def test_the_births_query_projects_each_calendar(self):
        select = privacy.BIRTHS_QUERY.split("\n")[0]
        assert "?calendar" in select.split()
        body = flat(privacy.BIRTHS_QUERY)
        assert body.count("?node wikibase:timeCalendarModel ?calendar") == 3

    def test_the_report_fetches_each_times_calendar(self):
        template = load_templates()[disagreement.FETCH]
        select = flat(
            template[template.index("SELECT") : template.index("WHERE")]
        )
        body = flat(template[template.index("WHERE") :])
        assert "?calendar" in select.split()
        assert "?valueNode wikibase:timeCalendarModel ?calendar" in body
        for column in disagreement.TIME_COLUMNS.values():
            assert f"?{column}Calendar" in select.split()
            assert (
                f"?{column}Node wikibase:timeCalendarModel ?{column}Calendar"
                in body
            )

    def test_the_graph_check_searches_values_qualifiers_and_references(self):
        query = flat(
            dates.UNSUPPORTED_QUERY.format(gregorian=GREGORIAN, limit=1)
        )
        assert (
            "?node wikibase:timeValue ?time ; "
            "wikibase:timeCalendarModel ?calendar ."
        ) in query
        # a statement's value and its qualifiers link it to the node by
        # any predicate, and a reference by any of its own
        assert "{ ?statement ?link ?node . }" in query
        assert (
            "{ ?reference ?link ?node . "
            "?statement prov:wasDerivedFrom ?reference . }"
        ) in query
        assert "?entity ?claim ?statement ." in query

    def test_the_graph_check_says_when_it_lists_its_limit(self):
        row = {
            "entity": {"value": EUGEN},
            "statement": {"value": STATEMENT},
            "time": {"value": "-1836-01-07T00:00:00Z"},
            "calendar": {"value": GREGORIAN},
        }
        with pytest.raises(UnsupportedDate) as one:
            unsupported_in_graph(lambda query: [row], "")
        assert "possibly more" not in str(one.value)
        with pytest.raises(UnsupportedDate) as full:
            unsupported_in_graph(lambda query: [row] * dates.LISTED, "")
        assert "(and possibly more)" in str(full.value)


class TestTheTimeline:
    """The timeline page and its feed refuse a date before the common era,
    naming the person, as a backstop to the build's graph check."""

    ROW = {
        "person": {"value": EUGEN},
        "personLabel": {"value": "Eugen Löbig"},
        "date": {"value": "-1836-01-07T00:00:00Z"},
        "precision": {"value": "11"},
        "what": {"value": "born"},
    }

    @pytest.mark.parametrize("render", ["timeline_page", "timeline_feed"])
    def test_a_row_with_one_is_refused(self, tmp_path, render):
        builder = SiteBuilder(
            SimpleNamespace(concept_uri=CONCEPT),
            tmp_path / "site",
            run=lambda query: [],
            vocabulary=({}, {}),
        )
        with pytest.raises(UnsupportedDate, match="^Q1001 timeline date: "):
            getattr(builder, render)([self.ROW])
        supported = {**self.ROW, "date": {"value": "1836-01-07T00:00:00Z"}}
        assert "1836" in getattr(builder, render)([supported])


class TestTheExport:
    """The non-privileged export stops on an unsupported date anywhere in
    the graph, at any rank, before it assesses anyone."""

    def test_the_export_stops_before_assessing_anyone(self):
        sent = []

        def run(query: str) -> list[dict]:
            sent.append(query)
            if dates.UNSUPPORTED_QUERY.split("\n")[0] in query:
                # Eugen's Julian birth, which a curator deprecated: the
                # births query never reads it, the graph check does
                return [
                    {
                        "entity": {"value": EUGEN},
                        "statement": {"value": STATEMENT},
                        "time": {"value": "1836-01-07T00:00:00Z"},
                        "calendar": {"value": JULIAN},
                    }
                ]
            raise AssertionError("a person was assessed")

        vocabulary = {key: f"P{n}" for n, key in enumerate(PROPERTY_KEYS)}
        with pytest.raises(
            UnsupportedDate, match="Q1001 statement Q1001-born"
        ):
            exclusion_report(
                SimpleNamespace(concept_uri=CONCEPT),
                run=run,
                vocabulary=(vocabulary, {}),
            )
        (query,) = sent
        assert query.startswith(f"PREFIX wd: <{CONCEPT}/entity/>")
