"""Entity reconciliation (ADR-0007): proposals, never assertions.

Deterministic parts first — a roster prefetch of every graph entity
(one SPARQL query; a family archive's roster fits a prompt for years)
and candidate blocking (shared kind + a shared whole name form). Only
then an
LLM adjudication call, which weighs RELATIONAL evidence (same spouse,
compatible dates beats edit distance) and emits equivalence proposals.

Every proposal lands in a review CSV with the same approve/reject
contract as claim review; a human approves each equivalence before it
takes effect (identity is never auto-confirmed — a wrong merge corrupts
every downstream attribution). Approved matches are applied by
`apply_equivalences`, which sets EntityRecord.qid and rewrites local
refs to the existing item, so the writer attaches claims (and the
document's aliases) there instead of minting a duplicate.

The same blocking + adjudication over roster x roster proposes pairs of
EXISTING items (`propose_graph_pairs`) — cleanup of duplicates already
in the graph. Executing those is a curator action: a MediaWiki item
merge, or a referenced `said to be the same as` statement when the
equivalence is itself a contestable judgement.

Blocking and adjudication take the roster, and the place containment,
as inputs. Only `fetch_roster` and `fetch_place_containment` read them
from the graph, for the commands a curator runs on it (`ingest
reconcile`, `reconcile-graph`): a stage before the write reads nothing
from the wiki (ADR-0025 §2), so `ingest run`'s reconcile step has no
roster to match against. `fetch_roster` first asks the graph for any
date before the common era or in a calendar other than the proleptic
Gregorian, and stops with UnsupportedDate naming each item and statement
that holds one (ADR-0034): the roster's dates are adjudication facts, and
such a date is never put to the adjudicator.
"""

from __future__ import annotations

import csv
import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from yizkr.claims import (
    ClaimBatch,
    EntityRecord,
    ItemValue,
    LocalRef,
    PersonStructure,
    PlaceStructure,
    QidRef,
    name_form_key,
)
from yizkr.core import sparql
from yizkr.core.config import Config
from yizkr.core.dates import time_text, unsupported_in_graph
from yizkr.core.wiki import WikiClient
from yizkr.disagreement import followed
from yizkr.pipeline.extract import scan_json_objects
from yizkr.pipeline.llm import CUT_OFF_AT_LIMIT, LlmClient, at_output_limit
from yizkr.pipeline.names import NOT_PERSONS
from yizkr.queries.publish import (
    load_templates,
    prefix_block,
    render_for_instance,
)


class RosterEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qid: str
    label: str
    aliases: list[str] = []
    kind: str = ""  # manifest item key when recognized, else the kind's QID
    # an unrecognized kind's label, shown beside its QID to the
    # adjudicator and never read
    kind_label: str = ""
    # each date as text at the precision its statement states ("the 19th
    # century", "1919-01-07"), one sampled per item
    birth: str = ""
    death: str = ""
    spouses: list[str] = []
    fathers: list[str] = []
    mothers: list[str] = []
    birth_names: list[str] = []
    married_names: list[str] = []
    nicknames: list[str] = []
    official_names: list[str] = []
    # the values of its `referred_to_as` statements not deprecated, the
    # forms its sources give as appellations of it (ADR-0018): a person's
    # label or alias equal to one is no name it blocks on
    appellations: list[str] = []
    # raw related-entity QIDs for the impossibility filter (ADR-0017):
    # a kinship edge between two candidates proves they are two people.
    spouse_qids: list[str] = []
    father_qids: list[str] = []
    mother_qids: list[str] = []
    child_qids: list[str] = []
    sibling_qids: list[str] = []

    @property
    def kin_qids(self) -> set[str]:
        """QIDs this item is joined to by any irreflexive kinship edge."""
        return {
            qid
            for qid in (
                *self.spouse_qids,
                *self.father_qids,
                *self.mother_qids,
                *self.child_qids,
                *self.sibling_qids,
            )
            if qid
        }

    def facts(self) -> str:
        parts = [f"{self.qid} {self.label!r}"]
        if self.aliases:
            parts.append(f"aliases: {', '.join(self.aliases)}")
        if self.kind:
            shown = f" ({self.kind_label})" if self.kind_label else ""
            parts.append(f"kind: {self.kind}{shown}")
        if self.birth_names:
            parts.append(f"birth name: {', '.join(self.birth_names)}")
        if self.married_names:
            parts.append(f"married name: {', '.join(self.married_names)}")
        if self.nicknames:
            parts.append(f"nickname: {', '.join(self.nicknames)}")
        if self.official_names:
            parts.append(f"official name: {', '.join(self.official_names)}")
        if self.birth:
            parts.append(f"born: {self.birth}")
        if self.death:
            parts.append(f"died: {self.death}")
        if self.spouses:
            parts.append(f"spouse of: {', '.join(self.spouses)}")
        if self.fathers:
            parts.append(f"father: {', '.join(self.fathers)}")
        if self.mothers:
            parts.append(f"mother: {', '.join(self.mothers)}")
        return "; ".join(parts)


ROSTER_QUERY = """\
SELECT ?item ?itemLabel ?kind ?kindLabel
       (GROUP_CONCAT(DISTINCT ?alias; separator="|") AS ?aliases)
       (SAMPLE(?birthDate) AS ?birth) (SAMPLE(?deathDate) AS ?death)
       (SAMPLE(?birthEventDate) AS ?eventBirth)
       (SAMPLE(?deathEventDate) AS ?eventDeath)
       (GROUP_CONCAT(DISTINCT ?spouseDesc; separator="|") AS ?spouses)
       (GROUP_CONCAT(DISTINCT ?fatherDesc; separator="|") AS ?fathers)
       (GROUP_CONCAT(DISTINCT ?motherDesc; separator="|") AS ?mothers)
       (GROUP_CONCAT(DISTINCT ?birthName; separator="|") AS ?birthNames)
       (GROUP_CONCAT(DISTINCT ?marriedName; separator="|") AS ?marriedNames)
       (GROUP_CONCAT(DISTINCT ?nick; separator="|") AS ?nicknames)
       (GROUP_CONCAT(DISTINCT ?officialName; separator="|") AS ?officialNames)
       (SAMPLE(?itemAppellations) AS ?appellations)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?spouseItem), "entity/");
                      separator="|") AS ?spouseQids)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?fatherItem), "entity/");
                      separator="|") AS ?fatherQids)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?motherItem), "entity/");
                      separator="|") AS ?motherQids)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?childItem), "entity/");
                      separator="|") AS ?childQids)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?siblingItem), "entity/");
                      separator="|") AS ?siblingQids)
WHERE {{
  ?item wdt:{instance_of} ?kind .
  OPTIONAL {{ ?item skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }}
  OPTIONAL {{ ?item wdt:{birth_name} ?birthName . }}
  OPTIONAL {{ ?item wdt:{married_name} ?marriedName . }}
  OPTIONAL {{ ?item wdt:{nickname} ?nick . }}
  OPTIONAL {{ ?item wdt:{official_name} ?officialName . }}
  # appellations at any rank but deprecated: a curator's preference among
  # them leaves each form an appellation, and a deprecated statement
  # asserts none; grouped apart, so they multiply no other value's rows
  OPTIONAL {{
    SELECT ?item (GROUP_CONCAT(DISTINCT ?appellation; separator="|")
                  AS ?itemAppellations)
    WHERE {{
      ?item p:{referred_to_as} ?appellationStatement .
      ?appellationStatement ps:{referred_to_as} ?appellation .
      FILTER NOT EXISTS {{
        ?appellationStatement wikibase:rank wikibase:DeprecatedRank .
      }}
    }}
    GROUP BY ?item
  }}
  # each date read from its best-rank statement's value node, as the
  # truthy wdt: value is, with its precision, and joined to it as
  # "time|precision" so the sample takes both from one statement
  OPTIONAL {{ ?item p:{date_of_birth} ?birthStatement .
              ?birthStatement a wikibase:BestRank .
              wd:{date_of_birth} wikibase:statementValue ?birthLink .
              ?birthStatement ?birthLink ?birthNode .
              ?birthNode wikibase:timeValue ?birthTime ;
                         wikibase:timePrecision ?birthPrecision .
              BIND(CONCAT(STR(?birthTime), "|", STR(?birthPrecision))
                   AS ?birthDate) }}
  OPTIONAL {{ ?item p:{date_of_death} ?deathStatement .
              ?deathStatement a wikibase:BestRank .
              wd:{date_of_death} wikibase:statementValue ?deathLink .
              ?deathStatement ?deathLink ?deathNode .
              ?deathNode wikibase:timeValue ?deathTime ;
                         wikibase:timePrecision ?deathPrecision .
              BIND(CONCAT(STR(?deathTime), "|", STR(?deathPrecision))
                   AS ?deathDate) }}
  # dates asserted via event items (participant in a birth/death event)
  OPTIONAL {{ ?birthEvent wdt:{participant} ?item ;
                          wdt:{instance_of} wd:{birth_item} ;
                          p:{point_in_time} ?birthEventStatement .
              ?birthEventStatement a wikibase:BestRank .
              wd:{point_in_time} wikibase:statementValue ?birthEventLink .
              ?birthEventStatement ?birthEventLink ?birthEventNode .
              ?birthEventNode wikibase:timeValue ?birthEventTime ;
                              wikibase:timePrecision ?birthEventPrecision .
              BIND(CONCAT(STR(?birthEventTime), "|",
                          STR(?birthEventPrecision)) AS ?birthEventDate) }}
  OPTIONAL {{ ?deathEvent wdt:{participant} ?item ;
                          wdt:{instance_of} wd:{death_item} ;
                          p:{point_in_time} ?deathEventStatement .
              ?deathEventStatement a wikibase:BestRank .
              wd:{point_in_time} wikibase:statementValue ?deathEventLink .
              ?deathEventStatement ?deathEventLink ?deathEventNode .
              ?deathEventNode wikibase:timeValue ?deathEventTime ;
                              wikibase:timePrecision ?deathEventPrecision .
              BIND(CONCAT(STR(?deathEventTime), "|",
                          STR(?deathEventPrecision)) AS ?deathEventDate) }}
  # relation labels carry QIDs: bare labels are ambiguous (two Ursulas)
  OPTIONAL {{ ?item wdt:{spouse} ?spouseItem .
              ?spouseItem rdfs:label ?spouseLabel .
              FILTER(LANG(?spouseLabel) = "en")
              BIND(CONCAT(?spouseLabel, " (",
                          STRAFTER(STR(?spouseItem), "entity/"), ")")
                   AS ?spouseDesc) }}
  OPTIONAL {{ ?item wdt:{father} ?fatherItem .
              ?fatherItem rdfs:label ?fatherLabel .
              FILTER(LANG(?fatherLabel) = "en")
              BIND(CONCAT(?fatherLabel, " (",
                          STRAFTER(STR(?fatherItem), "entity/"), ")")
                   AS ?fatherDesc) }}
  OPTIONAL {{ ?item wdt:{mother} ?motherItem .
              ?motherItem rdfs:label ?motherLabel .
              FILTER(LANG(?motherLabel) = "en")
              BIND(CONCAT(?motherLabel, " (",
                          STRAFTER(STR(?motherItem), "entity/"), ")")
                   AS ?motherDesc) }}
  # child and sibling are fetched for their QIDs only (the impossibility
  # filter reads them; the adjudicator's prose uses father/mother/spouse).
  OPTIONAL {{ ?item wdt:{child} ?childItem . }}
  OPTIONAL {{ ?item wdt:{sibling} ?siblingItem . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
GROUP BY ?item ?itemLabel ?kind ?kindLabel
"""


def fetch_roster(client: WikiClient, config: Config) -> list[RosterEntity]:
    """Every instance-of-bearing item with identity-relevant facts. A
    graph holding an unsupported date raises UnsupportedDate, before the
    roster is read (ADR-0034)."""
    unsupported_in_graph(
        sparql.runner(config), prefix_block(config.concept_uri)
    )
    properties, items = client.vocabulary()
    # a kind item maps back to its manifest key by its QID, never by label
    kind_key = {qid: key for key, qid in items.items()}
    query = prefix_block(config.concept_uri) + ROSTER_QUERY.format(
        instance_of=properties["instance_of"],
        date_of_birth=properties["date_of_birth"],
        date_of_death=properties["date_of_death"],
        spouse=properties["spouse"],
        father=properties["father"],
        mother=properties["mother"],
        child=properties["child"],
        sibling=properties["sibling"],
        participant=properties["participant"],
        point_in_time=properties["point_in_time"],
        birth_name=properties["birth_name"],
        married_name=properties["married_name"],
        nickname=properties["nickname"],
        official_name=properties["official_name"],
        referred_to_as=properties["referred_to_as"],
        birth_item=items["birth"],
        death_item=items["death"],
    )
    url = (
        config.sparql_url
        + "?"
        + urllib.parse.urlencode({"query": query, "format": "json"})
    )
    with urllib.request.urlopen(url, timeout=120) as response:
        bindings = json.load(response)["results"]["bindings"]

    def field(binding: dict, name: str) -> str:
        return binding.get(name, {}).get("value", "")

    def date(binding: dict, *names: str) -> str:
        # the first of `names` the item has, each a "time|precision" pair
        stamp = next(filter(None, (field(binding, n) for n in names)), "")
        if not stamp:
            return ""
        time, _, precision = stamp.rpartition("|")
        return time_text(time, precision, "a roster date")

    def split(binding: dict, name: str) -> list[str]:
        raw = field(binding, name)
        return [part for part in raw.split("|") if part] if raw else []

    roster = []
    for binding in bindings:
        kind_qid = field(binding, "kind").rsplit("/", 1)[-1]
        roster.append(
            RosterEntity(
                qid=field(binding, "item").rsplit("/", 1)[-1],
                label=field(binding, "itemLabel"),
                aliases=split(binding, "aliases"),
                kind=kind_key.get(kind_qid, kind_qid),
                kind_label=(
                    "" if kind_qid in kind_key else field(binding, "kindLabel")
                ),
                # direct statements win; event-derived dates fill gaps;
                # each at the precision its statement states
                birth=date(binding, "birth", "eventBirth"),
                death=date(binding, "death", "eventDeath"),
                spouses=split(binding, "spouses"),
                fathers=split(binding, "fathers"),
                mothers=split(binding, "mothers"),
                birth_names=split(binding, "birthNames"),
                married_names=split(binding, "marriedNames"),
                nicknames=split(binding, "nicknames"),
                official_names=split(binding, "officialNames"),
                appellations=split(binding, "appellations"),
                spouse_qids=split(binding, "spouseQids"),
                father_qids=split(binding, "fatherQids"),
                mother_qids=split(binding, "motherQids"),
                child_qids=split(binding, "childQids"),
                sibling_qids=split(binding, "siblingQids"),
            )
        )
    return roster


def _keys(names: Iterable[str]) -> set[str]:
    """Each whole name's keys: its folded words, and its words with any
    umlaut written out (``clustering.part_keys``, ADR-0035 §7). The words
    are read in the form the instance keeps a term in
    (writer.stored_term), so a control character inside a name keys as
    the space the instance keeps it as: "Cäcilie\\x08Löbig" meets the
    graph's "Cäcilie Löbig"."""
    from yizkr.pipeline.clustering import part_keys

    return {key for name in names for key in part_keys(name)}


def _composed(record: PersonStructure) -> list[str]:
    """The whole forms a person's typed parts make, in the order the
    writer's labels give a name: each given name, as written and as its
    standard form, and each nickname, before each surname and
    patronymic. A pair whose two parts fold alike makes none, as a given
    name that repeats the surname is no name of two parts."""
    from yizkr.pipeline.clustering import part_keys

    firsts = [
        form
        for name in record.given
        for form in (name.written, name.standard)
        if form
    ] + list(record.nicknames)
    lasts = [name.written for name in record.surnames] + list(
        record.patronymics
    )
    return [
        f"{first} {last}"
        for first in firsts
        for last in lasts
        if not part_keys(first) & part_keys(last)
    ]


def entity_name_keys(entity: EntityRecord) -> set[str]:
    """What a manifest entity blocks on: its whole name forms, read
    through its mention record (ADR-0035 §2), never split into words.

    A person's record keys only when its status is `name`, a place's only
    when it is `toponym`: a role, a kinship term, a courtesy appellation,
    a placeholder, a composed description, a deictic, a generic head or
    a country's adjective of nationality names no one, so it keys
    nothing. A person keys by its label, its aliases, its record's
    unsplit forms and nicknames, and the whole forms its typed parts make
    (``_composed``: "Gustl", read as Gustav, with the surname Löbig makes
    "Gustl Löbig" and "Gustav Löbig"); a
    place by its record's own names and address, a short form that is
    another of its names' toponym left out (so a camp never meets its
    town), or by its label where the record names none. Every other
    entity, and one without a record, keys by its label and aliases,
    each whole."""
    record = entity.structure
    if isinstance(record, PersonStructure):
        if record.status != "name":
            return set()
        return _keys(
            [
                entity.label,
                *entity.aliases,
                *record.unsplit,
                *record.nicknames,
                *_composed(record),
            ]
        )
    if isinstance(record, PlaceStructure):
        if record.status != "toponym":
            return set()
        toponyms = _keys(
            name.toponym
            for name in record.names
            if name.toponym and _keys([name.toponym]) != _keys([name.written])
        )
        names = [
            name.written
            for name in record.names
            if not _keys([name.written]) & toponyms
        ]
        if record.address is not None:
            names.append(record.address.written)
        return _keys(names or [entity.label])
    return _keys([entity.label, *entity.aliases])


def roster_name_keys(roster_entity: RosterEntity) -> set[str]:
    """What a graph item blocks on: its label and aliases, and the values
    of its typed name statements (birth, nickname, official name), each
    whole. A label or alias equal to the value of one of the item's own
    `referred_to_as` statements not deprecated (ADR-0018), as
    ``name_form_key`` compares them, is an appellation, no name form, and
    keys nothing, so two grandmothers each labelled or aliased "Oma"
    never pair by it, while a longer name holding it as a word still
    keys. Only a kind that may be a person has appellations: a place's,
    an organisation's or a family's every name keys. A typed name
    statement keys whatever its value, so a nickname that is also an
    appellation still keys. A married name is a family name alone by its
    property, so it never stands for a whole name. A birth name is a full
    name by its property; one a source gave as a surname alone keys as
    that surname, which meets only an entity named by it whole, since
    telling the two apart would read the value's words."""
    appellations = (
        set()
        if roster_entity.kind in NOT_PERSONS
        else {name_form_key(form) for form in roster_entity.appellations}
    )
    return _keys(
        [
            *(
                name
                for name in (roster_entity.label, *roster_entity.aliases)
                if name_form_key(name) not in appellations
            ),
            *roster_entity.birth_names,
            *roster_entity.nicknames,
            *roster_entity.official_names,
        ]
    )


# -- impossibility filter (ADR-0017) -----------------------------------------
#
# A shared name form blocks two entities as candidates, but the graph may
# already assert a fact that makes identity IMPOSSIBLE — an irreflexive
# kinship edge between them, or one place transitively inside the other.
# Both are read by QID (or, in clustering's mention space, by the id that
# stands in for one), never by a label: places share names.
# Pruning those before adjudication saves an LLM call and a review row, and
# is RECALL-SAFE: it drops only pairs the graph's own referenced statements
# structurally contradict, never a pair that is merely dissimilar.
#
# Deliberately NOT a rule: a DATE contradiction between two candidates.
# Contradictory dates across sources are a healthy resting state, not proof
# of distinctness (ADR-0002, neutral coexistence) — the reviewer must see
# them, and they carry precision the filter would have to narrow to compare
# (a "decade" is ten years, not its first; ADR-0002 forbids narrowing).
# Temporal disagreement is surfaced to adjudication, never pruned.

PLACE_KIND = "place"
KINSHIP_PROPERTIES = ("father", "mother", "spouse", "child", "sibling")


class PlaceContainment:
    """Transitive place-containment closure, read once from the graph.

    ``ancestors[q]`` is every place that transitively CONTAINS ``q``, along
    the edges the inconsistency report follows: a ``part_of`` or
    ``location`` statement that a source states and a curator has not
    deprecated, at any time it carries (ADR-0038 §5). A place is never
    identical to a place strictly containing it, so a containment edge
    between two candidates rules the pair out.
    """

    def __init__(self, ancestors: dict[str, set[str]]) -> None:
        self._ancestors = ancestors

    def contains(self, container_qid: str, contained_qid: str) -> bool:
        """Does ``container_qid`` transitively contain ``contained_qid``?"""
        return container_qid in self._ancestors.get(contained_qid, set())

    def containers(self, qid: str) -> set[str]:
        """Every place that transitively contains ``qid``."""
        return set(self._ancestors.get(qid, set()))


def read_place_containment(rows: Iterable[dict]) -> PlaceContainment:
    """The containment query's rows (``place-containment.rq``, the one the
    inconsistency report reads) as the closure over the edges
    ``yizkr.disagreement.followed`` keeps, by QID. A merge redirect's row
    is no statement and states no source, so it is passed over: it is no
    containment."""
    up: dict[str, set[str]] = {}
    for row in rows:
        stated = row.get("stated", {}).get("value") == "true"
        if not followed(row.get("rank", {}).get("value", ""), stated):
            continue
        item, container = (
            row[name]["value"].rsplit("/", 1)[-1]
            for name in ("item", "target")
        )
        up.setdefault(item, set()).add(container)
    ancestors: dict[str, set[str]] = {}
    for item in up:
        seen: set[str] = set()
        stack = list(up[item])
        while stack:
            container = stack.pop()
            if container in seen:
                continue
            seen.add(container)
            stack.extend(up.get(container, ()))
        ancestors[item] = seen
    return PlaceContainment(ancestors)


def fetch_place_containment(
    client: WikiClient, config: Config
) -> PlaceContainment:
    """One SPARQL query, the inconsistency report's containment query ->
    the transitive place-containment closure."""
    properties, items = client.vocabulary()
    query = render_for_instance(
        load_templates()["place-containment"],
        properties,
        items,
        config.concept_uri,
    )
    return read_place_containment(sparql.runner(config)(query))


@dataclass
class PairFacts:
    """The subset of an entity's graph facts the impossibility filter reads.

    Built from a RosterEntity (graph side) or an EntityRecord + its batch
    claims (new-document side). ``qid`` is None for a batch entity not yet
    written, which no containment rule can reach: the graph places it
    nowhere yet, and a label never stands in for a place. Corpus
    clustering (yizkr.pipeline.clustering) builds it in mention space: a
    mention's fingerprint stands in for the QID, and its kin are the
    mentions its manifest relates it to, lifted from the manifest's local
    ids.
    """

    kind: str
    qid: str | None = None
    kin_qids: set[str] = field(default_factory=set)


def _roster_pair_facts(roster_entity: RosterEntity) -> PairFacts:
    return PairFacts(
        kind=roster_entity.kind,
        qid=roster_entity.qid,
        kin_qids=roster_entity.kin_qids,
    )


def _batch_pair_facts(
    entity: EntityRecord, batch: ClaimBatch | None
) -> PairFacts:
    """Facts for a not-yet-written batch entity, from its own claims.

    A batch entity references others by local id; only relations to items
    ALREADY in the graph (a QidRef) contribute kin_qids — those are the
    only ones the graph-side candidate can be compared against."""
    facts = PairFacts(kind=entity.kind, qid=entity.qid)
    if batch is None:
        return facts
    for claim in batch.claims:
        if getattr(claim.subject, "local_id", None) != entity.id:
            continue
        value = claim.value
        if (
            claim.property in KINSHIP_PROPERTIES
            and isinstance(value, ItemValue)
            and isinstance(value.value, QidRef)
        ):
            facts.kin_qids.add(value.value.qid)
    return facts


def impossible_pair(
    a: PairFacts,
    b: PairFacts,
    *,
    containment: PlaceContainment | None = None,
) -> str | None:
    """Reason two candidates CANNOT be the same entity, or None.

    Deterministic, symmetric, no I/O. Fires only on STRUCTURAL graph facts
    that identity would contradict — an irreflexive kinship edge, or one
    place transitively inside the other, each read by QID. Anything softer
    (a date disagreement, which is a healthy coexisting state, ADR-0002,
    or a name shared with a container: namesake places exist) stays a
    candidate for the human; rules 5 & 6 forbid silently narrowing."""
    # R1: an irreflexive kinship edge between them (you are never your own
    # parent, child, sibling, or spouse).
    if a.qid and a.qid in b.kin_qids:
        return "kinship-edge"
    if b.qid and b.qid in a.kin_qids:
        return "kinship-edge"
    # R2: transitive place containment (a place is not its own container).
    if (
        containment is not None
        and a.kind == PLACE_KIND
        and b.kind == PLACE_KIND
        and a.qid
        and b.qid
        and (
            containment.contains(a.qid, b.qid)
            or containment.contains(b.qid, a.qid)
        )
    ):
        return "place-containment"
    return None


def block_candidates(
    entities: list[EntityRecord],
    roster: list[RosterEntity],
    *,
    batch: ClaimBatch | None = None,
    containment: PlaceContainment | None = None,
) -> dict[str, list[RosterEntity]]:
    """Deterministic candidate generation: shared kind + a shared whole
    name form (``entity_name_keys`` against ``roster_name_keys``), minus
    pairs the graph proves impossible (ADR-0017). The LLM never
    free-searches the graph; it only adjudicates the candidates this
    produces (auditability).

    ``batch`` supplies each entity's own claims (dates, resolved kin) for
    the impossibility filter; ``containment`` the place-containment closure.
    Both optional — without them the filter simply prunes less."""
    candidates: dict[str, list[RosterEntity]] = {}
    for entity in entities:
        keys = entity_name_keys(entity)
        entity_pair_facts = _batch_pair_facts(entity, batch)
        matches = [
            roster_entity
            for roster_entity in roster
            if roster_entity.kind == entity.kind
            and keys & roster_name_keys(roster_entity)
            and not impossible_pair(
                entity_pair_facts,
                _roster_pair_facts(roster_entity),
                containment=containment,
            )
        ]
        if matches:
            candidates[entity.id] = matches
    return candidates


def entity_facts(batch: ClaimBatch, entity_id: str) -> str:
    """Compact relational facts about a manifest entity, from the batch: a
    date at the precision its source states (`adjudication.date_text`)."""
    lines = []
    for claim in batch.claims:
        if getattr(claim.subject, "local_id", None) != entity_id:
            continue
        value = claim.value
        if isinstance(value, ItemValue):
            rendered = getattr(value.value, "label", None) or getattr(
                value.value, "qid", ""
            )
        elif value.type == "time":
            # imported here: adjudication imports clustering, which
            # imports this module
            from yizkr.pipeline.adjudication import date_text

            rendered = date_text(value)
        elif value.type == "monolingual":
            rendered = value.value.text  # names, causes: just the text
        else:
            rendered = str(getattr(value, "value", value))
        lines.append(f"{claim.property}: {rendered}")
    return "; ".join(lines[:12])


ADJUDICATION_PROMPT = """\
You adjudicate entity identity for a family archive. For each entity
extracted from a new document, decide whether it is the same real-world
entity as each candidate already in the graph. Weigh RELATIONAL
evidence over spelling: the same spouse, the same parents, compatible
birth/death dates, or an alias relationship between names is strong;
a shared surname alone is weak. People recur across family documents
under fuller or shorter names. Typed names are strong evidence when they
line up: one entity's married name equalling another's birth/maiden name
(a name change on marriage), or a shared nickname, points to identity —
a differing birth name to distinctness.

Verdicts: "same" (confident identity), "uncertain" (plausible, needs
the reviewer's judgement), "distinct" (different entities).

For every (entity, candidate) pair below, output one NDJSON line:
{{"entity_id": "<id>", "qid": "<Q..>", "verdict": "same|uncertain|distinct",
"confidence": <0-1>, "evidence": "<one sentence naming the deciding facts>"}}
No wrapper object, no code fences, no prose. After the last line, end
with exactly one final line:
{{"done": true}}

Entities from the new document:
{entities_block}

Candidates already in the graph:
{candidates_block}
"""


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str
    qid: str
    verdict: str  # same | uncertain | distinct
    confidence: float | None = None
    evidence: str = ""


# Pairs per adjudication call. Bounds each response — the same
# output-size constraint chunked extraction enforces (ADR-0006), one
# level up: an unbatched call over a large roster would overflow one
# response and hit the same multi-turn fragility.
ADJUDICATION_BATCH_PAIRS = 40


def _batch_candidates(
    candidates: dict[str, list[RosterEntity]], batch_pairs: int
) -> list[dict[str, list[RosterEntity]]]:
    """Split by entity, keeping each entity's candidates together —
    the adjudicator compares a candidate against its alternatives."""
    batches: list[dict[str, list[RosterEntity]]] = []
    current: dict[str, list[RosterEntity]] = {}
    count = 0
    for entity_id, matches in candidates.items():
        if current and count + len(matches) > batch_pairs:
            batches.append(current)
            current, count = {}, 0
        current[entity_id] = matches
        count += len(matches)
    if current:
        batches.append(current)
    return batches


def _parse_proposals(
    response: str,
    valid_pairs: set[tuple[str, str]],
    *,
    cut_off: bool = False,
):
    """The proposals of an adjudication answer on the pairs it was given.
    Raises ValueError for an answer cut off: one the engine stopped at its
    output limit (`cut_off`, llm.at_output_limit), or one without its
    terminal sentinel, a line whose `done` is true."""
    proposals: list[Proposal] = []
    finished = False
    for obj in scan_json_objects(response):
        if obj.get("done") is True:
            finished = True
            continue
        if "entity_id" not in obj or "qid" not in obj:
            continue
        try:
            proposal = Proposal.model_validate(obj)
        except Exception:
            continue
        if proposal.verdict not in ("same", "uncertain", "distinct"):
            continue
        # the adjudicator may only rule on pairs it was given
        pair = (proposal.entity_id, proposal.qid)
        if pair in valid_pairs or pair[::-1] in valid_pairs:
            proposals.append(proposal)
    if cut_off:
        raise ValueError(f"adjudication response {CUT_OFF_AT_LIMIT}")
    if not finished:
        raise ValueError("adjudication response carried no terminal sentinel")
    return proposals


def adjudicate(
    client: LlmClient,
    entities: list[EntityRecord],
    candidates: dict[str, list[RosterEntity]],
    batch: ClaimBatch,
    batch_pairs: int = ADJUDICATION_BATCH_PAIRS,
) -> list[Proposal]:
    """Batched LLM calls over the blocked pairs -> verified proposals."""
    proposals: list[Proposal] = []
    chunks = _batch_candidates(candidates, batch_pairs)
    total_pairs = sum(len(matches) for matches in candidates.values())
    print(
        f"adjudicating {total_pairs} pairs in {len(chunks)} "
        f"batch(es) of <= {batch_pairs}",
        file=sys.stderr,
    )
    for index, chunk in enumerate(chunks, start=1):
        entities_block = "\n".join(
            f"- {entity.id}: {entity.label}"
            + (
                f" (aliases: {', '.join(entity.aliases)})"
                if entity.aliases
                else ""
            )
            + f"; kind: {entity.kind}"
            + (f"; {entity.summary}" if entity.summary else "")
            + (
                f"\n  facts: {facts}"
                if (facts := entity_facts(batch, entity.id))
                else ""
            )
            for entity in entities
            if entity.id in chunk
        )
        candidates_block = "\n".join(
            f"- for {entity_id}: {roster_entity.facts()}"
            for entity_id, matches in chunk.items()
            for roster_entity in matches
        )
        response = client.complete(
            ADJUDICATION_PROMPT.format(
                entities_block=entities_block,
                candidates_block=candidates_block,
            )
        )
        valid_pairs = {
            (entity_id, roster_entity.qid)
            for entity_id, matches in chunk.items()
            for roster_entity in matches
        }
        parsed = _parse_proposals(
            response,
            valid_pairs,
            cut_off=at_output_limit(client, response),
        )
        print(
            f"  batch {index}/{len(chunks)}: {len(valid_pairs)} pairs -> "
            f"{len(parsed)} proposals (sentinel ok)",
            file=sys.stderr,
        )
        proposals.extend(parsed)
    return proposals


EQUIVALENCE_FIELDS = [
    "entity_id",
    "entity_label",
    "aliases",
    "qid",
    "qid_label",
    "verdict",
    "confidence",
    "evidence",
    "decision",
]


def export_equivalences_csv(
    proposals: list[Proposal],
    entities: list[EntityRecord],
    roster: list[RosterEntity],
    path: Path,
) -> int:
    """Proposals -> review CSV. `distinct` verdicts are included (a
    reviewer can overrule the machine in either direction).

    The `aliases` column shows the document aliases that approval will
    APPEND to the matched item — an approval is also an alias
    contribution, so the reviewer must see (and may prune) it: delete
    a bad entry from the cell and only the rest are appended.
    """
    entity_index = {entity.id: entity for entity in entities}
    roster_labels = {
        roster_entity.qid: roster_entity.label for roster_entity in roster
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EQUIVALENCE_FIELDS)
        writer.writeheader()
        for proposal in proposals:
            entity = entity_index.get(proposal.entity_id)
            writer.writerow(
                {
                    "entity_id": proposal.entity_id,
                    "entity_label": entity.label if entity else "",
                    "aliases": "|".join(entity.aliases) if entity else "",
                    "qid": proposal.qid,
                    "qid_label": roster_labels.get(proposal.qid, ""),
                    "verdict": proposal.verdict,
                    "confidence": proposal.confidence,
                    "evidence": proposal.evidence,
                    "decision": "",
                }
            )
    return len(proposals)


def read_approved_equivalences(path: Path) -> dict[str, str]:
    """entity_id -> QID for rows a human approved.

    Only the human decision column grants effect; the machine verdict
    alone never does. Approving two rows for one entity is a reviewer
    error and raises.
    """
    approved: dict[str, str] = {}
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("decision", "").strip().lower() != "approve":
                continue
            entity_id = row["entity_id"]
            if entity_id in approved and approved[entity_id] != row["qid"]:
                raise ValueError(
                    f"equivalences CSV approves {entity_id!r} to both "
                    f"{approved[entity_id]} and {row['qid']}"
                )
            approved[entity_id] = row["qid"]
    return approved


def ratify_equivalence_verdicts(rows: list[dict]) -> dict[str, int]:
    """Fill each equivalence row's `decision` in place from the machine
    `verdict`, ratifying the adjudicator wholesale — the `accept-all`
    bulk path. This is NOT a blanket approve: it accepts what the machine
    decided, per row, so distinct-judged pairs are NOT merged.

        same      -> approve   (accept the proposed match)
        distinct  -> reject    (accept the non-match; the entity mints fresh)
        uncertain -> ""        (the machine punted — nothing to ratify;
                                left blank for the human, no merge)

    An entity the adjudicator marks `same` against MORE THAN ONE item is a
    contradiction it cannot ratify — `read_approved_equivalences` rejects a
    double approval — so those rows are left blank and counted separately,
    for the human to resolve. The output is therefore always applicable.

    Returns a breakdown: {approved, rejected, uncertain, conflicted}.
    """
    same_targets: dict[str, set[str]] = {}
    for row in rows:
        if row.get("verdict", "").strip().lower() == "same":
            same_targets.setdefault(row["entity_id"], set()).add(row["qid"])
    conflicted_entities = {
        entity_id for entity_id, qids in same_targets.items() if len(qids) > 1
    }

    counts = {"approved": 0, "rejected": 0, "uncertain": 0, "conflicted": 0}
    for row in rows:
        verdict = row.get("verdict", "").strip().lower()
        if verdict == "same" and row["entity_id"] in conflicted_entities:
            row["decision"] = ""
            counts["conflicted"] += 1
        elif verdict == "same":
            row["decision"] = "approve"
            counts["approved"] += 1
        elif verdict == "distinct":
            row["decision"] = "reject"
            counts["rejected"] += 1
        else:  # uncertain (or, defensively, any unrecognised verdict)
            row["decision"] = ""
            counts["uncertain"] += 1
    return counts


def read_reviewed_aliases(path: Path) -> dict[str, list[str]]:
    """entity_id -> the alias list as the reviewer left it, for approved
    rows of a CSV that carries the `aliases` column.

    The reviewer prunes bad machine aliases (a form's MOTHER label is a
    role, not a name) by deleting them from the cell; the writer then
    appends only what survived review. A CSV without the column yields
    nothing — the batch's aliases apply unchanged.
    """
    reviewed: dict[str, list[str]] = {}
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        if "aliases" not in (reader.fieldnames or []):
            return {}
        for row in reader:
            if row.get("decision", "").strip().lower() != "approve":
                continue
            reviewed[row["entity_id"]] = [
                alias.strip()
                for alias in row.get("aliases", "").split("|")
                if alias.strip()
            ]
    return reviewed


def apply_equivalences(
    batch: ClaimBatch,
    approved: dict[str, str],
    reviewed_aliases: dict[str, list[str]] | None = None,
) -> ClaimBatch:
    """Rewrite approved entities to their existing items.

    Sets EntityRecord.qid (the writer appends the document's aliases to
    that item) and replaces local refs with qid refs in every claim.
    `reviewed_aliases` (from the CSV's aliases column) overrides an
    approved entity's alias list — what the reviewer pruned is never
    appended.
    """

    def resolve_ref(ref):
        if isinstance(ref, LocalRef) and ref.local_id in approved:
            return QidRef(qid=approved[ref.local_id])
        return ref

    def resolve_claim(claim):
        updates: dict = {"subject": resolve_ref(claim.subject)}
        if isinstance(claim.value, ItemValue):
            updates["value"] = ItemValue(value=resolve_ref(claim.value.value))
        updates["qualifiers"] = [
            qualifier.model_copy(
                update={
                    "value": ItemValue(
                        value=resolve_ref(qualifier.value.value)
                    )
                }
            )
            if isinstance(qualifier.value, ItemValue)
            else qualifier
            for qualifier in claim.qualifiers
        ]
        return claim.model_copy(update=updates)

    # two document entities approved to the SAME item (a source with
    # internally duplicated records) can leave byte-identical claims;
    # collapse them here, where identity collapses (the ADR-0006 merge
    # invariant, applied post-equivalence)
    resolved_claims = []
    seen: set[str] = set()
    for claim in (resolve_claim(claim) for claim in batch.claims):
        key = claim.model_dump_json()
        if key not in seen:
            seen.add(key)
            resolved_claims.append(claim)

    def updated_entity(entity):
        if entity.id not in approved:
            return entity
        update: dict = {"qid": approved[entity.id]}
        if reviewed_aliases is not None and entity.id in reviewed_aliases:
            update["aliases"] = reviewed_aliases[entity.id]
        return entity.model_copy(update=update)

    return batch.model_copy(
        update={
            "claims": resolved_claims,
            "entities": [updated_entity(entity) for entity in batch.entities],
        }
    )


# -- graph-side pass (existing duplicates) -----------------------------------


GRAPH_ADJUDICATION_PROMPT = """\
You adjudicate whether pairs of items ALREADY in a family archive graph
are the same real-world entity (duplicates from separate documents).
Weigh RELATIONAL evidence over spelling: same spouse, same parents,
compatible dates. Fuller and shorter forms of one name are common.

Verdicts: "same", "uncertain", "distinct".

For every pair below, output one NDJSON line:
{{"entity_id": "<QID A>", "qid": "<QID B>",
"verdict": "same|uncertain|distinct",
"confidence": <0-1>, "evidence": "<one sentence naming the deciding facts>"}}
No wrapper object, no code fences, no prose.

After the last line, end with exactly one final line:
{{"done": true}}

Pairs:
{pairs_block}
"""


def propose_graph_pairs(
    client: LlmClient,
    roster: list[RosterEntity],
    batch_pairs: int = ADJUDICATION_BATCH_PAIRS,
    *,
    containment: PlaceContainment | None = None,
) -> list[Proposal]:
    """Blocked roster x roster pairs -> adjudicated duplicate proposals,
    in bounded batches per call. Two items of the same kind pair when they
    share a whole name form (``roster_name_keys``); ``impossible_pair``
    then prunes a pair before adjudication when a structural graph fact
    rules identity out: a kinship edge (father, mother, spouse, child or
    sibling) between the two, or, for two places, one transitively inside
    the other. Without ``containment`` only the kinship rule applies.
    Dates never prune a pair: contradictory dates go to adjudication and
    review (ADR-0017).

    Executing an approved pair is a curator action (item merge, or a
    referenced `said to be the same as` statement) — deliberately not
    automated here.
    """
    pairs: list[tuple[RosterEntity, RosterEntity]] = []
    keys = [roster_name_keys(roster_entity) for roster_entity in roster]
    for index, first in enumerate(roster):
        for offset, second in enumerate(roster[index + 1 :], index + 1):
            if first.kind != second.kind:
                continue
            if not keys[index] & keys[offset]:
                continue
            if impossible_pair(
                _roster_pair_facts(first),
                _roster_pair_facts(second),
                containment=containment,
            ):
                continue
            pairs.append((first, second))
    if not pairs:
        return []
    proposals: list[Proposal] = []
    for start in range(0, len(pairs), batch_pairs):
        chunk = pairs[start : start + batch_pairs]
        pairs_block = "\n".join(
            f"- {first.facts()}\n  vs {second.facts()}"
            for first, second in chunk
        )
        response = client.complete(
            GRAPH_ADJUDICATION_PROMPT.format(pairs_block=pairs_block)
        )
        valid_pairs = {(first.qid, second.qid) for first, second in chunk}
        proposals.extend(
            _parse_proposals(
                response,
                valid_pairs,
                cut_off=at_output_limit(client, response),
            )
        )
    return proposals


def export_graph_pairs_csv(
    proposals: list[Proposal], roster: list[RosterEntity], path: Path
) -> int:
    labels = {
        roster_entity.qid: roster_entity.label for roster_entity in roster
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "qid_a",
                "label_a",
                "qid_b",
                "label_b",
                "verdict",
                "confidence",
                "evidence",
                "decision",
            ],
        )
        writer.writeheader()
        for proposal in proposals:
            writer.writerow(
                {
                    "qid_a": proposal.entity_id,
                    "label_a": labels.get(proposal.entity_id, ""),
                    "qid_b": proposal.qid,
                    "label_b": labels.get(proposal.qid, ""),
                    "verdict": proposal.verdict,
                    "confidence": proposal.confidence,
                    "evidence": proposal.evidence,
                    "decision": "",
                }
            )
    return len(proposals)
