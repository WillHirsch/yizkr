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
        # birth, Klara's emigration, the arrest, Leopold's and Agnes's births
        assert len(refined) == 6
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

    def test_a_century_is_stored_as_its_first_year(self):
        # the convention claims.py writes: the 19th century is 1800
        assert century(1800) == (1800, 1899)
        assert century(1836) == (1800, 1899)

    def test_a_century_names_a_hundred_years(self):
        # Eugen born in the 19th century (here), stored as 1800, and in 1836
        statements = (
            said(
                "eugen-loebig",
                "date_of_birth",
                Term("literal", "1800-01-01T00:00:00Z", XSD_DATETIME),
                TREE,
                precision=CENTURY,
            ),
            said("eugen-loebig", "date_of_birth", u.Date(1836), GRAVESTONE),
        )
        assert listed(*statements) == []

    def test_a_precision_coarser_than_a_century_names_no_span(self):
        # Eugen's birth given (here) only to the millennium, stored as 1800,
        # beside 1836: a millennium names no span, so the pair is listed
        statements = (
            said(
                "eugen-loebig",
                "date_of_birth",
                Term("literal", "1800-01-01T00:00:00Z", XSD_DATETIME),
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
    ranges their statements state."""

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

    def test_each_reading_of_one_statement_keeps_its_sources(self):
        # a statement that carries two earliest dates puts up a reading of
        # its value for each; given (here) to the millennium, no reading
        # refines another. Alone, its readings come from one artifact.
        stored = Term("literal", "1836-01-01T00:00:00Z", XSD_DATETIME)
        century_bound = Time(stored.value, CENTURY)
        twice = said(
            "eugen-loebig",
            "date_of_birth",
            stored,
            TREE,
            precision=MILLENNIUM,
            earliest=[century_bound, at(u.Date(1909))],
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
            earliest=century_bound,
        )
        (row,) = compare([twice, alike], NOTHING)
        assert row.a.value.earliest == century_bound
        assert set(row.a.sources) == {source(GRAVESTONE), source(TREE)}
        assert row.b.sources == (source(TREE),)


class TestComparedAsStated:
    """compared_as_stated: a residence's or a spouse's earliest and latest
    dates may date either end of it."""

    def residence(self, source_key: str, start: Time, **bounds) -> Statement:
        return said(
            "klara-loebig",
            "residence",
            "juniper-street-31",
            source_key,
            start=start,
            **bounds,
        )

    def test_a_start_beside_a_range_is_compared_as_stated(self):
        # a start in 1938 on a statement whose earliest and latest dates are
        # 1938 and 1939, and a start on 22 June 1938
        ranged = self.residence(
            INTERVIEW,
            at(u.Date(1938)),
            earliest=at(u.Date(1938)),
            latest=at(u.Date(1939)),
        )
        day = self.residence(POSTCARD, at(u.Date(1938, 6, 22)))
        assert listed(ranged, day) == [
            (
                "Klara Löbig",
                "residence",
                "31 Juniper Street",
                "start_time",
                "1938-01-01/9",
                "1938-06-22/11",
            )
        ]
        # with no range on either, the day refines the year
        plain = self.residence(INTERVIEW, at(u.Date(1938)))
        assert listed(plain, day) == []

    def test_times_stored_alike_at_two_precisions_are_compared_as_stated(
        self,
    ):
        # released from prison on 1 October 1939 per the card and (here) in
        # October 1939 per the interview, both stored as 1939-10-01: beside
        # a range they are listed, and without one the day refines the
        # month
        day = said(
            "leopold-loebig",
            "residence",
            "peiltal-court-prison",
            CARD,
            end=at(u.Date(1939, 10, 1)),
            earliest=at(u.Date(1939, 9, 3)),
        )
        month = said(
            "leopold-loebig",
            "residence",
            "peiltal-court-prison",
            INTERVIEW,
            end=at(u.Date(1939, 10)),
        )
        assert listed(day, month) == [
            (
                "Leopold Löbig",
                "residence",
                "Gerichtsgefängnis Peiltal",
                "end_time",
                "1939-10-01/10",
                "1939-10-01/11",
            )
        ]
        unranged = said(
            "leopold-loebig",
            "residence",
            "peiltal-court-prison",
            CARD,
            end=at(u.Date(1939, 10, 1)),
        )
        assert listed(unranged, month) == []

    def test_one_time_stated_alike_is_one_value_whatever_the_range(self):
        # married in 1947 per the tree and (here) "before 1947" per the
        # interview: the start times are stated alike
        bounded = said(
            "leopold-loebig",
            "spouse",
            "agnes-loebig",
            INTERVIEW,
            start=at(u.Date(1947)),
            latest=at(u.Date(1947)),
        )
        plain = said(
            "leopold-loebig",
            "spouse",
            "agnes-loebig",
            TREE,
            start=at(u.Date(1947)),
        )
        assert listed(bounded, plain) == []

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
