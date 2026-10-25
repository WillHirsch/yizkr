"""The site's deaths on the person (ADR-0040 §6): a date of death of
unknown value, the "died by" dates the shared derivation gives, each
union's stated or derived end, a death's place on the timeline, and an
unknown value on an artifact's page.

Every example comes from the fictional universe (docs/EXAMPLES.md): the
1909 burial register's "Witwer der Cäcilie Löbig geb. Tarle", and a
hypothetical "der selige Felix".
"""

from __future__ import annotations

import pytest
import yaml

from tests.unit.test_staticsite import (
    ARTIFACTS,
    RESPONSES,
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
