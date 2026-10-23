"""The disagreement report: the values that different artifacts attribute
to one entity and property, side by side (ADR-0002, ADR-0033).

Its rows are a healthy resting state, never errors: the report exists so a
reader can weigh the evidence, not to pick a value. Two library queries
fetch what it compares. ``disagreement-report.rq`` lists every statement of
the compared properties with its rank, its value as stated, the range and
dates it carries, and its stated-in sources; ``place-containment.rq`` lists
the recorded location and part-of statements, each with its rank and
whether a source states it, and the merge redirects. This
module compares those rows. It does no I/O itself: the caller passes a
runner (``yizkr.core.sparql``), and the comparison is a function of the
rows alone.

Each rule of MODELING.md "Contradictions" is one function here:

* ``in_contention``: a deprecated statement is left out, and a preferred
  one is compared like any other, so nothing needs a preference;
* ``attributed``: a statement with no stated-in source takes no part;
* ``compared_values``: what a statement puts up for comparison, its own
  value for a property that holds one true value per subject, or for a
  residence or a spouse the times ``dated_times`` names;
* ``dated_times``: a residence's or a marriage's start and end times,
  each with the range its statement states for that end: a start runs
  from its earliest date to its latest start date, an end from its
  earliest end date to its latest date (MODELING.md, "Marriages,
  residences and other periods");
* ``comparison_key``: a name compares as text, letter case and language
  tag aside, and an item through its merge redirect;
* ``span``: the period a date names, which is the range its source states
  or else the period its precision names (``period``, with centuries
  counted as Wikibase counts them); ``named`` and
  ``spans_nest`` then set aside a date whose span lies inside the other's;
* ``stored_alike_order``: two dates stored alike are compared through the
  ranges their statements state;
* ``followed``: a containment edge is a source's claim, and the path
  follows one that a source states and a curator has not deprecated, at
  any time it carries (ADR-0038 §5);
* ``place_within``: a place the graph records inside the other differs
  only in resolution;
* ``from_different_artifacts``: two readings of one artifact are not two
  sources, and a page fragment counts as its artifact; ``one_claim``: nor
  are two readings of one statement's value, however many artifacts
  it cites.

Every time the fetch lists, a statement's value or a qualifier's, is read
with the calendar model its value node states, and one before the common
era or in a calendar other than the proleptic Gregorian stops the report
with ``UnsupportedDate``, naming the entity and the statement (ADR-0034):
such a date is never compared.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from functools import cache
from itertools import permutations, product

import yaml

from yizkr.core.dates import check_time
from yizkr.model.schema import read_manifest_data
from yizkr.queries.publish import load_templates, render_for_instance

# the two library queries the report reads
FETCH = "disagreement-report"
CONTAINMENT = "place-containment"

XSD_DATETIME = "http://www.w3.org/2001/XMLSchema#dateTime"
OWL_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"
DEPRECATED = "http://wikiba.se/ontology#DeprecatedRank"

# Properties that hold at most one true value per subject: two values
# from different sources disagree. The dates are an entity's own (a birth,
# an event's point in time, start or end, or a document's date of making,
# its inception: ADR-0039); the last three identify a place.
SINGLE_VALUED = frozenset(
    {
        "date_of_birth",
        "date_of_death",
        "place_of_birth",
        "place_of_death",
        "father",
        "mother",
        "birth_name",
        "point_in_time",
        "start_time",
        "end_time",
        "inception",
        "coordinate_location",
        "geonames_id",
        "whg_place_id",
    }
)
# the place-valued ones, which the recorded containment refines
PLACES = frozenset({"place_of_birth", "place_of_death"})
# Properties whose values accumulate over a life (two spouses from one
# source are remarriages), compared only through the dates of one place
# or one spouse.
DATED = frozenset({"residence", "spouse"})
# every property the fetch lists
COMPARED = SINGLE_VALUED | DATED

# Wikibase's time precisions
CENTURY, DECADE, YEAR, MONTH, DAY = 7, 8, 9, 10, 11

Runner = Callable[[str], list[dict]]
# a span: the first and last day a date names, as yyyymmdd numbers; an
# open end is infinite
Span = tuple[float, float]


# -- what the fetch returns ---------------------------------------------------


@dataclass(frozen=True, order=True)
class Term:
    """An RDF term as the query service returns it: an IRI (``uri``), a
    literal with its datatype or language tag, or a blank node."""

    kind: str
    value: str
    datatype: str = ""
    lang: str = ""

    @classmethod
    def of(cls, binding: Mapping[str, str]) -> Term:
        kind = binding["type"]
        return cls(
            "literal" if kind == "typed-literal" else kind,
            binding["value"],
            binding.get("datatype", ""),
            binding.get("xml:lang", ""),
        )

    @property
    def is_time(self) -> bool:
        return self.kind == "literal" and self.datatype == XSD_DATETIME


@dataclass(frozen=True, order=True)
class Time:
    """A time as its source states it: an xsd:dateTime and Wikibase's
    precision for it (9 a year, 11 a day)."""

    value: str
    precision: int


@dataclass(frozen=True, order=True)
class Source:
    """A stated-in source (an artifact, or a page fragment of one), its
    label, and the artifact it belongs to."""

    iri: str
    label: str
    artifact: str

    @property
    def qid(self) -> str:
        return self.iri.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Statement:
    """One statement of a compared property, as the fetch lists it: its
    value as stated, the earliest and latest dates its source gives (and
    on a residence or a spouse, the latest start and earliest end dates
    that bound its ends), the start and end times and points in time it
    carries as qualifiers, and its stated-in sources."""

    iri: str
    entity: str
    entity_label: str
    property: str  # the manifest key
    rank: str
    value: Term
    value_label: str = ""
    precision: int | None = None
    earliest: frozenset[Time] = frozenset()
    latest: frozenset[Time] = frozenset()
    latest_start: frozenset[Time] = frozenset()
    earliest_end: frozenset[Time] = frozenset()
    start: frozenset[Time] = frozenset()
    end: frozenset[Time] = frozenset()
    point_in_time: frozenset[Time] = frozenset()
    sources: frozenset[Source] = frozenset()


class Containment:
    """The recorded hierarchy: location and part-of edges, which lead from
    an item to what contains it, and merge redirects, which make two items
    one."""

    def __init__(
        self,
        edges: Iterable[tuple[str, str]] = (),
        redirects: Iterable[tuple[str, str]] = (),
    ):
        self._up: defaultdict[str, set[str]] = defaultdict(set)
        self._across: defaultdict[str, set[str]] = defaultdict(set)
        self._redirects: dict[str, str] = {}
        self._reachable: dict[str, frozenset[str]] = {}
        for item, container in edges:
            self.add_edge(item, container)
        for redirect, target in redirects:
            self.add_redirect(redirect, target)

    def add_edge(self, item: str, container: str) -> None:
        self._up[item].add(container)
        self._reachable.clear()

    def add_redirect(self, redirect: str, target: str) -> None:
        self._redirects[redirect] = target
        self._across[redirect].add(target)
        self._across[target].add(redirect)
        self._reachable.clear()

    def canonical(self, iri: str) -> str:
        """The item a merge redirect resolves to; any other IRI itself."""
        return self._redirects.get(iri, iri)

    def reachable(self, iri: str) -> frozenset[str]:
        """Every item one step or more from `iri`, each step up an edge or
        across a redirect either way."""
        found = self._reachable.get(iri)
        if found is None:
            seen: set[str] = set()
            frontier = [iri]
            while frontier:
                node = frontier.pop()
                for steps in (self._up, self._across):
                    for step in steps.get(node, ()):
                        if step not in seen:
                            seen.add(step)
                            frontier.append(step)
            found = self._reachable[iri] = frozenset(seen)
        return found


def _cell(row: Mapping[str, dict], name: str) -> str:
    return row.get(name, {}).get("value", "")


def _time(row: Mapping[str, dict], name: str, where: str) -> Time | None:
    """The time a column holds, with its precision, or None. One that is
    unsupported, by its year or by the calendar model the column's
    ``Calendar`` cell states, raises UnsupportedDate naming `where`."""
    value = _cell(row, name)
    precision = _cell(row, f"{name}Precision")
    if not value or not precision:
        return None
    check_time(value, _cell(row, f"{name}Calendar") or None, where)
    return Time(value, int(precision))


def _where(row: Mapping[str, dict]) -> str:
    """The entity and the statement a fetch row is about, by their ids."""
    entity = row["entity"]["value"].rsplit("/", 1)[-1]
    statement = row["statement"]["value"].rsplit("/", 1)[-1]
    return f"{entity} statement {statement}"


# the fetch's time columns, by the Statement field each fills
TIME_COLUMNS = {
    "earliest": "earliest",
    "latest": "latest",
    "latest_start": "latestStart",
    "earliest_end": "earliestEnd",
    "start": "start",
    "end": "end",
    "point_in_time": "pointInTime",
}


def read_statements(
    rows: Iterable[Mapping[str, dict]], properties: Mapping[str, str]
) -> list[Statement]:
    """The fetch's rows as statements. The fetch lists a statement once per
    stated-in source (and per further value of a qualifier it carries
    twice); `properties` maps the manifest keys to the instance's ids.
    A time before the common era or in a calendar other than the
    proleptic Gregorian, as the value or a qualifier, raises
    UnsupportedDate naming the entity and the statement (ADR-0034)."""
    key_of = {pid: key for key, pid in properties.items()}
    found: dict[str, dict] = {}
    for row in rows:
        iri = row["statement"]["value"]
        where = _where(row)
        entry = found.get(iri)
        if entry is None:
            value = Term.of(row["value"])
            if value.is_time:
                check_time(value.value, _cell(row, "calendar") or None, where)
            precision = _cell(row, "precision")
            entry = found[iri] = {
                "iri": iri,
                "entity": row["entity"]["value"],
                "entity_label": _cell(row, "entityLabel"),
                "property": key_of[
                    row["property"]["value"].rsplit("/", 1)[-1]
                ],
                "rank": row["rank"]["value"],
                "value": value,
                "value_label": _cell(row, "valueLabel"),
                "precision": int(precision) if precision else None,
                "sources": set(),
                **{name: set() for name in TIME_COLUMNS},
            }
        for name, column in TIME_COLUMNS.items():
            time = _time(row, column, where)
            if time is not None:
                entry[name].add(time)
        if "source" in row:
            source = row["source"]["value"]
            entry["sources"].add(
                Source(
                    source,
                    _cell(row, "sourceLabel"),
                    _cell(row, "artifact") or source,
                )
            )
    return [
        Statement(
            **{
                name: frozenset(value) if isinstance(value, set) else value
                for name, value in entry.items()
            }
        )
        for entry in found.values()
    ]


def read_containment(rows: Iterable[Mapping[str, dict]]) -> Containment:
    """The containment query's rows: an edge per location or part-of
    statement that ``followed`` keeps, and each merge redirect."""
    containment = Containment()
    for row in rows:
        item, link, target = (
            row[name]["value"] for name in ("item", "link", "target")
        )
        if link == OWL_SAME_AS:
            containment.add_redirect(item, target)
        elif followed(_cell(row, "rank"), _cell(row, "stated") == "true"):
            containment.add_edge(item, target)
    return containment


# -- the rules ----------------------------------------------------------------


def in_contention(statement: Statement) -> bool:
    """Rank is an optional curator overlay: a statement a curator
    deprecated is left out, and a preferred one is compared like a normal
    one, so no row needs a preference."""
    return statement.rank != DEPRECATED


def attributed(statement: Statement) -> bool:
    """A statement with no stated-in source attributes its value to no
    artifact and takes no part."""
    return bool(statement.sources)


@dataclass(frozen=True)
class Aspect:
    """What two values must share to be compared: the entity and the
    property, and for a residence or a spouse the place or person (through
    merge redirects) and which of its dates."""

    entity: str
    property: str
    shared: str = ""
    qualifier: str = ""


@dataclass(frozen=True)
class Value:
    """A value as its source states it: the stored term, an item's label,
    a time's precision, and for a time the range its statement states for
    it, from an earliest to a latest date: for a residence's or a spouse's
    start or end, the bounds of that end (``dated_times``)."""

    term: Term
    label: str = ""
    precision: int | None = None
    earliest: Time | None = None
    latest: Time | None = None


@dataclass(frozen=True)
class Reading:
    """One value, as one statement attributes it."""

    statement: Statement
    aspect: Aspect
    value: Value


@dataclass(frozen=True)
class Dated:
    """One date of a residence or a spouse statement: the times it
    carries, and the earliest and latest dates that bound them."""

    times: frozenset[Time]
    earliest: frozenset[Time]
    latest: frozenset[Time]


# The qualifiers that bound each date compared (MODELING.md, "Marriages,
# residences and other periods"): a time value's earliest and latest date,
# and on a residence or a spouse statement each end's own pair, as on
# Wikidata's spouse (P26): a start runs from its earliest date to its
# latest start date, an end from its earliest end date to its latest date.
BOUNDS = {
    "": ("earliest_date", "latest_date"),
    "start_time": ("earliest_date", "latest_start_date"),
    "end_time": ("earliest_end_date", "latest_date"),
}


def dated_times(statement: Statement) -> dict[str, Dated]:
    """The dates a residence or a spouse statement is compared by, each
    with the range its source states for that date (``BOUNDS``): its start
    time, between its earliest date and its latest start date, and its end
    time, between its earliest end date and its latest date. The earliest
    date of a marriage never bounds its end, nor its latest date its start.
    A point in time on it dates an observation, not the residence or the
    marriage, and is not compared."""
    return {
        "start_time": Dated(
            statement.start, statement.earliest, statement.latest_start
        ),
        "end_time": Dated(
            statement.end, statement.earliest_end, statement.latest
        ),
    }


def _ranges(
    earliest: frozenset[Time], latest: frozenset[Time]
) -> list[tuple[Time | None, Time | None]]:
    """Each earliest date with each latest date, and None for a bound
    the statement does not state."""
    return list(product(sorted(earliest) or [None], sorted(latest) or [None]))


def compared_values(
    statement: Statement, containment: Containment
) -> Iterator[Reading]:
    """What a statement puts up for comparison. A property that holds one
    true value per subject compares the statement's own value. Residence
    and spouse accumulate values over a life, so their values never
    conflict; the same place or spouse dated differently by another source
    does, so they compare the times ``dated_times`` names, per place or
    spouse through merge redirects. Other properties compare nothing.

    A time is put up once for each earliest date that bounds it with each
    latest date, so a statement that carries two earliest dates puts its
    time up twice. A range dates a time only: an item, a name or any other
    value is put up once, with no range, so the earliest or latest date on
    its statement never sets it apart from the same value another source
    states."""
    if statement.property in SINGLE_VALUED:
        aspect = Aspect(statement.entity, statement.property)
        ranges = (
            _ranges(statement.earliest, statement.latest)
            if statement.value.is_time
            else [(None, None)]
        )
        for earliest, latest in ranges:
            value = Value(
                statement.value,
                statement.value_label,
                statement.precision,
                earliest,
                latest,
            )
            yield Reading(statement, aspect, value)
    elif statement.property in DATED:
        shared = containment.canonical(statement.value.value)
        for qualifier, dated in dated_times(statement).items():
            aspect = Aspect(
                statement.entity, statement.property, shared, qualifier
            )
            ranges = _ranges(dated.earliest, dated.latest)
            for time in sorted(dated.times):
                term = Term("literal", time.value, XSD_DATETIME)
                for earliest, latest in ranges:
                    value = Value(term, "", time.precision, earliest, latest)
                    yield Reading(statement, aspect, value)


def comparison_key(value: Value, containment: Containment) -> str:
    """What makes two values one value. A name compares as text, so
    spellings that differ only in letter case or language tag are one
    value. An item written before a merge still points at the redirect,
    so it compares as the item that redirect resolves to. Any other value
    compares as stored."""
    term = value.term
    if term.kind == "literal" and term.lang:
        return term.value.casefold()
    if term.kind == "uri":
        return containment.canonical(term.value)
    return term.value


_DATE = re.compile(r"^([+-]?\d+)-(\d{2})-(\d{2})")


def period(time: Time) -> Span:
    """The days a time's precision names, as yyyymmdd numbers: its day,
    month, year, decade or century. A century is counted as Wikibase counts
    it, from year 1: a date stored as any year from 1801 to 1900 names the
    19th century, 1801 to 1900, whichever year a writer chose for it. A
    precision coarser than a century is given its century too, which only
    orders two dates stored alike, since it names no span (``named``). A
    time before the common era raises UnsupportedDate (ADR-0034); this is
    a backstop, as ``read_statements`` has already refused one, naming its
    entity and statement."""
    match = _DATE.match(time.value)
    if match is None:
        raise ValueError(f"not an xsd:dateTime: {time.value!r}")
    check_time(time.value, None, "a compared date")
    year, month, day = (int(part) for part in match.groups())
    if time.precision >= DAY:
        first = last = year * 10000 + month * 100 + day
    elif time.precision == MONTH:
        first = year * 10000 + month * 100
        last = first + 99
    elif time.precision == YEAR:
        first = year * 10000
        last = first + 9999
    elif time.precision == DECADE:
        first = year // 10 * 10 * 10000
        last = first + 99999
    else:
        first_year = (year - 1) // 100 * 100 + 1
        first, last = first_year * 10000, (first_year + 99) * 10000 + 9999
    return first, last


def span(reading: Reading) -> Span | None:
    """The period a date names: the range its source states with earliest
    and latest dates, open-ended when only one is given ("after 1945" runs
    on from 1945), or else the period its precision names. A stated range
    replaces that period and is never widened back to it (rule 5): the
    year 1938 stated "between 1938 and 1939" spans both years, and a
    decade stated with a narrower range spans only the range. A
    residence's or a spouse's start or end takes the range stated for that
    end (``dated_times``). None for a value that is not a time."""
    value = reading.value
    if not value.term.is_time or value.precision is None:
        return None
    if value.earliest or value.latest:
        first = period(value.earliest)[0] if value.earliest else -math.inf
        last = period(value.latest)[1] if value.latest else math.inf
        return first, last
    return period(Time(value.term.value, value.precision))


def named(value: Value) -> bool:
    """A precision coarser than a century (a millennium) names no span, in
    the value or in the range stated for it: such a date is never set aside
    as a difference of resolution."""
    return all(
        time.precision >= CENTURY
        for time in (value.earliest, value.latest)
        if time is not None
    ) and (value.precision is not None and value.precision >= CENTURY)


def spans_nest(a: Span, b: Span) -> bool:
    """One span lies inside the other: 1909 and 16 March 1909, or "between
    1938 and 1939" and 22 June 1938."""
    return (a[0] <= b[0] and b[1] <= a[1]) or (b[0] <= a[0] and a[1] <= b[1])


def stored_alike_order(reading: Reading) -> tuple | None:
    """How two dates stored alike are told apart and put in order. The
    GEDCOM adapter stores "before 1926" as 1926, with 1926 as its latest
    date, and a decade is stored as its first year (the 1890s as 1890), so
    two dates stored alike may differ only through the ranges their
    statements state: they are ordered by their spans, then by which
    bounds each states and at what precision (below a century no span is
    named, so the bounds count there), and with the same span and bounds
    they are one value. A residence's or a spouse's start or end is
    ordered alike, by the range stated for that end."""
    extent = span(reading)
    if extent is None:
        return None
    value = reading.value
    bounds = tuple(
        time.precision if time is not None else -1
        for time in (value.earliest, value.latest)
    )
    return extent, bounds


def followed(rank: str, stated: bool) -> bool:
    """A location or part-of statement is a source's claim (ADR-0038 §1),
    and the containment path reads it as the comparison reads any value:
    one a curator deprecated is left out, and one with no stated-in source
    takes no part. A time it carries does not matter: one path, at any
    time, is enough (ADR-0038 §5)."""
    return rank != DEPRECATED and stated


def place_within(containment: Containment, inner: str, outer: str) -> bool:
    """The graph records `inner` inside `outer`: a path of location and
    part-of edges leads from one to the other, across merge redirects
    either way (an address in the town another source names)."""
    return outer in containment.reachable(inner)


def from_different_artifacts(a: Iterable[Source], b: Iterable[Source]) -> bool:
    """Two readings of one artifact are not two sources (ADR-0023 §4): a
    curator's correction beside the machine reading it corrects is not a
    contradiction. Two values disagree unless every source of both belongs
    to one and the same artifact, a page fragment counting as the artifact
    it is part of."""
    return len({source.artifact for source in (*a, *b)}) > 1


# -- pairing ------------------------------------------------------------------


def paired(x: Reading, y: Reading) -> bool:
    """Whether two readings of one aspect are compared at all: two values
    that differ, and two dates stored alike, which the ranges their
    statements state may set apart. Two readings of one statement (one that
    carries two earliest dates, say) pair like any others, so beside
    another statement's reading each keeps its sources; alone they are
    ``one_claim`` and are never listed."""
    if x.value.term != y.value.term:
        return True
    return x.value.term.is_time


def one_claim(x: Reading, y: Reading) -> bool:
    """Two readings of one statement's one value, which only the earliest
    or latest dates it carries twice set apart. Every artifact the
    statement cites states that value with all those dates alike, so no
    source disagrees with another: a row whose every pair is one claim is
    never listed, however many artifacts cite the statement. Two start
    times on one residence statement are two values, not one claim."""
    return x.statement.iri == y.statement.iri and x.value.term == y.value.term


def in_order(
    x: Reading,
    y: Reading,
    keys: Mapping[Reading, str],
    orders: Mapping[Reading, tuple | None],
) -> bool:
    """Each pair once: its two values in the order of their comparison
    keys, so two values with one key are never paired (one value), and two
    dates stored alike in their ``stored_alike_order``."""
    if x.value.term != y.value.term:
        return keys[x] < keys[y]
    if orders[x] is None or orders[y] is None:
        return False
    return orders[x] < orders[y]


def differ_in_resolution(
    x: Reading, y: Reading, containment: Containment
) -> bool:
    """A place recorded inside the other, or a date whose span lies inside
    the other's, differs only in resolution, not in substance."""
    if x.aspect.property in PLACES:
        a, b = x.value.term.value, y.value.term.value
        return place_within(containment, a, b) or place_within(
            containment, b, a
        )
    a, b = span(x), span(y)
    if a is None or b is None:
        return False
    if not (named(x.value) and named(y.value)):
        return False
    return spans_nest(a, b)


@dataclass(frozen=True)
class Side:
    """One value of a disagreement, with every source that states it."""

    value: Value
    sources: tuple[Source, ...]


@dataclass(frozen=True)
class Disagreement:
    """Two values that different artifacts attribute to one entity and
    property: for a residence or a spouse, two datings of one place or
    person. Neither is preferred; each is listed with its sources."""

    entity: str
    entity_label: str
    property: str
    property_label: str
    shared: str
    shared_label: str
    qualifier: str
    qualifier_label: str
    a: Side
    b: Side

    @property
    def entity_qid(self) -> str:
        return self.entity.rsplit("/", 1)[-1]


@cache
def property_labels() -> dict[str, str]:
    """The English label of each manifest property: the label the
    instance's property carries, since its id is found by that label."""
    raw = yaml.safe_load(read_manifest_data())
    return {key: spec["label"] for key, spec in raw["properties"].items()}


@dataclass
class _Row:
    a: list[Reading] = field(default_factory=list)
    b: list[Reading] = field(default_factory=list)


def _side(readings: list[Reading], containment: Containment) -> Side:
    """The value a row shows for readings that share a key (one spelling
    of a name, an item as the one its redirect resolves to) and the
    sources of all of them."""
    shown = min(
        (reading.value for reading in readings),
        key=lambda value: (value.term.value, value.term.lang),
    )
    if shown.term.kind == "uri":
        canonical = containment.canonical(shown.term.value)
        shown = replace(shown, term=replace(shown.term, value=canonical))
    sources = sorted(
        {
            source
            for reading in readings
            for source in reading.statement.sources
        }
    )
    return Side(shown, tuple(sources))


def compare(
    statements: Iterable[Statement], containment: Containment
) -> list[Disagreement]:
    """The report's rows: each pair of values that different artifacts
    attribute to one aspect, listed once, with differences of resolution
    and a statement's ``one_claim`` set aside. Sources that state one
    value alike are listed together."""
    aspects: defaultdict[Aspect, list[Reading]] = defaultdict(list)
    for statement in statements:
        if in_contention(statement) and attributed(statement):
            for reading in compared_values(statement, containment):
                aspects[reading.aspect].append(reading)

    # one row per pair of side keys: the readings on a side share a
    # comparison key, a precision and a stated range, and their sources
    # are listed together
    rows: dict[tuple, _Row] = {}
    for aspect, readings in aspects.items():
        keys = {r: comparison_key(r.value, containment) for r in readings}
        orders = {r: stored_alike_order(r) for r in readings}
        sides = {
            r: (keys[r], r.value.precision, r.value.earliest, r.value.latest)
            for r in readings
        }
        for x, y in permutations(readings, 2):
            if (
                paired(x, y)
                and in_order(x, y, keys, orders)
                and not differ_in_resolution(x, y, containment)
            ):
                row = rows.setdefault((aspect, sides[x], sides[y]), _Row())
                row.a.append(x)
                row.b.append(y)

    labels = property_labels()
    listed = []
    for (aspect, side_a, side_b), row in rows.items():
        a, b = _side(row.a, containment), _side(row.b, containment)
        if not from_different_artifacts(a.sources, b.sources):
            continue
        if all(one_claim(x, y) for x, y in zip(row.a, row.b, strict=True)):
            continue
        first = row.a[0].statement
        disagreement = Disagreement(
            entity=aspect.entity,
            entity_label=first.entity_label,
            property=aspect.property,
            property_label=labels.get(aspect.property, aspect.property),
            shared=aspect.shared,
            shared_label=first.value_label if aspect.shared else "",
            qualifier=aspect.qualifier,
            qualifier_label=labels.get(aspect.qualifier, aspect.qualifier),
            a=a,
            b=b,
        )
        order = (
            disagreement.entity_label,
            disagreement.entity,
            disagreement.property_label,
            disagreement.shared_label,
            disagreement.qualifier_label,
            _sortable(side_a),
            _sortable(side_b),
        )
        listed.append((order, disagreement))
    listed.sort(key=lambda item: item[0])
    return [disagreement for _, disagreement in listed]


def _sortable(side: tuple) -> tuple:
    """A side key in an order that never compares None with a value."""
    key, precision, *bounds = side
    return (
        key,
        -1 if precision is None else precision,
        *(
            (time.value, time.precision) if time else ("", -1)
            for time in bounds
        ),
    )


# -- entry points -------------------------------------------------------------


def disagreements(
    run: Runner,
    properties: Mapping[str, str],
    items: Mapping[str, str],
    concept_uri: str,
) -> list[Disagreement]:
    """The disagreement report of one instance: its two library queries,
    rendered for the instance's vocabulary and run with `run`, and
    compared. The static site's inconsistencies page reads it."""
    templates = load_templates()

    def fetch(name: str) -> list[dict]:
        return run(
            render_for_instance(
                templates[name], properties, items, concept_uri
            )
        )

    statements = read_statements(fetch(FETCH), properties)
    return compare(statements, read_containment(fetch(CONTAINMENT)))


def counts_by_property(rows: Iterable[Disagreement]) -> dict[str, int]:
    """How many disagreements each property shows: the report as aggregate
    counts, with no row about any one entity."""
    return dict(sorted(Counter(row.property for row in rows).items()))
