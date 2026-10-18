"""Offline tests for the possibly-living determination (ADR-0020;
docs/MODELING.md, "Living persons and privacy"). No stack: the rows of
the births query are given as the query service returns them. A birth
releases a person only at the latest year it allows, and only when every
birth statement does so by the cutoff (#334). The check assesses every
item with person evidence, read at every rank, so no type hides a person
from it (#344).

CASES is shared with the non-privileged export's tests (test_backup.py) and the
site's (test_staticsite.py), so both paths are held to one table.
"""

from __future__ import annotations

import re

import yaml

from yizkr.core.privacy import (
    BIRTH_EVENT_DATES_QUERY,
    BIRTHS_QUERY,
    CENTURY,
    DECADE,
    FAMILY_CLASS,
    MILLENNIUM,
    NON_PERSON_CLASSES,
    PARTICIPANTS_QUERY,
    PEOPLE_SELECTION,
    PERSON_CLASS,
    PERSON_EVENTS,
    PERSON_SUBJECT_PROPERTIES,
    PERSON_VALUE_PROPERTIES,
    PRINCIPAL_ROLE,
    UNKNOWN,
    YEAR,
    Participation,
    birth_event_dates_query,
    event_principals,
    is_possibly_living,
    latest_birth_year,
    latest_births,
    participants_query,
    people_selection,
    principals_by_event,
    read_evidence,
    year_of,
    years_allowed,
)
from yizkr.model.schema import read_manifest_data

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

WIKIBASE = "http://wikiba.se/ontology#"


def participant_rows(event: str, kind: str, parts) -> list[dict]:
    """PARTICIPANTS_QUERY's rows for one event: `parts` holds each
    participant statement as (its value's QID, "" for no value or "?" for
    an unknown one; its roles; its rank, by default normal; whether its
    value is typed a non-person)."""
    rows = []
    for n, (value, roles, *rest) in enumerate(parts):
        rank = rest[0] if rest else "NormalRank"
        non_person = rest[1] if len(rest) > 1 else False
        base = {
            "event": uri(event),
            "eventKind": literal(kind),
            "part": {
                "type": "uri",
                "value": f"{CONCEPT}/entity/statement/{event}-p{n}",
            },
            "partRank": {"type": "uri", "value": f"{WIKIBASE}{rank}"},
            "nonPerson": literal("true" if non_person else "false"),
        }
        if value == "?":
            base["value"] = {"type": "bnode", "value": f"b{event}{n}"}
        elif value:
            base["value"] = uri(value)
        for role in roles or [None]:
            row = dict(base)
            if role is not None:
                row["role"] = (
                    {"type": "bnode", "value": "unknown"}
                    if role == "?"
                    else uri(f"role-{role}")
                )
                row["principal"] = literal(
                    "true" if role == "principal" else "false"
                )
            rows.append(row)
    return rows


# Fictional birth and death events, content-marked by their QIDs (the
# Q32x range), each with its participant statements and, for a birth, its
# date: who takes part, in which role.
EVENTS = {
    # Florence's death, with an informant who was born in 1990
    "Q320": ("death", [("Q321", ["principal"]), ("Q322", ["informant"])]),
    # a death that states no role and has one participant
    "Q323": ("death", [("Q324", [])]),
    # a death that states no role and has two
    "Q325": ("death", [("Q326", []), ("Q327", [])]),
    # a birth in 1990, with its principal and an informant
    "Q328": ("birth", [("Q329", ["principal"]), ("Q331", ["informant"])]),
}
EVENT_DATES = {"Q328": [(at(1990), [], [])]}
# The people of EVENTS, as CASES gives a person: each one's own birth
# statements, and whether both the export and the site must take them for
# possibly living. None has death evidence of their own.
EVENT_CASES = {
    "Q321": ("the principal of a death", [(at(1990), [], [])], False),
    "Q322": (
        "an informant at someone else's death",
        [(at(1990), [], [])],
        True,
    ),
    "Q324": (
        "the one participant of a role-less death",
        [(at(1990), [], [])],
        False,
    ),
    "Q326": ("one of two in a role-less death", [(at(1990), [], [])], True),
    "Q327": (
        "the other of two in a role-less death",
        [(at(1990), [], [])],
        True,
    ),
    "Q329": (
        "born 1850, and the principal of a birth in 1990",
        [(at(1850), [], [])],
        True,
    ),
    "Q331": (
        "born 1850, and an informant at a birth in 1990",
        [(at(1850), [], [])],
        False,
    ),
}
EVENT_PARTICIPANTS = [
    row
    for event, (kind, parts) in EVENTS.items()
    for row in participant_rows(event, kind, parts)
]
EVENT_BIRTHS = [
    row
    for event, births in EVENT_DATES.items()
    for row in birth_rows(event, births)
]
EVENT_PEOPLE_BIRTHS = [
    row
    for qid, case in EVENT_CASES.items()
    for row in birth_rows(qid, case[1])
]
EVENT_EXCLUDED = {qid for qid, case in EVENT_CASES.items() if case[2]}


class TestYearOf:
    def test_plain_and_signed_years(self):
        assert year_of("1900-01-01T00:00:00Z") == 1900
        assert year_of("+1862-02-15T00:00:00Z") == 1862

    def test_a_year_past_9999_is_read_whole(self):
        assert year_of("12026-01-01T00:00:00Z") == 12026

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


MANIFEST = yaml.safe_load(read_manifest_data())
# the instance's ids as the unit tests give them: P1.. and Q1.., in the
# manifest's order
PROPERTIES = {key: f"P{n}" for n, key in enumerate(MANIFEST["properties"], 1)}
ITEMS = {key: f"Q{n}" for n, key in enumerate(MANIFEST["items"], 1)}


def flat(query: str) -> str:
    """A query with each run of whitespace as one space."""
    return " ".join(query.split())


# Person evidence as ADR-0020 (§4, amended by #344) and MODELING.md list
# it: each list is pinned here, not built from the module's own tuples, so
# that dropping an entry fails a test (test_backup.py marks a person with
# each one in turn).
DOCUMENTED_SUBJECT_EVIDENCE = (
    "date_of_birth",
    "date_of_death",
    "place_of_birth",
    "place_of_death",
    "burial_place",
    "cause_of_death",
    "father",
    "mother",
    "spouse",
    "child",
    "sibling",
    "relative",
    "birth_name",
    "married_name",
    "nickname",
    "name",
    "referred_to_as",
    "sex_or_gender",
    "number_of_children",
    "occupation",
    "employer",
    "educated_at",
    "country_of_citizenship",
    "residence",
)
DOCUMENTED_VALUE_EVIDENCE = (
    "father",
    "mother",
    "spouse",
    "child",
    "sibling",
    "relative",
    "gave_refuge_to",
)
DOCUMENTED_PERSON_EVENTS = (
    "birth",
    "death",
    "marriage",
    "migration",
    "military_service",
    "deportation",
    "imprisonment",
    "release",
)
DOCUMENTED_NON_PERSON_CLASSES = ("vessel", "military_unit", "organization")


class TestThePeopleSelection:
    """Who the check assesses (#344): every item with person evidence,
    read through statement nodes at every rank, so that a deprecated or
    outranked `human` type hides no one. A vessel, a military unit or an
    organization is no person on its part in a person event alone."""

    selection = flat(people_selection(PROPERTIES, ITEMS))
    typing = PROPERTIES["instance_of"]
    # a type, read through the redirects it may name
    typed = f"ps:{typing}/<http://www.w3.org/2002/07/owl#sameAs>*"
    non_person = " ".join(f"wd:{ITEMS[key]}" for key in NON_PERSON_CLASSES)
    # the type that sets a participant aside: one not deprecated
    set_aside = (
        "FILTER NOT EXISTS { "
        f"VALUES ?otherKind {{ {non_person} }} "
        f"?person p:{typing} ?otherTyping . "
        f"?otherTyping {typed} ?otherKind ; "
        "wikibase:rank ?otherRank . "
        "FILTER(?otherRank != wikibase:DeprecatedRank) }"
    )

    def test_people_are_read_through_statement_nodes_at_every_rank(self):
        # never the truthy values, which hold only the best-ranked type,
        # and no rank filter on any evidence, so a deprecated statement
        # counts too: only the type that sets a participant aside is read
        # with its rank
        assert "wdt:" not in PEOPLE_SELECTION
        assert "rank" not in self.selection.replace(self.set_aside, "")
        typing = self.typing
        assert (
            f"{{ ?person p:{typing} ?typing . "
            f"?typing {self.typed} wd:{ITEMS['human']} . }} UNION"
        ) in self.selection

    def test_each_kind_of_evidence_selects(self):
        subjects = " ".join(
            f"p:{PROPERTIES[key]}" for key in PERSON_SUBJECT_PROPERTIES
        )
        # a person-valued property marks its value in a statement and in
        # a qualifier alike
        values = " ".join(
            f"{prefix}:{PROPERTIES[key]}"
            for key in PERSON_VALUE_PROPERTIES
            for prefix in ("ps", "pq")
        )
        events = " ".join(f"wd:{ITEMS[key]}" for key in PERSON_EVENTS)
        typing, participant = (
            PROPERTIES["instance_of"],
            PROPERTIES["participant"],
        )
        assert (
            f"{{ VALUES ?link {{ {subjects} }} ?person ?link ?statement . }}"
        ) in self.selection
        assert (
            f"{{ VALUES ?link {{ {values} }} ?statement ?link ?person . }}"
        ) in self.selection
        assert (
            f"{{ VALUES ?kind {{ {events} }} "
            f"?event p:{typing} ?eventTyping . "
            f"?eventTyping {self.typed} ?kind . "
            f"?event p:{participant} ?participation . "
            f"?participation ps:{participant} ?person . "
            f"{self.set_aside} }}"
        ) in self.selection

    def test_every_type_is_read_through_its_redirects(self):
        # a class a curator merged into `human`, an event kind or a
        # non-person class types as the one it redirects to
        assert self.selection.count(self.typed) == 3

    def test_a_non_person_type_sets_only_a_participant_aside(self):
        # once, in the participant's part of the union, and after it the
        # union closes: the other evidence selects whatever the type
        assert self.selection.count(self.set_aside) == 1
        assert f"{self.set_aside} }} }} }} FILTER(REGEX" in self.selection

    def test_an_unknown_value_or_a_redirect_is_no_person(self):
        # an unknown value is a blank node or a skolem IRI, never an
        # item; a redirect has no statements, so its target is selected.
        # Each item is tested once, after DISTINCT.
        assert (
            "SELECT ?person WHERE { { SELECT DISTINCT ?person WHERE {"
        ) in self.selection
        assert self.selection.endswith(
            '} } FILTER(REGEX(STR(?person), "/entity/Q[1-9][0-9]*$")) '
            "FILTER NOT EXISTS { "
            "?person <http://www.w3.org/2002/07/owl#sameAs> ?target . "
            "} } }"
        )

    def test_every_id_is_filled_in(self):
        assert not re.search(r"\{[a-z_]+\}", self.selection)

    def test_the_evidence_is_the_documented_lists(self):
        assert PERSON_CLASS == "human"
        # every family stays out of anything public (owner, 2026-09-29)
        assert FAMILY_CLASS == "family"
        assert PERSON_SUBJECT_PROPERTIES == DOCUMENTED_SUBJECT_EVIDENCE
        assert PERSON_VALUE_PROPERTIES == DOCUMENTED_VALUE_EVIDENCE
        assert PERSON_EVENTS == DOCUMENTED_PERSON_EVENTS
        assert NON_PERSON_CLASSES == DOCUMENTED_NON_PERSON_CLASSES

    def test_the_evidence_is_the_manifests_vocabulary(self):
        assert PERSON_CLASS in MANIFEST["items"]
        assert set(NON_PERSON_CLASSES) <= set(MANIFEST["items"])
        for key in (*PERSON_SUBJECT_PROPERTIES, *PERSON_VALUE_PROPERTIES):
            assert MANIFEST["properties"][key]["datatype"]
        # a marriage is a partnership, not an event (ADR-0036); its
        # participants are people all the same
        for key in PERSON_EVENTS:
            assert MANIFEST["items"][key]["subclass_of"] in (
                "event",
                "partnership",
            )
        for key in PERSON_VALUE_PROPERTIES:
            assert MANIFEST["properties"][key]["datatype"] == "wikibase-item"

    def test_places_artifacts_and_vocabulary_are_no_evidence(self):
        # the value of a property that names a place, an organization, an
        # artifact or a vocabulary item is never a person, and names or
        # ties that places and organizations share mark no one; nor does
        # an identity judgement, a depiction or an appellation's
        # perspective
        for key in (
            "instance_of",
            "stated_in",
            "part_of",
            "place_of_birth",
            "place_of_death",
            "burial_place",
            "residence",
            "location",
            "country_of_citizenship",
            "sex_or_gender",
            "occupation",
            "employer",
            "educated_at",
            "member_of",
            "depicts",
            "image",
            "participant",
            "said_to_be_the_same_as",
            "perspective",
        ):
            assert key not in PERSON_VALUE_PROPERTIES
        for key in (
            "official_name",
            "religion",
            "member_of",
            "location",
            "part_of",
            "coordinate_location",
            "said_to_be_the_same_as",
        ):
            assert key not in PERSON_SUBJECT_PROPERTIES
        # any group may take part in an event of no person kind; a family
        # can be a person known by a surname alone, and the unclassified
        # entity is where a mistyped person lands
        assert "event" not in PERSON_EVENTS
        assert "family" not in NON_PERSON_CLASSES
        assert "unclassified_entity" not in NON_PERSON_CLASSES
        assert PERSON_CLASS not in NON_PERSON_CLASSES


# -- birth and death events: whose own each is (#345) -----------------------


def part(value, *roles, rank="NormalRank", non_person=False, n=0):
    """A participant statement: `roles` each "principal" or another role."""
    return Participation(
        statement=f"s{n}-{value}",
        value=value,
        rank=rank,
        roles=frozenset((role, role == "principal") for role in roles),
        non_person=non_person,
    )


class TestEventPrincipals:
    """A birth or death event is its principal's own (ADR-0035 §3.2), and
    no one else's: an informant or a witness at someone else's death is not
    released by it (#345)."""

    def own(self, *parts, best_rank_only=True):
        return event_principals(parts, best_rank_only=best_rank_only)

    def test_the_principal_alone_counts(self):
        assert self.own(
            part("Q321", "principal"), part("Q322", "informant")
        ) == {"Q321"}

    def test_an_informant_or_a_witness_never_counts(self):
        assert self.own(part("Q322", "informant")) == set()
        assert self.own(part("Q322", "witness")) == set()

    def test_a_role_beside_the_principal_takes_it_away(self):
        # a statement that also states another role is not read as the
        # principal's: when in doubt, possibly living
        assert self.own(part("Q321", "principal", "informant")) == set()

    def test_an_unknown_role_is_another_role(self):
        unknown = Participation("s", "Q321", roles=frozenset({("_:b", False)}))
        assert self.own(unknown) == set()

    def test_a_role_less_event_with_one_participant_is_theirs(self):
        assert self.own(part("Q324")) == {"Q324"}

    def test_a_role_less_event_with_two_participants_is_no_ones(self):
        assert self.own(part("Q326"), part("Q327")) == set()

    def test_a_role_on_any_participant_makes_the_roles_decide(self):
        # one participant states a role, so the other, stating none, is not
        # taken for the event's one participant
        assert self.own(part("Q326"), part("Q327", "informant")) == set()

    def test_an_unknown_participant_is_someone_else(self):
        assert self.own(part("Q324"), part(UNKNOWN, n=1)) == set()

    def test_two_unknown_participants_are_two(self):
        assert self.own(part(UNKNOWN, n=1), part(UNKNOWN, n=2)) == set()

    def test_a_statement_of_no_value_names_no_one(self):
        assert self.own(part("Q324"), part("", n=1)) == {"Q324"}

    def test_a_non_person_is_no_other_participant(self):
        # a vessel in someone's death at sea leaves it theirs
        assert self.own(part("Q324"), part("Q340", non_person=True)) == {
            "Q324"
        }

    def test_one_item_twice_is_one_participant(self):
        assert self.own(part("Q324", n=1), part("Q324", n=2)) == {"Q324"}

    def test_a_deprecated_statement_never_counts(self):
        assert (
            self.own(part("Q321", "principal", rank="DeprecatedRank")) == set()
        )
        assert self.own(part("Q324", rank="DeprecatedRank")) == set()

    def test_a_deprecated_participant_or_role_still_takes_a_release_away(self):
        assert (
            self.own(part("Q326"), part("Q327", rank="DeprecatedRank"))
            == set()
        )
        assert (
            self.own(
                part("Q326"), part("Q327", "witness", rank="DeprecatedRank")
            )
            == set()
        )

    def test_a_death_counts_only_at_the_best_rank(self):
        parts = (
            part("Q321", "principal"),
            part("Q322", "informant", rank="PreferredRank"),
        )
        assert self.own(*parts) == set()
        assert self.own(*parts, best_rank_only=False) == {"Q321"}
        preferred = part("Q321", "principal", rank="PreferredRank")
        assert self.own(preferred, part("Q322", "informant")) == {"Q321"}

    def test_the_role_is_the_manifests(self):
        assert PRINCIPAL_ROLE in MANIFEST["items"]
        assert "object_has_role" in MANIFEST["properties"]


class TestPrincipalsByEvent:
    def test_each_event_gives_its_principals(self):
        found = principals_by_event(EVENT_PARTICIPANTS)
        assert found["death"] == {
            "Q320": {"Q321"},
            "Q323": {"Q324"},
            "Q325": set(),
        }
        assert found["birth"] == {"Q328": {"Q329"}}

    def test_an_unknown_value_is_read_as_someone(self):
        rows = participant_rows("Q323", "death", [("Q324", []), ("?", [])])
        assert principals_by_event(rows)["death"] == {"Q323": set()}

    def test_a_birth_counts_at_any_rank_but_deprecated(self):
        rows = participant_rows(
            "Q328",
            "birth",
            [
                ("Q329", ["principal"]),
                ("Q331", ["principal"], "PreferredRank"),
            ],
        )
        assert principals_by_event(rows)["birth"] == {"Q328": {"Q329", "Q331"}}
        assert principals_by_event(
            participant_rows("Q323", "death", [("Q324", [], "DeprecatedRank")])
        )["death"] == {"Q323": set()}

    def test_a_row_per_role_gives_the_statement_every_role(self):
        rows = participant_rows(
            "Q320", "death", [("Q321", ["principal", "witness"])]
        )
        assert len(rows) == 2
        assert principals_by_event(rows)["death"] == {"Q320": set()}

    def test_a_non_person_value_is_read(self):
        rows = participant_rows(
            "Q323", "death", [("Q324", []), ("Q340", [], "NormalRank", True)]
        )
        assert principals_by_event(rows)["death"] == {"Q323": {"Q324"}}


def evidence_runner(calls=None, *, participants=EVENT_PARTICIPANTS):
    """A query runner that answers the evidence queries with the event
    cases."""

    def run(query: str) -> list[dict]:
        if calls is not None:
            calls.append(query)
        if "?eventKind" in query:
            return participants
        if "/ps:" in query:
            return EVENT_BIRTHS
        if 'BIND("value" AS ?bound)' in query:
            return EVENT_PEOPLE_BIRTHS
        raise AssertionError(f"unexpected query: {query}")

    return run


class TestReadEvidence:
    """Both the export and the site read the evidence here (#345)."""

    def vocabulary(self):
        properties = {
            key: f"P{n}" for n, key in enumerate(MANIFEST["properties"], 1)
        }
        items = {key: f"Q{n}" for n, key in enumerate(MANIFEST["items"], 1)}
        return properties, items

    def test_a_death_event_marks_its_principal_alone(self):
        found = read_evidence(evidence_runner(), "", *self.vocabulary())
        assert found.died == {"Q321", "Q324"}

    def test_a_birth_event_is_a_further_birth_of_its_principal(self):
        found = read_evidence(evidence_runner(), "", *self.vocabulary())
        assert sorted(found.births["Q329"]) == [1850, 1990]
        assert found.births["Q331"] == [1850]

    def test_each_person_is_decided_as_the_cases_say(self):
        found = read_evidence(evidence_runner(), "", *self.vocabulary())
        possibly = {
            qid
            for qid in EVENT_CASES
            if is_possibly_living(
                found.births.get(qid, []), qid in found.died, CUTOFF
            )[0]
        }
        assert possibly == EVENT_EXCLUDED

    def test_the_queries_are_read_with_the_prefixes(self):
        calls = []
        properties, items = self.vocabulary()
        read_evidence(evidence_runner(calls), "PREFIXES\n", properties, items)
        assert calls == [
            "PREFIXES\n" + BIRTHS_QUERY.format(**properties),
            "PREFIXES\n" + participants_query(properties, items),
            "PREFIXES\n" + birth_event_dates_query(properties, items),
        ]

    def test_no_birth_event_reads_no_event_dates(self):
        calls = []
        read_evidence(
            evidence_runner(calls, participants=[]), "", *self.vocabulary()
        )
        assert len(calls) == 2


class TestTheEventQueries:
    def test_a_death_event_is_read_by_its_truthy_type(self):
        assert "?event wdt:{instance_of} wd:{death} ." in PARTICIPANTS_QUERY
        assert "wd:{death}" not in PARTICIPANTS_QUERY.split("UNION")[1]

    def test_a_birth_event_is_read_by_its_type_at_any_rank(self):
        assert (
            "?event p:{instance_of} ?birthTyping .\n"
            "    ?birthTyping ps:{instance_of}/{same_as}* wd:{birth} ."
        ) in PARTICIPANTS_QUERY

    def test_every_participant_statement_is_read_with_its_rank(self):
        assert "?event p:{participant} ?part .\n" in PARTICIPANTS_QUERY
        assert "?part wikibase:rank ?partRank ." in PARTICIPANTS_QUERY
        assert "wdt:{participant}" not in PARTICIPANTS_QUERY
        # a statement of no value or unknown value still gives a row
        assert "OPTIONAL {{ ?part ps:{participant} ?value . }}" in (
            PARTICIPANTS_QUERY
        )

    def test_the_role_is_read_from_the_statements_qualifier(self):
        assert "?part pq:{object_has_role} ?role ." in PARTICIPANTS_QUERY
        assert (
            "BIND(EXISTS {{ ?role {same_as}* wd:{principal} . }} "
            "AS ?principal)" in PARTICIPANTS_QUERY
        )

    def test_a_non_person_is_typed_at_normal_or_preferred_rank(self):
        assert "BIND(BOUND(?value) && EXISTS {{" in PARTICIPANTS_QUERY
        assert (
            "FILTER(?partTypingRank != wikibase:DeprecatedRank)"
            in PARTICIPANTS_QUERY
        )
        assert (
            "VALUES ?partKind {{ {non_person_kinds} }}" in PARTICIPANTS_QUERY
        )

    def test_every_id_is_filled_in(self):
        properties, items = TestReadEvidence().vocabulary()
        filled = participants_query(properties, items)
        assert "{" not in filled.replace("{\n", "").replace("{ ", "")
        assert f"wd:{items['principal']}" in filled
        assert f"pq:{properties['object_has_role']}" in filled
        for key in NON_PERSON_CLASSES:
            assert f"wd:{items[key]}" in filled
        dated = birth_event_dates_query(properties, items)
        assert f"wd:{items['birth']}" in dated

    def test_a_birth_events_date_is_read_as_a_birth_is(self):
        assert (
            BIRTH_EVENT_DATES_QUERY.count(
                "?person p:{instance_of}/ps:{instance_of}/{same_as}* "
                "wd:{birth} .\n"
                "    ?person p:{point_in_time} ?statement ."
            )
            == 3
        )
        assert "date_of_birth" not in BIRTH_EVENT_DATES_QUERY
        assert (
            "wd:{point_in_time} wikibase:statementValue"
            in BIRTH_EVENT_DATES_QUERY
        )
        assert BIRTH_EVENT_DATES_QUERY.endswith(BIRTHS_QUERY[-120:])
