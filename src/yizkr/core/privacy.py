"""Runtime "possibly living" determination (privacy-first, R7 / ADR-0020).

There is **no stored "living" flag**. Whether a person is possibly living
is computed at the moment a privacy-sensitive action runs — the public
export (`yizkr backup`) and the private static-site badge — from the
*absence of death evidence* in the graph. The call is conservative by
design: unknown means possibly living, hence excluded from anything
public.

A person is *not* possibly living when the graph holds positive evidence
of death, or when every birth it records lies beyond the cutoff. "Death
evidence" is any source-asserted signal that presupposes death:

* a `date of death`, `place of death`, or `cause of death`;
* a `place of burial` (burial presupposes death — the requirement's
  explicit example);
* participation in a `death` event (ADR-0014 models death as an event
  like any other).

None of these requires *both* a date and a place — a cause of death, a
burial place, or an undated death event each records "deceased" with no
particulars. So a person can be identified as deceased without a
time/place of death, and no curator "living/deceased" decision is ever
demanded: privacy falls out of what sources already assert.

Births fail closed. ``BIRTHS_QUERY`` reads every `date of birth`
statement a curator has not deprecated, through its statement node, so no
value is sampled or left out. Each statement allows a birth as late as
``latest_birth_year`` says, which reads its value at its precision (the
last year of a decade, a century or a millennium) and the range its
source states. A person is released by birth only when every one of those
years is at or before the cutoff year; a statement that allows no latest
year keeps them possibly living. The public export and the site read
births with this one query and this one reading.

Who counts as a person fails closed too. The check assesses every item
with person evidence (``PEOPLE_SELECTION``): a `human` type, a property
only a person has, the value of a kinship relation (or of `gave refuge
to`), or a part in an event that happens to people, each read through its
statement node at every rank, so that no curator's rank and no missing or
outranked type hides anyone. A part in such an event is no evidence for
an item a curator has left typed as a vessel, a group or an organization
(``NON_PERSON_CLASSES``). The public export reads the same evidence from
its own dump, and keeps an item that has any of it only once this check
has released it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

# Birth-recency cutoff: a birth at least this many years ago is taken as
# proof the person is not living, absent any death evidence. Shared by
# the public export and the private static-site badge so both agree.
DEFAULT_CUTOFF_YEARS = 123

# Statement properties (manifest keys) that presuppose the subject's
# death. `age_at_death` is intentionally absent: it is a qualifier on
# `date of death`, never a standalone statement, so it adds nothing.
# Death-event participation is checked separately (it is a relation
# through an event item, not a property on the person).
DEATH_EVIDENCE_PROPERTIES = (
    "date_of_death",
    "place_of_death",
    "cause_of_death",
    "burial_place",
)

# Person evidence (manifest keys): what marks an item as a person, read at
# every rank, deprecated included, since a statement a curator deprecated
# still records that a source said so. An item is a person when it is
# typed `PERSON_CLASS`, is the subject of a `PERSON_SUBJECT_PROPERTIES`
# statement, is the value of a `PERSON_VALUE_PROPERTIES` one, or takes
# part in an event typed as one of `PERSON_EVENTS`.
PERSON_CLASS = "human"
# Properties that only a person is the subject of: a birth, a death and a
# burial; kinship; a birth or married name; a sex or gender and a number
# of children; and an occupation, an employer, a school, a citizenship
# and a residence. Names that a place or an organization can have too (a
# nickname, an official name, what it is referred to as) and ties that an
# organization shares (a religion, a membership) are not evidence.
PERSON_SUBJECT_PROPERTIES = (
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
    "sex_or_gender",
    "number_of_children",
    "occupation",
    "employer",
    "educated_at",
    "country_of_citizenship",
    "residence",
)
# Properties whose value is only ever a person: kinship, and the one
# given refuge.
PERSON_VALUE_PROPERTIES = (
    "father",
    "mother",
    "spouse",
    "child",
    "sibling",
    "relative",
    "gave_refuge_to",
)
# The kinds of event that happen to people, so that whoever takes part in
# one is a person, unless one of `NON_PERSON_CLASSES` types them. A
# participant in any other event may be an organization or a group.
PERSON_EVENTS = (
    "birth",
    "death",
    "marriage",
    "migration",
    "military_service",
    "deportation",
    "imprisonment",
    "release",
)
# The types of what takes part in an event without being a person: a
# vessel, a group (a family, a military unit) and an organization, as a
# ship, a family or a firm takes part in a migration. An item typed so,
# at a rank other than deprecated, is not taken for a person on its part
# in a person event alone. It is a person on any other evidence, a
# `human` type at any rank included; one typed so only at deprecated rank,
# or not at all, is a person on its part alone.
NON_PERSON_CLASSES = ("vessel", "family", "military_unit", "organization")

# Every item with person evidence, once each, read through statement
# nodes at every rank, never through the truthy wdt: values; only the
# type that sets a participant aside must be one a curator has not
# deprecated. A value that is not an item (an unknown value) is left out,
# and so is a redirect, which has no statements of its own: the item it
# redirects to is selected for what it holds. The filters test each item
# once, after DISTINCT, not each statement that names it.
# `people_selection` fills in the lists.
PEOPLE_SELECTION = """\
  {{
    SELECT ?person WHERE {{
      {{
        SELECT DISTINCT ?person WHERE {{
          {{
            ?person p:{instance_of} ?typing .
            ?typing ps:{instance_of} wd:{human} .
          }} UNION {{
            VALUES ?link {{ {subject_links} }}
            ?person ?link ?statement .
          }} UNION {{
            VALUES ?link {{ {value_links} }}
            ?statement ?link ?person .
          }} UNION {{
            VALUES ?kind {{ {event_kinds} }}
            ?event p:{instance_of} ?eventTyping .
            ?eventTyping ps:{instance_of} ?kind .
            ?event p:{participant} ?participation .
            ?participation ps:{participant} ?person .
            FILTER NOT EXISTS {{
              VALUES ?otherKind {{ {non_person_kinds} }}
              ?person p:{instance_of} ?otherTyping .
              ?otherTyping ps:{instance_of} ?otherKind ;
                           wikibase:rank ?otherRank .
              FILTER(?otherRank != wikibase:DeprecatedRank)
            }}
          }}
        }}
      }}
      FILTER(REGEX(STR(?person), "/entity/Q[1-9][0-9]*$"))
      FILTER NOT EXISTS {{
        ?person <http://www.w3.org/2002/07/owl#sameAs> ?target .
      }}
    }}
  }}
"""


def people_selection(
    properties: Mapping[str, str], items: Mapping[str, str]
) -> str:
    """``PEOPLE_SELECTION`` with an instance's ids: a group that binds
    ?person to each item with person evidence, for a query to join on."""
    return PEOPLE_SELECTION.format(
        instance_of=properties["instance_of"],
        participant=properties["participant"],
        human=items[PERSON_CLASS],
        subject_links=" ".join(
            f"p:{properties[key]}" for key in PERSON_SUBJECT_PROPERTIES
        ),
        value_links=" ".join(
            f"ps:{properties[key]}" for key in PERSON_VALUE_PROPERTIES
        ),
        event_kinds=" ".join(f"wd:{items[key]}" for key in PERSON_EVENTS),
        non_person_kinds=" ".join(
            f"wd:{items[key]}" for key in NON_PERSON_CLASSES
        ),
    )


# Wikibase's time precisions run from 0 (a billion years) to 14 (a
# second): 6 is a millennium, 7 a century, 8 a decade and 9 a year.
MILLENNIUM, CENTURY, DECADE, YEAR = 6, 7, 8, 9
PRECISIONS = range(15)

# A time as the query service gives it: its xsd:dateTime and its
# precision (None when the query found none).
Time = tuple[str, int | None]

# Every birth statement a curator has not deprecated, read through its
# statement node with its rank, never through the truthy values: one row
# for the statement's own value, with its time and precision when it has
# one (an unknown value has neither), and one row for each earliest or
# latest date it carries, with that date's time and, when the date is
# known, its precision. Each part of the union binds the statement itself,
# so each is evaluated whole on its own.
BIRTHS_QUERY = """\
SELECT ?person ?statement ?bound ?time ?precision
WHERE {{
  {{
    ?person p:{date_of_birth} ?statement .
    BIND("value" AS ?bound)
    OPTIONAL {{
      wd:{date_of_birth} wikibase:statementValue ?valueLink .
      ?statement ?valueLink ?node .
      ?node wikibase:timeValue ?time ;
            wikibase:timePrecision ?precision .
    }}
  }} UNION {{
    ?person p:{date_of_birth} ?statement .
    ?statement pq:{earliest_date} ?time .
    BIND("earliest" AS ?bound)
    OPTIONAL {{
      wd:{earliest_date} wikibase:qualifierValue ?earliestLink .
      ?statement ?earliestLink ?node .
      ?node wikibase:timeValue ?time ;
            wikibase:timePrecision ?precision .
    }}
  }} UNION {{
    ?person p:{date_of_birth} ?statement .
    ?statement pq:{latest_date} ?time .
    BIND("latest" AS ?bound)
    OPTIONAL {{
      wd:{latest_date} wikibase:qualifierValue ?latestLink .
      ?statement ?latestLink ?node .
      ?node wikibase:timeValue ?time ;
            wikibase:timePrecision ?precision .
    }}
  }}
  FILTER NOT EXISTS {{ ?statement wikibase:rank wikibase:DeprecatedRank . }}
}}
"""

_DATE_TIME = re.compile(r"\+?(\d{4,})-\d{2}-\d{2}T")


def year_of(literal: str) -> int | None:
    """The year of a time as the query service or the API gives it
    (``1919-01-07T00:00:00Z``, ``+1919-00-00T00:00:00Z``), else None.

    A year before the common era (a leading ``-``, or the year 0, which
    xsd:dateTime uses for 1 BCE) gives None too: family records date no
    birth that early, so such a value is taken for a mistake, and a birth
    that cannot be read keeps its person possibly living.
    """
    match = _DATE_TIME.match(literal)
    if match is None:
        return None
    year = int(match.group(1))
    return year if year > 0 else None


def years_allowed(
    literal: str, precision: int | None
) -> tuple[int, int] | None:
    """The first and last year a time allows at its precision, read so
    as to allow the latest birth; None when it names no years that a
    cutoff can bound.

    * A year, and every finer precision down to a second, allows its year.
    * A decade runs from its round ten, as Wikibase shows it: a date
      stored as any year from 1890 to 1899 at decade precision is the
      1890s.
    * Wikibase counts centuries and millennia from year 1, so 1801 to
      1900 is the 19th century and 1001 to 2000 the 2nd millennium, and a
      date at either precision may be stored as any of its years. A writer
      that names a century by its round hundred means 1900 to 1999 by
      1900, so a round hundred at century precision, or a round thousand
      at millennium precision, is read as the first year of the span it
      opens: of the two readings, that one allows the later birth.
    * A precision coarser than a millennium names a span of ten thousand
      years or more, which reaches past any cutoff, and a precision
      Wikibase does not have names nothing: both give None.
    """
    year = year_of(literal)
    if year is None or precision not in PRECISIONS:
        return None
    if precision >= YEAR:
        return year, year
    if precision == DECADE:
        first = year - year % 10
        return first, first + 9
    if precision in (CENTURY, MILLENNIUM):
        unit = 100 if precision == CENTURY else 1000
        first = year if year % unit == 0 else year - year % unit + 1
        return first, first + unit - 1
    return None


def latest_birth_year(
    value: Time | None,
    earliest: Sequence[Time | None] = (),
    latest: Sequence[Time | None] = (),
) -> int | None:
    """The latest year one birth statement allows, or None when it
    allows no latest year. `value` is the statement's own time, or None
    when it has none that can be read (an unknown value); `earliest` and
    `latest` are the times of its earliest and latest date qualifiers,
    None for one whose time is unknown.

    A range its source states bounds the birth in place of the value's
    precision, as the disagreement report reads a date's span
    (docs/MODELING.md, "Contradictions"), since a range is stored with its
    first date as the value ("between 1850 and 1860" is 1850, with those
    two years as its earliest and latest dates):

    * with a latest date, the birth is no later than the last year that
      date allows (the latest of them, when there are several);
    * with earliest dates alone, the range is open at its late end ("born
      after 1900"), and bounds nothing;
    * with neither, the value bounds it: the last year it allows at its
      precision (``years_allowed``).

    A latest date that cannot be read leaves the range's end unknown, so
    the statement allows no latest year. A statement that contradicts
    itself, whose value or earliest date lies wholly after its latest
    date, is read without that latest date.
    """
    allowed = years_allowed(*value) if value else None
    if latest:
        ends = [years_allowed(*time) if time else None for time in latest]
        if None in ends:
            return None
        bound = max(last for _, last in ends)
        starts = [
            found[0]
            for found in (
                allowed,
                *(years_allowed(*time) if time else None for time in earliest),
            )
            if found is not None
        ]
        if all(first <= bound for first in starts):
            return bound
    if earliest:
        return None
    return allowed[1] if allowed else None


def _cell(row: Mapping[str, Mapping[str, str]], name: str) -> str:
    return row.get(name, {}).get("value", "")


def _time(row: Mapping[str, Mapping[str, str]]) -> Time | None:
    literal = _cell(row, "time")
    if not literal:
        return None
    precision = _cell(row, "precision")
    return literal, int(precision) if precision.isdigit() else None


def latest_births(
    rows: Iterable[Mapping[str, Mapping[str, str]]],
) -> dict[str, list[int | None]]:
    """The latest year each birth statement allows
    (``latest_birth_year``), by the QID of the person it is about, from
    the rows of ``BIRTHS_QUERY``: one row for each statement's value,
    known or not, and one for each earliest or latest date it carries."""
    statements: dict[tuple[str, str], dict[str, list[Time | None]]] = {}
    for row in rows:
        person = _cell(row, "person").rsplit("/", 1)[-1]
        parts = statements.setdefault(
            (person, _cell(row, "statement")),
            {"value": [], "earliest": [], "latest": []},
        )
        parts[_cell(row, "bound")].append(_time(row))
    births: dict[str, list[int | None]] = {}
    for (person, _), parts in statements.items():
        years = [
            latest_birth_year(value, parts["earliest"], parts["latest"])
            for value in [time for time in parts["value"] if time] or [None]
        ]
        births.setdefault(person, []).append(
            None if None in years else max(years)
        )
    return births


def is_possibly_living(
    births: Sequence[int | None],
    has_death_evidence: bool,
    cutoff_year: int,
) -> tuple[bool, str]:
    """Conservative call: unknown means possibly living (excluded).

    `births` holds the latest year each of the person's birth statements
    allows (``latest_births``), None for one that allows none. Returns
    (possibly_living, reason). Only positive evidence of death, or births
    that all lie at or before the cutoff year, mark a person not living;
    one birth after it, or one that allows no latest year, keeps them
    possibly living. There is no override in either direction — the
    determination is exactly what the graph's own referenced statements
    assert (ADR-0020).
    """
    if has_death_evidence:
        return False, "death recorded"
    if not births:
        return (
            True,
            "no death evidence and no birth date: unknown means excluded",
        )
    if None in births:
        return (
            True,
            "no death evidence, and a birth date with no latest year it "
            "allows: unknown means excluded",
        )
    latest = max(births)
    if latest <= cutoff_year:
        return False, f"born by {latest}, beyond the {cutoff_year} cutoff"
    return True, f"born as late as {latest}, with no death evidence"
