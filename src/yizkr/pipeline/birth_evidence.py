"""Attributed birth statements for identity advice, without a winning date.

The birth query keeps direct and principal-event statements at every rank.
Collection does not decide identity or privacy. Rendering marks deprecated
dates or participant links as inactive, while retaining them for inspection.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.core.dates import check_time, time_text
from yizkr.queries.publish import load_templates, render_for_instance

QUERY = "birth-evidence"
Rank = Literal["normal", "preferred", "deprecated"]
_RANKS = {
    f"http://wikiba.se/ontology#{name.title()}Rank": name
    for name in ("normal", "preferred", "deprecated")
}


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BirthTime(_Record):
    value: str
    precision: int = Field(ge=0, le=14)
    calendar: str | None = None

    @model_validator(mode="after")
    def supported(self):
        check_time(self.value, self.calendar, "recorded birth evidence")
        return self

    def text(self) -> str:
        return time_text(self.value, self.precision, "birth evidence")


class BirthTerm(_Record):
    """A reference value, preserving RDF kind, datatype and language."""

    kind: Literal["uri", "literal", "bnode"]
    value: str
    datatype: str = ""
    language: str = ""


class BirthReferenceSnak(_Record):
    property: str
    value: BirthTerm
    time: BirthTime | None = None


class BirthSource(_Record):
    source: str
    artifact: str


class BirthReference(_Record):
    reference: str
    snaks: tuple[BirthReferenceSnak, ...] = ()
    sources: tuple[BirthSource, ...] = ()

    def facts(self) -> str:
        values = []
        for snak in self.snaks:
            if snak.time is not None:
                shown = snak.time.text()
            elif snak.value.kind == "uri":
                short = _short(snak.value.value)
                shown = (
                    short
                    if re.fullmatch(r"[PQ][1-9][0-9]*", short)
                    else snak.value.value
                )
            else:
                shown = json.dumps(snak.value.value, ensure_ascii=False)
                if snak.value.language:
                    shown += f"@{snak.value.language}"
            values.append(f"{snak.property}={shown}")
        for source in self.sources:
            if source.source != source.artifact:
                values.append(
                    f"source {source.source} belongs to {source.artifact}"
                )
        return f"{self.reference}: " + ", ".join(values)


class BirthParticipation(_Record):
    statement: str
    rank: Rank
    references: tuple[BirthReference, ...] = ()


class BirthEvidence(_Record):
    person: str
    statement: str
    rank: Rank
    value: BirthTime | None
    value_kind: Literal["time", "somevalue", "novalue"] = "time"
    earliest: tuple[BirthTime, ...] = ()
    latest: tuple[BirthTime, ...] = ()
    circumstances: tuple[str, ...] = ()
    references: tuple[BirthReference, ...] = ()
    event: str | None = None
    participation: BirthParticipation | None = None

    @model_validator(mode="after")
    def event_has_principal(self):
        if (self.event is None) != (self.participation is None):
            raise ValueError("a birth event requires its principal statement")
        if (self.value_kind == "time") != (self.value is not None):
            raise ValueError("a dated birth requires exactly one time value")
        return self

    def facts(self) -> str:
        origin = (
            f"birth event {self.event} point_in_time"
            if self.event
            else "date_of_birth"
        )
        inactive = self.rank == "deprecated" or (
            self.participation is not None
            and self.participation.rank == "deprecated"
        )
        shown = (
            self.value.text()
            if self.value is not None
            else "unknown value (date not stated)"
            if self.value_kind == "somevalue"
            else "no value"
        )
        parts = [
            f"{origin}: {shown}",
            f"rank={self.rank}",
            f"statement={self.statement}",
        ]
        if inactive:
            parts.append("not active identity evidence")
        for name in ("earliest", "latest"):
            parts.extend(
                f"{name}_date={value.text()}" for value in getattr(self, name)
            )
        if self.circumstances:
            parts.append(
                "sourcing_circumstances=" + ", ".join(self.circumstances)
            )
        parts.extend(_reference_facts(self.references))
        if self.participation is not None:
            parts.append(
                f"principal={self.person} via {self.participation.statement} "
                f"rank={self.participation.rank}"
            )
            parts.extend(
                "principal " + text
                for text in _reference_facts(self.participation.references)
            )
        return "; ".join(parts)


def _reference_facts(references):
    if not references:
        return ("references: none recorded",)
    return tuple(f"reference [{ref.facts()}]" for ref in references)


def _short(value: str) -> str:
    return value.rsplit("/", 1)[-1]


def _cell(row: Mapping, name: str) -> str:
    return row.get(name, {}).get("value", "")


def _rank(row, name):
    try:
        return _RANKS[_cell(row, name)]
    except KeyError as exc:
        raise ValueError(f"birth evidence has an unknown {name}") from exc


def _time(row, name):
    value = _cell(row, name)
    if not value:
        return None
    calendar = _cell(row, name + "Calendar") or None
    check_time(
        value,
        calendar,
        f"birth statement {_cell(row, 'statement')} ({name})",
    )
    return BirthTime(
        value=value,
        precision=int(_cell(row, name + "Precision")),
        calendar=calendar,
    )


def _unique(values):
    """Canonical order independent of joins, query order or duplicate rows."""
    return tuple(
        sorted(set(values), key=lambda value: value.model_dump_json())
    )


def _references(rows, prefix, property_keys):
    grouped = defaultdict(list)
    for row in rows:
        reference = _cell(row, prefix + "Reference")
        if reference:
            grouped[reference].append(row)
    references = []
    for reference, found in sorted(grouped.items()):
        snaks, sources = [], []
        for row in found:
            binding = row.get(prefix + "ReferenceValue")
            if binding:
                kind = binding["type"]
                property_qid = _short(_cell(row, prefix + "ReferenceProperty"))
                snaks.append(
                    BirthReferenceSnak(
                        property=property_keys.get(property_qid, property_qid),
                        value=BirthTerm(
                            kind="literal"
                            if kind == "typed-literal"
                            else kind,
                            value=binding["value"],
                            datatype=binding.get("datatype", ""),
                            language=binding.get("xml:lang", ""),
                        ),
                        time=_time(row, prefix + "ReferenceTime"),
                    )
                )
            source = _cell(row, prefix + "Source")
            if source:
                sources.append(
                    BirthSource(
                        source=_short(source),
                        artifact=_short(_cell(row, prefix + "Artifact")),
                    )
                )
        references.append(
            BirthReference(
                reference=reference,
                snaks=_unique(snaks),
                sources=_unique(sources),
            )
        )
    return tuple(references)


def from_rows(
    rows: Iterable[Mapping],
    properties: Mapping[str, str],
    items: Mapping[str, str],
) -> dict[str, tuple[BirthEvidence, ...]]:
    """Group date statements and principal edges, retaining attribution."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[
            tuple(
                _cell(row, key)
                for key in ("person", "statement", "participation")
            )
        ].append(row)
    property_keys = {qid: key for key, qid in properties.items()}
    item_keys = {qid: key for key, qid in items.items()}
    by_person = defaultdict(list)
    for (person, statement, participation), found in sorted(grouped.items()):
        row = found[0]
        # Wikibase statements have one value and rank. Do not select one
        # arbitrarily if malformed rows disagree on either.
        value_fields = (
            "rank",
            "event",
            "participationRank",
            "valueKind",
            "time",
            "timePrecision",
            "timeCalendar",
        )
        if (
            len({tuple(_cell(r, key) for key in value_fields) for r in found})
            != 1
        ):
            raise ValueError(
                f"birth statement {statement} has inconsistent rows"
            )
        evidence = BirthEvidence(
            person=_short(person),
            statement=statement,
            rank=_rank(row, "rank"),
            value=_time(row, "time"),
            value_kind=_cell(row, "valueKind") or "time",
            earliest=_unique(
                time
                for r in found
                if (time := _time(r, "earliest")) is not None
            ),
            latest=_unique(
                time for r in found if (time := _time(r, "latest")) is not None
            ),
            circumstances=tuple(
                sorted(
                    {
                        item_keys.get(_short(value), _short(value))
                        for r in found
                        if (value := _cell(r, "circumstance"))
                    }
                )
            ),
            references=_references(found, "date", property_keys),
            event=_short(_cell(row, "event")) or None,
            participation=(
                BirthParticipation(
                    statement=participation,
                    rank=_rank(row, "participationRank"),
                    references=_references(found, "principal", property_keys),
                )
                if participation
                else None
            ),
        )
        by_person[evidence.person].append(evidence)
    return {
        person: _unique(evidence) for person, evidence in by_person.items()
    }


def fetch_birth_evidence(run, properties, items, concept_uri):
    query = render_for_instance(
        load_templates()[QUERY], properties, items, concept_uri
    )
    return from_rows(run(query), properties, items)
