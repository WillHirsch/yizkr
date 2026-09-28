"""Offline tests for the possibly-living determination (ADR-0020;
docs/MODELING.md, "Living persons and privacy"). No stack: the rows of
the births query are given as the query service returns them. A birth
releases a person only at the latest year it allows, and only when every
birth statement does so by the cutoff (#334).

CASES is shared with the public export's tests (test_backup.py) and the
site's (test_staticsite.py), so both paths are held to one table.
"""

from __future__ import annotations

from yizkr.core.privacy import (
    BIRTHS_QUERY,
    CENTURY,
    DECADE,
    MILLENNIUM,
    YEAR,
    is_possibly_living,
    latest_birth_year,
    latest_births,
    year_of,
    years_allowed,
)

CUTOFF = 1903  # as if today were 2026 with the default 123-year window
CONCEPT = "http://wikibase.example"
XSD = "http://www.w3.org/2001/XMLSchema#"


def at(year: int, precision: int = YEAR) -> tuple[str, int]:
    """A time as the query service gives it: its xsd:dateTime (a coarse
    precision's stored year, month and day as 01) and its precision."""
    return f"{year:04d}-01-01T00:00:00Z", precision


# -- rows as the query service returns them ---------------------------------


def uri(qid: str) -> dict:
    return {"type": "uri", "value": f"{CONCEPT}/entity/{qid}"}


def literal(text: str) -> dict:
    return {"type": "literal", "value": text}


def time_cells(time) -> dict:
    """The ?time and ?precision cells of a row: none for an unknown value,
    a blank node's label and no precision for an unknown qualifier."""
    if time is None:
        return {}
    value, precision = time
    if precision is None:
        return {"time": {"type": "bnode", "value": value}}
    return {
        "time": {
            "type": "literal",
            "datatype": f"{XSD}dateTime",
            "value": value,
        },
        "precision": {
            "type": "literal",
            "datatype": f"{XSD}integer",
            "value": str(precision),
        },
    }


def birth_rows(qid: str, births) -> list[dict]:
    """BIRTHS_QUERY's rows for one person: `births` holds each statement
    as (its value's time or None, its earliest dates, its latest dates)."""
    rows = []
    for n, (value, earliest, latest) in enumerate(births):
        statement = {
            "type": "uri",
            "value": f"{CONCEPT}/entity/statement/{qid}-{n}",
        }
        base = {"person": uri(qid), "statement": statement}
        rows.append({**base, "bound": literal("value"), **time_cells(value)})
        for bound, times in (("earliest", earliest), ("latest", latest)):
            for time in times:
                rows.append(
                    {**base, "bound": literal(bound), **time_cells(time)}
                )
    return rows


# Fictional people, each with the birth statements a curator has not
# deprecated, whether the graph holds death evidence, and whether both the
# export and the site must take them for possibly living. Every date lies
# far enough from the cutoff that the call holds for decades to come.
CASES = {
    "Q301": (
        "a 20th-century birth",
        [(at(1901, CENTURY), [], [])],
        False,
        True,
    ),
    "Q302": (
        "a birth in the 1890s",
        [(at(1890, DECADE), [], [])],
        False,
        False,
    ),
    "Q303": (
        "a birth in the 1990s",
        [(at(1990, DECADE), [], [])],
        False,
        True,
    ),
    "Q304": (
        "two births either side of the cutoff",
        [(at(1850), [], []), (at(1990), [], [])],
        False,
        True,
    ),
    "Q305": (
        "two births before the cutoff",
        [(at(1850), [], []), (at(1860), [], [])],
        False,
        False,
    ),
    "Q306": (
        "a birth in the 2nd millennium, no later than 1850",
        [(at(1001, MILLENNIUM), [], [at(1850)])],
        False,
        False,
    ),
    "Q307": (
        "a birth after 1850",
        [(at(1850), [at(1850)], [])],
        False,
        True,
    ),
    "Q308": (
        "a birth between 1850 and 1990",
        [(at(1850), [at(1850)], [at(1990)])],
        False,
        True,
    ),
    "Q309": (
        "a birth before 1850",
        [(at(1850), [], [at(1850)])],
        False,
        False,
    ),
    "Q310": ("a birth of unknown date", [(None, [], [])], False, True),
    "Q311": ("no birth", [], False, True),
    "Q312": ("a recent birth and a death", [(at(1990), [], [])], True, False),
    "Q313": (
        "a birth at century precision stored as a round hundred",
        [(at(1900, CENTURY), [], [])],
        False,
        True,
    ),
    "Q314": (
        "a birth of unknown date, no later than 1850",
        [(None, [], [at(1850)])],
        False,
        False,
    ),
    # a curator's preferred 1990 birth, listed before a normal 1850 one: the
    # query gives no order, so neither the first nor the last birth decides
    "Q315": (
        "two births either side of the cutoff, the later one first",
        [(at(1990), [], []), (at(1850), [], [])],
        False,
        True,
    ),
}
BIRTHS = [
    row for qid, case in CASES.items() for row in birth_rows(qid, case[1])
]
EXCLUDED = {qid for qid, case in CASES.items() if case[3]}


class TestYearOf:
    def test_plain_and_signed_years(self):
        assert year_of("1900-01-01T00:00:00Z") == 1900
        assert year_of("+1862-02-15T00:00:00Z") == 1862

    def test_a_year_past_9999_is_read_whole(self):
        assert year_of("12026-01-01T00:00:00Z") == 12026

    def test_a_year_before_the_common_era_is_not_read(self):
        assert year_of("-0044-03-15T00:00:00Z") is None
        # xsd:dateTime's year 0 is 1 BCE
        assert year_of("0000-01-01T00:00:00Z") is None

    def test_missing_or_malformed_is_none(self):
        assert year_of("") is None
        assert year_of("not-a-date") is None
        assert year_of("1919") is None
        # an unknown value, as a blank node or a skolem IRI
        assert year_of("t42") is None
        assert year_of(f"{CONCEPT}/.well-known/genid/1a2b") is None


class TestYearsAllowed:
    def test_a_year_and_every_finer_precision_allow_the_year(self):
        for precision in range(YEAR, 15):
            assert years_allowed(*at(1919, precision)) == (1919, 1919)

    def test_a_decade_runs_from_its_round_ten(self):
        assert years_allowed(*at(1890, DECADE)) == (1890, 1899)
        assert years_allowed(*at(1895, DECADE)) == (1890, 1899)
        assert years_allowed(*at(1900, DECADE)) == (1900, 1909)

    def test_a_century_is_counted_as_wikibase_counts_it(self):
        # the 19th century is 1801-1900, whichever of its years is stored
        assert years_allowed(*at(1801, CENTURY)) == (1801, 1900)
        assert years_allowed(*at(1850, CENTURY)) == (1801, 1900)
        assert years_allowed(*at(1901, CENTURY)) == (1901, 2000)

    def test_a_round_hundred_is_read_as_the_century_it_opens(self):
        # the last year of the 19th in Wikibase's count, and the 20th
        # century to a writer that names centuries by their round hundred
        assert years_allowed(*at(1900, CENTURY)) == (1900, 1999)

    def test_a_millennium_is_counted_as_wikibase_counts_it(self):
        assert years_allowed(*at(1001, MILLENNIUM)) == (1001, 2000)
        assert years_allowed(*at(1500, MILLENNIUM)) == (1001, 2000)
        assert years_allowed(*at(2000, MILLENNIUM)) == (2000, 2999)

    def test_a_precision_coarser_than_a_millennium_bounds_nothing(self):
        for precision in range(MILLENNIUM):
            assert years_allowed(*at(1850, precision)) is None

    def test_a_precision_wikibase_does_not_have_names_nothing(self):
        assert years_allowed("1850-01-01T00:00:00Z", 15) is None
        assert years_allowed("1850-01-01T00:00:00Z", -1) is None
        assert years_allowed("1850-01-01T00:00:00Z", None) is None

    def test_an_unreadable_time_names_nothing(self):
        assert years_allowed("not-a-date", YEAR) is None
        assert years_allowed("-1850-01-01T00:00:00Z", YEAR) is None


class TestLatestBirthYear:
    def test_a_year(self):
        assert latest_birth_year(at(1919)) == 1919

    def test_a_century_allows_its_last_year(self):
        assert latest_birth_year(at(1901, CENTURY)) == 2000

    def test_a_decade_allows_its_last_year(self):
        assert latest_birth_year(at(1900, DECADE)) == 1909

    def test_a_millennium_allows_its_last_year(self):
        assert latest_birth_year(at(1001, MILLENNIUM)) == 2000

    def test_a_latest_date_bounds_a_coarse_birth(self):
        # the 20th century, no later than 1902
        assert latest_birth_year(at(1901, CENTURY), latest=[at(1902)]) == 1902

    def test_a_stated_range_bounds_the_birth_past_its_first_date(self):
        # "between 1850 and 1990" is stored as 1850 with the two dates
        assert (
            latest_birth_year(at(1850), earliest=[at(1850)], latest=[at(1990)])
            == 1990
        )

    def test_before_a_year(self):
        assert latest_birth_year(at(1900), latest=[at(1900)]) == 1900

    def test_a_range_open_at_its_late_end_bounds_nothing(self):
        # "after 1850" is stored as 1850 with 1850 as its earliest date
        assert latest_birth_year(at(1850), earliest=[at(1850)]) is None

    def test_a_latest_date_allows_its_own_last_year(self):
        assert latest_birth_year(at(1850), latest=[at(1850, DECADE)]) == 1859

    def test_the_latest_of_several_latest_dates_bounds_it(self):
        assert (
            latest_birth_year(at(1850), latest=[at(1860), at(1860, DECADE)])
            == 1869
        )

    def test_an_unknown_latest_date_bounds_nothing(self):
        assert latest_birth_year(at(1850), latest=[("t42", None)]) is None
        assert latest_birth_year(at(1850), latest=[None]) is None
        assert latest_birth_year(at(1850), latest=[at(1860), None]) is None

    def test_an_unknown_value_is_bounded_by_its_latest_date(self):
        assert latest_birth_year(None, latest=[at(1850)]) == 1850

    def test_an_unknown_value_alone_allows_no_latest_year(self):
        assert latest_birth_year(None) is None

    def test_an_unparseable_birth_allows_no_latest_year(self):
        assert latest_birth_year(("1850-01-01T00:00:00Z", 15)) is None
        assert latest_birth_year(("1850-01-01T00:00:00Z", None)) is None
        assert latest_birth_year(("not-a-date", YEAR)) is None

    def test_a_value_after_its_latest_date_is_read_without_it(self):
        assert latest_birth_year(at(1990), latest=[at(1850)]) == 1990
        assert latest_birth_year(at(1901, CENTURY), latest=[at(1850)]) == 2000

    def test_an_earliest_date_after_the_latest_date_leaves_it_open(self):
        assert (
            latest_birth_year(at(1850), earliest=[at(1990)], latest=[at(1850)])
            is None
        )

    def test_an_unknown_earliest_date_leaves_the_latest_date_to_bound_it(self):
        assert (
            latest_birth_year(
                None, earliest=[("t42", None)], latest=[at(1850)]
            )
            == 1850
        )


class TestLatestBirths:
    def test_each_statement_gives_one_year_per_person(self):
        found = latest_births(BIRTHS)
        assert sorted(found["Q304"]) == [1850, 1990]
        assert found["Q301"] == [2000]
        assert found["Q306"] == [1850]
        assert found["Q308"] == [1990]
        assert found["Q314"] == [1850]
        assert found["Q315"] == [1990, 1850]

    def test_a_statement_of_unknown_date_is_kept(self):
        assert latest_births(BIRTHS)["Q310"] == [None]

    def test_an_unknown_latest_date_is_kept(self):
        rows = birth_rows("Q320", [(at(1850), [], [("t42", None)])])
        assert latest_births(rows) == {"Q320": [None]}


class TestIsPossiblyLiving:
    """Runtime determination (ADR-0020): no stored flag, no override —
    only death evidence, or births that all lie beyond the cutoff,
    include a person."""

    def test_unknown_means_excluded(self):
        possibly, reason = is_possibly_living([], False, CUTOFF)
        assert possibly
        assert "unknown means excluded" in reason

    def test_death_evidence_includes(self):
        # a single boolean; the query decides what counts as evidence
        # (date/place/cause of death, burial place, or a death event)
        assert is_possibly_living([], True, CUTOFF)[0] is False
        assert is_possibly_living([1980], True, CUTOFF)[0] is False
        assert is_possibly_living([None], True, CUTOFF)[0] is False

    def test_births_beyond_the_cutoff_include(self):
        possibly, reason = is_possibly_living([1850, 1900], False, CUTOFF)
        assert possibly is False
        assert reason == "born by 1900, beyond the 1903 cutoff"

    def test_a_birth_in_the_cutoff_year_includes(self):
        assert is_possibly_living([CUTOFF], False, CUTOFF)[0] is False

    def test_a_birth_the_year_after_the_cutoff_excludes(self):
        assert is_possibly_living([CUTOFF + 1], False, CUTOFF)[0] is True

    def test_two_births_either_side_of_the_cutoff_exclude(self):
        for births in ([1850, 1990], [1990, 1850]):
            possibly, reason = is_possibly_living(births, False, CUTOFF)
            assert possibly
            assert reason == "born as late as 1990, with no death evidence"

    def test_a_birth_with_no_latest_year_excludes(self):
        possibly, reason = is_possibly_living([1850, None], False, CUTOFF)
        assert possibly
        assert "unknown means excluded" in reason

    def test_recent_birth_without_death_excludes(self):
        assert is_possibly_living([1950], False, CUTOFF)[0] is True


class TestTheBirthsQuery:
    def test_every_statement_is_read_through_its_node_and_rank(self):
        # every birth statement, never one value sampled from the truthy
        # ones, and none a curator deprecated
        assert (
            BIRTHS_QUERY.count("?person p:{date_of_birth} ?statement .") == 3
        )
        assert "wdt:" not in BIRTHS_QUERY
        assert "SAMPLE" not in BIRTHS_QUERY
        assert (
            "FILTER NOT EXISTS {{ ?statement wikibase:rank "
            "wikibase:DeprecatedRank . }}" in BIRTHS_QUERY
        )

    def test_each_row_names_the_part_of_the_statement_it_reads(self):
        for bound in ("value", "earliest", "latest"):
            assert f'BIND("{bound}" AS ?bound)' in BIRTHS_QUERY

    def test_a_statement_with_no_readable_value_still_gives_a_row(self):
        # the value's time is optional: a birth of unknown value, or with no
        # value, still gives its statement a row, which keeps its person
        # possibly living beside any other birth
        assert 'BIND("value" AS ?bound)\n    OPTIONAL {{' in BIRTHS_QUERY

    def test_each_branch_reads_its_own_qualifier(self):
        for key, bound in (
            ("earliest_date", "earliest"),
            ("latest_date", "latest"),
        ):
            assert (
                f"?statement pq:{{{key}}} ?time .\n"
                f'    BIND("{bound}" AS ?bound)\n'
                "    OPTIONAL {{\n"
                f"      wd:{{{key}}} wikibase:qualifierValue"
            ) in BIRTHS_QUERY

    def test_the_rank_filter_holds_for_every_row(self):
        # after the union, so a deprecated statement gives no row at all:
        # neither its value nor its earliest or latest dates
        assert BIRTHS_QUERY.endswith(
            "  }}\n"
            "  FILTER NOT EXISTS {{ ?statement wikibase:rank "
            "wikibase:DeprecatedRank . }}\n"
            "}}\n"
        )
