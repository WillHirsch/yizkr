"""Entity reconciliation (ADR-0007): proposals, never assertions.

Deterministic parts first — a roster prefetch of every graph entity
(one SPARQL query; a family archive's roster fits a prompt for years)
and candidate blocking (shared kind + shared name token). Only then an
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
"""

from __future__ import annotations

import csv
import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from yizkr.claims import ClaimBatch, EntityRecord, ItemValue, LocalRef, QidRef
from yizkr.core.config import Config
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.extract import scan_json_objects
from yizkr.pipeline.llm import LlmClient
from yizkr.queries.publish import prefix_block


class RosterEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qid: str
    label: str
    aliases: list[str] = []
    kind: str = ""  # manifest item key when recognized, else raw label
    birth: str = ""
    death: str = ""
    spouses: list[str] = []
    fathers: list[str] = []
    mothers: list[str] = []
    birth_names: list[str] = []
    married_names: list[str] = []
    nicknames: list[str] = []
    official_names: list[str] = []
    # raw related-entity QIDs for the impossibility filter (ADR-0017):
    # a kinship edge between two candidates proves they are two people.
    spouse_qids: list[str] = []
    father_qids: list[str] = []
    mother_qids: list[str] = []
    child_qids: list[str] = []
    sibling_qids: list[str] = []
    # latest dated NON-death activity (living act) — a person cannot act
    # after another's death, so this bounds temporal identity (ADR-0017).
    last_activity: str = ""

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
            parts.append(f"kind: {self.kind}")
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
SELECT ?item ?itemLabel ?kindLabel
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
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?spouseItem), "entity/"); separator="|") AS ?spouseQids)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?fatherItem), "entity/"); separator="|") AS ?fatherQids)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?motherItem), "entity/"); separator="|") AS ?motherQids)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?childItem), "entity/"); separator="|") AS ?childQids)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?siblingItem), "entity/"); separator="|") AS ?siblingQids)
       (MAX(?activityDate) AS ?lastActivity)
WHERE {{
  ?item wdt:{instance_of} ?kind .
  OPTIONAL {{ ?item skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }}
  OPTIONAL {{ ?item wdt:{birth_name} ?birthName . }}
  OPTIONAL {{ ?item wdt:{married_name} ?marriedName . }}
  OPTIONAL {{ ?item wdt:{nickname} ?nick . }}
  OPTIONAL {{ ?item wdt:{official_name} ?officialName . }}
  OPTIONAL {{ ?item wdt:{date_of_birth} ?birthDate . }}
  OPTIONAL {{ ?item wdt:{date_of_death} ?deathDate . }}
  # dates asserted via event items (participant in a birth/death event)
  OPTIONAL {{ ?birthEvent wdt:{participant} ?item ;
                          wdt:{instance_of} wd:{birth_item} ;
                          wdt:{point_in_time} ?birthEventDate . }}
  OPTIONAL {{ ?deathEvent wdt:{participant} ?item ;
                          wdt:{instance_of} wd:{death_item} ;
                          wdt:{point_in_time} ?deathEventDate . }}
  # latest dated NON-death event the item took part in: a living act
  # (marriage, migration, release...). Bounds temporal identity — nobody
  # acts after they die, so activity past a candidate's death rules the
  # pair out (ADR-0017). Death events are excluded (a burial is posthumous).
  OPTIONAL {{ ?activityEvent wdt:{participant} ?item ;
                             wdt:{point_in_time} ?activityDate ;
                             wdt:{instance_of} ?activityKind .
              FILTER(?activityKind != wd:{death_item}) }}
  # relation labels carry QIDs: bare labels are ambiguous (two Davids)
  OPTIONAL {{ ?item wdt:{spouse} ?spouseItem . ?spouseItem rdfs:label ?spouseLabel .
              FILTER(LANG(?spouseLabel) = "en")
              BIND(CONCAT(?spouseLabel, " (", STRAFTER(STR(?spouseItem), "entity/"), ")") AS ?spouseDesc) }}
  OPTIONAL {{ ?item wdt:{father} ?fatherItem . ?fatherItem rdfs:label ?fatherLabel .
              FILTER(LANG(?fatherLabel) = "en")
              BIND(CONCAT(?fatherLabel, " (", STRAFTER(STR(?fatherItem), "entity/"), ")") AS ?fatherDesc) }}
  OPTIONAL {{ ?item wdt:{mother} ?motherItem . ?motherItem rdfs:label ?motherLabel .
              FILTER(LANG(?motherLabel) = "en")
              BIND(CONCAT(?motherLabel, " (", STRAFTER(STR(?motherItem), "entity/"), ")") AS ?motherDesc) }}
  # child and sibling are fetched for their QIDs only (the impossibility
  # filter reads them; the adjudicator's prose uses father/mother/spouse).
  OPTIONAL {{ ?item wdt:{child} ?childItem . }}
  OPTIONAL {{ ?item wdt:{sibling} ?siblingItem . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
GROUP BY ?item ?itemLabel ?kindLabel
"""


def fetch_roster(client: WikiClient, config: Config) -> list[RosterEntity]:
    """Every instance-of-bearing item with identity-relevant facts."""
    properties, items = client.vocabulary()
    # map kind item labels back to manifest keys where possible
    kind_label_to_key = {key.replace("_", " "): key for key in items}
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

    def split(binding: dict, name: str) -> list[str]:
        raw = field(binding, name)
        return [part for part in raw.split("|") if part] if raw else []

    roster = []
    for binding in bindings:
        kind_label = field(binding, "kindLabel")
        roster.append(
            RosterEntity(
                qid=field(binding, "item").rsplit("/", 1)[-1],
                label=field(binding, "itemLabel"),
                aliases=split(binding, "aliases"),
                kind=kind_label_to_key.get(kind_label, kind_label),
                # direct statements win; event-derived dates fill gaps
                birth=(field(binding, "birth") or field(binding, "eventBirth"))[:10],
                death=(field(binding, "death") or field(binding, "eventDeath"))[:10],
                spouses=split(binding, "spouses"),
                fathers=split(binding, "fathers"),
                mothers=split(binding, "mothers"),
                birth_names=split(binding, "birthNames"),
                married_names=split(binding, "marriedNames"),
                nicknames=split(binding, "nicknames"),
                official_names=split(binding, "officialNames"),
                spouse_qids=split(binding, "spouseQids"),
                father_qids=split(binding, "fatherQids"),
                mother_qids=split(binding, "motherQids"),
                child_qids=split(binding, "childQids"),
                sibling_qids=split(binding, "siblingQids"),
                last_activity=(field(binding, "lastActivity"))[:10],
            )
        )
    return roster


def _name_tokens(*names: str) -> set[str]:
    return {
        token.strip(".,;:()\"'").lower()
        for name in names
        for token in name.split()
        if len(token.strip(".,;:()\"'")) >= 3
    }


def _tokens_overlap(first: set[str], second: set[str]) -> bool:
    """Prefix-aware token match: 'Alex' must block against 'Alexander'
    (nicknames and clipped forms are how family documents refer to
    people)."""
    if first & second:
        return True
    return any(a.startswith(b) or b.startswith(a) for a in first for b in second)


# -- impossibility filter (ADR-0017) -----------------------------------------
#
# A shared name token blocks two entities as candidates, but the graph may
# already assert a fact that makes identity IMPOSSIBLE — a kinship edge
# between them, one place transitively inside the other, or lifespans that
# cannot be one life. Pruning those before adjudication saves an LLM call
# and a review row, and is RECALL-SAFE: it only drops pairs the graph's own
# referenced statements contradict, never a pair that is merely dissimilar.
# Unknowns are never impossible (rule 5: uncertainty is structural).

PLACE_KIND = "place"
KINSHIP_PROPERTIES = ("father", "mother", "spouse", "child", "sibling")
# slack for coarse/uncertain dates: never prune two lifespans that are only
# a year apart — a source's year may be imprecise (rule 5).
LIFESPAN_TOLERANCE_YEARS = 1

CONTAINMENT_QUERY = """\
SELECT ?place ?ancestor ?ancestorLabel WHERE {{
  ?place wdt:{instance_of} wd:{place_item} .
  ?place (wdt:{part_of}|wdt:{location})+ ?ancestor .
  ?ancestor rdfs:label ?ancestorLabel . FILTER(LANG(?ancestorLabel) = "en")
}}"""


class PlaceContainment:
    """Transitive place-containment closure, read once from the graph.

    ``ancestors[q]`` is every place that transitively CONTAINS ``q`` (via
    ``(part_of|location)+`` — the same closure the reconcile-places pass
    uses, ADR-0009). A place is never identical to a place strictly
    containing it, so a containment edge between two candidates rules the
    pair out.
    """

    def __init__(
        self, ancestors: dict[str, set[str]], labels: dict[str, str]
    ) -> None:
        self._ancestors = ancestors
        self._labels = labels

    def contains(self, container_qid: str, contained_qid: str) -> bool:
        """Does ``container_qid`` transitively contain ``contained_qid``?"""
        return container_qid in self._ancestors.get(contained_qid, set())

    def container_labels(self, qid: str) -> set[str]:
        """Labels of every place that transitively contains ``qid`` — used
        to rule out a not-yet-written batch place (no QID) against a graph
        candidate strictly inside a place of the batch entity's name."""
        return {
            self._labels[ancestor]
            for ancestor in self._ancestors.get(qid, set())
            if ancestor in self._labels
        }


def fetch_place_containment(client: WikiClient, config: Config) -> PlaceContainment:
    """One SPARQL query -> the transitive place-containment closure."""
    properties, items = client.vocabulary()
    query = prefix_block(config.concept_uri) + CONTAINMENT_QUERY.format(
        instance_of=properties["instance_of"],
        part_of=properties["part_of"],
        location=properties["location"],
        place_item=items[PLACE_KIND],
    )
    url = (
        config.sparql_url
        + "?"
        + urllib.parse.urlencode({"query": query, "format": "json"})
    )
    with urllib.request.urlopen(url, timeout=120) as response:
        bindings = json.load(response)["results"]["bindings"]
    ancestors: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    for binding in bindings:
        place = binding["place"]["value"].rsplit("/", 1)[-1]
        ancestor = binding["ancestor"]["value"].rsplit("/", 1)[-1]
        ancestors.setdefault(place, set()).add(ancestor)
        labels[ancestor] = binding.get("ancestorLabel", {}).get("value", "")
    return PlaceContainment(ancestors, labels)


@dataclass
class _PairFacts:
    """The subset of an entity's graph facts the impossibility filter reads.

    Built from a RosterEntity (graph side) or an EntityRecord + its batch
    claims (new-document side). ``qid`` is None for a batch entity not yet
    written — its containment is matched by label instead.
    """

    kind: str
    label: str
    qid: str | None = None
    kin_qids: set[str] = field(default_factory=set)
    birth: str = ""
    death: str = ""
    last_activity: str = ""


def _roster_pair_facts(roster_entity: RosterEntity) -> _PairFacts:
    return _PairFacts(
        kind=roster_entity.kind,
        label=roster_entity.label,
        qid=roster_entity.qid,
        kin_qids=roster_entity.kin_qids,
        birth=roster_entity.birth,
        death=roster_entity.death,
        last_activity=roster_entity.last_activity,
    )


def _batch_pair_facts(entity: EntityRecord, batch: ClaimBatch | None) -> _PairFacts:
    """Facts for a not-yet-written batch entity, from its own claims.

    A batch entity references others by local id; only relations to items
    ALREADY in the graph (a QidRef) contribute kin_qids. Dates come from
    the entity's own time-valued claims; the latest non-death date is its
    activity bound.
    """
    facts = _PairFacts(kind=entity.kind, label=entity.label, qid=entity.qid)
    if batch is None:
        return facts

    def _iso(time_value) -> str:
        return "-".join(
            f"{part:02d}" if index else f"{part:04d}"
            for index, part in enumerate(
                (time_value.year, time_value.month, time_value.day)
            )
            if part is not None
        )

    activity: list[str] = []
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
        elif getattr(value, "type", None) == "time":
            if claim.property == "date_of_birth":
                facts.birth = _iso(value)
            elif claim.property == "date_of_death":
                facts.death = _iso(value)
            else:
                activity.append(_iso(value))
        # a living act's date is usually a point-in-time qualifier (a
        # marriage, a move); count those too — but never the earliest/latest
        # precision qualifiers on the birth/death dates themselves.
        if claim.property not in ("date_of_birth", "date_of_death"):
            for qualifier in claim.qualifiers:
                if getattr(qualifier.value, "type", None) == "time":
                    activity.append(_iso(qualifier.value))
    if activity:
        facts.last_activity = max(activity)
    return facts


def _year(iso: str) -> int | None:
    head = (iso or "")[:4]
    return int(head) if head.isdigit() else None


def impossible_pair(
    a: _PairFacts,
    b: _PairFacts,
    *,
    containment: PlaceContainment | None = None,
) -> str | None:
    """Reason two candidates CANNOT be the same entity, or None.

    Deterministic, symmetric, no I/O. Fires only on hard graph facts:
    an irreflexive kinship edge, transitive place containment, or lifespans
    that cannot overlap. Anything softer stays a candidate for the human
    (rules 5 & 6 forbid silently narrowing)."""
    # R1: an irreflexive kinship edge between them (you are never your own
    # parent, child, sibling, or spouse).
    if a.qid and a.qid in b.kin_qids:
        return "kinship-edge"
    if b.qid and b.qid in a.kin_qids:
        return "kinship-edge"
    # R2: transitive place containment (a place is not its own container).
    if containment is not None and a.kind == PLACE_KIND and b.kind == PLACE_KIND:
        if a.qid and b.qid and (
            containment.contains(a.qid, b.qid) or containment.contains(b.qid, a.qid)
        ):
            return "place-containment"
        # label form: a batch place (no QID) named like a container of the
        # graph candidate — e.g. new "Poland" vs the graph's Chełmno camp.
        if b.qid and a.label and a.label in containment.container_labels(b.qid):
            return "place-containment"
        if a.qid and b.label and b.label in containment.container_labels(a.qid):
            return "place-containment"
    # R3: temporal impossibility.
    a_birth, a_death = _year(a.birth), _year(a.death)
    b_birth, b_death = _year(b.birth), _year(b.death)
    a_last, b_last = _year(a.last_activity), _year(b.last_activity)
    tol = LIFESPAN_TOLERANCE_YEARS
    # disjoint lifespan: one dies before the other is born.
    if a_death is not None and b_birth is not None and a_death < b_birth - tol:
        return "disjoint-lifespan"
    if b_death is not None and a_birth is not None and b_death < a_birth - tol:
        return "disjoint-lifespan"
    # post-mortem activity: one is active (a living act) after the other died.
    if a_death is not None and b_last is not None and a_death < b_last - tol:
        return "post-mortem-activity"
    if b_death is not None and a_last is not None and b_death < a_last - tol:
        return "post-mortem-activity"
    return None


def block_candidates(
    entities: list[EntityRecord],
    roster: list[RosterEntity],
    *,
    batch: ClaimBatch | None = None,
    containment: PlaceContainment | None = None,
) -> dict[str, list[RosterEntity]]:
    """Deterministic candidate generation: shared kind + shared name
    token (prefix-aware), minus pairs the graph proves impossible
    (ADR-0017). The LLM never free-searches the graph; it only adjudicates
    the candidates this produces (auditability).

    ``batch`` supplies each entity's own claims (dates, resolved kin) for
    the impossibility filter; ``containment`` the place-containment closure.
    Both optional — without them the filter simply prunes less."""
    candidates: dict[str, list[RosterEntity]] = {}
    for entity in entities:
        tokens = _name_tokens(entity.label, *entity.aliases)
        entity_pair_facts = _batch_pair_facts(entity, batch)
        matches = [
            roster_entity
            for roster_entity in roster
            if roster_entity.kind == entity.kind
            and _tokens_overlap(
                tokens, _name_tokens(roster_entity.label, *roster_entity.aliases)
            )
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
    """Compact relational facts about a manifest entity, from the batch."""
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
            rendered = "-".join(
                str(part)
                for part in (value.year, value.month, value.day)
                if part is not None
            )
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
{{"done": true, "proposals_emitted": N}}

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
    # hand-off (ADR-0017): on a `distinct` verdict where one item is
    # spatially/organizationally INSIDE the other, the QID of the CONTAINER.
    # A rejected equivalence that is really a containment feeds a part_of
    # proposal to reconcile-places instead of being discarded.
    contained_in: str | None = None


# Pairs per adjudication call. Bounds each response — the same
# output-size constraint chunked extraction enforces (ADR-0006), one
# level up: an unbatched call over a large roster is the multi-turn
# fragility all over again.
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


def _parse_proposals(response: str, valid_pairs: set[tuple[str, str]]):
    proposals: list[Proposal] = []
    sentinel = None
    for obj in scan_json_objects(response):
        if obj.get("done") is True:
            sentinel = obj.get("proposals_emitted")
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
    if sentinel is None:
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
            + (f" (aliases: {', '.join(entity.aliases)})" if entity.aliases else "")
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
                entities_block=entities_block, candidates_block=candidates_block
            )
        )
        valid_pairs = {
            (entity_id, roster_entity.qid)
            for entity_id, matches in chunk.items()
            for roster_entity in matches
        }
        parsed = _parse_proposals(response, valid_pairs)
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
    roster_labels = {roster_entity.qid: roster_entity.label for roster_entity in roster}
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


def read_reviewed_aliases(path: Path) -> dict[str, list[str]]:
    """entity_id -> the alias list as the reviewer left it, for approved
    rows of a CSV that carries the `aliases` column.

    The reviewer prunes bad machine aliases (a form's MOTHER label is a
    role, not a name) by deleting them from the cell; the writer then
    appends only what survived review. CSVs from before the column
    existed yield nothing — the batch's aliases apply unchanged.
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
                update={"value": ItemValue(value=resolve_ref(qualifier.value.value))}
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
{{"entity_id": "<QID A>", "qid": "<QID B>", "verdict": "same|uncertain|distinct",
"confidence": <0-1>, "evidence": "<one sentence naming the deciding facts>"}}
No wrapper object, no code fences, no prose.

CONTAINMENT: if you rule a pair "distinct" AND one is a place or
organization spatially INSIDE the other (a camp within a country, a
street within a town), add `"contained_in": "<QID of the LARGER,
containing one>"` to that line. This is not identity — it records a
part-of relation the graph may be missing, for a separate review. Only
add it when one genuinely contains the other.

After the last line, end with exactly one final line:
{{"done": true, "proposals_emitted": N}}

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
    in bounded batches per call. Pairs the graph proves impossible
    (kinship edge, existing containment, disjoint lifespan) are pruned
    before adjudication (ADR-0017).

    Executing an approved pair is a curator action (item merge, or a
    referenced `said to be the same as` statement) — deliberately not
    automated here.
    """
    pairs: list[tuple[RosterEntity, RosterEntity]] = []
    for index, first in enumerate(roster):
        first_tokens = _name_tokens(first.label, *first.aliases)
        for second in roster[index + 1 :]:
            if first.kind != second.kind:
                continue
            if not _tokens_overlap(
                first_tokens, _name_tokens(second.label, *second.aliases)
            ):
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
            f"- {first.facts()}\n  vs {second.facts()}" for first, second in chunk
        )
        response = client.complete(
            GRAPH_ADJUDICATION_PROMPT.format(pairs_block=pairs_block)
        )
        valid_pairs = {(first.qid, second.qid) for first, second in chunk}
        proposals.extend(_parse_proposals(response, valid_pairs))
    return proposals


def export_graph_pairs_csv(
    proposals: list[Proposal], roster: list[RosterEntity], path: Path
) -> int:
    labels = {roster_entity.qid: roster_entity.label for roster_entity in roster}
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


def place_containment_proposals(
    proposals: list[Proposal], roster: list[RosterEntity]
) -> list[dict]:
    """Rejected-equivalence containments -> reconcile-places rows (ADR-0017).

    A `distinct` proposal whose `contained_in` names one of its pair as the
    container becomes an `a_within_b` row (child within parent) in the
    reconcile-places CSV format, so `reconcile-places --apply` writes the
    referenced part_of edge after human review. The graph gains the
    containment the equivalence pass discovered; future reconcile passes
    then prune the pair deterministically via the impossibility filter."""
    labels = {roster_entity.qid: roster_entity.label for roster_entity in roster}
    rows: list[dict] = []
    for proposal in proposals:
        parent = proposal.contained_in
        if proposal.verdict != "distinct" or not parent:
            continue
        pair = {proposal.entity_id, proposal.qid}
        if parent not in pair or len(pair) != 2:
            # the container must be exactly one of the adjudicated pair
            continue
        (child,) = pair - {parent}
        rows.append(
            {
                "qid_a": child,
                "label_a": labels.get(child, ""),
                "qid_b": parent,
                "label_b": labels.get(parent, ""),
                "question": f"reconcile flagged {child} as contained in {parent}",
                "verdict": "a_within_b",
                "confidence": proposal.confidence,
                "evidence": proposal.evidence,
                "decision": "",
            }
        )
    return rows


def export_place_containment_csv(rows: list[dict], path: Path) -> int:
    """Write containment hand-off rows in the reconcile-places CSV format,
    so `reconcile-places --apply <path>` consumes them after review."""
    from yizkr.pipeline.places import CSV_FIELDS

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
