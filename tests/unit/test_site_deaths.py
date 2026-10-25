"""The site's deaths on the person (ADR-0040 §6): a date of death of
unknown value, the "died by" dates the shared derivation gives, each
union's stated or derived end, a death's place on the timeline, and an
unknown value on an artifact's page.

Every example comes from the fictional universe (docs/EXAMPLES.md): the
1909 burial register's "Witwer der Cäcilie Löbig geb. Tarle", and a
hypothetical "der selige Felix".
"""

from __future__ import annotations

import re

import pytest
import yaml

from tests.unit.test_staticsite import (
    ARTIFACTS,
    RESPONSES,
    TIMELINE,
    build,
    literal,
    property_id,
    qualifier_cells,
    time_literal,
    uri,
)
from yizkr.model.schema import read_manifest_data

E = "http://wikibase.example/entity/"


@pytest.fixture(scope="module")
def vocabulary():
    """The vocabulary as the site's tests number it from the manifest."""
    raw = yaml.safe_load(read_manifest_data())
    properties = {key: f"P{n}" for n, key in enumerate(raw["properties"], 1)}
    items = {key: f"Q{n}" for n, key in enumerate(raw["items"], 1)}
    return properties, items


GENID = {
    "type": "uri",
    "value": "http://wikibase.example/.well-known/genid/0f3c",
}

PEOPLE = [
    {
        "person": uri("Q7"),
        "personLabel": literal("Eugen Löbig"),
        "deceased": literal("true"),
        "aliases": literal(""),
    },
    {
        "person": uri("Q9"),
        "personLabel": literal("Cäcilie Löbig"),
        "deceased": literal("false"),
        "aliases": literal(""),
    },
    {
        "person": uri("Q11"),
        "personLabel": literal("Felix Löbig"),
        "deceased": literal("true"),
        "aliases": literal(""),
    },
]

# Eugen's dated death, and a death of Felix the burial register states
# without its date (hypothetically, "der selige Felix")
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
        "person": uri("Q11"),
        "prop": uri(property_id("date_of_death")),
        "propLabel": literal("date of death"),
        "st": uri("s2"),
        "value": GENID,
        "sources": literal("Q23~burial register"),
        "qualifierCells": literal(""),
    },
]


def row(kind: str, subject: str, statement: str, **cells) -> dict:
    """A row of died-by.rq, stated in the burial register (Q23)."""
    found = {
        "kind": literal(kind),
        "subject": uri(subject),
        "statement": uri(statement),
        "rank": {
            "type": "uri",
            "value": "http://wikiba.se/ontology#NormalRank",
        },
        "artifact": uri("Q23"),
    }
    for name, value in cells.items():
        found[name] = value if isinstance(value, dict) else literal(value)
    return found


# "Witwer der Cäcilie": the marriage Q40 ended by Cäcilie's death, Eugen
# widowed at his own death; the register dated by its own entry; and
# Felix's death of unknown value
DIED_BY = [
    row("end", "Q40", "d1", value=uri("Q9"), partner=uri("Q7")),
    row("end", "Q40", "d1", value=uri("Q9"), partner=uri("Q9")),
    row(
        "widowed",
        "Q7",
        "d2",
        pointInTime="1909-03-16T00:00:00Z",
        pointInTimePrecision="11",
    ),
    row(
        "inception",
        "Q23",
        "d3",
        time="1909-03-20T00:00:00Z",
        timePrecision="11",
    ),
    row("death", "Q11", "d4", value=GENID),
    row(
        "death",
        "Q7",
        "d5",
        value=time_literal("+1909-03-16T00:00:00Z"),
        time="1909-03-16T00:00:00Z",
        timePrecision="11",
    ),
]

UNIONS = [
    {
        "union": uri("Q40"),
        "unionLabel": literal("marriage of Eugen and Cäcilie Löbig"),
        "partner": uri(partner),
        "partnerLabel": literal(label),
        "endProp": uri(property_id("ended_by_death_of")),
        "end": uri("Q9"),
        "endLabel": literal("Cäcilie Löbig"),
        "endStatement": uri("d1"),
        "source": uri("Q23"),
        "sourceLabel": literal("burial register"),
    }
    for partner, label in (("Q7", "Eugen Löbig"), ("Q9", "Cäcilie Löbig"))
]


def responses(died_by=DIED_BY, unions=UNIONS, statements=STATEMENTS) -> dict:
    return {
        'BIND("death" AS ?kind)': died_by,
        "?endStatement": unions,
        **RESPONSES,
        "GROUP_CONCAT(DISTINCT ?alias": PEOPLE,
        "GROUP_CONCAT(DISTINCT ?sourceLabel": statements,
        "?kind wdt:": ARTIFACTS,
    }


def page(site, qid: str) -> str:
    return (site / f"people/{qid}.html").read_text()


class TestThePersonPage:
    def test_an_unknown_value_shows_as_a_death_with_no_date(
        self, tmp_path, vocabulary
    ):
        site, summary = build(tmp_path, vocabulary, responses())
        text = page(site, "Q11")
        assert (
            "<dt>date of death</dt><dd>died (date not stated)"
            '<span class="source"><a href="../artifacts/Q23.html">'
            "burial register</a></span></dd>" in text
        )
        # never its blank node
        assert "genid" not in text and "0f3c" not in text
        assert summary["unknown_deaths"] == 1

    def test_a_derived_date_is_labelled_as_derived(self, tmp_path, vocabulary):
        # Cäcilie, by Eugen's widowhood on 16 March 1909 (ADR-0040 §2.4)
        site, summary = build(tmp_path, vocabulary, responses())
        text = page(site, "Q9")
        assert "<dt>date of death</dt>" in text
        assert "died by 1909-03-16 (derived)" in text
        assert (
            "the date it presents the survivor as widowed, or else its own"
            in text
        )
        assert '<a href="../artifacts/Q23.html">burial register</a>' in text
        # an undated death gets its document's own date
        assert "died by 1909-03-20 (derived)" in page(site, "Q11")
        assert summary["derived_died_by"] == {
            "undated death": 1,
            "union end": 1,
        }

    def test_a_stated_end_shows_with_its_source(self, tmp_path, vocabulary):
        site, summary = build(tmp_path, vocabulary, responses())
        text = page(site, "Q7")
        assert "<h2>Unions</h2>" in text
        assert (
            "marriage of Eugen and Cäcilie Löbig with "
            '<a href="../people/Q9.html">Cäcilie Löbig</a>' in text
        )
        assert (
            'ended by the death of <a href="../people/Q9.html">Cäcilie '
            "Löbig</a>" in text
        )
        assert summary["unions"] == 1

    def test_an_unstated_end_is_derived_from_the_deaths(
        self, tmp_path, vocabulary
    ):
        # no source states how it ended: Eugen died on 16 March 1909, and
        # Cäcilie's only death is the derived one, so only "no later than"
        unstated = [
            {
                k: v
                for k, v in each.items()
                if not k.startswith(("end", "source"))
            }
            for each in UNIONS
        ]
        rows = [r for r in DIED_BY if r["kind"]["value"] != "end"]
        site, _ = build(tmp_path, vocabulary, responses(rows, unstated))
        text = page(site, "Q7")
        assert "no source states how it ended" in text
        assert "ended no later than Eugen Löbig&#x27;s death (derived)" in text


class TestTheTimeline:
    def test_an_unknown_value_with_no_dated_death_has_no_date_stated(
        self, tmp_path, vocabulary
    ):
        site, _ = build(tmp_path, vocabulary, responses())
        timeline = (site / "timeline.html").read_text()
        foot = timeline[timeline.index("<h2>No date stated</h2>") :]
        assert (
            '<a href="people/Q11.html">Felix Löbig</a>, died (date not stated)'
            in foot
        )

    def test_it_stands_beside_the_dated_death_of_the_same_person(
        self, tmp_path, vocabulary
    ):
        # a GEDCOM `1 DEAT Y` beside the register's dated death (§6)
        rows = [*DIED_BY, row("death", "Q7", "d6", value=GENID)]
        site, _ = build(tmp_path, vocabulary, responses(rows))
        timeline = (site / "timeline.html").read_text()
        dated = timeline.index("1909-03-16")
        beside = timeline.index(
            '<a href="people/Q7.html">Eugen Löbig</a>, died (date not stated)'
        )
        assert dated < beside
        assert "<h2>No date stated</h2>" in timeline
        foot = timeline[timeline.index("<h2>No date stated</h2>") :]
        assert "Q7.html" not in foot


def timeline(site) -> str:
    return (site / "timeline.html").read_text()


def entries(text: str) -> list[str]:
    return re.findall(r'<div class="entry">(.*?)</div>', text)


class TestTheTimelinesDerivedDates:
    """ADR-0040 §2.4, §2.6, §6: the timeline shows the derived "died by"
    dates, labelled as derived, with their sources, and each union's end
    by a death, stated or derived."""

    def test_a_derived_date_stands_among_its_persons_rows(
        self, tmp_path, vocabulary
    ):
        site, _ = build(tmp_path, vocabulary, responses())
        (caecilie,) = [
            e
            for e in entries(timeline(site))
            if e.startswith("1909-03-16 — ") and "people/Q9.html" in e
        ]
        assert "died by 1909-03-16 (derived)" in caecilie
        assert '<a href="artifacts/Q23.html">burial register</a>' in caecilie
        felix = [
            e for e in entries(timeline(site)) if "died by 1909-03-20" in e
        ]
        assert felix and felix[0].startswith("1909-03-20 — ")

    def test_a_stated_union_end_stands_beside_the_death(
        self, tmp_path, vocabulary
    ):
        # Cäcilie's death dated 13 December 1902 by her register
        dated = {
            **TIMELINE[0],
            "person": uri("Q9"),
            "personLabel": literal("Cäcilie Löbig"),
            "date": time_literal("+1902-12-13T00:00:00Z"),
        }
        site, _ = build(
            tmp_path,
            vocabulary,
            {**responses(), 'date of birth"@en': [dated, *TIMELINE]},
        )
        found = entries(timeline(site))
        death = next(
            i for i, e in enumerate(found) if e.startswith("1902-12-13 — ")
        )
        assert found[death + 1].startswith(
            "marriage of Eugen and Cäcilie Löbig, ended by the death of "
            '<a href="people/Q9.html">Cäcilie Löbig</a>'
        )
        assert "burial register" in found[death + 1]

    def test_a_stated_end_with_no_dated_death_has_no_date_stated(
        self, tmp_path, vocabulary
    ):
        site, _ = build(tmp_path, vocabulary, responses())
        text = timeline(site)
        foot = text[text.index("<h2>No date stated</h2>") :]
        assert "ended by the death of" in foot
        assert "ended by the death of" not in text[: text.index("<h2>No")]

    def test_a_derived_end_stands_beside_the_partners_death(
        self, tmp_path, vocabulary
    ):
        unstated = [
            {
                k: v
                for k, v in each.items()
                if not k.startswith(("end", "source"))
            }
            for each in UNIONS
        ]
        rows = [r for r in DIED_BY if r["kind"]["value"] != "end"]
        site, _ = build(tmp_path, vocabulary, responses(rows, unstated))
        found = entries(timeline(site))
        death = next(
            i
            for i, e in enumerate(found)
            if e.startswith("1909-03-16 — ") and "people/Q7.html" in e
        )
        assert found[death + 1] == (
            "marriage of Eugen and Cäcilie Löbig, ended no later than "
            '<a href="people/Q7.html">Eugen Löbig</a>&#x27;s death (derived)'
        )

    def test_a_curators_dated_death_event_is_a_death_entry(
        self, tmp_path, vocabulary
    ):
        # Felix's death a curator makes, dated by its point in time: his
        # date of death of unknown value stands beside it
        event = {
            "person": uri("Q11"),
            "personLabel": literal("Felix Löbig"),
            "eventTypes": literal("death"),
            "date": time_literal("+1931-06-01T00:00:00Z"),
            "source": uri("Q23"),
            "sourceLabel": literal("burial register"),
        }
        site, _ = build(
            tmp_path,
            vocabulary,
            {**responses(), 'date of birth"@en': [*TIMELINE, event]},
        )
        text = timeline(site)
        found = entries(text)
        dated = next(
            i for i, e in enumerate(found) if e.startswith("1931-06-01 — ")
        )
        assert "died (date not stated)" in found[dated + 1]
        assert "people/Q11.html" in found[dated + 1]
        assert (
            "No date stated" not in text
            or "Q11.html" not in text[text.index("<h2>No date stated</h2>") :]
        )

    def divorce(self, tmp_path, vocabulary, dates=None):
        # the Landgericht Rekau's 1957 judgment: Leopold and Agnes's
        # marriage ended by their divorce, each of whose date statements
        # (`dates`, by default one of 2 January 1957 in the court's
        # register, Q24) gives a row for each partner
        people = [
            *PEOPLE,
            *(
                {
                    "person": uri(qid),
                    "personLabel": literal(label),
                    "deceased": literal("false"),
                    "aliases": literal(""),
                }
                for qid, label in (
                    ("Q12", "Leopold Löbig"),
                    ("Q13", "Agnes Löbig"),
                )
            ),
        ]
        unions = [
            {
                "union": uri("Q41"),
                "unionLabel": literal("marriage of Leopold and Agnes Löbig"),
                "partner": uri(partner),
                "partnerLabel": literal(label),
                "endProp": uri(property_id("ended_by")),
                "end": uri("Q42"),
                "endLabel": literal("divorce of Leopold and Agnes Löbig"),
                "endStatement": uri("d9"),
                "source": uri("Q23"),
                "sourceLabel": literal("divorce judgment"),
            }
            for partner, label in (
                ("Q12", "Leopold Löbig"),
                ("Q13", "Agnes Löbig"),
            )
        ]
        if dates is None:
            dates = [("t1", "+1957-01-02T00:00:00Z", {}, "Q24")]
        date_rows = [
            {
                "union": uri("Q41"),
                "end": uri("Q42"),
                "dateStatement": uri(statement),
                "person": uri(partner),
                "personLabel": literal(label),
                "date": time_literal(stamp),
                "precision": literal("11"),
                "low": uri(property_id("earliest_date")),
                "high": uri(property_id("latest_date")),
                "source": uri(source),
                "sourceLabel": literal(f"court register {source}"),
                **{name: literal(value) for name, value in cells.items()},
            }
            for statement, stamp, cells, source in dates
            for partner, label in (
                ("Q12", "Leopold Löbig"),
                ("Q13", "Agnes Löbig"),
            )
        ]
        site, _ = build(
            tmp_path,
            vocabulary,
            {
                "?dateStatement": date_rows,
                **responses(unions=unions),
                "GROUP_CONCAT(DISTINCT ?alias": people,
            },
        )
        return timeline(site)

    def test_a_divorce_stands_at_its_date(self, tmp_path, vocabulary):
        found = entries(self.divorce(tmp_path, vocabulary))
        dated = [e for e in found if e.startswith("1957-01-02 — ")]
        assert {"Q12" if "people/Q12.html" in e else "Q13" for e in dated} == {
            "Q12",
            "Q13",
        }
        for entry in dated:
            # the union's end with its own source, then the date's
            assert (
                "marriage of Leopold and Agnes Löbig, ended by divorce of "
                'Leopold and Agnes Löbig<span class="source">'
                '<a href="artifacts/Q23.html">' in entry
            )
            assert entry.endswith(
                '; its date<span class="source">'
                '<a href="artifacts/Q24.html">court register Q24</a>'
                "</span>"
            )

    def test_every_date_statement_shows_with_its_own_uncertainty(
        self, tmp_path, vocabulary
    ):
        # two sources date the divorce, one of them twice: the court's
        # register on 2 January 1957, and a letter (Q25) "about 1957" and
        # "between 1956 and 1958"; none is picked, whatever its rank
        dates = [
            ("t1", "+1957-01-02T00:00:00Z", {}, "Q24"),
            (
                "t2",
                "+1957-01-02T00:00:00Z",
                {"circumstances": "circa"},
                "Q25",
            ),
            (
                "t3",
                "+1957-01-02T00:00:00Z",
                {
                    "earliest": "1956-01-01T00:00:00Z",
                    "earliestPrecision": "9",
                    "latest": "1958-01-01T00:00:00Z",
                    "latestPrecision": "9",
                },
                "Q25",
            ),
        ]
        found = entries(self.divorce(tmp_path, vocabulary, dates))
        dated = [e for e in found if "ended by divorce" in e]
        assert len(dated) == 6
        leopold = [e for e in dated if "people/Q12.html" in e]
        assert sum("court register Q24" in e for e in leopold) == 1
        assert sum("court register Q25" in e for e in leopold) == 2
        assert any("sourcing circumstances: circa" in e for e in leopold)
        assert any("1956" in e and "1958" in e for e in leopold)

    def test_the_dates_are_every_date_statement_of_the_end(self):
        from yizkr.staticsite import UNION_END_DATES_QUERY

        query = " ".join(UNION_END_DATES_QUERY.split())
        # every point in time at any rank but deprecated, never the best
        # ranked alone
        assert "?end p:{point_in_time} ?dateStatement ." in query
        assert "BestRank" not in query
        assert (
            "FILTER NOT EXISTS {{ ?dateStatement wikibase:rank "
            "wikibase:DeprecatedRank }}" in query
        )
        # each with its own bounds, circumstances and source
        for read in (
            "?lowNode wikibase:timeValue ?earliest",
            "?highNode wikibase:timeValue ?latest",
            "?dateStatement pq:{sourcing_circumstances} ?circumstance",
            "?dateStatement prov:wasDerivedFrom/pr:{stated_in} ?source",
        ):
            assert read in query, read

    def test_an_undated_divorce_has_no_date_stated(self, tmp_path, vocabulary):
        text = self.divorce(tmp_path, vocabulary, dates=[])
        foot = text[text.index("<h2>No date stated</h2>") :]
        assert foot.count("ended by divorce of Leopold and Agnes Löbig") == 2

    def test_hostile_labels_are_escaped(self, tmp_path, vocabulary):
        hostile = [
            {
                **each,
                "unionLabel": literal("<b>marriage</b> & co"),
                "partnerLabel": literal("<i>partner</i>"),
                "endLabel": literal("<i>end</i>"),
                "sourceLabel": literal("<u>source</u>"),
            }
            for each in UNIONS
        ]
        site, _ = build(tmp_path, vocabulary, responses(unions=hostile))
        for text in (timeline(site), page(site, "Q7")):
            assert "<b>" not in text and "<i>" not in text
            assert "<u>" not in text
        assert "&lt;b&gt;marriage&lt;/b&gt; &amp; co" in timeline(site)


class TestAnArtifactPage:
    def test_an_unknown_value_shows_as_what_its_source_states(
        self, tmp_path, vocabulary
    ):
        assertions = [
            {
                "artifact": uri("Q23"),
                "source": uri("Q23"),
                "st": uri("s2"),
                "reference": uri("r2"),
                "subject": uri("Q11"),
                "subjectLabel": literal("Felix Löbig"),
                "propLabel": literal("date of death"),
                "value": GENID,
                "excerpt": {
                    "type": "literal",
                    "value": "der selige Felix",
                    "xml:lang": "de",
                },
            }
        ]
        site, _ = build(
            tmp_path,
            vocabulary,
            {**responses(), "?printedPage": assertions},
        )
        text = (site / "artifacts/Q23.html").read_text()
        assert "<td>died (date not stated)</td>" in text
        assert "genid" not in text


class TestAParentsKind:
    @pytest.mark.parametrize(
        ("kind", "shown"),
        [
            ("adoptive (parentage)", "parentage: adoptive (parentage)"),
            ("foster (parentage)", "parentage: foster (parentage)"),
        ],
    )
    def test_the_kind_shows_beside_the_parent(
        self, tmp_path, vocabulary, kind, shown
    ):
        # a 1950 adoption order's "Adoptivvater" (ADR-0040 §4)
        father = {
            "person": uri("Q11"),
            "prop": uri(property_id("father")),
            "propLabel": literal("father"),
            "st": uri("s9"),
            "value": uri("Q7"),
            "valueLabel": literal("Eugen Löbig"),
            "sources": literal("Q23~burial register"),
            "qualifierCells": literal(
                qualifier_cells(
                    ("parentage", kind, ""),
                    ("kinship_type", "Adoptivvater", ""),
                )
            ),
        }
        site, _ = build(
            tmp_path, vocabulary, responses(statements=[*STATEMENTS, father])
        )
        text = page(site, "Q11")
        assert shown in text
        assert "kinship type: Adoptivvater" in text


def release(person: str, key: str, value: dict, kind: str) -> dict:
    """A row of the releases query: a death statement and the type of its
    source (an item id)."""
    return {
        "person": uri(person),
        "releaseKey": literal(key),
        "value": value,
        "sourceType": uri(kind),
    }


def releases(vocabulary) -> list[dict]:
    """Eugen's dated death and Felix's unknown one, both stated in a
    certificate (hypothetically, the register's extract)."""
    certificate = vocabulary[1]["certificate"]
    return [
        release(
            "Q7",
            "date_of_death",
            time_literal("+1909-03-16T00:00:00Z"),
            certificate,
        ),
        release("Q11", "date_of_death", GENID, certificate),
    ]


class TestThePrivacyRecord:
    """The run record's releases by cause (ADR-0040 §7), counted on the
    written graph as the site's badge decides them."""

    def record(
        self,
        tmp_path,
        vocabulary,
        people=PEOPLE,
        rows=None,
        ended=("Q9", "Q7"),
    ):
        _, summary = build(
            tmp_path,
            vocabulary,
            {
                "?releaseKey": releases(vocabulary) if rows is None else rows,
                # "Witwer der Cäcilie": the marriage ended by her death;
                # and, hypothetically, a second marriage ended by Eugen's
                "?named .": [{"person": uri(qid)} for qid in ended],
                **responses(),
                "GROUP_CONCAT(DISTINCT ?alias": people,
                # every artifact typed a certificate
                "?kind wdt:": [
                    {**each, "kind": uri(vocabulary[1]["certificate"])}
                    for each in ARTIFACTS
                ],
            },
        )
        return summary["privacy"]

    def test_each_release_counts_under_its_cause(self, tmp_path, vocabulary):
        record = self.record(tmp_path, vocabulary)
        assert record["people"] == 3
        assert record["possibly_living"] == 0
        assert record["released"] == 3
        assert record["releases"] == {
            "date_of_death": 1,
            "unknown_date_of_death": 1,
            "age_at_death": 0,
            "unknown_burial_place": 0,
            "other_death_property": 0,
            "ended_by_death_of": 2,
            "death_event": 0,
            "birth_cutoff": 0,
        }
        # Cäcilie has no death statement: the union end alone releases
        # her, and Eugen has his dated one besides
        assert record["ended_by_death_of_alone"] == 1
        assert record["unknown_date_of_death_by_source"] == {"certificate": 1}
        # her bound and Felix's, both resting on the register (Q23)
        assert record["derived_by_source"] == {"certificate": 2}

    def test_a_source_of_no_stated_type(self, tmp_path, vocabulary):
        row = {**releases(vocabulary)[1]}
        del row["sourceType"]
        record = self.record(tmp_path, vocabulary, rows=[row])
        assert record["unknown_date_of_death_by_source"] == {
            "no type stated": 1
        }

    def test_a_release_with_no_death_evidence_is_the_birth_cutoff(
        self, tmp_path, vocabulary
    ):
        # Eugen, born 1836, with no death statement on the graph
        people = [
            {**PEOPLE[0], "deceased": literal("false")},
            *PEOPLE[1:],
        ]
        record = self.record(
            tmp_path, vocabulary, people, releases(vocabulary)[1:], ("Q9",)
        )
        assert record["releases"]["birth_cutoff"] == 1
        assert record["releases"]["date_of_death"] == 0
        assert record["released"] == 3

    def test_an_unknown_burial_place_and_other_properties(
        self, tmp_path, vocabulary
    ):
        certificate = vocabulary[1]["certificate"]
        rows = [
            release("Q7", "burial_place", GENID, certificate),
            release("Q7", "place_of_death", uri("Q50"), certificate),
            release("Q11", "age_at_death", literal("72"), certificate),
        ]
        record = self.record(tmp_path, vocabulary, rows=rows)
        assert record["releases"]["unknown_burial_place"] == 1
        assert record["releases"]["other_death_property"] == 1
        assert record["releases"]["age_at_death"] == 1

    def test_a_possibly_living_person_is_no_release(
        self, tmp_path, vocabulary
    ):
        _, summary = build(tmp_path, vocabulary, responses())
        record = summary["privacy"]
        # Cäcilie, with no union end read and no death statement
        assert record["possibly_living"] == 1
        assert record["released"] == 2
        # and the fixture's artifacts give no type
        assert record["derived_by_source"] == {"no type stated": 2}

    def test_a_class_the_manifest_does_not_hold_is_other(
        self, tmp_path, vocabulary
    ):
        row = {**releases(vocabulary)[1], "sourceType": uri("Q99999")}
        record = self.record(tmp_path, vocabulary, rows=[row])
        assert record["unknown_date_of_death_by_source"] == {"other": 1}
