"""Runtime "possibly living" determination (privacy-first, R7 / ADR-0020).

There is **no stored "living" flag**. Whether a person is possibly living
is computed at the moment a privacy-sensitive action runs — the
non-privileged export (`yizkr backup`) and the private static-site badge —
from the *absence of death evidence* in the graph. The call is
conservative by design: unknown means possibly living, hence left out
of the non-privileged export.

A person is *not* possibly living when the graph holds positive evidence
of death, or when every birth it records lies beyond the cutoff. "Death
evidence" is any source-asserted signal that presupposes death:

* a `date of death`, dated or of unknown value (a source stated the death
  and not its date, ADR-0040 §2.2), a `place of death`, a `cause of
  death` or an `age at death` (ADR-0040 Q1);
* a `place of burial`, a place or of unknown value (burial presupposes
  death — the requirement's explicit example);
* a union `ended by death of` the person (ADR-0040 Q9): a source states
  the couple were married when that partner died;
* a `death` event a curator makes that is the person's own (ADR-0040
  Q5): no extraction or GEDCOM batch makes one.

None of these requires *both* a date and a place — a cause of death, a
burial place, an unknown date of death or a union's end each records
"deceased" with no particulars. Each is read at its truthy value, so a
deprecated statement releases no one, and a "no value" is no value. So a
person can be identified as deceased without a time/place of death, and
no curator "living/deceased" decision is ever demanded: privacy falls
out of what sources already assert.

A birth or a death event, which only a curator makes, is its principal's
own, the one it happens to,
and no one else's who takes part in it (``event_principal``, ADR-0035
§3.2): the one participant its statements name in the role `principal`.
An event with no principal, with several, or with one of unknown value
is nobody's own, and so is one whose participants state no role at all.
A death event is death evidence for its principal alone, through a
participant statement at the best rank, as a truthy value would be; an
informant or a witness at someone else's death is never released by it.
A dated birth event's `point in time` (``BIRTH_EVENT_DATES_QUERY``) is a
birth of its principal, read as a `date of birth` is, so it may release a
principal who has no other birth; a birth that is nobody's own adds its
date only to the births of its participants who have a birth already, so
it can only keep them possibly living. ``read_evidence`` reads both, with
the births, for the non-privileged export and the site alike.

Births fail closed. ``BIRTHS_QUERY`` reads every `date of birth`
statement a curator has not deprecated, through its statement node, so no
value is sampled or left out. Each statement allows a birth as late as
``latest_birth_year`` says, which reads its value at its precision (the
last year of a decade, a century or a millennium) and the range its
source states. A person is released by birth only when every one of those
years is at or before the cutoff year; a statement that allows no latest
year keeps them possibly living. The non-privileged export and the site read
births with this one query and this one reading. A birth before the common
era or in a calendar other than the proleptic Gregorian, as the value or a
bound, raises ``UnsupportedDate`` naming the person and the statement
(ADR-0034): the check, and so the export, stops rather than read it, which
fails closed.

Who counts as a person fails closed too. The check assesses every item
with person evidence (``PEOPLE_SELECTION``): a `human` type, a property
only a person has, a kinship relation's value (or the value of `gave
refuge to`), in a statement or a qualifier, or a part in an event that
happens to people, each read through its statement node at every rank, so
that a deprecated or outranked `human` type hides no one. An item typed,
at normal or preferred rank, as a vessel, a military unit or an
organization (``NON_PERSON_CLASSES``) is taken at its type on its part in
such an event: any other evidence makes it a person. An item typed, at
normal or preferred rank, as an event (``event_kinds``) is taken at its
type on the death evidence it states, which describes the death it is
(``EVENT_DEATH_PROPERTIES``): its statements of a property it has a
truthy value of are no person evidence, unless it is typed as a family
too, and any other evidence still makes it a person. The non-privileged export
reads the same evidence from its own dump, and keeps an item that has any
of it only once this check has released it; it leaves out an item that
states no type, so no type at all hides no one either. A family
(``FAMILY_CLASS``) stands for its people, any of whom may be living, so
it never enters the non-privileged export, whatever it holds: the
export leaves it out and the site badges it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from yizkr.core.dates import check_time
from yizkr.model.schema import load_manifest

# Birth-recency cutoff: a birth at least this many years ago is taken as
# proof the person is not living, absent any death evidence. Shared by
# the non-privileged export and the private static-site badge so both agree.
DEFAULT_CUTOFF_YEARS = 123

# Statement properties (manifest keys) that presuppose the subject's
# death, each read at its truthy value, an unknown value included: an age
# at death is a statement on the person, which needs no date of death
# beside it (ADR-0040 Q1). A union's end by the person's death and a
# curator's death event are read separately (``read_evidence``): each is
# a statement of another item, the union's or the event's, and counts
# for the one it names.
DEATH_EVIDENCE_PROPERTIES = (
    "date_of_death",
    "place_of_death",
    "cause_of_death",
    "burial_place",
    "age_at_death",
)

# Person evidence (manifest keys): what marks an item as a person, read at
# every rank, deprecated included, since a statement a curator deprecated
# still records that a source said so. An item is a person when it is
# typed `PERSON_CLASS`, is the subject of a `PERSON_SUBJECT_PROPERTIES`
# statement, is the value of a `PERSON_VALUE_PROPERTIES` statement or
# qualifier, or takes part in an event typed as one of `PERSON_EVENTS`.
PERSON_CLASS = "human"
# Properties that only a person is the subject of: a birth, a death, an age at
# death and a burial; kinship; the names only a person has (a birth or married
# name, a nickname, a name form of no stated type, what they are referred to
# as: ADR-0011, ADR-0018); a sex or gender and a number of children; and an
# occupation, an employer, a school, a citizenship and a residence. An official
# name, which a place or an organization can have too, and the ties an
# organization shares (a religion, a membership) are not evidence.
PERSON_SUBJECT_PROPERTIES = (
    "date_of_birth",
    "date_of_death",
    "place_of_birth",
    "place_of_death",
    "burial_place",
    "cause_of_death",
    "age_at_death",
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
# Properties whose value is only ever a person: kinship, the one given
# refuge, and the partner whose death ended a union (ADR-0040 §5). Their
# value marks a person in a statement and in a qualifier alike.
PERSON_VALUE_PROPERTIES = (
    "father",
    "mother",
    "spouse",
    "child",
    "sibling",
    "relative",
    "gave_refuge_to",
    "ended_by_death_of",
)
# The kinds of event that happen to people, so that whoever takes part in
# one is a person, unless one of `NON_PERSON_CLASSES` types them. A
# participant in any other event may be an organization or a group. A
# birth and a death stay among them for the events a curator makes.
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
# The class of every event, a key of the model manifest; ``event_kinds``
# gives it and the classes the manifest makes subclasses of it.
EVENT_CLASS = "event"
# The person-only properties that an event states of the death it is: an
# item typed, at normal or preferred rank, as an event (``event_kinds``) is
# not taken for a person on its statements of one of these when it has a
# truthy value of it, unless it is typed as a family too. Each is death
# evidence (``DEATH_EVIDENCE_PROPERTIES``), which the check reads through
# the truthy values, so such an item would be marked deceased were it a
# person: setting it aside releases no one the check would keep, and a
# family, badged whatever its evidence, stays listed. A statement of one
# with no truthy value beside it (one a curator deprecated, or of no value)
# is still evidence, and so is every other person-only property, a `date
# of birth` included, and a `human` type at any rank.
EVENT_DEATH_PROPERTIES = DEATH_EVIDENCE_PROPERTIES
# The types of what takes part in an event without being a person: a
# vessel, a military unit and an organization, as a ship or a firm takes
# part in a migration. An item typed so, at normal or preferred rank, is
# not taken for a person on its part in a person event alone. It is a
# person on any other evidence, a `human` type at any rank included; one
# typed so only at deprecated rank, or not at all, is a person on its part
# alone. A family is not among them: its label is a surname and its
# description names its members, and a person known by a surname alone
# can be typed as one. Nor is the unclassified entity, where a mistyped
# person lands.
NON_PERSON_CLASSES = ("vessel", "military_unit", "organization")

# A redirect's link to the item it redirects to, as the query service and
# the dump write it.
SAME_AS = "<http://www.w3.org/2002/07/owl#sameAs>"

# The class of an item that stands for a set of people: a family, whose
# people are those recorded as `member of` it. A family is private while
# any of its people may be living, and the graph cannot state that a
# family's membership is complete, so every item typed as one, at normal
# or preferred rank (read through redirects, as every type is), stays
# out of the non-privileged export, whatever else it holds, released or
# not.
FAMILY_CLASS = "family"

# Whether ?person is typed as a family, as `FAMILY_CLASS` says: a
# condition for a query to BIND. `family_type` fills it in.
FAMILY_TYPE = """\
EXISTS {{
    ?person p:{instance_of} ?familyTyping .
    ?familyTyping ps:{instance_of}/{same_as}* wd:{family} ;
                  wikibase:rank ?familyRank .
    FILTER(?familyRank != wikibase:DeprecatedRank)
  }}"""

# Every item with person evidence, once each, read through statement
# nodes at every rank, never through the truthy wdt: values; only the
# types that set a participant, or an event's own death evidence, aside
# must be ones a curator has not deprecated. The death evidence an event
# states of itself (``EVENT_DEATH_PROPERTIES``) is set aside only where the
# event has a truthy value of the property (wdt:), as the check reads
# death evidence, and never on an item typed as a family too
# (``FAMILY_TYPE``), which stays listed and badged. A person-valued
# property's value counts in a statement (ps:) and in a qualifier (pq:)
# alike. A type is read through the
# redirects it may name ({same_as}*), so a class a curator merged into
# `human`, an event kind or a non-person class types as that one does. A
# value that is not an item (an unknown value) is left out, and so is a
# redirect, which has no statements of its own: the item it redirects to
# is selected for what it holds. The filters test each item once, after
# DISTINCT, not each statement that names it. `people_selection` fills in
# the lists.
PEOPLE_SELECTION = """\
  {{
    SELECT ?person WHERE {{
      {{
        SELECT DISTINCT ?person WHERE {{
          {{
            ?person p:{instance_of} ?typing .
            ?typing ps:{instance_of}/{same_as}* wd:{human} .
          }} UNION {{
            VALUES ?link {{ {subject_links} }}
            ?person ?link ?statement .
          }} UNION {{
            VALUES (?link ?direct) {{ {death_links} }}
            ?person ?link ?statement .
            FILTER NOT EXISTS {{
              ?person ?direct ?death .
              VALUES ?eventClass {{ {event_classes} }}
              ?person p:{instance_of} ?eventTyping .
              ?eventTyping ps:{instance_of}/{same_as}* ?eventClass ;
                           wikibase:rank ?eventRank .
              FILTER(?eventRank != wikibase:DeprecatedRank)
              FILTER(!{family_type})
            }}
          }} UNION {{
            VALUES ?link {{ {value_links} }}
            ?statement ?link ?person .
          }} UNION {{
            VALUES ?kind {{ {event_kinds} }}
            ?event p:{instance_of} ?eventTyping .
            ?eventTyping ps:{instance_of}/{same_as}* ?kind .
            ?event p:{participant} ?participation .
            ?participation ps:{participant} ?person .
            FILTER NOT EXISTS {{
              VALUES ?otherKind {{ {non_person_kinds} }}
              ?person p:{instance_of} ?otherTyping .
              ?otherTyping ps:{instance_of}/{same_as}* ?otherKind ;
                           wikibase:rank ?otherRank .
              FILTER(?otherRank != wikibase:DeprecatedRank)
            }}
          }}
        }}
      }}
      FILTER(REGEX(STR(?person), "/entity/Q[1-9][0-9]*$"))
      FILTER NOT EXISTS {{ ?person {same_as} ?target . }}
    }}
  }}
"""


def people_selection(
    properties: Mapping[str, str], items: Mapping[str, str]
) -> str:
    """``PEOPLE_SELECTION`` with an instance's ids: a group that binds
    ?person to each item with person evidence, for a query to join on."""
    return PEOPLE_SELECTION.format(
        same_as=SAME_AS,
        family_type=family_type(properties, items),
        instance_of=properties["instance_of"],
        participant=properties["participant"],
        human=items[PERSON_CLASS],
        subject_links=" ".join(
            f"p:{properties[key]}"
            for key in PERSON_SUBJECT_PROPERTIES
            if key not in EVENT_DEATH_PROPERTIES
        ),
        death_links=" ".join(
            f"(p:{properties[key]} wdt:{properties[key]})"
            for key in EVENT_DEATH_PROPERTIES
        ),
        event_classes=" ".join(
            f"wd:{items[key]}" for key in sorted(event_kinds()) if key in items
        ),
        value_links=" ".join(
            f"{prefix}:{properties[key]}"
            for key in PERSON_VALUE_PROPERTIES
            for prefix in ("ps", "pq")
        ),
        event_kinds=" ".join(f"wd:{items[key]}" for key in PERSON_EVENTS),
        non_person_kinds=" ".join(
            f"wd:{items[key]}" for key in NON_PERSON_CLASSES
        ),
    )


def event_kinds(root: str = EVENT_CLASS) -> set[str]:
    """The keys of the model manifest's classes under `root`: `root` itself
    and every item the manifest makes a subclass of it, directly or
    through another; by default the event classes (``EVENT_CLASS``)."""
    manifest = load_manifest()
    kinds = {root}
    while wider := {
        key
        for key, spec in manifest.items.items()
        if spec.subclass_of in kinds and key not in kinds
    }:
        kinds |= wider
    return kinds


def family_type(
    properties: Mapping[str, str], items: Mapping[str, str]
) -> str:
    """``FAMILY_TYPE`` with an instance's ids."""
    return FAMILY_TYPE.format(
        same_as=SAME_AS,
        instance_of=properties["instance_of"],
        family=items[FAMILY_CLASS],
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
# known, its precision, with the calendar model its value node states. Each
# part of the union binds the statement itself, so each is evaluated whole
# on its own.
BIRTHS_QUERY = """\
SELECT ?person ?statement ?bound ?time ?precision ?calendar
WHERE {{
  {{
    ?person p:{date_of_birth} ?statement .
    BIND("value" AS ?bound)
    OPTIONAL {{
      wd:{date_of_birth} wikibase:statementValue ?valueLink .
      ?statement ?valueLink ?node .
      ?node wikibase:timeValue ?time ;
            wikibase:timePrecision ?precision .
      OPTIONAL {{ ?node wikibase:timeCalendarModel ?calendar . }}
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
      OPTIONAL {{ ?node wikibase:timeCalendarModel ?calendar . }}
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
      OPTIONAL {{ ?node wikibase:timeCalendarModel ?calendar . }}
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
    xsd:dateTime uses for 1 BCE) raises UnsupportedDate (ADR-0034). This
    is a backstop: ``latest_births`` has already refused such a birth,
    naming its person and statement.
    """
    check_time(literal, None, "a birth date")
    match = _DATE_TIME.match(literal)
    if match is None:
        return None
    return int(match.group(1))


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
    """A row's time and precision, or None for an unknown value. One
    before the common era or in a calendar other than the proleptic
    Gregorian raises UnsupportedDate naming the person and the statement
    (ADR-0034)."""
    literal = _cell(row, "time")
    if not literal:
        return None
    where = (
        f"{_cell(row, 'person').rsplit('/', 1)[-1]} birth statement "
        f"{_cell(row, 'statement').rsplit('/', 1)[-1]}"
    )
    check_time(literal, _cell(row, "calendar") or None, where)
    precision = _cell(row, "precision")
    return literal, int(precision) if precision.isdigit() else None


def latest_births(
    rows: Iterable[Mapping[str, Mapping[str, str]]],
) -> dict[str, list[int | None]]:
    """The latest year each birth statement allows
    (``latest_birth_year``), by the QID of the person it is about, from
    the rows of ``BIRTHS_QUERY``: one row for each statement's value,
    known or not, and one for each earliest or latest date it carries. An
    unsupported date in any of them raises UnsupportedDate (``_time``)."""
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


# The role of the one an event happens to (ADR-0035 §3.2), a key of the
# model manifest: the person born in a birth, the one who died in a death.
PRINCIPAL_ROLE = "principal"

# Every participant statement of every birth and death event, read through
# its statement node at every rank: one row for each role it states (one
# row with no role when it states none), with the statement's rank, its
# value when it has one (an item, or an unknown value) and whether the
# role is `principal` (read through redirects). A death event is an item
# whose truthy type is `death`, as the other death evidence is read
# truthy; a birth event is an item with a `birth` type at any rank,
# through redirects. ``participants_query`` fills in the ids.
PARTICIPANTS_QUERY = """\
SELECT ?event ?eventKind ?part ?partRank ?value ?role ?principal
WHERE {{
  {{
    ?event wdt:{instance_of} wd:{death} .
    BIND("death" AS ?eventKind)
  }} UNION {{
    ?event p:{instance_of} ?birthTyping .
    ?birthTyping ps:{instance_of}/{same_as}* wd:{birth} .
    BIND("birth" AS ?eventKind)
  }}
  ?event p:{participant} ?part .
  ?part wikibase:rank ?partRank .
  OPTIONAL {{ ?part ps:{participant} ?value . }}
  OPTIONAL {{
    ?part pq:{object_has_role} ?role .
    BIND(EXISTS {{ ?role {same_as}* wd:{principal} . }} AS ?principal)
  }}
}}
"""


def participants_query(
    properties: Mapping[str, str], items: Mapping[str, str]
) -> str:
    """``PARTICIPANTS_QUERY`` with an instance's ids."""
    return PARTICIPANTS_QUERY.format(
        same_as=SAME_AS,
        instance_of=properties["instance_of"],
        participant=properties["participant"],
        object_has_role=properties["object_has_role"],
        birth=items["birth"],
        death=items["death"],
        principal=items[PRINCIPAL_ROLE],
    )


# Every dated birth event's `point in time` statements, read as
# ``BIRTHS_QUERY`` reads a `date of birth`: statement by statement, with
# the earliest and latest dates each carries, deprecated ones aside. Here
# ?person is the event, whose date ``read_evidence`` gives its principal,
# or, when it is nobody's own, its participants who have a birth already.
# ``birth_event_dates_query`` fills in the ids.
BIRTH_EVENT_DATES_QUERY = BIRTHS_QUERY.replace(
    "?person p:{date_of_birth} ?statement .",
    "?person p:{instance_of}/ps:{instance_of}/{same_as}* wd:{birth} .\n"
    "    ?person p:{point_in_time} ?statement .",
).replace(
    "wd:{date_of_birth} wikibase:statementValue",
    "wd:{point_in_time} wikibase:statementValue",
)


def birth_event_dates_query(
    properties: Mapping[str, str], items: Mapping[str, str]
) -> str:
    """``BIRTH_EVENT_DATES_QUERY`` with an instance's ids."""
    return BIRTH_EVENT_DATES_QUERY.format(
        **properties, same_as=SAME_AS, birth=items["birth"]
    )


_ENTITY = re.compile(r"/entity/(Q[1-9][0-9]*)$")
_DEPRECATED = "DeprecatedRank"
_PREFERRED = "PreferredRank"


@dataclass(frozen=True)
class Participation:
    """One participant statement of an event: `value` is the QID it names,
    ``UNKNOWN`` for an unknown value or one that is no item, and "" for
    no value; `rank` is the local name of its rank (``NormalRank``);
    `roles` holds, for each role it states, whether that role is
    `principal`."""

    value: str
    rank: str = "NormalRank"
    roles: frozenset[tuple[str, bool]] = frozenset()


UNKNOWN = "?"


def event_principal(
    parts: Iterable[Participation], *, best_rank_only: bool
) -> str | None:
    """The QID whose own the event is (ADR-0035 §3.2), from its
    participant statements, or None when it is nobody's own. It is its
    principal's: the one value that every statement stating a `principal`
    role names, at any rank. With no principal, or with several, or with
    one of unknown value, it is nobody's own, and so is an event whose
    participants state no role at all. The principal's own statement must
    state no role but `principal` and must not be deprecated; with
    `best_rank_only` it must be at the best rank among the event's
    participant statements, as a truthy value is. A statement a curator
    deprecated can so name a second principal, and take a release away,
    but never give one."""
    parts = list(parts)
    claimed = {
        part.value
        for part in parts
        if part.value and any(principal for _, principal in part.roles)
    }
    if len(claimed) != 1 or UNKNOWN in claimed:
        return None
    (principal,) = claimed
    counted = [part for part in parts if part.rank != _DEPRECATED]
    if best_rank_only and counted:
        best = (
            _PREFERRED
            if any(part.rank == _PREFERRED for part in counted)
            else "NormalRank"
        )
        counted = [part for part in counted if part.rank == best]
    if any(
        part.value == principal
        and all(is_principal for _, is_principal in part.roles)
        for part in counted
    ):
        return principal
    return None


def principals_by_event(
    rows: Iterable[Mapping[str, Mapping[str, str]]],
) -> dict[str, dict[str, set[str]]]:
    """Whose own each event is, by its kind and its QID, from the rows of
    ``PARTICIPANTS_QUERY``: "death" and "birth" map each event to its
    principal, or to no one (``event_principal``: a death's at the best
    rank, a birth's at any rank a curator has not deprecated); "shared
    birth" maps each birth that is nobody's own to the QIDs it names in
    the participant statements a curator has not deprecated. A role row
    with no `principal` answer reads as another role."""
    found: dict[tuple[str, str], dict[str, dict]] = {}
    for row in rows:
        event = _cell(row, "event").rsplit("/", 1)[-1]
        part = found.setdefault(
            (_cell(row, "eventKind"), event), {}
        ).setdefault(_cell(row, "part"), {"roles": set(), "row": row})
        if "role" in row:
            part["roles"].add(
                (_cell(row, "role"), _cell(row, "principal") == "true")
            )
    events: dict[str, dict[str, set[str]]] = {
        "birth": {},
        "death": {},
        "shared birth": {},
    }
    for (kind, event), parts in found.items():
        if kind not in ("birth", "death"):
            continue
        participations = []
        for part in parts.values():
            row = part["row"]
            value = _cell(row, "value")
            item = _ENTITY.search(value) if value else None
            participations.append(
                Participation(
                    value=item.group(1) if item else UNKNOWN if value else "",
                    rank=_cell(row, "partRank").rsplit("#", 1)[-1],
                    roles=frozenset(part["roles"]),
                )
            )
        principal = event_principal(
            participations, best_rank_only=kind == "death"
        )
        events[kind][event] = {principal} if principal else set()
        if kind == "birth" and principal is None:
            events["shared birth"][event] = {
                part.value
                for part in participations
                if part.value not in ("", UNKNOWN) and part.rank != _DEPRECATED
            }
    return events


# Every person a union is `ended by death of` (ADR-0040 Q9), at the
# statement's truthy value, items only: an unknown value names no one, and
# a deprecated statement releases no one. The value is read through its
# redirect, as the person the check assesses is the item it redirects to.
ENDED_BY_DEATH_QUERY = """\
SELECT DISTINCT ?person
WHERE {{
  ?union wdt:{ended_by_death_of} ?named .
  FILTER(isIRI(?named))
  ?named {same_as}* ?person .
  FILTER NOT EXISTS {{ ?person {same_as} ?further . }}
  FILTER(REGEX(STR(?person), "/entity/Q[1-9][0-9]*$"))
}}
"""


def ended_by_death_query(properties: Mapping[str, str]) -> str:
    """``ENDED_BY_DEATH_QUERY`` with an instance's ids."""
    return ENDED_BY_DEATH_QUERY.format(
        same_as=SAME_AS, ended_by_death_of=properties["ended_by_death_of"]
    )


@dataclass
class LifeEvidence:
    """What the possibly-living call reads beside a person's own death
    statements: `births`, the latest year each birth allows by person
    (``latest_births``), those of birth events included
    (``read_evidence``); and `died`, everyone a union is `ended by death
    of`, and everyone who is the principal of a death event
    (``event_principal``). `ended` and `death_events` keep those two
    apart, for the run record's releases by cause."""

    births: dict[str, list[int | None]] = field(default_factory=dict)
    died: set[str] = field(default_factory=set)
    ended: set[str] = field(default_factory=set)
    death_events: set[str] = field(default_factory=set)


def read_evidence(
    run: Callable[[str], list[dict]],
    prefixes: str,
    properties: Mapping[str, str],
    items: Mapping[str, str],
) -> LifeEvidence:
    """The births and the deaths the possibly-living call weighs beside a
    person's own death statements, read with `run` (a query runner, given
    `prefixes`): every `date of birth` statement (``BIRTHS_QUERY``), and
    each dated birth event's `point in time`; everyone a union is `ended
    by death of` (``ENDED_BY_DEATH_QUERY``, where the instance has the
    property); and the principals of the death events. A birth
    event's date is a birth of its principal, as a `date of birth` is, so
    it may release them; a birth that is nobody's own only adds its date
    to the births of its participants who have one already, after the
    principals' have been added, so it never releases anyone. The
    non-privileged export and the site both read them here, so both
    decide each person the same way."""
    births = latest_births(run(prefixes + BIRTHS_QUERY.format(**properties)))
    events = principals_by_event(
        run(prefixes + participants_query(properties, items))
    )
    if events["birth"]:
        dated = latest_births(
            run(prefixes + birth_event_dates_query(properties, items))
        )
        for event, years in dated.items():
            for person in events["birth"].get(event, ()):
                births.setdefault(person, []).extend(years)
        for event, years in dated.items():
            for person in events["shared birth"].get(event, ()):
                if person in births:
                    births[person].extend(years)
    death_events = set().union(*events["death"].values())
    ended: set[str] = set()
    if "ended_by_death_of" in properties:
        ended = {
            _cell(row, "person").rsplit("/", 1)[-1]
            for row in run(prefixes + ended_by_death_query(properties))
        }
    return LifeEvidence(
        births=births,
        died=death_events | ended,
        ended=ended,
        death_events=death_events,
    )
