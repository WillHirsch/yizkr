#!/usr/bin/env python3
"""Write a deterministic synthetic Wikibase graph for query reviews.

  uv run python scripts/synthetic_graph.py --persons 250 --seed 7 \\
      --out DIR/g.ttl

writes DIR/g.ttl and, beside it, the vocabulary map DIR/g.vocab.json
(--vocab names another path). Load the graph into the query-engine
sandbox (scripts/wdqs_sandbox.py) and run the repository's query
templates against it: the map resolves their {P:key}/{Q:key}
placeholders to the graph's ids, P1.. and Q1.. for every manifest key in
manifest order.

The same --persons and --seed give the same bytes on a given Python
version (the random module keeps its draws stable only within a
version). The person count sets the size: places, artifacts, events and
statements scale with it. Every entity is invented and labelled with a
neutral placeholder ("Person 17", "Place 3", "Artifact 9", "Artifact 9,
page 2", "Event 4", "Run 1"), and so are names and other text values.
Coordinates lie in open ocean and external ids out of their registries'
ranges, so nothing points at a real place. Generated ids say what they
name: Person 17 is Q100017, Place 3 Q200003, Artifact 9 Q300009;
fragments are Q4xxxxx, events Q5xxxxx, runs Q6xxxxx, redirects Q9xxxxx.

The RDF is the query service's form of Wikibase data, as the updater
writes it: the manifest's properties (with the wikibase: predicates the
queries join on) and items, with their labels; statements (p:, ps:, pq:,
wikibase:rank) with time and coordinate value nodes (psv:, pqv:,
wikibase:timePrecision) and references (prov:wasDerivedFrom to wdref:
nodes carrying pr: snaks, one node per distinct reference, shared by
every statement that cites it); truthy wdt: triples for best-rank
statements only (preferred if any, else normal); and redirects, merged
items that are owl:sameAs their target and nothing else, which some
statements still point at. Like the updater, it keeps the type triples
of properties, best-rank statements and value nodes, and writes none for
items, statements or references. Most machine references carry the
claim's transcription excerpt (a placeholder, "Excerpt 12"), as the
writer's do, so a source reference is nearly one node per statement,
while registrar, run and containment references are shared.

Shapes, each present at least once from 20 persons up (a slot runs
through its scenarios once before drawing them by weight):
- dates of birth and death at day, month, year, decade and century
  precision; earliest/latest ranges, open-ended ones, circa; agreement,
  differences of resolution, contradictions between artifacts and
  between the fragments of one artifact, a curator's correction beside
  the machine reading; the same mix for events' own dates; and a date
  of death of unknown value (ADR-0040 §2.2), alone or beside a dated one;
- places of birth and death, contained in each other or not, and through
  redirects; parents, contradicting or redirected; spouses stated on
  both partners with start, end or point-in-time qualifiers, sometimes
  dated differently; residences with start and end times, redated,
  returned to or ranged; birth names varying in case, language tag or
  spelling;
- a place hierarchy (region > district > town by part of, address by
  location) with coordinates and external ids that sometimes disagree;
  artifacts with page fragments, photographs inside presentations, and
  events with participants and locations;
- deprecated and preferred ranks, on dates and on places too: a place
  of birth or death stated only at deprecated rank (no truthy value), and
  a preferred place beside a deprecated one; machine statements (stated
  in, and a run) beside curator ones (stated in only); statements without
  stated in: run-only references (containment edges among them), and a
  few with no reference at all, for the unreferenced-statements report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "src" / "yizkr" / "model" / "manifest.yaml"
CONCEPT_URI = "http://synthetic.example"
GREGORIAN = "http://www.wikidata.org/entity/Q1985727"
EARTH = "http://www.wikidata.org/entity/Q2"

# Below this size some shapes may be missing; see Builder.wants().
MIN_PERSONS = 20
MAX_PERSONS = 99_999

# Generated ids by kind: Person 17 is Q100017.
ID_BASE = {
    "person": 100_000,
    "place": 200_000,
    "artifact": 300_000,
    "fragment": 400_000,
    "event": 500_000,
    "run": 600_000,
    "redirect": 900_000,
}
KIND_LABEL = {
    "person": "Person",
    "place": "Place",
    "artifact": "Artifact",
    "event": "Event",
    "run": "Run",
}

CENTURY, DECADE, YEAR, MONTH, DAY = 7, 8, 9, 10, 11
RANKS = ("Normal", "Preferred", "Deprecated")

PREFIXES = {
    "wd": f"{CONCEPT_URI}/entity/",
    "wds": f"{CONCEPT_URI}/entity/statement/",
    "wdv": f"{CONCEPT_URI}/value/",
    "wdref": f"{CONCEPT_URI}/reference/",
    "wdt": f"{CONCEPT_URI}/prop/direct/",
    "p": f"{CONCEPT_URI}/prop/",
    "ps": f"{CONCEPT_URI}/prop/statement/",
    "psv": f"{CONCEPT_URI}/prop/statement/value/",
    "pq": f"{CONCEPT_URI}/prop/qualifier/",
    "pqv": f"{CONCEPT_URI}/prop/qualifier/value/",
    "pr": f"{CONCEPT_URI}/prop/reference/",
    "prv": f"{CONCEPT_URI}/prop/reference/value/",
    "wdno": f"{CONCEPT_URI}/prop/novalue/",
    "wikibase": "http://wikiba.se/ontology#",
    "prov": "http://www.w3.org/ns/prov#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "geo": "http://www.opengis.net/ont/geosparql#",
}

PROPERTY_TYPES = {
    "wikibase-item": "WikibaseItem",
    "string": "String",
    "time": "Time",
    "globe-coordinate": "GlobeCoordinate",
    "external-id": "ExternalId",
    "monolingualtext": "Monolingualtext",
    "url": "Url",
    "quantity": "Quantity",
}

# Scenario mixes per kind of slot. The first occurrences of each slot run
# its scenarios in this order, so every one appears in a small graph; the
# weights apply after that.
DATE_SCENARIOS = {
    "conflict": 12,
    "fragments": 5,
    "range": 6,
    "open_range": 3,
    "circa": 4,
    "resolution": 8,
    "agree": 8,
    "correction": 3,
    "deprecated": 2,
    "preferred": 2,
    "run_only": 2,
    "unreferenced": 1,
    "single": 44,
}
PRECISIONS = {CENTURY: 5, DECADE: 10, MONTH: 15, YEAR: 25, DAY: 45}
SCENARIOS = {
    # a person's dates of birth and death, an event's own dates, and the
    # precision each kind of date is stated at
    "date": DATE_SCENARIOS,
    "event_date": DATE_SCENARIOS,
    "date_precision": PRECISIONS,
    "event_date_precision": PRECISIONS,
    "place": {
        "conflict": 12,
        "resolution": 12,
        "redirect": 6,
        "deprecated_only": 2,
        "ranked": 3,
        "agree": 10,
        "single": 55,
    },
    "parent": {"conflict": 6, "redirect": 3, "single": 91},
    "residence": {
        "redated": 10,
        "returned": 5,
        "redirect": 3,
        "range": 3,
        "single": 79,
    },
    "marriage": {
        "redated": 15,
        "ended": 15,
        "observed": 15,
        "started": 55,
    },
    "name": {"case": 10, "language": 10, "spelling": 15, "single": 65},
    "identity": {"conflict": 8, "single": 92},
    "artifact": {
        "presentation": 5,
        "photograph": 15,
        "certificate": 35,
        "letter": 20,
        "census_page": 8,
        "newspaper_clipping": 5,
        "address_book": 5,
        "genealogical_report": 7,
    },
    "event": {
        "migration": 40,
        "military_service": 15,
        "deportation": 10,
        "imprisonment": 10,
        "release": 5,
        "birth": 5,
        "death": 15,
    },
}
# Events whose dates are a span rather than an instant.
SPAN_EVENTS = {"military_service", "imprisonment"}
KINSHIP_TYPES = ("cousin", "maternal aunt", "grandmother", "great-uncle")
GENERATIONS = 5


# -- values ------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    qid: str


@dataclass(frozen=True)
class Time:
    """A time value as the RDF stores it: parts finer than the precision
    are 01, a decade or century is its first year, a century counted as
    Wikibase counts it (the 19th is 1801 to 1900)."""

    date: str
    precision: int


@dataclass(frozen=True)
class Text:
    """Monolingual text."""

    text: str
    lang: str


@dataclass(frozen=True)
class Plain:
    """A string or external-id value."""

    text: str


@dataclass(frozen=True)
class Unknown:
    """Wikibase's "some value": a value its source states exists but does
    not give (ADR-0040 §2.2), which the query service gives as a skolem
    IRI of its own."""

    token: str


@dataclass(frozen=True)
class Coordinate:
    latitude: float
    longitude: float


def when(year: int, month: int = 1, day: int = 1, precision=DAY) -> Time:
    if precision == CENTURY:
        year, month, day = year - (year - 1) % 100, 1, 1
    elif precision == DECADE:
        year, month, day = year - year % 10, 1, 1
    elif precision == YEAR:
        month, day = 1, 1
    elif precision == MONTH:
        day = 1
    return Time(f"{year:04d}-{month:02d}-{day:02d}", precision)


def years_of(value: Time) -> tuple[int, int]:
    """The first and last year of the period a time value names."""
    first = int(value.date[:4])
    return first, first + {CENTURY: 99, DECADE: 9}.get(value.precision, 0)


def literal(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def term(value) -> str:
    if isinstance(value, Item):
        return f"wd:{value.qid}"
    if isinstance(value, Time):
        return f"{literal(value.date + 'T00:00:00Z')}^^xsd:dateTime"
    if isinstance(value, Text):
        return f"{literal(value.text)}@{value.lang}"
    if isinstance(value, Plain):
        return literal(value.text)
    if isinstance(value, Unknown):
        return f"<{CONCEPT_URI}/.well-known/genid/{value.token}>"
    if isinstance(value, Coordinate):
        point = f"Point({value.longitude:.4f} {value.latitude:.4f})"
        return f"{literal(point)}^^geo:wktLiteral"
    raise TypeError(f"no RDF form for {value!r}")


def digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


# A reference is a tuple of snaks, (property key, value) pairs.
def stated_in(source: str, page: int | None = None) -> tuple:
    snaks = [("stated_in", Item(source))]
    if page is not None:
        snaks.append(("page_number", Plain(str(page))))
    return tuple(snaks)


def by_run(run: str) -> tuple:
    return (("inferred_from_extraction_run", Item(run)),)


# -- vocabulary ---------------------------------------------------------------


@dataclass(frozen=True)
class Vocabulary:
    """The manifest's keys with the synthetic ids the graph gives them."""

    properties: dict[str, str]
    items: dict[str, str]
    property_labels: dict[str, str]
    property_types: dict[str, str]
    item_labels: dict[str, str]
    item_parents: dict[str, str]

    def as_map(self) -> dict:
        return {
            "concept_uri": CONCEPT_URI,
            "properties": dict(self.properties),
            "items": dict(self.items),
        }


def load_vocabulary(path: Path = MANIFEST) -> Vocabulary:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    properties, items = raw["properties"], raw["items"]
    return Vocabulary(
        properties={key: f"P{n}" for n, key in enumerate(properties, 1)},
        items={key: f"Q{n}" for n, key in enumerate(items, 1)},
        property_labels={k: v["label"] for k, v in properties.items()},
        property_types={
            k: PROPERTY_TYPES[v["datatype"]] for k, v in properties.items()
        },
        item_labels={k: v["label"] for k, v in items.items()},
        item_parents={
            k: v["subclass_of"]
            for k, v in items.items()
            if v.get("subclass_of")
        },
    )


# -- graph --------------------------------------------------------------------


@dataclass
class Statement:
    guid: str
    prop: str
    value: object
    rank: str
    qualifiers: tuple
    references: tuple


@dataclass
class Entity:
    qid: str
    label: str | None
    statements: list[Statement] = field(default_factory=list)
    redirect_to: str | None = None


class Graph:
    """Entities and their statements, serialised as the query service's
    Turtle: one triple per line, one block per entity."""

    def __init__(self, vocab: Vocabulary, rng: random.Random):
        self.vocab = vocab
        self.rng = rng
        self.entities: dict[str, Entity] = {}
        self.minted = dict.fromkeys(ID_BASE, 0)

    def mint(self, kind: str, label: str | None = None) -> str:
        self.minted[kind] += 1
        number = self.minted[kind]
        if number > MAX_PERSONS:
            raise ValueError(f"more than {MAX_PERSONS} {kind} entities")
        qid = f"Q{ID_BASE[kind] + number}"
        if label is None and kind in KIND_LABEL:
            label = f"{KIND_LABEL[kind]} {number}"
        self.entities[qid] = Entity(qid, label)
        return qid

    def vocabulary_item(self, key: str) -> str:
        qid = self.vocab.items[key]
        self.entities[qid] = Entity(qid, self.vocab.item_labels[key])
        return qid

    def redirect(self, target: str) -> str:
        qid = self.mint("redirect")
        self.entities[qid].redirect_to = target
        return qid

    def add(
        self, subject, prop, value, references=(), rank="Normal", qualifiers=()
    ) -> Statement:
        assert rank in RANKS, rank
        guid = uuid.UUID(int=self.rng.getrandbits(128), version=4)
        statement = Statement(
            guid=f"{subject}-{str(guid).upper()}",
            prop=prop,
            value=value,
            rank=rank,
            qualifiers=tuple(qualifiers),
            references=tuple(dict.fromkeys(references)),
        )
        self.entities[subject].statements.append(statement)
        return statement

    # -- serialisation --------------------------------------------------------

    def turtle(self, header: list[str]) -> str:
        lines = [f"# {line}" if line else "#" for line in header]
        lines += [f"@prefix {p}: <{iri}> ." for p, iri in PREFIXES.items()]
        for key, pid in self.vocab.properties.items():
            lines.append("")
            lines += self._property_lines(key, pid)
        values: dict[str, list[str]] = {}
        references: dict[str, list[str]] = {}
        for entity in self.entities.values():
            lines.append("")
            lines += self._entity_lines(entity, values, references)
        for block in (*references.values(), *values.values()):
            lines.append("")
            lines += block
        return "\n".join(lines) + "\n"

    def _property_lines(self, key: str, pid: str) -> list[str]:
        s = f"wd:{pid}"
        label = literal(self.vocab.property_labels[key])
        return [
            f"{s} a wikibase:Property .",
            f"{s} rdfs:label {label}@en .",
            f"{s} wikibase:propertyType "
            f"wikibase:{self.vocab.property_types[key]} .",
            f"{s} wikibase:directClaim wdt:{pid} .",
            f"{s} wikibase:claim p:{pid} .",
            f"{s} wikibase:statementProperty ps:{pid} .",
            f"{s} wikibase:statementValue psv:{pid} .",
            f"{s} wikibase:qualifier pq:{pid} .",
            f"{s} wikibase:qualifierValue pqv:{pid} .",
            f"{s} wikibase:reference pr:{pid} .",
            f"{s} wikibase:referenceValue prv:{pid} .",
            f"{s} wikibase:novalue wdno:{pid} .",
        ]

    def _entity_lines(self, entity, values, references) -> list[str]:
        # The updater keeps no item, statement or reference types; a
        # best-rank statement keeps its wikibase:BestRank type.
        s = f"wd:{entity.qid}"
        if entity.redirect_to is not None:
            return [f"{s} owl:sameAs wd:{entity.redirect_to} ."]
        lines = []
        if entity.label is not None:
            lines.append(f"{s} rdfs:label {literal(entity.label)}@en .")
        best = best_ranked(entity.statements)
        truthy: dict[str, None] = {}
        for st in entity.statements:
            pid = self.vocab.properties[st.prop]
            node = f"wds:{st.guid}"
            lines.append(f"{s} p:{pid} {node} .")
            if st.guid in best:
                lines.append(f"{node} a wikibase:BestRank .")
                truthy[f"{s} wdt:{pid} {term(st.value)} ."] = None
            lines.append(f"{node} wikibase:rank wikibase:{st.rank}Rank .")
            lines.append(f"{node} ps:{pid} {term(st.value)} .")
            value_node = self._value_node(st.value, values)
            if value_node:
                lines.append(f"{node} psv:{pid} wdv:{value_node} .")
            for key, value in st.qualifiers:
                qpid = self.vocab.properties[key]
                lines.append(f"{node} pq:{qpid} {term(value)} .")
                value_node = self._value_node(value, values)
                if value_node:
                    lines.append(f"{node} pqv:{qpid} wdv:{value_node} .")
            for reference in st.references:
                ref = self._reference_node(reference, references)
                lines.append(f"{node} prov:wasDerivedFrom wdref:{ref} .")
        return lines + list(truthy)

    def _value_node(self, value, values) -> str | None:
        if isinstance(value, Time):
            node = digest(f"time|{value.date}|{value.precision}|{GREGORIAN}")
            v = f"wdv:{node}"
            block = [
                f"{v} a wikibase:TimeValue .",
                f"{v} wikibase:timeValue {term(value)} .",
                f'{v} wikibase:timePrecision "{value.precision}"'
                "^^xsd:integer .",
                f'{v} wikibase:timeTimezone "0"^^xsd:integer .',
                f"{v} wikibase:timeCalendarModel <{GREGORIAN}> .",
            ]
        elif isinstance(value, Coordinate):
            lat, lon = f"{value.latitude:.4f}", f"{value.longitude:.4f}"
            node = digest(f"coordinate|{lat}|{lon}|{EARTH}")
            v = f"wdv:{node}"
            block = [
                f"{v} a wikibase:GlobecoordinateValue .",
                f'{v} wikibase:geoLatitude "{lat}"^^xsd:double .',
                f'{v} wikibase:geoLongitude "{lon}"^^xsd:double .',
                f'{v} wikibase:geoPrecision "0.0001"^^xsd:double .',
                f"{v} wikibase:geoGlobe <{EARTH}> .",
            ]
        else:
            return None
        values.setdefault(node, block)
        return node

    def _reference_node(self, snaks: tuple, references) -> str:
        """The node of a reference, named by its content as Wikibase names
        it, so every statement citing the same reference shares it."""
        pairs = [(self.vocab.properties[k], term(v)) for k, v in snaks]
        node = digest("|".join(f"{pid}={obj}" for pid, obj in pairs))
        block = [f"wdref:{node} pr:{pid} {obj} ." for pid, obj in pairs]
        references.setdefault(node, block)
        return node


def best_ranked(statements: list[Statement]) -> set[str]:
    """Guids of the statements Wikibase gives truthy triples: per
    property, the preferred ones if any, otherwise the normal ones."""
    by_property: dict[str, list[Statement]] = {}
    for st in statements:
        by_property.setdefault(st.prop, []).append(st)
    best: set[str] = set()
    for group in by_property.values():
        chosen = [st for st in group if st.rank == "Preferred"] or [
            st for st in group if st.rank == "Normal"
        ]
        best.update(st.guid for st in chosen)
    return best


# -- the synthetic world ------------------------------------------------------


@dataclass
class Person:
    qid: str
    generation: int
    born: int
    sex: str | None


class Builder:
    """Populates a Graph with the synthetic world, drawing every choice
    from one seeded random generator."""

    def __init__(self, graph: Graph, persons: int):
        self.g = graph
        self.rng = graph.rng
        self.size = persons
        self.pending = {slot: list(mix) for slot, mix in SCENARIOS.items()}
        self.parent_place: dict[str, str] = {}
        self.redirects: dict[str, str] = {}
        self.pages: dict[str, list[str]] = {}
        self.documents: list[str] = []
        self.runs: list[str] = []
        self.people: list[Person] = []
        self.regions: list[str] = []
        self.districts: list[str] = []
        self.towns: list[str] = []
        self.addresses: list[str] = []
        self.name_number = 0
        self.excerpt_number = 0
        self.unknown_deaths = 0

    # -- draws ----------------------------------------------------------------

    def scenario(self, slot: str) -> str:
        """The next scenario of a slot: each one once, in order, then
        drawn by weight."""
        if self.pending[slot]:
            return self.pending[slot].pop(0)
        mix = SCENARIOS[slot]
        return self.rng.choices(list(mix), weights=list(mix.values()))[0]

    def wants(self, slot: str, probability: float) -> bool:
        """Whether an optional slot is filled: always while any of its
        scenarios has yet to appear, so small graphs still hold them all."""
        return bool(self.pending[slot]) or self.rng.random() < probability

    def chance(self, probability: float) -> bool:
        return self.rng.random() < probability

    def moment(self, year: int, precision: int) -> Time:
        """A date in `year` with a drawn month and day, at `precision`."""
        month, day = self.rng.randint(1, 12), self.rng.randint(1, 28)
        return when(year, month, day, precision)

    def run(self) -> str:
        return self.rng.choice(self.runs[:-1])

    def cite(self, exclude: tuple[str, ...] = ()) -> tuple[str, tuple]:
        """A stated-in reference to a document: the artifact itself, the
        artifact with a page number, or one of its page fragments. Returns
        (artifact, reference); `exclude` names artifacts to avoid."""
        artifact = self.rng.choice(self.documents)
        while artifact in exclude:
            artifact = self.rng.choice(self.documents)
        roll = self.rng.random()
        if artifact in self.pages and roll < 0.2:
            return artifact, stated_in(self.rng.choice(self.pages[artifact]))
        if roll < 0.45:
            return artifact, stated_in(artifact, self.rng.randint(1, 4))
        return artifact, stated_in(artifact)

    def excerpted(self, reference: tuple) -> tuple:
        """A machine source reference as the writer makes it: most carry
        the claim's own transcription excerpt (here a placeholder), which
        makes the reference, and its node, the claim's alone."""
        if not self.chance(0.9):
            return reference
        self.excerpt_number += 1
        excerpt = Text(f"Excerpt {self.excerpt_number}", "en")
        return (*reference, ("transcription_excerpt", excerpt))

    def refs(self, reference: tuple, machine: bool | None = None) -> list:
        """A machine statement cites its source and its run; a curator
        statement cites its source only."""
        if machine is None:
            machine = self.chance(0.88)
        if not machine:
            return [reference]
        return [self.excerpted(reference), by_run(self.run())]

    def claim(self, subject, prop, value, *, exclude=(), **kwargs):
        """One statement from one freshly cited source; returns the
        artifact it cites."""
        artifact, reference = self.cite(exclude)
        self.g.add(subject, prop, value, self.refs(reference), **kwargs)
        return artifact

    def redirect_of(self, qid: str) -> str:
        if qid not in self.redirects:
            self.redirects[qid] = self.g.redirect(qid)
        return self.redirects[qid]

    # -- the world ------------------------------------------------------------

    def build(self) -> None:
        g = self.g
        for key in self.g.vocab.items:
            g.vocabulary_item(key)
        manifest = g.vocab.items["model_manifest"]
        for key, parent in g.vocab.item_parents.items():
            g.add(
                g.vocab.items[key],
                "subclass_of",
                Item(g.vocab.items[parent]),
                [stated_in(manifest)],
            )
        self.make_runs()
        self.make_artifacts()
        self.make_places()
        self.make_people()
        self.make_families()
        for person in self.people:
            self.describe(person)

    def make_runs(self) -> None:
        # three extraction runs, and a place-reconciliation run last
        for _ in range(4):
            run = self.g.mint("run")
            self.g.add(
                run,
                "instance_of",
                Item(self.g.vocab.items["extraction_run"]),
                [stated_in(run)],
            )
            self.runs.append(run)

    def make_artifacts(self) -> None:
        g, items = self.g, self.g.vocab.items
        count = max(12, round(self.size * 0.8))
        presentations: list[str] = []
        photographs: list[str] = []
        for number in range(1, count + 1):
            kind = self.scenario("artifact")
            artifact = g.mint("artifact")
            own = [stated_in(artifact)]
            g.add(artifact, "instance_of", Item(items[kind]), own)
            extension = {"photograph": "jpg", "presentation": "pptx"}.get(
                kind, "pdf"
            )
            g.add(
                artifact,
                "media_file",
                Plain(f"File:Artifact {number}.{extension}"),
                own,
            )
            g.add(
                artifact,
                "media_sha1",
                Plain(digest(f"synthetic artifact {number}")),
                own,
            )
            if kind == "photograph":
                photographs.append(artifact)
                continue
            if kind == "presentation":
                presentations.append(artifact)
            self.documents.append(artifact)
            if not self.pages or self.chance(0.3):
                self.make_pages(artifact, number)
        # photographs inside a presentation (compound artifacts): part of
        # the container, slide-qualified, stated in the container
        for photo in photographs:
            if presentations and self.chance(0.3):
                deck = self.rng.choice(presentations)
                g.add(
                    photo,
                    "part_of",
                    Item(deck),
                    [stated_in(deck)],
                    qualifiers=[
                        ("page_number", Plain(str(self.rng.randint(1, 9))))
                    ],
                )

    def make_pages(self, artifact: str, number: int) -> None:
        g, items = self.g, self.g.vocab.items
        pages = []
        for page in range(1, self.rng.randint(2, 4) + 1):
            fragment = g.mint("fragment", f"Artifact {number}, page {page}")
            own = [stated_in(artifact)]
            g.add(
                fragment,
                "instance_of",
                Item(items["artifact_fragment"]),
                own,
            )
            g.add(
                fragment,
                "part_of",
                Item(artifact),
                own,
                qualifiers=[("page_number", Plain(str(page)))],
            )
            pages.append(fragment)
        self.pages[artifact] = pages

    def make_places(self) -> None:
        g, items = self.g, self.g.vocab.items
        levels = [
            max(2, self.size // 60),
            max(3, self.size // 15),
            max(6, self.size // 4),
            max(6, self.size // 3),
        ]
        self.regions, self.districts, self.towns, self.addresses = (
            [g.mint("place") for _ in range(n)] for n in levels
        )
        places_run = self.runs[-1]
        for place in (
            self.regions + self.districts + self.towns + self.addresses
        ):
            self.claim(place, "instance_of", Item(items["place"]))
        # containment: world knowledge from the place pass, referenced to
        # its run only (no stated-in)
        for child_level, parent_level in (
            (self.districts, self.regions),
            (self.towns, self.districts),
        ):
            for child in child_level:
                parent = self.rng.choice(parent_level)
                self.parent_place[child] = parent
                g.add(child, "part_of", Item(parent), [by_run(places_run)])
        # an address is located in its town, as a source states it; some
        # statements name a redirect the town was merged from
        for address in self.addresses:
            town = self.rng.choice(self.towns)
            self.parent_place[address] = town
            value = town
            if self.chance(0.1):
                value = self.redirect_of(town)
            self.claim(address, "location", Item(value))
        for place in self.regions + self.districts + self.towns:
            self.identity(place, 0.6)
        for address in self.addresses:
            self.identity(address, 0.25)

    def identity(self, place: str, probability: float) -> None:
        """Coordinates and external ids, sometimes disagreeing."""
        number = int(place[1:]) - ID_BASE["place"]
        if self.wants("identity", probability):
            kind = self.scenario("identity")
            point = self.ocean_point()
            first = self.claim(place, "coordinate_location", point)
            if kind == "conflict":
                other = Coordinate(
                    round(point.latitude + self.rng.uniform(0.05, 0.5), 4),
                    round(point.longitude - self.rng.uniform(0.05, 0.5), 4),
                )
                self.claim(
                    place, "coordinate_location", other, exclude=(first,)
                )
            if self.chance(0.6):
                # out of GeoNames' range, so it names no real place
                geonames = Plain(str(900_000_000 + number))
                first = self.claim(place, "geonames_id", geonames)
                if kind == "conflict" and self.chance(0.5):
                    self.claim(
                        place,
                        "geonames_id",
                        Plain(str(910_000_000 + number)),
                        exclude=(first,),
                    )
            if self.chance(0.2):
                self.g.add(
                    place,
                    "whg_place_id",
                    Plain(str(800_000_000 + number)),
                    [by_run(self.runs[-1])],
                )

    def ocean_point(self) -> Coordinate:
        # open South Atlantic: no town lies there
        return Coordinate(
            round(self.rng.uniform(-45.0, -35.0), 4),
            round(self.rng.uniform(-35.0, -15.0), 4),
        )

    def make_people(self) -> None:
        items = self.g.vocab.items
        seen = dict.fromkeys(range(GENERATIONS), 0)
        for index in range(self.size):
            qid = self.g.mint("person")
            generation = index * GENERATIONS // self.size
            born = 1790 + 28 * generation + self.rng.randint(-8, 8)
            # a man and a woman open each generation, so every generation
            # holds a couple; the rest are drawn, some with no stated sex
            order = seen[generation]
            seen[generation] += 1
            if order < 2:
                sex = ("male", "female")[order]
            else:
                sex = self.rng.choices(
                    ("male", "female", None), weights=(45, 45, 10)
                )[0]
            person = Person(qid, generation, born, sex)
            self.people.append(person)
            # one instance-of statement; each later source adds its own
            # reference to it
            sources = [
                self.excerpted(self.cite()[1])
                for _ in range(self.rng.randint(1, 3))
            ]
            refs = sources + [by_run(self.run())]
            self.g.add(qid, "instance_of", Item(items["human"]), refs)
            if sex is not None:
                self.claim(qid, "sex_or_gender", Item(items[sex]))

    def make_families(self) -> None:
        """Couples within each generation; children of the next generation
        take their parents from a couple of the one before."""
        by_generation: dict[int, list[Person]] = {}
        for person in self.people:
            by_generation.setdefault(person.generation, []).append(person)
        couples: dict[int, list[tuple[Person, Person]]] = {}
        for generation, members in by_generation.items():
            men = [p for p in members if p.sex == "male"]
            women = [p for p in members if p.sex == "female"]
            self.rng.shuffle(men)
            self.rng.shuffle(women)
            pairs = list(zip(men, women, strict=False))
            couples[generation] = pairs
            for husband, wife in pairs:
                self.marriage(husband, wife)
            # a remarriage: a later spouse from the unpaired
            spare = men[len(pairs) :] + women[len(pairs) :]
            for husband, wife in pairs:
                if spare and self.chance(0.1):
                    second = spare.pop()
                    first = husband if second.sex == "female" else wife
                    self.marriage(first, second, later=True)
        for person in self.people:
            if person.generation == 0 or not couples[person.generation - 1]:
                continue
            father, mother = self.rng.choice(couples[person.generation - 1])
            for key, parent in (("father", father), ("mother", mother)):
                if self.chance(0.85):
                    self.parent(person, key, parent)
            if self.chance(0.05):
                # a parent's source states the child directly
                self.claim(father.qid, "child", Item(person.qid))
        for members in by_generation.values():
            for person in members:
                if self.chance(0.05):
                    other = self.rng.choice(members)
                    if other is not person:
                        self.claim(person.qid, "sibling", Item(other.qid))
                if self.chance(0.03):
                    ego = self.rng.choice(self.people)
                    if ego is not person:
                        self.claim(
                            person.qid,
                            "relative",
                            Item(ego.qid),
                            qualifiers=[
                                (
                                    "kinship_type",
                                    Plain(self.rng.choice(KINSHIP_TYPES)),
                                )
                            ],
                        )

    def marriage(self, a: Person, b: Person, later: bool = False) -> None:
        """Spouse statements on both partners from one source, dated by
        start, end or point-in-time qualifiers; sometimes a second source
        dates the same marriage differently."""
        kind = self.scenario("marriage")
        year = max(a.born, b.born) + self.rng.randint(18, 30)
        if later:
            year += self.rng.randint(8, 20)
        start = self.moment(year, self.rng.choice((DAY, MONTH, YEAR)))
        if kind == "observed":
            qualifiers = [("point_in_time", self.moment(year + 3, YEAR))]
        else:
            qualifiers = [("start_time", start)]
        if kind == "ended":
            end = self.moment(year + self.rng.randint(5, 40), YEAR)
            qualifiers.append(("end_time", end))
        artifact, reference = self.cite()
        for one, other in ((a, b), (b, a)):
            self.g.add(
                one.qid,
                "spouse",
                Item(other.qid),
                self.refs(reference),
                qualifiers=qualifiers,
            )
        if kind == "redated":
            moved = self.moment(year + self.rng.choice((-1, 1)), YEAR)
            self.claim(
                a.qid,
                "spouse",
                Item(b.qid),
                exclude=(artifact,),
                qualifiers=[("start_time", moved)],
            )
        if self.chance(0.3):
            self.event("marriage", [a, b], year, precision=start.precision)

    def parent(self, child: Person, key: str, parent: Person) -> None:
        kind = self.scenario("parent")
        first = self.claim(child.qid, key, Item(parent.qid))
        if kind == "conflict":
            others = [
                p
                for p in self.people
                if p.generation == parent.generation and p is not parent
            ]
            if others:
                other = self.rng.choice(others)
                self.claim(child.qid, key, Item(other.qid), exclude=(first,))
        elif kind == "redirect":
            self.claim(
                child.qid,
                key,
                Item(self.redirect_of(parent.qid)),
                exclude=(first,),
            )

    def describe(self, person: Person) -> None:
        """A person's single-valued facts, residences, names and events."""
        qid = person.qid
        self.dates(qid, "date_of_birth", person.born)
        if person.generation < GENERATIONS - 1 or self.chance(0.5):
            died = person.born + self.rng.randint(40, 90)
            if self.chance(0.85):
                self.dates(qid, "date_of_death", died)
            # a death its source states without its date, beside any dated
            # one; the first person who dies has one
            if not self.unknown_deaths or self.chance(0.1):
                self.unknown_deaths += 1
                token = digest(f"unknown|{qid}|{self.unknown_deaths}")
                self.claim(qid, "date_of_death", Unknown(token))
        if self.wants("place", 0.75):
            self.places(qid, "place_of_birth")
        if self.chance(0.5):
            self.places(qid, "place_of_death")
        for index in range(self.rng.randint(1, 3)):
            self.residence(qid, person.born + 20 * index)
        if self.wants("name", 0.25):
            self.names(qid)
        if self.chance(0.1):
            self.name_number += 1
            self.claim(
                qid, "married_name", Text(f"Name {self.name_number}", "en")
            )
        if self.chance(0.05):
            self.name_number += 1
            self.claim(qid, "nickname", Text(f"Name {self.name_number}", "en"))
        if self.wants("event", 0.35):
            kind = self.scenario("event")
            year = person.born + self.rng.randint(15, 50)
            self.event(kind, [person], year)

    def event(self, kind, people, year, precision: int | None = None) -> None:
        g, items = self.g, self.g.vocab.items
        event = g.mint("event")
        self.claim(event, "instance_of", Item(items[kind]))
        for person in people:
            self.claim(event, "participant", Item(person.qid))
        if self.chance(0.6):
            self.claim(event, "location", Item(self.rng.choice(self.towns)))
        if kind in SPAN_EVENTS:
            length = self.rng.randint(1, 6)
            self.dates(event, "start_time", year, "event_date")
            if self.chance(0.7):
                self.dates(event, "end_time", year + length, "event_date")
        else:
            self.dates(event, "point_in_time", year, "event_date", precision)

    def dates(self, subject, prop, year, slot="date", precision=None):
        """A single-valued date as one or more attributed statements."""
        kind = self.scenario(slot)
        month, day = self.rng.randint(1, 12), self.rng.randint(1, 28)
        # the value as the source states it; the scenarios that state
        # their own precisions draw none
        base = None
        if kind not in ("resolution", "range", "open_range", "circa"):
            stated = precision or self.scenario(f"{slot}_precision")
            base = when(year, month, day, stated)
        # a contradicting value lies outside the whole period the value
        # names (a year beside the 19th century inside it differs only in
        # resolution); a third one lies on the other side
        low, high = years_of(base) if base else (year, year)
        away = self.rng.randint(1, 3)
        later = self.chance(0.5)
        other = self.moment(
            high + away if later else low - away,
            self.rng.choice((DAY, YEAR)),
        )
        third = self.moment(low - away if later else high + away, YEAR)
        rng = self.rng
        if kind == "single":
            self.claim(subject, prop, base)
        elif kind == "agree":
            first, ref_a = self.cite()
            _, ref_b = self.cite(exclude=(first,))
            if self.chance(0.5):
                # one statement citing both sources
                refs = [self.excerpted(ref_a), self.excerpted(ref_b)]
                self.g.add(subject, prop, base, [*refs, by_run(self.run())])
            else:
                self.g.add(subject, prop, base, self.refs(ref_a))
                self.g.add(subject, prop, base, self.refs(ref_b))
        elif kind == "resolution":
            fine = when(year, month, day, DAY)
            coarse = when(year, month, day, rng.choice((MONTH, YEAR, DECADE)))
            first = self.claim(subject, prop, fine)
            self.claim(subject, prop, coarse, exclude=(first,))
        elif kind in ("conflict", "deprecated", "preferred"):
            ranks = ["Normal", "Normal"]
            if kind != "conflict":
                ranks[rng.randrange(2)] = kind.capitalize()
            first = self.claim(subject, prop, base, rank=ranks[0])
            self.claim(subject, prop, other, exclude=(first,), rank=ranks[1])
            if kind == "conflict" and self.chance(0.3):
                self.claim(subject, prop, third, exclude=(first,))
        elif kind == "fragments":
            artifact = rng.choice(list(self.pages))
            page_a, page_b = rng.sample(self.pages[artifact], 2)
            self.g.add(subject, prop, base, self.refs(stated_in(page_a)))
            self.g.add(subject, prop, other, self.refs(stated_in(page_b)))
        elif kind == "range":
            # "between 1905 and 1912", or the 1930s between 1930 and 1935
            coarse = when(year, precision=rng.choice((YEAR, DECADE)))
            low = int(coarse.date[:4])
            high = low + rng.randint(1, 7)
            qualifiers = [
                ("earliest_date", when(low, precision=YEAR)),
                ("latest_date", when(high, precision=YEAR)),
            ]
            first = self.claim(subject, prop, coarse, qualifiers=qualifiers)
            if self.chance(0.6):
                inside = rng.random() < 0.5
                probe = rng.randint(low, high) if inside else high + 2
                self.claim(
                    subject,
                    prop,
                    self.moment(probe, rng.choice((DAY, YEAR))),
                    exclude=(first,),
                )
        elif kind == "open_range":
            # "before 1920" beside "after 1920": one stored year, two ranges
            stored = when(year, precision=YEAR)
            bound = when(year, precision=YEAR)
            first = self.claim(
                subject, prop, stored, qualifiers=[("latest_date", bound)]
            )
            if self.chance(0.7):
                self.claim(
                    subject,
                    prop,
                    stored,
                    exclude=(first,),
                    qualifiers=[("earliest_date", bound)],
                )
        elif kind == "circa":
            circa = Item(self.g.vocab.items["circa"])
            first = self.claim(
                subject,
                prop,
                when(year, precision=YEAR),
                qualifiers=[("sourcing_circumstances", circa)],
            )
            if self.chance(0.5):
                self.claim(subject, prop, other, exclude=(first,))
        elif kind == "correction":
            # a curator's correction beside the machine reading it
            # corrects: two values, one artifact
            _, reference = self.cite()
            self.g.add(subject, prop, base, self.refs(reference, True))
            self.g.add(subject, prop, other, self.refs(reference, False))
        elif kind == "run_only":
            self.g.add(subject, prop, base, [by_run(self.run())])
            if self.chance(0.5):
                self.claim(subject, prop, other)
        elif kind == "unreferenced":
            self.g.add(subject, prop, base, [])
        else:
            raise AssertionError(kind)

    def places(self, subject: str, prop: str) -> None:
        kind = self.scenario("place")
        place = self.rng.choice(self.towns + self.addresses)
        if kind == "deprecated_only":
            # a curator deprecated the only statement: no truthy value
            self.claim(subject, prop, Item(place), rank="Deprecated")
            return
        rank = "Preferred" if kind == "ranked" else "Normal"
        first = self.claim(subject, prop, Item(place), rank=rank)
        if kind == "agree":
            self.claim(subject, prop, Item(place), exclude=(first,))
        elif kind == "resolution":
            # the place's town, district or region, as another source has it
            wider = self.parent_place[place]
            while wider in self.parent_place and self.chance(0.5):
                wider = self.parent_place[wider]
            self.claim(subject, prop, Item(wider), exclude=(first,))
        elif kind in ("conflict", "ranked"):
            # an unrelated town; beside a preferred place, deprecated
            other = self.rng.choice(self.towns)
            while other == place or self.parent_place.get(place) == other:
                other = self.rng.choice(self.towns)
            rank = "Deprecated" if kind == "ranked" else "Normal"
            self.claim(subject, prop, Item(other), exclude=(first,), rank=rank)
        elif kind == "redirect":
            value = Item(self.redirect_of(place))
            self.claim(subject, prop, value, exclude=(first,))

    def residence(self, subject: str, year: int) -> None:
        """A dated residence; sometimes redated by another source, returned
        to later, named through a redirect or given a range on each end."""
        kind = self.scenario("residence")
        place = self.rng.choice(self.towns + self.addresses)
        start = self.moment(year, self.rng.choice((DAY, MONTH, YEAR)))
        end = self.moment(year + self.rng.randint(2, 15), YEAR)
        qualifiers = []
        if kind != "single" or self.chance(0.7):
            qualifiers.append(("start_time", start))
        if kind in ("returned", "range") or self.chance(0.45):
            qualifiers.append(("end_time", end))
        if self.chance(0.15):
            qualifiers.append(("point_in_time", self.moment(year + 1, YEAR)))
        if kind == "range":
            # Each end has a range of its own (MODELING.md, "Marriages,
            # residences and other periods"): the start runs from
            # earliest_date to latest_start_date, the end from
            # earliest_end_date to latest_date. Both pairs contain the
            # value they bound: earliest_date is the start's own year and
            # latest_date the end's own year, each the tight bound.
            start_year = int(start.date[:4])
            end_year = int(end.date[:4])
            qualifiers += [
                ("earliest_date", when(start_year, precision=YEAR)),
                ("latest_start_date", when(start_year + 2, precision=YEAR)),
                ("earliest_end_date", when(end_year - 2, precision=YEAR)),
                ("latest_date", when(end_year, precision=YEAR)),
            ]
        first = self.claim(
            subject, "residence", Item(place), qualifiers=qualifiers
        )
        if kind == "redated":
            moved = self.moment(year + self.rng.choice((-2, -1, 1, 2)), YEAR)
            self.claim(
                subject,
                "residence",
                Item(place),
                exclude=(first,),
                qualifiers=[("start_time", moved)],
            )
        elif kind == "returned":
            back = self.moment(int(end.date[:4]) + 5, YEAR)
            self.claim(
                subject,
                "residence",
                Item(place),
                qualifiers=[("start_time", back)],
            )
        elif kind == "redirect":
            moved = self.moment(year + 1, YEAR)
            self.claim(
                subject,
                "residence",
                Item(self.redirect_of(place)),
                exclude=(first,),
                qualifiers=[("start_time", moved)],
            )

    def names(self, subject: str) -> None:
        """A birth name; another source may spell it in another case,
        language tag or spelling."""
        kind = self.scenario("name")
        self.name_number += 1
        name = f"Name {self.name_number}"
        first = self.claim(subject, "birth_name", Text(name, "en"))
        variant = {
            "case": Text(name.upper(), "en"),
            "language": Text(name, "de"),
            "spelling": Text(f"{name}-2", "en"),
        }.get(kind)
        if variant is not None:
            self.claim(subject, "birth_name", variant, exclude=(first,))


# -- entry points -------------------------------------------------------------


@dataclass(frozen=True)
class Synthetic:
    turtle: str
    vocabulary: dict
    counts: dict


def generate(persons: int, seed: int) -> Synthetic:
    """The graph for `persons` people from `seed`, with its vocabulary map
    and entity counts."""
    if not MIN_PERSONS <= persons <= MAX_PERSONS:
        raise ValueError(
            f"persons must be between {MIN_PERSONS} and {MAX_PERSONS}"
        )
    rng = random.Random(seed)
    vocab = load_vocabulary()
    graph = Graph(vocab, rng)
    Builder(graph, persons).build()
    header = [
        f"Synthetic Wikibase graph for query reviews: {persons} persons, "
        f"seed {seed}.",
        "Written by scripts/synthetic_graph.py. Every entity is invented "
        "and labelled with a placeholder.",
    ]
    turtle = graph.turtle(header)
    counts = dict(graph.minted)
    counts["statements"] = sum(
        len(e.statements) for e in graph.entities.values()
    )
    counts["triples"] = sum(
        1
        for line in turtle.splitlines()
        if line and not line.startswith(("#", "@prefix"))
    )
    return Synthetic(turtle, vocab.as_map(), counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--persons",
        type=int,
        default=250,
        metavar="N",
        help=(
            f"number of persons, which sets the size "
            f"({MIN_PERSONS}-{MAX_PERSONS}; default 250)"
        ),
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="random seed (default 0)"
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        metavar="FILE.ttl",
        help="Turtle file to write",
    )
    parser.add_argument(
        "--vocab",
        type=Path,
        metavar="MAP",
        help="vocabulary map to write (default: the .vocab.json beside it)",
    )
    args = parser.parse_args(argv)
    vocab_path = args.vocab or args.out.with_suffix(".vocab.json")
    if vocab_path.resolve() == args.out.resolve():
        parser.error("--vocab would overwrite the graph")
    if args.seed < 0:
        # random.Random(-n) draws what random.Random(n) draws
        parser.error("--seed must be 0 or more")
    try:
        result = generate(args.persons, args.seed)
    except ValueError as exc:
        parser.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    vocab_path.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(result.turtle, encoding="utf-8")
    vocab_path.write_text(
        json.dumps(result.vocabulary, indent=2) + "\n", encoding="utf-8"
    )
    counts = result.counts
    print(
        f"wrote {args.out}: {counts['person']} persons, "
        f"{counts['place']} places, {counts['artifact']} artifacts "
        f"({counts['fragment']} page fragments), {counts['event']} events, "
        f"{counts['redirect']} redirects; {counts['statements']} "
        f"statements, {counts['triples']} triples"
    )
    print(f"wrote {vocab_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
