"""The "died by" dates a view derives, and the derived end of a union
(ADR-0040 §2.4, §2.6).

Nothing here is stored or written, and nothing is death evidence of its
own: the statement a bound is derived from is the evidence. One shared
derivation serves every reader that compares or shows a death: the
site's person page and timeline, the disagreement report, clustering's
death comparison and reconcile's roster. It is a pure function over
inputs with their references, so the same rule holds whether the inputs
come from a batch's claims before the write (``from_batch``, clustering,
which reads no wiki: ADR-0025 §2) or from the graph (``from_rows``, over
the rows of ``died-by.rq``).

The rule, worded as one rule:

* a death statement with no date (a `date_of_death` of unknown value) is
  bounded by its document's own date, the latest that any of the
  document's non-deprecated `inception` statements allows;
* the late spouse of a union a source ends by that death
  (`ended_by_death_of`) died no later than the latest date at which the
  same source presents the survivor as widowed (the survivor's
  `civil_status` `widowed`, by its `point_in_time`), or, where it gives
  none, by the document's own date (Q7, Q5);
* neither is ever narrower than the date it comes from (rule 5): a date's
  bound is its `latest_date` where it states one, else its value at its
  stated precision, and a date open at its late end ("after 1945", an
  earliest date with no latest) gives none;
* an undated document gives no bound, and nor does a burial (Q3).

A derived bound is compared as a range open at its early end, beside the
statements it comes from (``span_of``).

The derived end of a union (§2.6): where no source states how a union
ended, each partner's death span is the hull of their dated death
statements, each at its stated bounds, with the derived bounds; where one
partner's span lies wholly before the other's, the union ended by the
first's death (derived); otherwise each partner with death evidence gives
only "ended no later than X's death".
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    ItemValue,
    LocalRef,
    QidRef,
    TimeValue,
    UnknownValue,
    VocabRef,
)

# how a "died by" date was derived: from a death statement of unknown
# value, by its document's date; or for the late spouse of a stated union
# end, by the survivor's widowhood or the document's date
UNDATED_DEATH = "undated death"
UNION_END = "union end"
VIA = (UNDATED_DEATH, UNION_END)

PRECISIONS = ("century", "decade", "year", "month", "day")
# Wikibase's precision numbers, by name
WIKIBASE_PRECISION = {
    "century": 7,
    "decade": 8,
    "year": 9,
    "month": 10,
    "day": 11,
}
DEPRECATED = "http://wikiba.se/ontology#DeprecatedRank"

# a span of days as yyyymmdd numbers, either end open (infinite)
Span = tuple[float, float]


@dataclass(frozen=True, order=True)
class When:
    """A date at its stated precision: its first year for a decade or a
    century (1890 for the 1890s, 1801 for the 19th century, as Wikibase
    counts centuries)."""

    year: int
    month: int | None = None
    day: int | None = None
    precision: str = "day"

    @classmethod
    def of(cls, value: TimeValue) -> When:
        return cls(value.year, value.month, value.day, value.precision)

    @classmethod
    def parse(cls, stamp: str, precision: int | str) -> When:
        """A time as the query service gives it (an xsd:dateTime) at a
        Wikibase precision number or name; a precision coarser than a
        century is read as a century."""
        match = re.match(r"^\+?(-?\d+)-(\d{2})-(\d{2})", stamp)
        if match is None:
            raise ValueError(f"not an xsd:dateTime: {stamp!r}")
        year, month, day = (int(part) for part in match.groups())
        if isinstance(precision, str) and not precision.isdigit():
            name = precision
        else:
            number = int(precision)
            name = next(
                (n for n, p in WIKIBASE_PRECISION.items() if p == number),
                "day" if number > 11 else "century",
            )
        return cls(
            year,
            month if name in ("month", "day") else None,
            day if name == "day" else None,
            name,
        )

    def period(self) -> Span:
        """The days it names, as yyyymmdd numbers."""
        year = self.year
        if self.precision == "day":
            first = last = (
                year * 10000 + (self.month or 0) * 100 + (self.day or 0)
            )
        elif self.precision == "month":
            first = year * 10000 + (self.month or 0) * 100
            last = first + 99
        elif self.precision == "year":
            first = year * 10000
            last = first + 9999
        elif self.precision == "decade":
            first = year // 10 * 10 * 10000
            last = first + 99999
        else:
            first_year = (year - 1) // 100 * 100 + 1
            first = first_year * 10000
            last = (first_year + 99) * 10000 + 9999
        return first, last

    def __str__(self) -> str:
        if self.precision == "day":
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        if self.precision == "month":
            return f"{self.year:04d}-{self.month:02d}"
        if self.precision == "decade":
            return f"{self.year // 10 * 10}s"
        if self.precision == "century":
            return f"{(self.year - 1) // 100 + 1}. century"
        return f"{self.year:04d}"


@dataclass(frozen=True)
class Dating:
    """A date a statement gives, its value or its point in time, with the
    earliest and latest dates its source states for it, and whether the
    source gives it as approximate (circa), which may lie outside any
    span it states."""

    value: When | None = None
    earliest: When | None = None
    latest: When | None = None
    circa: bool = False

    def bound(self) -> When | None:
        """The latest it allows, never narrower than it: its latest date
        where stated, else its value at its precision; None when it is
        open at its late end (an earliest date with no latest), approximate
        (circa) or gives no date."""
        if self.circa:
            return None
        if self.latest is not None:
            return self.latest
        if self.earliest is not None or self.value is None:
            return None
        return self.value

    def span(self) -> Span | None:
        """The range it states, open where only one end is given, or else
        the period its value's precision names; open at both ends when it
        is approximate."""
        if self.circa:
            return -math.inf, math.inf
        if self.earliest is not None or self.latest is not None:
            first = (
                self.earliest.period()[0]
                if self.earliest is not None
                else -math.inf
            )
            last = (
                self.latest.period()[1]
                if self.latest is not None
                else math.inf
            )
            return first, last
        if self.value is None:
            return None
        return self.value.period()


@dataclass(frozen=True)
class DeathStatement:
    """A `date_of_death` statement: its person, its dating (None for the
    unknown value) and the documents it cites."""

    person: str
    dating: Dating | None
    sources: frozenset[str]
    deprecated: bool = False


@dataclass(frozen=True)
class UnionEnd:
    """A union's `ended_by_death_of` statement: the union, the partner
    whose death ended it, the union's partners as its participants give
    them, and the documents it cites."""

    union: str
    late: str
    partners: frozenset[str]
    sources: frozenset[str]
    deprecated: bool = False


@dataclass(frozen=True)
class Widowhood:
    """A `civil_status` `widowed` statement: its person, the date at which
    the source presents the status as holding (its point in time, None
    when undated) and the documents it cites."""

    person: str
    dating: Dating | None
    sources: frozenset[str]
    deprecated: bool = False


@dataclass(frozen=True)
class Inputs:
    """What the derivation reads: the death statements, the union ends,
    the widowhoods, and each document's own dates (its non-deprecated
    `inception` statements), by document."""

    deaths: tuple[DeathStatement, ...] = ()
    ends: tuple[UnionEnd, ...] = ()
    widowhoods: tuple[Widowhood, ...] = ()
    inceptions: Mapping[str, tuple[Dating, ...]] = field(default_factory=dict)


@dataclass(frozen=True, order=True)
class DiedBy:
    """A "died by" date a view derives (ADR-0040 §2.4): ``person`` died no
    later than ``by``, at its precision, per the document ``source``, as
    ``via`` (VIA) derives it. It is labelled as derived wherever it shows,
    and never stored."""

    person: str
    by: When
    source: str
    via: str

    def span(self) -> Span:
        """The range it allows: open at its early end."""
        return -math.inf, self.by.period()[1]


def _latest(bounds: Iterable[When | None]) -> When | None:
    """The bound that allows the latest date, or None when any of them is
    open (None) or none is given: never narrower than any one of them."""
    found = list(bounds)
    if not found or any(bound is None for bound in found):
        return None
    return max(found, key=lambda when: when.period()[1])


def document_bound(
    inceptions: Mapping[str, Sequence[Dating]], document: str
) -> When | None:
    """The latest date a document's own dates allow, at its precision, or
    None for a document with none or with one open at its late end."""
    return _latest(dating.bound() for dating in inceptions.get(document, ()))


def died_by(inputs: Inputs) -> list[DiedBy]:
    """Every "died by" date the inputs give, sorted (the module docstring
    gives the rule). A deprecated statement gives none, and a widowhood
    counts only where it cites the union end's own document and names one
    of its survivors. Where a union's partners are not given, the
    document's own date bounds the late spouse."""
    found: set[DiedBy] = set()
    for death in inputs.deaths:
        if death.deprecated or death.dating is not None:
            continue
        for source in death.sources:
            bound = document_bound(inputs.inceptions, source)
            if bound is not None:
                found.add(DiedBy(death.person, bound, source, UNDATED_DEATH))
    for end in inputs.ends:
        if end.deprecated:
            continue
        survivors = end.partners - {end.late}
        for source in end.sources:
            presented = [
                widowhood
                for widowhood in inputs.widowhoods
                if not widowhood.deprecated
                and widowhood.person in survivors
                and source in widowhood.sources
            ]
            own = document_bound(inputs.inceptions, source)
            bound = (
                _latest(
                    (
                        widowhood.dating.bound()
                        if widowhood.dating is not None
                        and widowhood.dating.value is not None
                        else own
                    )
                    for widowhood in presented
                )
                if presented
                else own
            )
            if bound is not None:
                found.add(DiedBy(end.late, bound, source, UNION_END))
    return sorted(found)


def by_person(found: Iterable[DiedBy]) -> dict[str, list[DiedBy]]:
    """The derived bounds, grouped by person."""
    grouped: dict[str, list[DiedBy]] = {}
    for bound in found:
        grouped.setdefault(bound.person, []).append(bound)
    return grouped


def death_span(
    person: str, deaths: Iterable[DeathStatement], derived: Iterable[DiedBy]
) -> Span | None:
    """The hull of a person's non-deprecated dated death statements, each
    at its stated bounds, and of their derived bounds (each open at its
    early end); None when nothing dates the death. No one statement is
    picked (rule 6)."""
    spans = [
        span
        for death in deaths
        if death.person == person
        and not death.deprecated
        and death.dating is not None
        and (span := death.dating.span()) is not None
    ]
    spans += [bound.span() for bound in derived if bound.person == person]
    if not spans:
        return None
    return min(s[0] for s in spans), max(s[1] for s in spans)


def death_evidence(
    person: str, deaths: Iterable[DeathStatement], ends: Iterable[UnionEnd]
) -> bool:
    """Whether a non-deprecated statement states the person's death: a
    death statement, dated or unknown, or a union ended by their death."""
    return any(
        death.person == person and not death.deprecated for death in deaths
    ) or any(end.late == person and not end.deprecated for end in ends)


@dataclass(frozen=True)
class DerivedEnd:
    """How a view derives a union's end where no source states it (§2.6):
    ``by_death_of`` the partner whose death span lies wholly before the
    other's, or else ``no_later_than``, each partner with death evidence
    (the union ended no later than their death)."""

    by_death_of: str | None = None
    no_later_than: tuple[str, ...] = ()


def derived_end(
    partners: Iterable[str],
    deaths: Iterable[DeathStatement],
    ends: Iterable[UnionEnd],
    derived: Iterable[DiedBy],
) -> DerivedEnd | None:
    """The derived end of a union of `partners`, or None when no partner
    has death evidence. The caller asks only for a union with no
    non-deprecated stated end."""
    partners = sorted(set(partners))
    deaths, ends, derived = list(deaths), list(ends), list(derived)
    spans = {p: death_span(p, deaths, derived) for p in partners}
    if len(partners) == 2:
        first, second = partners
        a, b = spans[first], spans[second]
        if a is not None and b is not None:
            if a[1] < b[0]:
                return DerivedEnd(by_death_of=first)
            if b[1] < a[0]:
                return DerivedEnd(by_death_of=second)
    with_evidence = tuple(
        p
        for p in partners
        if death_evidence(p, deaths, ends) or spans[p] is not None
    )
    if not with_evidence:
        return None
    return DerivedEnd(no_later_than=with_evidence)


# -- inputs from a batch's claims (clustering, before the write) ------------


def _key(ref) -> str:
    """A claim's ref as the derivation keys it within one batch."""
    if isinstance(ref, LocalRef):
        return ref.local_id
    if isinstance(ref, QidRef):
        return ref.qid
    if isinstance(ref, ArtifactRef):
        return ref.sha1
    if isinstance(ref, VocabRef):
        return f"vocab:{ref.key}"
    raise TypeError(f"no key for {ref!r}")


def _claim_dating(claim, point_in_time: bool = False) -> Dating | None:
    """A claim's dating: its value (or, with `point_in_time`, its point in
    time qualifier) with its earliest and latest dates; None when it has
    no date to give."""
    qualifiers = {
        q.property: q.value
        for q in claim.qualifiers
        if isinstance(q.value, TimeValue)
    }
    value = (
        qualifiers.get("point_in_time")
        if point_in_time
        else claim.value
        if isinstance(claim.value, TimeValue)
        else None
    )
    if value is None:
        return None
    earliest = qualifiers.get("earliest_date")
    latest = qualifiers.get("latest_date")
    circa = any(
        q.property == "sourcing_circumstances"
        and isinstance(q.value, ItemValue)
        and isinstance(q.value.value, VocabRef)
        and q.value.value.key == "circa"
        for q in claim.qualifiers
    )
    return Dating(
        When.of(value),
        When.of(earliest) if earliest is not None else None,
        When.of(latest) if latest is not None else None,
        circa,
    )


def from_batch(batch: ClaimBatch, key=_key) -> Inputs:
    """The derivation's inputs from one batch's claims: each
    `date_of_death`, each union `ended_by_death_of` with the union's
    participants in the batch, each `civil_status` `widowed` with its
    point in time, and each document's `inception` claims about itself
    (or an artifact it places), by the SHA-1 of the document. People and
    unions are keyed by `key` (a local id, a QID), documents by SHA-1.
    Nothing in a batch is deprecated."""
    partners: dict[str, set[str]] = {}
    for claim in batch.claims:
        if claim.property == "participant" and isinstance(
            claim.value, ItemValue
        ):
            partners.setdefault(key(claim.subject), set()).add(
                key(claim.value.value)
            )
    deaths, ends, widowhoods = [], [], []
    inceptions: dict[str, list[Dating]] = {}
    for claim in batch.claims:
        source = frozenset({claim.source.artifact.sha1})
        if claim.property == "date_of_death":
            if isinstance(claim.value, UnknownValue):
                deaths.append(DeathStatement(key(claim.subject), None, source))
            elif isinstance(claim.value, TimeValue):
                deaths.append(
                    DeathStatement(
                        key(claim.subject), _claim_dating(claim), source
                    )
                )
        elif claim.property == "ended_by_death_of" and isinstance(
            claim.value, ItemValue
        ):
            union = key(claim.subject)
            ends.append(
                UnionEnd(
                    union,
                    key(claim.value.value),
                    frozenset(partners.get(union, ())),
                    source,
                )
            )
        elif (
            claim.property == "civil_status"
            and isinstance(claim.value, ItemValue)
            and isinstance(claim.value.value, VocabRef)
            and claim.value.value.key == "widowed"
        ):
            widowhoods.append(
                Widowhood(
                    key(claim.subject),
                    _claim_dating(claim, point_in_time=True),
                    source,
                )
            )
        elif (
            claim.property == "inception"
            and isinstance(claim.subject, ArtifactRef)
            and isinstance(claim.value, TimeValue)
        ):
            dating = _claim_dating(claim)
            if dating is not None:
                inceptions.setdefault(claim.subject.sha1, []).append(dating)
    return Inputs(
        tuple(deaths),
        tuple(ends),
        tuple(widowhoods),
        {doc: tuple(found) for doc, found in inceptions.items()},
    )


def merge(*given: Inputs) -> Inputs:
    """Several batches' inputs as one."""
    inceptions: dict[str, tuple[Dating, ...]] = {}
    for inputs in given:
        for doc, found in inputs.inceptions.items():
            inceptions[doc] = inceptions.get(doc, ()) + tuple(found)
    return Inputs(
        tuple(d for inputs in given for d in inputs.deaths),
        tuple(e for inputs in given for e in inputs.ends),
        tuple(w for inputs in given for w in inputs.widowhoods),
        inceptions,
    )


# -- inputs from the graph (the site, the disagreement report, reconcile) ---

# the library query whose rows ``from_rows`` reads
QUERY = "died-by"


def _cell(row: Mapping[str, Mapping[str, str]], name: str) -> str:
    return row.get(name, {}).get("value", "")


def unknown_term(binding: Mapping[str, str] | None) -> bool:
    """Whether a query result's term is Wikibase's "some value": a blank
    node, or the skolem IRI the query service gives one."""
    if not binding:
        return False
    return binding.get(
        "type"
    ) == "bnode" or "/.well-known/genid/" in binding.get("value", "")


def _when(row: Mapping[str, Mapping[str, str]], name: str) -> When | None:
    stamp = _cell(row, name)
    precision = _cell(row, f"{name}Precision")
    if not stamp or not precision:
        return None
    return When.parse(stamp, precision)


def _row_dating(row, name: str) -> Dating | None:
    value = _when(row, name)
    if value is None:
        return None
    return Dating(
        value,
        _when(row, "earliest"),
        _when(row, "latest"),
        _cell(row, "circa") == "true",
    )


def from_rows(rows: Iterable[Mapping[str, Mapping[str, str]]]) -> Inputs:
    """The derivation's inputs from the rows of ``died-by.rq``: one row per
    statement, stated-in source and partner, of kind ``death``, ``end``,
    ``widowed`` or ``inception``. Entities and documents are keyed by
    their IRIs, a page fragment by the artifact it belongs to."""
    entries: dict[str, dict] = {}
    for row in rows:
        statement = _cell(row, "statement")
        entry = entries.setdefault(
            statement,
            {
                "kind": _cell(row, "kind"),
                "subject": _cell(row, "subject"),
                "value": row.get("value"),
                "deprecated": _cell(row, "rank") == DEPRECATED,
                "dating": None,
                "sources": set(),
                "partners": set(),
            },
        )
        kind = entry["kind"]
        if entry["dating"] is None:
            entry["dating"] = _row_dating(
                row, "pointInTime" if kind == "widowed" else "time"
            )
        if _cell(row, "artifact"):
            entry["sources"].add(_cell(row, "artifact"))
        if _cell(row, "partner"):
            entry["partners"].add(_cell(row, "partner"))
    deaths, ends, widowhoods = [], [], []
    inceptions: dict[str, list[Dating]] = {}
    for entry in entries.values():
        sources = frozenset(entry["sources"])
        deprecated = entry["deprecated"]
        kind, subject = entry["kind"], entry["subject"]
        if kind == "death":
            unknown = unknown_term(entry["value"])
            if not unknown and entry["dating"] is None:
                continue
            deaths.append(
                DeathStatement(
                    subject,
                    None if unknown else entry["dating"],
                    sources,
                    deprecated,
                )
            )
        elif kind == "end":
            value = entry["value"] or {}
            if value.get("type") != "uri" or unknown_term(value):
                continue
            ends.append(
                UnionEnd(
                    subject,
                    value["value"],
                    frozenset(entry["partners"]),
                    sources,
                    deprecated,
                )
            )
        elif kind == "widowed":
            widowhoods.append(
                Widowhood(subject, entry["dating"], sources, deprecated)
            )
        elif kind == "inception" and not deprecated:
            if entry["dating"] is not None:
                inceptions.setdefault(subject, []).append(entry["dating"])
    return Inputs(
        tuple(deaths),
        tuple(ends),
        tuple(widowhoods),
        {doc: tuple(found) for doc, found in inceptions.items()},
    )
