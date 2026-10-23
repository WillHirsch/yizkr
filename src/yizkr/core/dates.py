"""The dates the project supports, and the failure for any other (ADR-0034).

A date is supported only if its year is 1 CE or later and its calendar is
the proleptic Gregorian (Wikibase's ``Q1985727``). A date before the
common era, or in the Julian or any other calendar, is never converted,
guessed or shown: wherever one is met, producing a claim, writing one or
reading the graph, the project stops with ``UnsupportedDate``, naming
where it met it.

Producers refuse one through the claim schema (``yizkr.claims.TimeValue``)
and the GEDCOM adapter, the writer refuses to write one, and each reader
of the graph checks every time it reads: ``time_problem`` reads a time as
the query service or the API gives it, with the calendar model its value
node states, and ``unsupported_in_graph`` lists every such time the graph
holds, for a command that reads the whole graph.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

# the one supported calendar model, as Wikibase stores it
GREGORIAN = "http://www.wikidata.org/entity/Q1985727"
# the only calendar name a claim takes
GREGORIAN_NAME = "gregorian"

WHY = (
    "dates before the common era and in calendars other than the "
    "proleptic Gregorian are unsupported (ADR-0034)"
)


class UnsupportedDate(ValueError):
    """A date before the common era or in a calendar other than the
    proleptic Gregorian, met where the project produces, writes or reads
    one (ADR-0034). Its message names where it was met and why."""


def year_problem(year: int) -> str | None:
    """Why a claim's year is unsupported, or None: a year of 0 or less
    names a date before the common era (1 BCE, and earlier)."""
    if year < 1:
        return f"unsupported: year {year} is before the common era; {WHY}"
    return None


def calendar_problem(calendar: str | None) -> str | None:
    """Why a calendar, a claim's name for it or the model IRI Wikibase
    stores, is unsupported, or None. None given (a query that read no
    calendar) is no problem here: the year is still checked."""
    if calendar is None or calendar in (GREGORIAN, GREGORIAN_NAME):
        return None
    return f"unsupported: calendar {calendar!r} is not the Gregorian; {WHY}"


# a time as the API writes it (+1919-01-07T00:00:00Z) or the query
# service serves it (1919-01-07T00:00:00Z, -1836-01-07T00:00:00Z)
_YEAR = re.compile(r"^\s*([+-]?)(\d+)-")


def time_problem(value: str, calendar: str | None = None) -> str | None:
    """Why a time, as the query service or the API gives it, with the
    calendar model its value node states where the reader has it, is
    unsupported, or None. A year with a leading minus is before the common
    era, and so is the year 0, which xsd:dateTime uses for 1 BCE. A value
    with no year this can read is left to the reader's own parse."""
    match = _YEAR.match(value)
    if match is not None:
        sign, digits = match.groups()
        if sign == "-" or int(digits) == 0:
            return (
                f"unsupported: {value.strip()} is before the common era; {WHY}"
            )
    return calendar_problem(calendar)


def check_time(value: str, calendar: str | None, where: str) -> None:
    """Raise UnsupportedDate, naming `where`, if the time is unsupported
    (``time_problem``)."""
    problem = time_problem(value, calendar)
    if problem is not None:
        raise UnsupportedDate(f"{where}: {problem}")


# Wikibase's precisions that name a span ("the 2nd millennium", "the 19th
# century", "the 1830s") rather than a date
SPAN_PRECISIONS = (6, 7, 8)


def time_text(value: str, precision: str | int, where: str) -> str:
    """A time as the query service gives it, with its Wikibase precision,
    at the precision its source states, as the site renders it
    (``staticsite.format_time``): a span as "the 19th century" or "the
    1830s", never its first year. An unsupported date raises
    UnsupportedDate naming `where` (ADR-0034)."""
    # imported here: the site's module imports this one
    from yizkr.staticsite import format_time

    text = format_time(value, str(precision), where)
    span = str(precision) != "" and int(precision) in SPAN_PRECISIONS
    return f"the {text}" if span else text


# Every time value node the graph holds that is unsupported, with the
# statement it belongs to (as a statement's value or qualifier, or in one
# of its references) and that statement's entity. A value node is shared
# by every statement holding the same time, so each is listed.
UNSUPPORTED_QUERY = """\
SELECT DISTINCT ?entity ?statement ?time ?calendar
WHERE {{
  ?node wikibase:timeValue ?time ;
        wikibase:timeCalendarModel ?calendar .
  FILTER(?calendar != <{gregorian}>
         || STRSTARTS(STR(?time), "-")
         || STRSTARTS(STR(?time), "0000-"))
  {{ ?statement ?link ?node . }}
  UNION
  {{ ?reference ?link ?node .
     ?statement prov:wasDerivedFrom ?reference . }}
  ?property wikibase:claim ?claim .
  ?entity ?claim ?statement .
}}
ORDER BY ?entity ?statement
LIMIT {limit}
"""

# how many the failure names; one is enough to stop a command
LISTED = 20


def _cell(row: Mapping[str, Mapping[str, str]], name: str) -> str:
    return row.get(name, {}).get("value", "")


def _short(iri: str) -> str:
    return iri.rsplit("/", 1)[-1]


def unsupported_in_graph(
    run: Callable[[str], list[dict]], prefixes: str
) -> None:
    """Raise UnsupportedDate if the graph holds any unsupported time,
    naming each entity and statement that holds one (up to ``LISTED``).
    `run` runs a query on the query service; `prefixes` is the instance's
    prefix block (``yizkr.queries.publish.prefix_block``)."""
    rows = run(
        prefixes + UNSUPPORTED_QUERY.format(gregorian=GREGORIAN, limit=LISTED)
    )
    if not rows:
        return
    found = "; ".join(
        f"{_short(_cell(row, 'entity'))} statement "
        f"{_short(_cell(row, 'statement'))} holds {_cell(row, 'time')} "
        f"(calendar {_short(_cell(row, 'calendar'))})"
        for row in rows
    )
    more = " (and possibly more)" if len(rows) >= LISTED else ""
    raise UnsupportedDate(
        f"the graph holds unsupported dates: {found}{more}; {WHY}"
    )
