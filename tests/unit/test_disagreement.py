"""Offline tests for the disagreement report's comparison (#266).

yizkr.disagreement compares what the two library queries fetch, and each
rule of MODELING.md "Contradictions" is one function there (ADR-0033). Each
class below pins one rule on the fictional universe (docs/EXAMPLES.md): the
statements are the universe's own, or plain variations of them with its
dates, built as the fetch lists them. test_queries.py pins the fetch
queries' text, and tests/integration/test_live_disagreement.py the report's
rows on the running stack.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import replace

import pytest
import yaml

from tests.fixtures import example_universe as u
from yizkr import disagreement
from yizkr.disagreement import (
    DEPRECATED,
    OWL_SAME_AS,
    XSD_DATETIME,
    Containment,
    Disagreement,
    Source,
    Statement,
    Term,
    Time,
    century,
    compare,
    counts_by_property,
    name_within,
    read_containment,
    read_statements,
)
from yizkr.model.schema import read_manifest_data

CONCEPT = "http://wikibase.example"
MANIFEST = yaml.safe_load(read_manifest_data())
PROPERTIES = {key: f"P{n}" for n, key in enumerate(MANIFEST["properties"], 1)}
ITEMS = {key: f"Q{n}" for n, key in enumerate(MANIFEST["items"], 1)}
UNIVERSE = u.by_key()
# every person, place, entity and source of the universe has an item
QIDS = {key: f"Q{n}" for n, key in enumerate(sorted(UNIVERSE), 1001)}

WIKIBASE = "http://wikiba.se/ontology#"
NORMAL = WIKIBASE + "NormalRank"
PREFERRED = WIKIBASE + "PreferredRank"
PRECISION = {"decade": 8, "year": 9, "month": 10, "day": 11}
CENTURY, MILLENNIUM = 7, 6
# the 19th century as claims.py writes it, by its first year (#241)
NINETEENTH_CENTURY = Term("literal", "1801-01-01T00:00:00Z", XSD_DATETIME)

EXTRACT = "birth-extract-1938"
NATURALISATION = "naturalisation-1952"
TREE = "family-tree-1984"
INTERVIEW = "interview-2002"
GRAVESTONE = "gravestone-photo-1956"
REGISTER = "burial-register-1909"
CARD = "registration-card-1945"
COMPENSATION = "compensation-file-1953"
POSTCARD = "postcard-1938"
JUDGMENT = "divorce-judgment-1957"


# Duplicates merged into a universe item: a statement written before the
# merge still points at the redirect.
MERGED = {"Q9001": "floerburg", "Q9002": "sebastian-loebig"}


def iri(key: str) -> str:
    """The entity IRI of a universe key, or of a bare QID."""
    return f"{CONCEPT}/entity/{QIDS.get(key, key)}"


def label(key: str) -> str:
    """A universe item's label; a redirect's is its target's, as the fetch
    reads it through the redirect."""
    entry = UNIVERSE.get(MERGED.get(key, key))
    if entry is None:
        return ""
    return getattr(entry, "title", None) or entry.label


def source(key: str, fragment: str | None = None) -> Source:
    """A universe source as a stated-in source; with `fragment`, a page of
    it (its own item, part of the artifact)."""
    if fragment is None:
        return Source(iri(key), label(key), iri(key))
    return Source(iri(fragment), f"{label(key)}, page 1", iri(key))


def stamp(date: u.Date) -> str:
    """A date as the query service returns a stored time: an absent month
    or day is the first."""
    return (
        f"{date.year:04d}-{date.month or 1:02d}-{date.day or 1:02d}T00:00:00Z"
    )


def at(date: u.Date, precision: int | None = None) -> Time:
    return Time(stamp(date), precision or PRECISION[date.precision])


def date_term(date: u.Date) -> Term:
    return Term("literal", stamp(date), XSD_DATETIME)


def stated(date: u.Date) -> tuple[Term, int, dict]:
    """A universe date as the GEDCOM adapter writes it: "between 1938 and
    1939" as 1938 with 1938 and 1939 as its earliest and latest dates,
    "before 1926" as 1926 with a latest date, "after 1945" as 1945 with an
    earliest date; circa is a sourcing circumstance, not a range."""
    bounds: dict[str, frozenset[Time]] = {}
    if date.qualifier == "between":
        bounds = {
            "earliest": frozenset({at(date)}),
            "latest": frozenset({at(date.until)}),
        }
    elif date.qualifier == "before":
        bounds = {"latest": frozenset({at(date)})}
    elif date.qualifier == "after":
        bounds = {"earliest": frozenset({at(date)})}
    return date_term(date), PRECISION[date.precision], bounds


_serial = itertools.count(1)


def said(
    subject: str,
    prop: str,
    value: u.Date | str | Term,
    *sources: str | Source,
    rank: str = NORMAL,
    precision: int | None = None,
    **qualifiers: Iterable[Time] | Time,
) -> Statement:
    """One statement as the fetch lists it. `value` is a universe date, a
    universe key (an item), or a term; `sources` are universe source keys
    or stated-in sources; `qualifiers` are its earliest, latest, start,
    end and point_in_time dates."""
    fields: dict = {}
    value_label = ""
    if isinstance(value, u.Date):
        term, stated_precision, fields = stated(value)
        precision = precision or stated_precision
    elif isinstance(value, Term):
        term = value
    else:
        term, value_label = Term("uri", iri(value)), label(value)
    for name, times in qualifiers.items():
        fields[name] = frozenset([times] if isinstance(times, Time) else times)
    return Statement(
        iri=f"{CONCEPT}/entity/statement/{QIDS.get(subject, subject)}"
        f"-{next(_serial)}",
        entity=iri(subject),
        entity_label=label(subject),
        property=prop,
        rank=rank,
        value=term,
        value_label=value_label,
        precision=precision,
        sources=frozenset(
            s if isinstance(s, Source) else source(s) for s in sources
        ),
        **fields,
    )


def text(value: str, language: str) -> Term:
    return Term("literal", value, lang=language)


def placed(*chains: tuple[str, ...], redirects=()) -> Containment:
    """The recorded containment: each chain inner to outer, and each
    redirect (a merged item's QID, the universe key it resolves to)."""
    return Containment(
        edges=[
            (iri(inner), iri(outer))
            for chain in chains
            for inner, outer in itertools.pairwise(chain)
        ],
        redirects=[(iri(qid), iri(key)) for qid, key in redirects],
    )


NOTHING = Containment()


def listed(*statements: Statement, containment=NOTHING) -> list[tuple]:
    """The report's rows, each as (entity, property, shared place or
    person, qualifier, value A, value B)."""
    return [row_of(row) for row in compare(statements, containment)]


def shown(side) -> str:
    """A value as a row shows it: a time's stored day and precision, an
    item's QID, a name's text."""
    value = side.value
    if value.term.is_time:
        return f"{value.term.value[:10]}/{value.precision}"
    return value.term.value.rsplit("/", 1)[-1]


def row_of(row: Disagreement) -> tuple:
    return (
        row.entity_label,
        row.property,
        row.shared_label,
        row.qualifier,
        shown(row.a),
        shown(row.b),
    )


GUSTAV_BIRTHS = [
    said("gustav-loebig", "date_of_birth", a.value, a.source)
    for a in u.about("gustav-loebig", "date_of_birth")
]
GUSTAV_BORN = (
    "Gustav Löbig",
    "date_of_birth",
    "",
    "",
    "1919-01-07/11",
    "1919-07-01/11",
)


class TestTheUniverse:
    """The whole universe, compared: its one contradiction of a compared
    property is listed, and none of its refinements."""

    @staticmethod
    def statements() -> list[Statement]:
        compared = [
            a for a in u.ASSERTIONS if a.property in disagreement.COMPARED
        ]
        found = []
        for a in compared:
            qualifiers = {
                {"start_time": "start", "end_time": "end"}.get(key, key): at(
                    date
                )
                for key, date in a.qualifiers
                if key in ("start_time", "end_time", "point_in_time")
            }
            value = a.value
            if a.property == "birth_name":
                value = text(value, UNIVERSE[a.source].lang or "en")
            found.append(
                said(a.subject, a.property, value, a.source, **qualifiers)
            )
        return found

    @staticmethod
    def containment() -> Containment:
        return Containment(
            edges=[
                (iri(place.key), iri(parent))
                for place in u.PLACES
                for parent in place.part_of
            ]
        )

    def test_only_the_contradiction_is_listed(self):
        rows = listed(*self.statements(), containment=self.containment())
        assert rows == [GUSTAV_BORN]
        assert ("gustav-loebig", "date_of_birth") in u.CONTRADICTIONS

    def test_no_refinement_is_listed(self):
        rows = compare(self.statements(), self.containment())
        refined = {
            (iri(subject), prop)
            for subject, prop in u.REFINEMENTS
            if prop in disagreement.COMPARED
        }
        # the refinements of a compared property: Eugen's death, Cäcilie's
        # birth, Klara's emigration, the arrest, Leopold's and Agnes's
        # births, and Rudolf's death
        assert len(refined) == 7
        assert not {(row.entity, row.property) for row in rows} & refined


class TestRank:
    """in_contention: rank is an optional overlay (rule 4)."""

    def test_a_preferred_value_is_listed_beside_the_value_it_outranks(self):
        extract, naturalisation = GUSTAV_BIRTHS[:2]
        preferred = said(
            "gustav-loebig",
            "date_of_birth",
            u.Date(1919, 1, 7),
            EXTRACT,
            rank=PREFERRED,
        )
        assert listed(preferred, naturalisation) == [GUSTAV_BORN]
        assert extract.value == preferred.value

    def test_a_deprecated_value_is_left_out(self):
        extract, _, _ = GUSTAV_BIRTHS
        deprecated = said(
            "gustav-loebig",
            "date_of_birth",
            u.Date(1919, 7, 1),
            NATURALISATION,
            rank=DEPRECATED,
        )
        assert listed(extract, deprecated) == []


class TestAttributed:
    """attributed: a value no artifact states takes no part."""

    def test_a_value_with_no_stated_in_source_is_not_listed(self):
        # 7 January per the extract and (here) the tree; 1 July with no
        # stated-in source, as a statement with a run reference alone
        agreed = said(
            "gustav-loebig",
            "date_of_birth",
            u.Date(1919, 1, 7),
            EXTRACT,
            TREE,
        )
        unsourced = said("gustav-loebig", "date_of_birth", u.Date(1919, 7, 1))
        assert listed(agreed, unsourced) == []
        # with a source, the same value is listed
        sourced = said(
            "gustav-loebig",
            "date_of_birth",
            u.Date(1919, 7, 1),
            NATURALISATION,
        )
        assert len(listed(agreed, sourced)) == 1


class TestComparedValues:
    """compared_values and dated_times: which values a statement puts up
    for comparison."""

    def test_names_that_accumulate_are_never_compared(self):
        # Klara's German pet name and her English name, as two names of
        # each kind that accumulates
        for prop in ("nickname", "married_name", "official_name"):
            statements = (
                said("klara-loebig", prop, text("Pünktchen", "de"), POSTCARD),
                said("klara-loebig", prop, text("Clara Lobig", "en"), TREE),
            )
            assert listed(*statements) == [], prop

    def test_two_spouses_or_residences_never_conflict(self):
        # two places of residence, and two spouses (the second a
        # placeholder): values that accumulate over a life
        statements = (
            said(
                "gustav-loebig", "residence", "hawthorn-road-8", NATURALISATION
            ),
            said("gustav-loebig", "residence", "floerburg", EXTRACT),
            said("leopold-loebig", "spouse", "agnes-loebig", JUDGMENT),
            said("leopold-loebig", "spouse", "Q9004", TREE),
        )
        assert listed(*statements) == []

    def test_one_residence_dated_two_ways_is_listed_by_its_start(self):
        # in Australia from 1939 per the naturalisation; from 1938 (here)
        # per the tree
        statements = (
            said(
                "gustav-loebig",
                "residence",
                "australia",
                NATURALISATION,
                start=at(u.Date(1939)),
            ),
            said(
                "gustav-loebig",
                "residence",
                "australia",
                TREE,
                start=at(u.Date(1938)),
            ),
        )
        assert listed(*statements) == [
            (
                "Gustav Löbig",
                "residence",
                "Australia",
                "start_time",
                "1938-01-01/9",
                "1939-01-01/9",
            )
        ]
        (row,) = compare(statements, NOTHING)
        assert row.shared == iri("australia")
        assert row.qualifier_label == "start time"

    def test_a_marriage_is_compared_by_its_start_and_end(self):
        # married 8 February 1947 and divorced 2 January 1957 per the
        # judgment; (here) 1946 and 1957 per the tree: the start disagrees,
        # the end is a refinement
        statements = (
            said(
                "leopold-loebig",
                "spouse",
                "agnes-loebig",
                JUDGMENT,
                start=at(u.Date(1947, 2, 8)),
                end=at(u.Date(1957, 1, 2)),
            ),
            said(
                "leopold-loebig",
                "spouse",
                "agnes-loebig",
                TREE,
                start=at(u.Date(1946)),
                end=at(u.Date(1957)),
            ),
        )
        assert listed(*statements) == [
            (
                "Leopold Löbig",
                "spouse",
                "Agnes Löbig",
                "start_time",
                "1946-01-01/9",
                "1947-02-08/11",
            )
        ]

    def test_a_point_in_time_on_a_residence_is_not_compared(self):
        # Felix at Wasserallee 59 in 1930 per the school report, and (here)
        # in 1912 per the directory: two observations, not a disagreement
        statements = (
            said(
                "felix-loebig",
                "residence",
                "wasserallee-59",
                "school-report-1930",
                point_in_time=at(u.Date(1930)),
            ),
            said(
                "felix-loebig",
                "residence",
                "wasserallee-59",
                "directory-1912",
                point_in_time=at(u.Date(1912)),
            ),
        )
        assert listed(*statements) == []

    def test_a_marriage_dated_only_by_a_point_in_time_is_not_compared(self):
        statements = (
            said(
                "leopold-loebig",
                "spouse",
                "agnes-loebig",
                JUDGMENT,
                point_in_time=at(u.Date(1947, 2, 8)),
            ),
            said(
                "leopold-loebig",
                "spouse",
                "agnes-loebig",
                TREE,
                point_in_time=at(u.Date(1946)),
            ),
        )
        assert listed(*statements) == []

    @pytest.mark.parametrize(
        ("subject", "prop", "shared", "ranged", "bound", "others"),
        [
            # Gustav's father is Sebastian per the extract, whose statement
            # (here) carries an earliest date of 7 January 1919, and per the
            # tree; (here) Felix per the naturalisation
            (
                "gustav-loebig",
                "father",
                "sebastian-loebig",
                (EXTRACT, TREE),
                {"earliest": at(u.Date(1919, 1, 7))},
                ("felix-loebig", NATURALISATION),
            ),
            # Eugen died in Flörburg per the register, whose statement
            # (here) carries a latest date of 16 March 1909, and (here) per
            # the gravestone; in Peiltal per the tree
            (
                "eugen-loebig",
                "place_of_death",
                "floerburg",
                (REGISTER, GRAVESTONE),
                {"latest": at(u.Date(1909, 3, 16))},
                ("peiltal", TREE),
            ),
        ],
    )
    def test_a_range_sets_apart_only_a_time(
        self, subject, prop, shared, ranged, bound, others
    ):
        # a range dates a time: on an item it sets nothing apart, so the
        # value is listed once, with every source that states it
        first, second = ranged
        statements = (
            said(subject, prop, shared, first, **bound),
            said(subject, prop, shared, second),
            said(subject, prop, others[0], others[1]),
        )
        (row,) = compare(statements, NOTHING)
        sides = {shown(side): side for side in (row.a, row.b)}
        assert set(sides[QIDS[shared]].sources) == {
            source(first),
            source(second),
        }
        assert sides[QIDS[shared]].value.earliest is None
        assert sides[QIDS[shared]].value.latest is None
        assert sides[QIDS[others[0]]].sources == (source(others[1]),)

    def test_two_start_times_on_one_residence_are_two_values(self):
        # (here) one statement, cited by the naturalisation and the tree,
        # dates Gustav's residence in Australia from both 1938 and 1939:
        # its two start times are two values, not one claim, and are
        # listed as two statements' would be
        both = said(
            "gustav-loebig",
            "residence",
            "australia",
            NATURALISATION,
            TREE,
            start=[at(u.Date(1938)), at(u.Date(1939))],
        )
        assert listed(both) == [
            (
                "Gustav Löbig",
                "residence",
                "Australia",
                "start_time",
                "1938-01-01/9",
                "1939-01-01/9",
            )
        ]

    def test_one_place_through_a_merge_redirect_is_one_residence(self):
        # the naturalisation's "Floerburg" was a duplicate item, merged
        # into Flörburg: its residence is the same place
        statements = (
            said(
                "gustav-loebig",
                "residence",
                "Q9001",
                NATURALISATION,
                start=at(u.Date(1938)),
            ),
            said(
                "gustav-loebig",
                "residence",
                "floerburg",
                EXTRACT,
                start=at(u.Date(1919, 1, 7)),
            ),
        )
        merged = placed(redirects=[("Q9001", "floerburg")])
        assert listed(*statements, containment=merged) == [
            (
                "Gustav Löbig",
                "residence",
                "Flörburg",
                "start_time",
                "1919-01-07/11",
                "1938-01-01/9",
            )
        ]
        (row,) = compare(statements, merged)
        assert row.shared == iri("floerburg")
        # without the redirect, they are two places
        assert listed(*statements) == []


class TestComparisonKey:
    """comparison_key: a name compares as text; an item through its merge
    redirect."""

    def test_spellings_that_differ_in_case_or_language_are_one_value(self):
        # the registration card writes names in capitals
        statements = (
            said(
                "ida-loebig",
                "birth_name",
                text("Ida Sörne", "de"),
                COMPENSATION,
            ),
            said("ida-loebig", "birth_name", text("IDA SÖRNE", "en"), CARD),
        )
        assert listed(*statements) == []

    def test_one_spelling_is_shown_with_the_sources_of_all(self):
        # and (here) the tree gives her married name as her birth name
        statements = (
            said(
                "ida-loebig",
                "birth_name",
                text("Ida Sörne", "de"),
                COMPENSATION,
            ),
            said("ida-loebig", "birth_name", text("IDA SÖRNE", "en"), CARD),
            said("ida-loebig", "birth_name", text("Ida Löbig", "en"), TREE),
        )
        (row,) = compare(statements, NOTHING)
        assert {shown(row.a), shown(row.b)} == {"IDA SÖRNE", "Ida Löbig"}
        sides = {shown(side): side for side in (row.a, row.b)}
        assert {s.iri for s in sides["IDA SÖRNE"].sources} == {
            iri(COMPENSATION),
            iri(CARD),
        }

    def test_letter_case_is_folded_in_full(self):
        # a sharp s is written SS in capitals: the two spellings differ
        # only in letter case (a placeholder name)
        statements = (
            said(
                "ida-loebig",
                "birth_name",
                text("Fixture Straße", "de"),
                COMPENSATION,
            ),
            said(
                "ida-loebig", "birth_name", text("FIXTURE STRASSE", "en"), CARD
            ),
        )
        assert listed(*statements) == []

    def test_an_item_compares_as_the_item_its_redirect_resolves_to(self):
        # the tree's father is a duplicate of Sebastian, merged into him
        statements = (
            said("gustav-loebig", "father", "sebastian-loebig", EXTRACT),
            said("gustav-loebig", "father", "Q9002", TREE),
        )
        merged = placed(redirects=[("Q9002", "sebastian-loebig")])
        assert listed(*statements, containment=merged) == []
        assert len(listed(*statements)) == 1

    def test_a_row_shows_an_item_as_the_one_its_redirect_resolves_to(self):
        statements = (
            said("gustav-loebig", "father", "Q9002", EXTRACT),
            said("gustav-loebig", "father", "felix-loebig", TREE),
        )
        merged = placed(redirects=[("Q9002", "sebastian-loebig")])
        (row,) = compare(statements, merged)
        assert {row.a.value.term.value, row.b.value.term.value} == {
            iri("sebastian-loebig"),
            iri("felix-loebig"),
        }


class TestSpans:
    """span, period, century, named and spans_nest: a date whose span lies
    inside the other's differs only in resolution."""

    def test_a_date_inside_the_period_its_precision_names_agrees(self):
        # 1909 and 16 March 1909; 1914 and 29 May 1914; c. September 1939
        # and 3 September 1939
        for subject, prop in (
            ("eugen-loebig", "date_of_death"),
            ("leopold-loebig", "date_of_birth"),
            ("arrest-1939", "point_in_time"),
        ):
            statements = [
                said(subject, prop, a.value, a.source)
                for a in u.about(subject, prop)
            ]
            assert len({s.value for s in statements}) > 1, subject
            assert listed(*statements) == [], subject

    def test_dates_whose_periods_do_not_nest_are_listed(self):
        assert listed(*GUSTAV_BIRTHS) == [GUSTAV_BORN]

    def test_a_decade_names_ten_years(self):
        # the studio portrait of the 1890s; (here) 1899 and 1909
        portrait = said(
            "studio-photo-1890s",
            "point_in_time",
            u.Date(1890, decade=True),
            "studio-photo-1890s",
        )
        within = said(
            "studio-photo-1890s", "point_in_time", u.Date(1899), TREE
        )
        after = said("studio-photo-1890s", "point_in_time", u.Date(1909), TREE)
        assert listed(portrait, within) == []
        assert len(listed(portrait, after)) == 1

    def test_a_century_is_counted_as_wikibase_counts_it(self):
        # the 19th century runs from 1801 to 1900 whichever year in it is
        # stored: claims.py writes its first year, and Wikibase stores its
        # last for a curator who types "19. century" (#241)
        assert century(1801) == (1801, 1900)
        assert century(1836) == (1801, 1900)
        assert century(1900) == (1801, 1900)
        # 1800 is the 18th century's last year, 1901 the 20th's first
        assert century(1800) == (1701, 1800)
        assert century(1901) == (1901, 2000)

    def test_a_century_before_the_common_era_is_counted_back(self):
        # the query service gives a century's year as stored, and a year,
        # month or day before the common era one year on, in XSD 1.1's
        # count where 1 BCE is year 0: the 18th century BCE, stored as
        # -1800 to -1701, spans -1799 to -1700 in the count its years
        # arrive in
        assert century(-1800) == (-1799, -1700)
        assert century(-1701) == (-1799, -1700)
        assert century(-1700) == (-1699, -1600)
        # the 1st century BCE ends with 1 BCE, XSD 1.1's year 0
        assert century(-1) == (-99, 0)
        assert century(-100) == (-99, 0)

    def test_a_century_names_a_hundred_years(self):
        # Eugen born in the 19th century (here), stored as its first year,
        # 1801, and in 1836
        statements = (
            said(
                "eugen-loebig",
                "date_of_birth",
                NINETEENTH_CENTURY,
                TREE,
                precision=CENTURY,
            ),
            said("eugen-loebig", "date_of_birth", u.Date(1836), GRAVESTONE),
        )
        assert listed(*statements) == []

    @pytest.mark.parametrize(
        ("date", "inside"),
        [
            # the century's first and last years and days
            (u.Date(1801), True),
            (u.Date(1801, 1, 1), True),
            (u.Date(1900), True),
            (u.Date(1900, 12, 31), True),
            # the years on either side of it
            (u.Date(1800), False),
            (u.Date(1800, 12, 31), False),
            (u.Date(1901), False),
            (u.Date(1901, 1, 1), False),
            # a decade beside it: the 1890s lie inside it, while the
            # decades of 1800 to 1809 and of 1900 to 1909 reach past it
            (u.Date(1890, decade=True), True),
            (u.Date(1800, decade=True), False),
            (u.Date(1900, decade=True), False),
        ],
        ids=str,
    )
    def test_a_century_holds_the_years_wikibase_counts_in_it(
        self, date, inside
    ):
        # Eugen born in the 19th century (here), and on another date
        statements = (
            said(
                "eugen-loebig",
                "date_of_birth",
                NINETEENTH_CENTURY,
                TREE,
                precision=CENTURY,
            ),
            said("eugen-loebig", "date_of_birth", date, GRAVESTONE),
        )
        assert (listed(*statements) == []) is inside

    def test_a_century_stored_as_another_of_its_years_is_the_same_century(
        self,
    ):
        # the 19th century as claims.py writes it, 1801, and as Wikibase
        # stores it for a curator who types "19. century", 1900
        statements = (
            said(
                "eugen-loebig",
                "date_of_birth",
                NINETEENTH_CENTURY,
                TREE,
                precision=CENTURY,
            ),
            said(
                "eugen-loebig",
                "date_of_birth",
                Term("literal", "1900-01-01T00:00:00Z", XSD_DATETIME),
                GRAVESTONE,
                precision=CENTURY,
            ),
        )
        assert listed(*statements) == []

    @pytest.mark.parametrize(
        ("stamp", "inside"),
        [
            # a year before the common era as the query service gives it,
            # one on: 1800 BCE as -1799 and 1701 BCE as -1700 lie inside
            # the 18th century BCE, and 1801 BCE as -1800 and 1700 BCE as
            # -1699 do not
            ("-1799-01-01T00:00:00Z", True),
            ("-1700-01-01T00:00:00Z", True),
            ("-1800-01-01T00:00:00Z", False),
            ("-1699-01-01T00:00:00Z", False),
        ],
    )
    def test_a_century_before_the_common_era_holds_its_years(
        self, stamp, inside
    ):
        # Eugen born (here) in the 18th century BCE, stored as its first
        # year, -1800, and in one year
        statements = (
            said(
                "eugen-loebig",
                "date_of_birth",
                Term("literal", "-1800-01-01T00:00:00Z", XSD_DATETIME),
                TREE,
                precision=CENTURY,
            ),
            said(
                "eugen-loebig",
                "date_of_birth",
                Term("literal", stamp, XSD_DATETIME),
                GRAVESTONE,
                precision=PRECISION["year"],
            ),
        )
        assert (listed(*statements) == []) is inside

    def test_a_precision_coarser_than_a_century_names_no_span(self):
        # Eugen's birth given (here) only to the millennium, stored as 1801,
        # beside 1836: a millennium names no span, so the pair is listed
        statements = (
            said(
                "eugen-loebig",
                "date_of_birth",
                NINETEENTH_CENTURY,
                TREE,
                precision=MILLENNIUM,
            ),
            said("eugen-loebig", "date_of_birth", u.Date(1836), GRAVESTONE),
        )
        assert len(listed(*statements)) == 1
        # at century precision, the same stored year names the century
        assert (
            listed(replace(statements[0], precision=CENTURY), statements[1])
            == []
        )

    def test_a_range_bound_coarser_than_a_century_names_no_span(self):
        # Eugen's death in 1909 per the tree, stated (here) with an earliest
        # date given only to the millennium, beside 16 March 1909: the
        # range names no span, so the pair is listed
        coarse = said(
            "eugen-loebig",
            "date_of_death",
            u.Date(1909),
            TREE,
            earliest=Time("1800-01-01T00:00:00Z", MILLENNIUM),
        )
        register = said(
            "eugen-loebig", "date_of_death", u.Date(1909, 3, 16), REGISTER
        )
        assert len(listed(coarse, register)) == 1
        # given to the century, the same bound names a span that holds it
        century_bound = replace(
            coarse,
            earliest=frozenset({Time("1800-01-01T00:00:00Z", CENTURY)}),
        )
        assert listed(century_bound, register) == []

    def test_a_stated_range_is_the_span(self):
        # Klara left "between 1938 and 1939" per the interview and on 22
        # June 1938 per the postcard; (here) 21 August 1942 per the tree
        interview, postcard = (
            said("emigration-1938", "point_in_time", a.value, a.source)
            for a in sorted(
                u.about("emigration-1938", "point_in_time"),
                key=lambda a: a.source,
            )
        )
        assert interview.earliest and interview.latest
        later = said(
            "emigration-1938", "point_in_time", u.Date(1942, 8, 21), TREE
        )
        assert listed(interview, postcard) == []
        # a day in the range's second year lies inside it too: (here) 3
        # September 1939 per the tree
        second_year = said(
            "emigration-1938", "point_in_time", u.Date(1939, 9, 3), TREE
        )
        assert listed(interview, second_year) == []
        rows = compare([interview, later], NOTHING)
        assert [row_of(row) for row in rows] == [
            (
                "Emigration of Klara Löbig",
                "point_in_time",
                "",
                "",
                "1938-01-01/9",
                "1942-08-21/11",
            )
        ]
        # the range travels with its value
        assert rows[0].a.value.earliest == at(u.Date(1938))
        assert rows[0].a.value.latest == at(u.Date(1939))

    def test_a_stated_range_is_never_widened_back_to_its_precision(self):
        # 1939, narrowed (here) to between September 1939 and 1 October
        # 1939, and August 1939: inside the year, outside the range
        narrowed = said(
            "arrest-1939",
            "point_in_time",
            u.Date(1939),
            CARD,
            earliest=at(u.Date(1939, 9)),
            latest=at(u.Date(1939, 10, 1)),
        )
        august = said(
            "arrest-1939", "point_in_time", u.Date(1939, 8), INTERVIEW
        )
        third = said("arrest-1939", "point_in_time", u.Date(1939, 9, 3), TREE)
        assert len(listed(narrowed, august)) == 1
        assert listed(narrowed, third) == []

    def test_an_open_range_runs_on(self):
        # Rahel died "after 1945" per the tree; (here) in 1946, and in 1941
        after = said(
            "rahel-loebig",
            "date_of_death",
            u.Date(1945, qualifier="after"),
            TREE,
        )
        assert (
            listed(
                after,
                said("rahel-loebig", "date_of_death", u.Date(1946), INTERVIEW),
            )
            == []
        )
        assert (
            len(
                listed(
                    after,
                    said(
                        "rahel-loebig",
                        "date_of_death",
                        u.Date(1941),
                        INTERVIEW,
                    ),
                )
            )
            == 1
        )
        # and the firm, gone "before 1926": (here) gone in 1912, or in 1931
        before = said(
            "zum-blauen-hut",
            "end_time",
            u.Date(1926, qualifier="before"),
            INTERVIEW,
        )
        assert (
            listed(
                before, said("zum-blauen-hut", "end_time", u.Date(1912), TREE)
            )
            == []
        )
        assert (
            len(
                listed(
                    before,
                    said("zum-blauen-hut", "end_time", u.Date(1931), TREE),
                )
            )
            == 1
        )


class TestStoredAlike:
    """stored_alike_order: two dates stored alike are compared through the
    ranges their statements state; one_claim: the readings of one
    statement's value alone are never listed."""

    def test_before_and_after_one_year_are_listed_once(self):
        # Rahel died "after 1945" per the tree and (here) "before 1945" per
        # the interview: both are stored as 1945
        after = said(
            "rahel-loebig",
            "date_of_death",
            u.Date(1945, qualifier="after"),
            TREE,
        )
        before = said(
            "rahel-loebig",
            "date_of_death",
            u.Date(1945, qualifier="before"),
            INTERVIEW,
        )
        assert after.value == before.value
        rows = compare([after, before], NOTHING)
        assert len(rows) == 1
        (row,) = rows
        # in the order of their spans: the one open to the past first
        assert row.a.value.latest == at(u.Date(1945))
        assert row.a.value.earliest is None
        assert row.b.value.earliest == at(u.Date(1945))
        assert {s.iri for s in row.a.sources} == {iri(INTERVIEW)}

    def test_the_same_range_from_two_artifacts_is_one_value(self):
        statements = [
            said(
                "rahel-loebig",
                "date_of_death",
                u.Date(1945, qualifier="after"),
                key,
            )
            for key in (TREE, INTERVIEW)
        ]
        assert listed(*statements) == []

    def test_dates_stored_alike_with_nesting_spans_agree(self):
        # the studio portrait of the 1890s is stored as 1890 whether a
        # source gives the decade, the GEDCOM form "between 1890 and 1899",
        # or (here) the year 1890: each lies within the decade
        decade = said(
            "studio-photo-1890s",
            "point_in_time",
            u.Date(1890, decade=True),
            "studio-photo-1890s",
        )
        between = said(
            "studio-photo-1890s",
            "point_in_time",
            u.Date(1890, qualifier="between", until=u.Date(1899)),
            TREE,
        )
        year = said(
            "studio-photo-1890s", "point_in_time", u.Date(1890), INTERVIEW
        )
        assert decade.value == between.value == year.value
        assert listed(decade, between, year) == []

    def test_below_a_century_the_stated_bounds_set_dates_apart(self):
        # a millennium names no span, so the bounds its statement states
        # tell two such dates apart (Eugen's birth, given here only to the
        # millennium, once with a century as its range)
        stored = Term("literal", "1836-01-01T00:00:00Z", XSD_DATETIME)
        bare = said(
            "eugen-loebig", "date_of_birth", stored, TREE, precision=MILLENNIUM
        )
        bounded = said(
            "eugen-loebig",
            "date_of_birth",
            stored,
            GRAVESTONE,
            precision=MILLENNIUM,
            earliest=Time(stored.value, CENTURY),
            latest=Time(stored.value, CENTURY),
        )
        assert len(listed(bare, bounded)) == 1

    @pytest.mark.parametrize("bound", ["earliest", "latest"])
    def test_each_reading_of_one_statement_keeps_its_sources(self, bound):
        # a statement that carries two earliest (or latest) dates puts up a
        # reading of its value for each; given (here) to the millennium, no
        # reading refines another. Alone, its readings are one claim.
        stored = Term("literal", "1836-01-01T00:00:00Z", XSD_DATETIME)
        century_bound = Time(stored.value, CENTURY)
        twice = said(
            "eugen-loebig",
            "date_of_birth",
            stored,
            TREE,
            precision=MILLENNIUM,
            **{bound: [century_bound, at(u.Date(1909))]},
        )
        assert listed(twice) == []
        # beside another statement stored alike, each reading is compared
        other = said(
            "eugen-loebig",
            "date_of_birth",
            stored,
            GRAVESTONE,
            precision=MILLENNIUM,
        )
        assert len(listed(twice, other)) == 2
        # and beside a statement that states one of its readings, that
        # value is listed with every source that states it: the tree too
        alike = said(
            "eugen-loebig",
            "date_of_birth",
            stored,
            GRAVESTONE,
            precision=MILLENNIUM,
            **{bound: century_bound},
        )
        (row,) = compare([twice, alike], NOTHING)
        assert getattr(row.a.value, bound) == century_bound
        assert set(row.a.sources) == {source(GRAVESTONE), source(TREE)}
        assert row.b.sources == (source(TREE),)

    @pytest.mark.parametrize("bound", ["earliest", "latest"])
    def test_one_statement_is_never_listed_against_itself(self, bound):
        # the statement above, cited (here) by the gravestone too, as the
        # writer adds a second artifact's reference to a statement with the
        # same value and qualifiers: both artifacts state every reading
        stored = Term("literal", "1836-01-01T00:00:00Z", XSD_DATETIME)
        cited_twice = said(
            "eugen-loebig",
            "date_of_birth",
            stored,
            TREE,
            GRAVESTONE,
            precision=MILLENNIUM,
            **{bound: [Time(stored.value, CENTURY), at(u.Date(1909))]},
        )
        assert listed(cited_twice) == []

    def test_readings_whose_ranges_overlap_are_one_claim(self):
        # Eugen's death in 1909 per the register and (here) the tree, on
        # one statement that carries (here) earliest dates of 1899 and 1905
        # and latest dates of 1911 and 1917: its readings from 1899 to
        # 1911 and from 1905 to 1917 overlap without nesting
        ranged = said(
            "eugen-loebig",
            "date_of_death",
            u.Date(1909),
            REGISTER,
            TREE,
            earliest=[at(u.Date(1899)), at(u.Date(1905))],
            latest=[at(u.Date(1911)), at(u.Date(1917))],
        )
        assert listed(ranged) == []
        # stated by two statements, the two readings disagree
        first, second = (
            said(
                "eugen-loebig",
                "date_of_death",
                u.Date(1909),
                key,
                earliest=at(u.Date(start)),
                latest=at(u.Date(end)),
            )
            for key, start, end in ((REGISTER, 1899, 1911), (TREE, 1905, 1917))
        )
        assert len(listed(first, second)) == 1


class TestEachEndItsOwnRange:
    """dated_times: a residence's or a spouse's start and end are dates like
    any other, each with the range its statement states for that end
    (MODELING.md, "Marriages, residences and other periods"): a start runs
    from its earliest date to its latest start date, an end from its
    earliest end date to its latest date, as on Wikidata's spouse (P26)."""

    @staticmethod
    def marriage(source_key: str, **dates) -> Statement:
        """Leopold's marriage to Agnes, dated as `source_key` dates it."""
        return said(
            "leopold-loebig", "spouse", "agnes-loebig", source_key, **dates
        )

    @staticmethod
    def row(qualifier: str, a: str, b: str) -> tuple:
        return ("Leopold Löbig", "spouse", "Agnes Löbig", qualifier, a, b)

    def test_a_start_between_two_dates_holds_a_day_inside_them(self):
        # married on 8 February 1947 per the judgment, and (here) "between
        # 1946 and 1947" per the tree: the day refines the range
        ranged = self.marriage(
            TREE,
            start=at(u.Date(1946)),
            earliest=at(u.Date(1946)),
            latest_start=at(u.Date(1947)),
        )
        day = self.marriage(JUDGMENT, start=at(u.Date(1947, 2, 8)))
        assert listed(ranged, day) == []
        # without its range, 1946 disagrees with the day
        bare = replace(ranged, earliest=frozenset(), latest_start=frozenset())
        assert listed(bare, day) == [
            self.row("start_time", "1946-01-01/9", "1947-02-08/11")
        ]

    def test_an_end_between_two_dates_holds_a_day_inside_them(self):
        # divorced on 2 January 1957 per the judgment, and (here) "between
        # 1956 and 1957" per the tree
        ranged = self.marriage(
            TREE,
            end=at(u.Date(1956)),
            earliest_end=at(u.Date(1956)),
            latest=at(u.Date(1957)),
        )
        day = self.marriage(JUDGMENT, end=at(u.Date(1957, 1, 2)))
        assert listed(ranged, day) == []
        bare = replace(ranged, earliest_end=frozenset(), latest=frozenset())
        assert listed(bare, day) == [
            self.row("end_time", "1956-01-01/9", "1957-01-02/11")
        ]

    def test_a_latest_date_bounds_the_end_never_the_start(self):
        # (here) the tree has them married in 1946 and the marriage over by
        # 1957: its latest date sets no range for the start, which
        # disagrees with the judgment's 8 February 1947
        tree = self.marriage(
            TREE, start=at(u.Date(1946)), latest=at(u.Date(1957))
        )
        judgment = self.marriage(JUDGMENT, start=at(u.Date(1947, 2, 8)))
        assert listed(tree, judgment) == [
            self.row("start_time", "1946-01-01/9", "1947-02-08/11")
        ]

    def test_an_earliest_date_bounds_the_start_never_the_end(self):
        # (here) the tree has the marriage begun no earlier than 1946 and
        # ended in 1956: its earliest date sets no range for the end, which
        # disagrees with the judgment's 2 January 1957
        tree = self.marriage(
            TREE, end=at(u.Date(1956)), earliest=at(u.Date(1946))
        )
        judgment = self.marriage(JUDGMENT, end=at(u.Date(1957, 1, 2)))
        assert listed(tree, judgment) == [
            self.row("end_time", "1956-01-01/9", "1957-01-02/11")
        ]

    def test_a_row_shows_the_range_of_the_end_it_compares(self):
        # (here) the tree dates the marriage "between 1948 and 1949" and has
        # it over by 1957 at the latest: the start row shows the start's own
        # range, beside the judgment's 8 February 1947
        tree = self.marriage(
            TREE,
            start=at(u.Date(1948)),
            earliest=at(u.Date(1948)),
            latest_start=at(u.Date(1949)),
            latest=at(u.Date(1957)),
        )
        judgment = self.marriage(JUDGMENT, start=at(u.Date(1947, 2, 8)))
        (row,) = compare((tree, judgment), NOTHING)
        assert row.qualifier == "start_time"
        assert row.b.sources == (source(TREE),)
        assert row.b.value.earliest == at(u.Date(1948))
        assert row.b.value.latest == at(u.Date(1949))
        assert disagreement.BOUNDS["start_time"] == (
            "earliest_date",
            "latest_start_date",
        )
        assert disagreement.BOUNDS["end_time"] == (
            "earliest_end_date",
            "latest_date",
        )

    @staticmethod
    def dated(prop: str, source_key: str, stored: Time, low, high):
        """A start (on the spouse statement) or a date of birth, stored as
        `stored` and bounded by `low` and `high`."""
        bounds = {}
        if low is not None:
            bounds["earliest"] = low
        if high is not None:
            bounds["latest_start" if prop == "spouse" else "latest"] = high
        if prop == "spouse":
            return said(
                "leopold-loebig",
                "spouse",
                "agnes-loebig",
                source_key,
                start=stored,
                **bounds,
            )
        term = Term("literal", stored.value, XSD_DATETIME)
        return said(
            "leopold-loebig",
            "date_of_birth",
            term,
            source_key,
            precision=stored.precision,
            **bounds,
        )

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            # "before 1947" and "after 1947", both stored as 1947
            (
                (at(u.Date(1947)), None, at(u.Date(1947))),
                (at(u.Date(1947)), at(u.Date(1947)), None),
            ),
            # (here) a day and its month, stored alike
            (
                (at(u.Date(1947, 2, 1)), None, None),
                (at(u.Date(1947, 2)), None, None),
            ),
            # (here) the century and the millennium, stored alike as 1800
            (
                (Time("1800-01-01T00:00:00Z", CENTURY), None, None),
                (Time("1800-01-01T00:00:00Z", MILLENNIUM), None, None),
            ),
        ],
        ids=["before-and-after", "day-and-month", "century-and-millennium"],
    )
    def test_dates_stored_alike_compare_as_any_date_does(self, first, second):
        # a start stored alike with another is told apart by the ranges
        # both statements state, exactly as two dates of birth are
        rows = {
            prop: [
                row[4:]
                for row in listed(
                    self.dated(prop, TREE, *first),
                    self.dated(prop, JUDGMENT, *second),
                )
            ]
            for prop in ("spouse", "date_of_birth")
        }
        assert rows["spouse"] == rows["date_of_birth"]

    def test_a_marriage_dated_by_year_and_by_day_is_a_refinement(self):
        # the judgment's 8 February 1947 and 2 January 1957, the tree's
        # 1947 and 1957
        statements = [
            said(
                "leopold-loebig",
                "spouse",
                "agnes-loebig",
                a.source,
                **{
                    {"start_time": "start", "end_time": "end"}[key]: at(date)
                    for key, date in a.qualifiers
                    if key in ("start_time", "end_time")
                },
            )
            for a in u.about("leopold-loebig", "spouse")
        ]
        assert {s.sources for s in statements} == {
            frozenset({source(JUDGMENT)}),
            frozenset({source(TREE)}),
        }
        assert listed(*statements) == []


class TestPlaces:
    """place_within: a place the graph records inside the other differs
    only in resolution."""

    # the extract registers Gustav's birth at his father's home, Brunnenring
    # 35, and the naturalisation names Flörburg
    BIRTHPLACES = (
        said("gustav-loebig", "place_of_birth", "brunnenring-35", EXTRACT),
        said("gustav-loebig", "place_of_birth", "floerburg", NATURALISATION),
    )
    ADDRESS = ("brunnenring-35", "brunnenring", "floerburg")

    def test_a_place_recorded_inside_the_other_is_a_refinement(self):
        assert (
            listed(*self.BIRTHPLACES, containment=placed(self.ADDRESS)) == []
        )

    def test_without_the_hierarchy_the_two_places_are_listed(self):
        assert listed(*self.BIRTHPLACES) == [
            (
                "Gustav Löbig",
                "place_of_birth",
                "",
                "",
                QIDS["brunnenring-35"],
                QIDS["floerburg"],
            )
        ]

    def test_containment_crosses_merge_redirects(self):
        # the naturalisation's "Floerburg" is a duplicate merged into
        # Flörburg
        statements = (
            self.BIRTHPLACES[0],
            said("gustav-loebig", "place_of_birth", "Q9001", NATURALISATION),
        )
        merged = placed(self.ADDRESS, redirects=[("Q9001", "floerburg")])
        assert listed(*statements, containment=merged) == []

    def test_places_side_by_side_are_listed(self):
        # Eugen died in Flörburg per the register, and (here) in Peiltal:
        # both lie in Kreis Peiltal, neither inside the other
        statements = (
            said("eugen-loebig", "place_of_death", "floerburg", REGISTER),
            said("eugen-loebig", "place_of_death", "peiltal", TREE),
        )
        hierarchy = placed(
            ("floerburg", "kreis-peiltal"), ("peiltal", "kreis-peiltal")
        )
        assert len(listed(*statements, containment=hierarchy)) == 1


class TestNames:
    """name_within: a birth name whose every word the other also writes
    states less of the name, not another one (#388)."""

    def test_a_surname_alone_beside_the_full_name_is_not_listed(self):
        # the card gives only her birth surname; the compensation file the
        # full name
        statements = (
            said("ida-loebig", "birth_name", text("SÖRNE", "en"), CARD),
            said(
                "ida-loebig",
                "birth_name",
                text("Ida Sörne", "de"),
                COMPENSATION,
            ),
        )
        assert listed(*statements) == []
        assert listed(*reversed(statements)) == []

    def test_the_order_and_edge_punctuation_of_the_words_aside(self):
        statements = (
            said("ida-loebig", "birth_name", text("SÖRNE, Ida", "en"), CARD),
            said(
                "ida-loebig",
                "birth_name",
                text("Ida Sörne", "de"),
                COMPENSATION,
            ),
        )
        assert listed(*statements) == []

    def test_a_word_the_other_lacks_is_another_name(self):
        # the tree gives her married name as her birth name
        statements = (
            said("ida-loebig", "birth_name", text("Sörne", "de"), CARD),
            said("ida-loebig", "birth_name", text("Ida Löbig", "en"), TREE),
        )
        assert [row[4:] for row in listed(*statements)] == [
            ("Ida Löbig", "Sörne")
        ]

    def test_a_shared_prefix_is_no_shared_word(self):
        statements = (
            said("ida-loebig", "birth_name", text("Sör", "de"), CARD),
            said("ida-loebig", "birth_name", text("Ida Sörne", "de"), TREE),
        )
        assert len(listed(*statements)) == 1

    def test_accents_are_not_folded(self):
        # two spellings of the surname are two values, as before
        statements = (
            said("ida-loebig", "birth_name", text("Sorne", "de"), CARD),
            said("ida-loebig", "birth_name", text("Ida Sörne", "de"), TREE),
        )
        assert len(listed(*statements)) == 1

    def test_name_within(self):
        assert name_within("Sörne", "Ida Sörne")
        assert name_within("(Ida) Sörne.", "SÖRNE Ida")
        assert not name_within("Ida Sörne", "Sörne")
        assert not name_within("", "Ida Sörne")
        assert not name_within("...", "Ida Sörne")


class TestDifferentArtifacts:
    """from_different_artifacts: two readings of one artifact are not two
    sources (ADR-0023 §4)."""

    def test_a_curators_correction_beside_the_machine_reading(self):
        # the machine read 1 July from the extract; a curator's correction
        # reads 7 January from the same extract
        machine = said(
            "gustav-loebig", "date_of_birth", u.Date(1919, 7, 1), EXTRACT
        )
        curator = said(
            "gustav-loebig", "date_of_birth", u.Date(1919, 1, 7), EXTRACT
        )
        assert listed(machine, curator) == []

    def test_a_page_fragment_counts_as_its_artifact(self):
        page = said(
            "gustav-loebig",
            "date_of_birth",
            u.Date(1919, 7, 1),
            source(EXTRACT, fragment="Q9003"),
        )
        whole = said(
            "gustav-loebig", "date_of_birth", u.Date(1919, 1, 7), EXTRACT
        )
        assert listed(page, whole) == []

    def test_a_value_another_artifact_shares_is_listed(self):
        # the extract states both days, the naturalisation one of them
        statements = (
            said(
                "gustav-loebig", "date_of_birth", u.Date(1919, 1, 7), EXTRACT
            ),
            said(
                "gustav-loebig",
                "date_of_birth",
                u.Date(1919, 7, 1),
                EXTRACT,
                NATURALISATION,
            ),
        )
        assert listed(*statements) == [GUSTAV_BORN]


class TestRows:
    """The rows: each disagreement once, every source of each value, in a
    stable order."""

    def test_each_value_is_listed_with_every_source_that_states_it(self):
        statements = (
            *GUSTAV_BIRTHS[:2],
            said("gustav-loebig", "date_of_birth", u.Date(1919, 1, 7), TREE),
        )
        (row,) = compare(statements, NOTHING)
        assert row.a.sources == (source(EXTRACT), source(TREE))
        assert row.b.sources == (source(NATURALISATION),)
        assert row.property_label == "date of birth"
        assert row.qualifier == row.qualifier_label == ""

    def test_rows_are_ordered_by_entity_property_and_values(self):
        # (here) Eugen's death in 1902 and birth in 1833 per the tree
        statements = (
            *GUSTAV_BIRTHS,
            said(
                "eugen-loebig", "date_of_death", u.Date(1909, 3, 16), REGISTER
            ),
            said("eugen-loebig", "date_of_death", u.Date(1902), TREE),
            said("eugen-loebig", "date_of_birth", u.Date(1836), GRAVESTONE),
            said("eugen-loebig", "date_of_birth", u.Date(1833), TREE),
        )
        assert [row[:2] for row in listed(*statements)] == [
            ("Eugen Löbig", "date_of_birth"),
            ("Eugen Löbig", "date_of_death"),
            ("Gustav Löbig", "date_of_birth"),
        ]

    def test_the_counts_are_aggregates_by_property(self):
        rows = compare(
            [
                *GUSTAV_BIRTHS,
                said(
                    "eugen-loebig", "date_of_birth", u.Date(1836), GRAVESTONE
                ),
                said("eugen-loebig", "date_of_birth", u.Date(1833), TREE),
            ],
            NOTHING,
        )
        assert counts_by_property(rows) == {"date_of_birth": 2}


# -- the fetch's rows ---------------------------------------------------------


def binding(term: Term) -> dict:
    found = {"type": term.kind, "value": term.value}
    if term.datatype:
        found["datatype"] = term.datatype
    if term.lang:
        found["xml:lang"] = term.lang
    return found


def plain(value) -> dict:
    return {"type": "literal", "value": str(value)}


# the fetch's time columns (disagreement-report.rq), by Statement field
FETCHED_TIMES = {
    "earliest": "earliest",
    "latest": "latest",
    "latest_start": "latestStart",
    "earliest_end": "earliestEnd",
    "start": "start",
    "end": "end",
    "point_in_time": "pointInTime",
}


def rows_of(statement: Statement, properties=PROPERTIES) -> list[dict]:
    """A statement as the fetch lists it: a row per stated-in source and
    per further value of a qualifier it carries twice."""
    row = {
        "entity": {"type": "uri", "value": statement.entity},
        "property": {
            "type": "uri",
            "value": f"{CONCEPT}/entity/{properties[statement.property]}",
        },
        "propertyLabel": plain(statement.property.replace("_", " ")),
        "statement": {"type": "uri", "value": statement.iri},
        "rank": {"type": "uri", "value": statement.rank},
        "value": binding(statement.value),
    }
    if statement.entity_label:
        row["entityLabel"] = plain(statement.entity_label)
    if statement.value_label:
        row["valueLabel"] = plain(statement.value_label)
    if statement.precision is not None:
        row["precision"] = plain(statement.precision)
    columns = []
    for name, column in FETCHED_TIMES.items():
        times = sorted(getattr(statement, name))
        columns.append(
            [
                {
                    column: binding(Term("literal", t.value, XSD_DATETIME)),
                    f"{column}Precision": plain(t.precision),
                }
                for t in times
            ]
            or [{}]
        )
    sources = [
        {
            "source": {"type": "uri", "value": s.iri},
            "sourceLabel": plain(s.label),
            "artifact": {"type": "uri", "value": s.artifact},
        }
        for s in sorted(statement.sources)
    ] or [{}]
    return [
        {k: v for part in (row, *parts, cited) for k, v in part.items()}
        for *parts, cited in itertools.product(*columns, sources)
    ]


def containment_rows(
    edges: Iterable[tuple[str, str]] = (),
    redirects: Iterable[tuple[str, str]] = (),
) -> list[dict]:
    link = f"{CONCEPT}/prop/direct/{PROPERTIES['part_of']}"
    return [
        {
            "item": {"type": "uri", "value": item},
            "link": {"type": "uri", "value": predicate},
            "target": {"type": "uri", "value": target},
        }
        for pairs, predicate in ((edges, link), (redirects, OWL_SAME_AS))
        for item, target in pairs
    ]


class TestReading:
    """read_statements and read_containment: the fetch's rows."""

    def test_rows_read_back_into_the_statements_they_list(self):
        statements = [
            *GUSTAV_BIRTHS,
            said(
                "leopold-loebig",
                "spouse",
                "agnes-loebig",
                JUDGMENT,
                TREE,
                start=[at(u.Date(1947, 2, 8)), at(u.Date(1947))],
                end=at(u.Date(1957, 1, 2)),
                point_in_time=at(u.Date(1947, 2, 8)),
                earliest=at(u.Date(1946)),
                latest_start=at(u.Date(1947)),
                earliest_end=at(u.Date(1956)),
                latest=at(u.Date(1957)),
            ),
            said(
                "emigration-1938",
                "point_in_time",
                u.Date(1938, qualifier="between", until=u.Date(1939)),
                INTERVIEW,
                rank=PREFERRED,
            ),
            said("ida-loebig", "birth_name", text("Ida Sörne", "de"), CARD),
            said("gustav-loebig", "date_of_birth", u.Date(1919, 7, 1)),
        ]
        rows = [row for s in statements for row in rows_of(s)]
        # one row per source and per further start time
        assert len(rows) == 3 + 4 + 1 + 1 + 1
        assert read_statements(rows, PROPERTIES) == statements

    def test_a_fragment_is_read_as_part_of_its_artifact(self):
        page = source(EXTRACT, fragment="Q9003")
        (read,) = read_statements(
            rows_of(
                said(
                    "gustav-loebig", "date_of_birth", u.Date(1919, 7, 1), page
                )
            ),
            PROPERTIES,
        )
        (cited,) = read.sources
        assert cited.qid == "Q9003"
        assert cited.artifact == iri(EXTRACT)

    def test_containment_rows_read_edges_and_redirects(self):
        rows = containment_rows(
            edges=[(iri("brunnenring-35"), iri("brunnenring"))],
            redirects=[(iri("Q9001"), iri("floerburg"))],
        )
        containment = read_containment(rows)
        assert containment.canonical(iri("Q9001")) == iri("floerburg")
        assert containment.canonical(iri("floerburg")) == iri("floerburg")
        assert iri("brunnenring") in containment.reachable(
            iri("brunnenring-35")
        )
        assert containment.reachable(iri("brunnenring")) == frozenset()


class TestTheEntryPoint:
    """disagreements: the two library queries, rendered for the instance,
    run and compared."""

    def test_it_runs_both_library_queries_and_compares_their_rows(self):
        sent = []

        def run(query: str) -> list[dict]:
            sent.append(query)
            if "?item ?link ?target" in query:
                return containment_rows(
                    edges=[
                        (iri("brunnenring-35"), iri("brunnenring")),
                        (iri("brunnenring"), iri("floerburg")),
                    ]
                )
            return [
                row
                for s in (*GUSTAV_BIRTHS, *TestPlaces.BIRTHPLACES)
                for row in rows_of(s)
            ]

        rows = disagreement.disagreements(run, PROPERTIES, ITEMS, CONCEPT)
        assert [row_of(row) for row in rows] == [GUSTAV_BORN]
        assert len(sent) == 2
        for query in sent:
            assert f"PREFIX wd: <{CONCEPT}/entity/>" in query
            assert "{P:" not in query and "{Q:" not in query
