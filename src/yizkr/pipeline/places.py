"""Place hierarchy (ADR-0009, ADR-0038): which existing place a container
a source states means.

A place's mention record states the chain of containers its source puts
it in, which the pass reads nearest first, as the record gives it
(ADR-0035 §2.2: "Fläckfeld, Kreis Mockburg, Hesse" is Fläckfeld within
Kreis Mockburg within Hesse). Each link of the chain is a link the
source states: the place within the nearest container, and each
container within the next (stated_links). A label's punctuation never
says what contains what, and no model is asked whether one place lies
within another (ADR-0038 §3): a containment reaches the graph only from
a source.

The one question asked is of identity: which existing place a container
of a chain means, among the places whose label or an alias is its name,
folded for case and accents and with umlauts written out (ADR-0035 §7),
every namesake a candidate, never one guessed. A link is asked
(build_worklist) where its container names more than one existing place,
or where no claim of its document gives it, a link its record alone
states. A link a claim of its document gives, whose container names one
place, is not asked: the writer wrote it from the claim. A link whose
container names no place, or whose place is no place of the graph, has
no name to be asked by, and is left out and counted (`unnamed`). The
answer names one of the candidates by its number, or none (UNCERTAIN),
which writes nothing.

Each answered link is written as its source states it (links_to_write,
write_link): `part of` the item the answer names, from the place, or from
the item the link before it resolved to (its answer, or the one place its
name names), stated in the stating document's artifact and inferred from
the pass's run item. It carries the time the document's claim gives
(the claim's qualifiers), and none where the record alone states the
link, as a record states no time and no page. A link written already,
with the same time and stated in the same artifact, is not written
again; an equal statement another source states gains the link's
references. A link that would close a cycle (`plan`), through the
graph's containment or the links planned before it, at any time, is
refused and counted, never written.

The prompt names each place by its label and its item's description,
with the labels of the places the graph puts it in and the chains of
containers its records state; places whose names still coincide are
numbered by the documents whose records name them, else in the order
they are given (place_names). It numbers the questions of a batch and
the candidates of each, and an answer names both by number, never by a
QID. Questions are ordered spine-first, a question whose candidates the
graph puts more places inside first, then by their text, so the prompt
is the same for the same graph whatever QIDs an instance gave its items
(ADR-0032).

The command (`yizkr reconcile-places`) exports the answers as a CSV a
curator reviews, and its `--apply` writes the approved rows' links, cited
as above. A development-stage scratch run writes every answer unattended
instead, in its `places` stage (yizkr.pipeline.scratchwrite, ADR-0025
§1).
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from yizkr.claims import (
    ClaimBatch,
    ItemValue,
    LocalRef,
    PlaceStructure,
    QidRef,
    Qualifier,
)
from yizkr.core.config import Config
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.extract import scan_json_objects
from yizkr.pipeline.llm import (
    CUT_OFF_AT_LIMIT,
    LlmClient,
    at_output_limit,
    reject_response,
)
from yizkr.queries.publish import prefix_block

# the answer that names no candidate
UNCERTAIN = "uncertain"
BATCH_QUESTIONS = 40

# the review CSV (yizkr reconcile-places): A is the place whose record
# states the chain, B the candidate the answer names; `verdict` is SAME
# when the answer names B, UNCERTAIN when it names none; `chain` (JSON) and
# `container` (its number in the chain, from 1, as the question gives
# it) name the question
CSV_FIELDS = [
    "qid_a",
    "label_a",
    "qid_b",
    "label_b",
    "question",
    "verdict",
    "confidence",
    "evidence",
    "decision",
    "chain",
    "container",
]
# the verdict of a row whose answer names a candidate: the container is
# that place
SAME = "same"

# what write_link did with a link: a statement of its own, references an
# equal statement gained, or nothing, as the statement cites the artifact
WRITTEN, REFERENCED, PRESENT = "written", "referenced", "present"
# the edit summaries of a link's write
WRITTEN_SUMMARY = "places: a containment its source states"
REFERENCED_SUMMARY = "places: another source of a containment"


@dataclass
class PlaceRecord:
    qid: str
    label: str
    aliases: list[str] = field(default_factory=list)
    # the places the graph puts it in, through a part_of or location
    # statement of any time that is not deprecated
    parents: list[str] = field(default_factory=list)
    # the item's English description, which the instance keeps unique with
    # its label, and which the writer makes from the identity map alone
    description: str = ""
    # each of its mention records' chains of containers, as written,
    # nearest first (ADR-0035 §2.2)
    chains: list[list[str]] = field(default_factory=list)
    # the documents (SHA-1s) stating each chain, by its place in
    # `chains`; empty where no one has said
    chain_sources: list[list[str]] = field(default_factory=list)

    def state(self, chain: list[str], sources: Iterable[str] = ()) -> None:
        """Add a chain its records state, with the documents stating it;
        a chain it holds already gains those documents."""
        chain = list(chain)
        if chain not in self.chains:
            self.chains.append(chain)
        index = self.chains.index(chain)
        while len(self.chain_sources) < len(self.chains):
            self.chain_sources.append([])
        for sha1 in sources:
            if sha1 not in self.chain_sources[index]:
                self.chain_sources[index].append(sha1)


@dataclass(frozen=True)
class StatedLink:
    """One link of a chain of containers a document's mention record
    states: `chain[index - 1]` (the place itself, at 0) within
    `chain[index]`. `qualifiers` is the time the claim of the document
    that gives the link carries, each qualifier as canonical JSON, and
    `claimed` whether such a claim exists; a link the record alone states
    carries none."""

    sha1: str
    place: str
    chain: tuple[str, ...]
    index: int
    qualifiers: tuple[str, ...] = ()
    claimed: bool = False

    @property
    def container(self) -> str:
        return self.chain[self.index]

    @property
    def key(self) -> tuple[str, tuple[str, ...], int]:
        return (self.place, self.chain, self.index)

    def qualifier_models(self) -> list[Qualifier]:
        return [
            Qualifier.model_validate_json(text) for text in self.qualifiers
        ]

    def as_json(self) -> dict:
        return {
            "sha1": self.sha1,
            "place": self.place,
            "chain": list(self.chain),
            "index": self.index,
            "qualifiers": list(self.qualifiers),
            "claimed": self.claimed,
        }

    @classmethod
    def from_json(cls, data: dict) -> StatedLink:
        return cls(
            sha1=data["sha1"],
            place=data["place"],
            chain=tuple(data["chain"]),
            index=data["index"],
            qualifiers=tuple(data.get("qualifiers") or ()),
            claimed=bool(data.get("claimed")),
        )


@dataclass
class Question:
    """Which of `candidates` (QIDs) the container `chain[index]` of the
    chain a record states for `place` means; `links` are the stated links
    it decides, one per document and time."""

    place: str
    chain: tuple[str, ...]
    index: int
    candidates: list[str]
    links: list[StatedLink] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, tuple[str, ...], int]:
        return (self.place, self.chain, self.index)

    @property
    def container(self) -> str:
        return self.chain[self.index]


@dataclass
class Worklist:
    """The questions to ask, in order; the links resolved without one
    (`settled`, key -> the one place a claimed container names); and how
    many stated links were left out with no name to be asked by."""

    questions: list[Question] = field(default_factory=list)
    settled: dict[tuple, str] = field(default_factory=dict)
    unnamed: int = 0


PLACES_QUERY = """\
SELECT ?place ?placeLabel
       (GROUP_CONCAT(DISTINCT ?alias; separator="|") AS ?aliases)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?parent), "/entity/");
                      separator="|") AS ?parents)
       (SAMPLE(?about) AS ?description)
WHERE {{
  ?place wdt:{instance_of} wd:{place} .
  ?place rdfs:label ?placeLabel . FILTER(LANG(?placeLabel) = "en")
  OPTIONAL {{ ?place skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }}
  OPTIONAL {{
    ?place <http://schema.org/description> ?about .
    FILTER(LANG(?about) = "en")
  }}
  OPTIONAL {{
    VALUES ?link {{ wd:{part_of} wd:{location} }}
    ?link wikibase:claim ?p ;
          wikibase:statementProperty ?ps .
    ?place ?p ?edge .
    ?edge ?ps ?parent .
    FILTER NOT EXISTS {{ ?edge wikibase:rank wikibase:DeprecatedRank . }}
  }}
}}
GROUP BY ?place ?placeLabel
"""


def _run_query(config: Config, query: str) -> list[dict]:
    url = (
        config.sparql_url
        + "?"
        + urllib.parse.urlencode({"query": query, "format": "json"})
    )
    with urllib.request.urlopen(url, timeout=120) as response:
        return json.load(response)["results"]["bindings"]


def fetch_places(client: WikiClient, config: Config) -> list[PlaceRecord]:
    """The graph's places, each with its aliases, its description and the
    places its part_of and location statements put it in, at any time
    and from any source, but for a deprecated statement: what a cycle
    can run through."""
    properties, items = client.vocabulary()
    query = prefix_block(config.concept_uri) + PLACES_QUERY.format(
        instance_of=properties["instance_of"],
        part_of=properties["part_of"],
        location=properties["location"],
        place=items["place"],
    )
    records = []
    for row in _run_query(config, query):
        records.append(
            PlaceRecord(
                qid=row["place"]["value"].rsplit("/", 1)[-1],
                label=row["placeLabel"]["value"],
                aliases=[
                    a
                    for a in row.get("aliases", {}).get("value", "").split("|")
                    if a
                ],
                parents=[
                    p
                    for p in row.get("parents", {}).get("value", "").split("|")
                    if p
                ],
                description=row.get("description", {}).get("value", ""),
            )
        )
    return records


# a run directory's document views, newest first: the batch the writer
# wrote, the one reconcile resolved, and the extraction's own
_BATCH_VIEWS = ("batch.approved.json", "batch.resolved.json", "batch.json")


def _document_dirs(run_dir: Path) -> list[Path]:
    """Each directory of a run directory, an excerpt's too; one that
    holds no write.json is passed over by the caller."""
    from yizkr.pipeline.orchestrate import EXCERPTS_DIR

    try:
        entries = sorted(run_dir.iterdir())
    except OSError:
        return []
    with contextlib.suppress(OSError):
        entries += sorted((run_dir / EXCERPTS_DIR).iterdir())
    return [
        entry
        for entry in entries
        if entry.is_dir()
        and not entry.name.startswith(".")
        and entry.name != EXCERPTS_DIR
    ]


def record_links(run_dir: Path) -> list[StatedLink]:
    """The links the mention records of the places a run directory wrote
    state (stated_links), each document's from the batch it was written
    from and the items its write used (`write.json`'s `created_items`).
    The stating document is the one whose state the directory holds
    (its state.json's content SHA-1), else each artifact the place's
    claims cite (batch_artifacts). A directory set aside (a dot name), or
    one whose write.json is missing or no object, is passed over."""
    found: list[StatedLink] = []
    for directory in _document_dirs(run_dir):
        try:
            written = json.loads((directory / "write.json").read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(written, dict):
            continue
        created = written.get("created_items")
        if not isinstance(created, dict):
            created = {}
        batch = None
        for name in _BATCH_VIEWS:
            try:
                batch = ClaimBatch.model_validate_json(
                    (directory / name).read_text()
                )
            except (OSError, ValueError):
                continue
            break
        if batch is None:
            continue
        for link in stated_links(batch, created, _content_sha1(directory)):
            if link not in found:
                found.append(link)
    return found


def _content_sha1(directory: Path) -> str | None:
    """The SHA-1 of the document a run directory's document directory
    holds the state of, as its state.json records it; None when it
    records none or cannot be read."""
    from yizkr.pipeline.orchestrate import DocumentState

    try:
        return DocumentState.load(directory / "state.json").content_sha1
    except (OSError, ValueError, TypeError):
        return None


def batch_artifacts(batch: ClaimBatch, local_id: str) -> list[str]:
    """The SHA-1s of the artifacts that the claims mentioning the entity
    `local_id` cite, else those every claim of the batch cites: the
    document whose record the entity's is."""

    def mentions(claim) -> bool:
        refs = [claim.subject]
        if isinstance(claim.value, ItemValue):
            refs.append(claim.value.value)
        return any(
            isinstance(ref, LocalRef) and ref.local_id == local_id
            for ref in refs
        )

    cited = sorted(
        {
            claim.source.artifact.sha1
            for claim in batch.claims
            if mentions(claim)
        }
    )
    return cited or sorted(
        {claim.source.artifact.sha1 for claim in batch.claims}
    )


def _canonical(qualifier: Qualifier) -> str:
    return json.dumps(qualifier.model_dump(mode="json"), sort_keys=True)


def stated_links(
    batch: ClaimBatch, created: dict, sha1: str | None = None
) -> list[StatedLink]:
    """The links the mention records of `batch`'s places state, each
    container of a chain within the next, and the place within the
    first, for each place with a QID: the one reconcile resolved it to,
    else the item the writer used for its local id (`created`, local id
    -> QID). `sha1` names the stating document; without it, each artifact
    the place's claims cite does (batch_artifacts).

    A claim of the batch gives a link when it states `part_of` from the
    link's inner end to its container: from the place itself at the
    first link, else from an entity a name of which is the inner
    container's, to an entity a name of which is the container's, names
    compared folded (clustering.part_keys). An entity a claim names by
    its item, as an approved equivalence leaves it (reconcile
    .apply_equivalences), goes by the names of the batch's entity
    resolved to that item. Such a link carries each
    claim's qualifiers, the time its source gives, one link per distinct
    set; a link no claim gives carries none."""
    from yizkr.pipeline.clustering import part_keys

    records = {entity.id: entity for entity in batch.entities}

    resolved = {entity.qid: entity for entity in batch.entities if entity.qid}

    def keys(ref) -> frozenset[str]:
        if isinstance(ref, LocalRef):
            record = records.get(ref.local_id)
            fallback = [ref.label or ""]
        elif isinstance(ref, QidRef):
            # an entity an approved equivalence resolved to its item
            record, fallback = resolved.get(ref.qid), []
        else:
            return frozenset()
        names = (
            [record.label, *record.aliases] if record is not None else fallback
        )
        return frozenset(key for name in names for key in part_keys(name))

    claims = [
        claim
        for claim in batch.claims
        if claim.property == "part_of" and isinstance(claim.value, ItemValue)
    ]
    found: list[StatedLink] = []
    for entity in batch.entities:
        record = entity.structure
        if not isinstance(record, PlaceStructure) or not record.within:
            continue
        qid = entity.qid or created.get(entity.id)
        if not isinstance(qid, str) or not qid:
            continue
        chain = tuple(record.within)
        documents = [sha1] if sha1 else batch_artifacts(batch, entity.id)
        for index, container in enumerate(chain):
            outer = part_keys(container)
            inner = part_keys(chain[index - 1]) if index else frozenset()
            giving = [
                claim
                for claim in claims
                if keys(claim.value.value) & outer
                and (
                    _is_entity(claim.subject, entity)
                    if index == 0
                    else bool(keys(claim.subject) & inner)
                )
            ]
            times = sorted(
                {
                    tuple(sorted(_canonical(q) for q in claim.qualifiers))
                    for claim in giving
                }
            )
            for document in documents:
                for time in times or [()]:
                    link = StatedLink(
                        sha1=document,
                        place=qid,
                        chain=chain,
                        index=index,
                        qualifiers=time,
                        claimed=bool(times),
                    )
                    if link not in found:
                        found.append(link)
    return found


def attach_chains(places: list[PlaceRecord], links: list[StatedLink]) -> None:
    """Give each of `places` the chains its records state in `links`,
    with their documents, by which the prompt names it (place_names)."""
    by_qid = {place.qid: place for place in places}
    for link in links:
        if link.place in by_qid:
            by_qid[link.place].state(list(link.chain), [link.sha1])


def _is_entity(subject, entity) -> bool:
    """Whether a claim's subject is the batch's entity `entity`."""
    if isinstance(subject, LocalRef):
        return subject.local_id == entity.id
    return isinstance(subject, QidRef) and subject.qid == entity.qid


def _ancestors(places: list[PlaceRecord]) -> dict[str, set[str]]:
    """QID -> every place the graph transitively puts it in."""
    parents = {place.qid: set(place.parents) for place in places}
    closure: dict[str, set[str]] = {}
    for qid in parents:
        seen: set[str] = set()
        stack = sorted(parents[qid])
        while stack:
            parent = stack.pop()
            if parent in seen or parent == qid:
                continue
            seen.add(parent)
            stack.extend(sorted(parents.get(parent, ())))
        closure[qid] = seen
    return closure


def build_worklist(
    places: list[PlaceRecord], links: list[StatedLink]
) -> Worklist:
    """The questions the stated `links` ask (the module docstring), one
    per container of a chain a place's records state, with every link
    that container decides. Spine-first: a question whose candidates the
    graph puts more places inside comes first (larger scale), then one
    whose candidates it puts in fewer; among equals, the text the prompt
    gives the question decides (question_text), never a QID, so the same
    graph asks its questions in the same batches, in the same order, on
    any instance. A question's candidates go in the order of their
    names."""
    from yizkr.pipeline.clustering import part_keys

    names = place_names(places)
    known = {place.qid for place in places}
    by_key: dict[str, list[str]] = {}
    for place in places:
        for name in (place.label, *place.aliases):
            for key in part_keys(name):
                listed = by_key.setdefault(key, [])
                if place.qid not in listed:
                    listed.append(place.qid)

    def named(container: str, besides: str) -> list[str]:
        found: list[str] = []
        for key in sorted(part_keys(container)):
            for qid in by_key.get(key, ()):
                if qid != besides and qid not in found:
                    found.append(qid)
        return sorted(found, key=lambda qid: names[qid])

    groups: dict[tuple, list[StatedLink]] = {}
    for link in links:
        groups.setdefault(link.key, []).append(link)
    found = Worklist()
    for key, group in groups.items():
        place, chain, index = key
        candidates = named(chain[index], place) if place in known else []
        if not candidates:
            # no name to be asked by: the place is none of the graph's,
            # or its container names no place
            found.unnamed += len(group)
            continue
        if len(candidates) == 1 and all(link.claimed for link in group):
            # the claims of its documents give it, and the writer wrote it
            found.settled[key] = candidates[0]
            continue
        found.questions.append(
            Question(
                place=place,
                chain=chain,
                index=index,
                candidates=candidates,
                links=group,
            )
        )
    ancestors = _ancestors(places)
    inside: dict[str, int] = dict.fromkeys(ancestors, 0)
    for above in ancestors.values():
        for qid in above:
            inside[qid] = inside.get(qid, 0) + 1
    found.questions.sort(
        key=lambda q: (
            -max(inside.get(qid, 0) for qid in q.candidates),
            min(len(ancestors.get(qid, ())) for qid in q.candidates),
            question_text(q, names),
        )
    )
    return found


ADJUDICATION_PROMPT = """\
You identify PLACES for a family archive. Each question below gives the
chain of containers a source states for a place, nearest first, and names
one container of that chain. More than one existing place may bear that
name, or the source's name may fit none of them: decide which of the
numbered candidates the source means by it, from the chain and from what
is known of each candidate. Answer only which place the name means,
never whether one place lies within another.

For every question output one NDJSON line, naming the question and the
place by their numbers:
{{"question": <its number>, "place": <a candidate's number, or
"uncertain" when you cannot tell or no candidate fits>,
"confidence": <0-1>, "evidence": "<one sentence>"}}
No wrapper object, no code fences, no prose. End with exactly:
{{"done": true}}

Questions:
{questions_block}
"""


def place_names(places: list[PlaceRecord]) -> dict[str, str]:
    """QID -> how the prompt names the place: its label, its English
    description, the labels of the places the graph puts it in, and the
    chains of containers its records state, each in order, so two places
    of one name read apart where their descriptions, the graph or their
    records tell them apart. The instance keeps an item's label and
    description unique together, and the writer makes a minted item's
    description from the identity map alone, so items the writer minted
    read apart, and alike, after any reset. Places whose names still
    coincide (with no description, or one alike) are numbered [1], [2],
    ... in the order of the documents whose records name them, never of
    their QIDs; namesakes no stating document tells apart (curated items,
    say) are numbered in the order they are given."""
    labels = {place.qid: place.label for place in places}
    names = {}
    for place in places:
        within = sorted(
            {labels[parent] for parent in place.parents if parent in labels}
        )
        stated = sorted({", ".join(chain) for chain in place.chains if chain})
        parts = [place.description] if place.description else []
        if within:
            parts.append(f"within {', '.join(map(repr, within))}")
        if stated:
            parts.append(
                f"a record places it in {'; '.join(map(repr, stated))}"
            )
        names[place.qid] = repr(place.label) + (
            f" ({'; '.join(parts)})" if parts else ""
        )
    by_name: dict[str, list[PlaceRecord]] = {}
    for place in places:
        by_name.setdefault(names[place.qid], []).append(place)
    for name, same in by_name.items():
        if len(same) < 2:
            continue
        same.sort(
            key=lambda place: sorted(
                sha1 for found in place.chain_sources for sha1 in found
            )
        )
        for number, place in enumerate(same, start=1):
            names[place.qid] = f"{name} [{number}]"
    return names


def question_text(question: Question, names: dict[str, str]) -> str:
    """A question as the prompt gives it, without its number: the place,
    its chain, the container asked of, and the candidates, numbered from
    1 in their order."""
    chain = " within ".join(repr(name) for name in question.chain)
    lines = [
        f"A record states {names.get(question.place, '?')} within "
        f"{chain}. Which place does {question.container!r}, container "
        f"{question.index + 1} of that chain, name?"
    ]
    lines += [
        f"   place {number}: {names.get(qid, '?')}"
        for number, qid in enumerate(question.candidates, start=1)
    ]
    return "\n".join(lines)


def adjudication_prompt(
    questions: list[Question], names: dict[str, str]
) -> str:
    """The prompt that asks one batch of questions, numbered from 1."""
    block = "\n".join(
        f"{number}. {question_text(question, names)}"
        for number, question in enumerate(questions, start=1)
    )
    return ADJUDICATION_PROMPT.format(questions_block=block)


@dataclass
class Answer:
    """The answer to `question`: the QID of the candidate it names, or
    None for UNCERTAIN."""

    question: Question
    place: str | None
    confidence: float | None = None
    evidence: str = ""


@dataclass
class Adjudication:
    """What the adjudicator answered: its `answers`, one at most per
    question; how many answer lines the answer schema `rejected` (one
    naming no question asked, a place that is neither a candidate's
    number nor UNCERTAIN, a confidence that is no number from 0 to 1,
    evidence that is no text); the questions it answered twice or more,
    differently (`conflicts`), each refused whole; and each batch's raw
    answer (`responses`), in order."""

    answers: list[Answer] = field(default_factory=list)
    rejected: int = 0
    conflicts: list[Question] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)


def adjudicate(
    client: LlmClient,
    questions: list[Question],
    places: list[PlaceRecord],
    batch_questions: int = BATCH_QUESTIONS,
) -> Adjudication:
    """Ask the questions in batches of `batch_questions`, in their order,
    each batch one call (adjudication_prompt)."""
    names = place_names(places)
    found = Adjudication()
    batches = [
        questions[start : start + batch_questions]
        for start in range(0, len(questions), batch_questions)
    ]
    print(
        f"asking {len(questions)} place questions in "
        f"{len(batches)} batch(es) of <= {batch_questions}",
        file=sys.stderr,
    )
    for index, chunk in enumerate(batches, start=1):
        prompt = adjudication_prompt(chunk, names)
        response = client.complete(prompt)
        try:
            parsed = parse_answers(
                response, chunk, cut_off=at_output_limit(client, response)
            )
        except ValueError as exc:
            # an answer the LLM cache kept from an earlier invocation is
            # rejected and asked once more; an answer the engine gave in
            # this run is rejected and fails the batch
            if not reject_response(client, response, f"places: {exc}"):
                raise
            response = client.complete(prompt)
            try:
                parsed = parse_answers(
                    response, chunk, cut_off=at_output_limit(client, response)
                )
            except ValueError as again:
                reject_response(client, response, f"places: {again}")
                raise
        found.responses.append(response)
        print(
            f"  batch {index}/{len(batches)}: {len(chunk)} questions -> "
            f"{len(parsed.answers)} answers (sentinel ok)",
            file=sys.stderr,
        )
        found.answers.extend(parsed.answers)
        found.rejected += parsed.rejected
        found.conflicts.extend(parsed.conflicts)
    return found


def _number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _index(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_answers(
    response: str, questions: list[Question], *, cut_off: bool = False
) -> Adjudication:
    """The answers of a place adjudication answer to the questions it was
    asked, numbered from 1 in their order. Raises ValueError for an
    answer cut off: one the engine stopped at its output limit
    (`cut_off`, llm.at_output_limit), or one without its terminal
    sentinel, a line whose `done` is true."""
    found = Adjudication()
    finished = False
    given: dict[int, list[Answer]] = {}
    for obj in scan_json_objects(response):
        if obj.get("done") is True:
            finished = True
            continue
        number = obj.get("question")
        choice = obj.get("place")
        confidence = obj.get("confidence")
        evidence = obj.get("evidence", "")
        question = (
            questions[number - 1]
            if _index(number) and 1 <= number <= len(questions)
            else None
        )
        if (
            question is None
            or not (
                choice == UNCERTAIN
                or (_index(choice) and 1 <= choice <= len(question.candidates))
            )
            or (
                confidence is not None
                and (not _number(confidence) or not 0 <= confidence <= 1)
            )
            or not isinstance(evidence, str)
        ):
            # the adjudicator may only answer asked questions, naming one
            # of their candidates or none, in the answer's schema
            found.rejected += 1
            continue
        given.setdefault(number, []).append(
            Answer(
                question=question,
                place=None
                if choice == UNCERTAIN
                else question.candidates[choice - 1],
                confidence=confidence,
                evidence=evidence,
            )
        )
    if cut_off:
        raise ValueError(f"place adjudication response {CUT_OFF_AT_LIMIT}")
    if not finished:
        raise ValueError(
            "place adjudication response carried no terminal sentinel"
        )
    for number in sorted(given):
        answers = given[number]
        if len({answer.place for answer in answers}) > 1:
            found.conflicts.append(questions[number - 1])
            continue
        found.answers.append(answers[0])
    return found


@dataclass
class Planned:
    """The links to write, as (child, parent, link), and how many stated
    links an answer decided but could not place: their inner end was
    left unresolved by the link before it (`unresolved`)."""

    edges: list[tuple[str, str, StatedLink]] = field(default_factory=list)
    unresolved: int = 0


def links_to_write(work: Worklist, answers: list[Answer]) -> Planned:
    """Each link of an answered question, from its inner end to the place
    the answer names. The inner end is the place itself at a chain's
    first link, else the place the link before it resolved to: its
    answer, or the one place a claimed container names (`settled`). A
    question answered UNCERTAIN, or not answered, writes nothing."""
    resolved: dict[tuple, str | None] = dict(work.settled)
    for answer in answers:
        resolved[answer.question.key] = answer.place
    planned = Planned()
    for answer in answers:
        if answer.place is None:
            continue
        place, chain, index = answer.question.key
        child = (
            place if index == 0 else resolved.get((place, chain, index - 1))
        )
        for link in answer.question.links:
            if child is None:
                planned.unresolved += 1
                continue
            planned.edges.append((child, answer.place, link))
    return planned


def plan(
    edges: list[tuple[str, str, StatedLink]],
    parents_of: Callable[[str], Iterable[str]] = lambda node: (),
) -> tuple[
    list[tuple[str, str, StatedLink]], list[tuple[str, str, StatedLink]]
]:
    """The cycle guard: (kept, refused). An edge is refused when its
    parent is its child, or the parent already lies within the child,
    walking up the graph's containment (`parents_of(node)`: the places
    the graph puts `node` in, at any time) and the edges kept before it,
    so a cycle through either or both is caught."""
    kept: list[tuple[str, str, StatedLink]] = []
    refused: list[tuple[str, str, StatedLink]] = []
    planned: dict[str, set[str]] = {}

    def reaches(node: str, target: str) -> bool:
        """Walking up the planned edges and the graph's from `node`, do we
        hit `target`? Iterative, so a chain of any depth is walked."""
        seen: set[str] = set()
        stack = [node]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for parent in planned.get(current, set()) | set(
                parents_of(current)
            ):
                if parent == target:
                    return True
                stack.append(parent)
        return False

    for child, parent, link in edges:
        if child == parent or reaches(parent, child):
            refused.append((child, parent, link))
            continue
        kept.append((child, parent, link))
        planned.setdefault(child, set()).add(parent)
    return kept, refused


def write_link(
    writer,
    child: str,
    parent: str,
    link: StatedLink,
    artifact: str,
    run_item: Callable[[], str],
) -> str:
    """Write `child part of parent` as `link` states it, with the time
    its claim gives (none for a record's alone), stated in `artifact` and
    inferred from the run item, each in a reference block of its own, at
    normal rank, through `writer` (a writer.BatchWriter, whose statement
    matching and reference comparison it uses). `run_item()` gives the
    run item's QID, and is called only when something is to be written.
    Returns PRESENT, writing nothing, when a statement of the same value
    and qualifiers already cites the artifact; REFERENCED when such a
    statement citing other sources gains the link's blocks; WRITTEN when
    the link is a statement of its own."""
    from wikibaseintegrator.datatypes import Item
    from wikibaseintegrator.models import Reference, References

    from yizkr.pipeline.writer import WriteResult

    properties = writer.properties
    claim = Item(prop_nr=properties["part_of"], value=parent)
    result = WriteResult(run_qid="")
    for qualifier in link.qualifier_models():
        claim.qualifiers.add(
            writer._to_wbi_claim(
                qualifier.property, qualifier.value, result, artifact
            )
        )
    existing = writer._find_matching_statement(child, claim)
    if existing is not None and writer._has_artifact_reference(
        existing, artifact
    ):
        return PRESENT
    references = References()
    stated = Reference()
    stated.add(Item(prop_nr=properties["stated_in"], value=artifact))
    references.add(stated)
    inferred = Reference()
    inferred.add(
        Item(
            prop_nr=properties["inferred_from_extraction_run"],
            value=run_item(),
        )
    )
    references.add(inferred)
    entity = writer._entity(child)
    if existing is not None:
        present = {
            writer._reference_key(block)
            for block in existing.references.references
        }
        for block in references.references:
            if writer._reference_key(block) not in present:
                existing.references.add(block)
        entity.write(summary=REFERENCED_SUMMARY)
        outcome = REFERENCED
    else:
        claim.references = references
        writer._append(entity, claim)
        entity.write(summary=WRITTEN_SUMMARY)
        outcome = WRITTEN
    writer._entity_cache[child] = writer.client.wbi.item.get(child)
    return outcome


def export_places_csv(
    adjudication: Adjudication,
    places: list[PlaceRecord],
    path: Path,
) -> int:
    """One row per answer, for a curator (CSV_FIELDS), with an empty
    `decision` to fill in."""
    labels = {place.qid: place.label for place in places}
    names = place_names(places)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for answer in adjudication.answers:
            question = answer.question
            writer.writerow(
                {
                    "qid_a": question.place,
                    "label_a": labels.get(question.place, ""),
                    "qid_b": answer.place or "",
                    "label_b": labels.get(answer.place or "", ""),
                    "question": question_text(question, names),
                    "verdict": SAME if answer.place else UNCERTAIN,
                    "confidence": answer.confidence,
                    "evidence": answer.evidence,
                    "decision": "",
                    "chain": json.dumps(
                        list(question.chain), ensure_ascii=False
                    ),
                    "container": question.index + 1,
                }
            )
    return len(adjudication.answers)


def approved_answers(rows: list[dict], work: Worklist) -> list[Answer]:
    """The answers a reviewed CSV approves, matched to the questions
    `work` asks by their place, chain and container. A row that is not
    approved, whose verdict is not SAME, whose question `work` does not
    ask, or whose B is no candidate of it, gives none."""
    by_key = {question.key: question for question in work.questions}
    found = []
    for row in rows:
        if row.get("decision", "").strip().lower() != "approve":
            continue
        if row.get("verdict", "") != SAME:
            continue
        try:
            chain = tuple(json.loads(row.get("chain") or "[]"))
            index = int(row.get("container", "")) - 1
        except ValueError:
            continue
        question = by_key.get((row.get("qid_a", ""), chain, index))
        choice = row.get("qid_b", "")
        if question is None or choice not in question.candidates:
            continue
        found.append(Answer(question=question, place=choice))
    return found


RUN_DESCRIPTION = (
    "reconcile-places pass (ADR-0009, ADR-0038): which place a container "
    "a source states means, curator-reviewed"
)


def ensure_places_run_item(
    client: WikiClient,
    fingerprint: str,
    model: str,
    *,
    description: str = RUN_DESCRIPTION,
    marks: dict[str, str] | None = None,
) -> str:
    """The pass's run item, found by its label, which must name one item
    alone (WikiClient.find_item_by_label raises when two items hold it),
    or made. Its ``instance of
    -> extraction run`` cites the item itself, so it is written in a
    second write, and a run item that a pass cut short between the two
    writes left without it gets it when the next pass finds it
    (registrar.ensure_self_typed). Raises ValueError, naming the
    characters, before anything is read when the label holds a character
    Python's Unicode tables do not know (writer.unknown_characters): the
    instance may keep it in another form, and the next pass would not
    find the item by it and would make another. The label is looked for
    and sent in the form the instance keeps a label in, as the writer
    sends one (writer._term_text: trimmed, one line, in Unicode NFC, cut
    to the characters the instance holds), so a model identifier given
    in another form, such as NFD, finds the item made for it.
    `description` is the description a new item takes, and `marks` the
    string statements it states of itself (registrar.ensure_self_typed),
    as a scratch run's place stage marks its run item."""
    from yizkr.pipeline.registrar import ensure_self_typed
    from yizkr.pipeline.writer import (
        _term_text,
        _unknown_fault,
        unknown_characters,
    )

    label = f"place-hierarchy run {model} {fingerprint}"
    unknown = unknown_characters(label)
    if unknown:
        raise ValueError(
            f"the place-hierarchy run's label {_unknown_fault(unknown)}: "
            f"give the model identifier without it. Nothing was written"
        )
    label = _term_text(label)
    qid = client.find_item_by_label(label)
    if qid is None:
        item = client.wbi.item.new()
        item.labels.set("en", label)
        item.descriptions.set("en", description)
        qid = item.write(summary="reconcile-places: run item").id
    ensure_self_typed(
        client,
        qid,
        "extraction_run",
        summary="reconcile-places: run item statements",
        marks=marks,
    )
    return qid


def apply_answers(
    client: WikiClient,
    config: Config,
    csv_path: Path,
    links: list[StatedLink],
    model: str,
) -> dict:
    """Write the links a reviewed CSV's approved answers decide, cited as
    write_link cites them, through the cycle guard against the graph's
    containment. The worklist is derived again from the graph and the
    stated `links`, so a row whose question the graph does not ask writes
    nothing."""
    from yizkr.pipeline.writer import BatchWriter

    with open(csv_path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    places = fetch_places(client, config)
    attach_chains(places, links)
    work = build_worklist(places, links)
    planned = links_to_write(work, approved_answers(rows, work))
    graph = {place.qid: place.parents for place in places}
    kept, refused = plan(planned.edges, lambda node: graph.get(node, ()))
    labels = {place.qid: place.label for place in places}
    notes = [
        f"{labels.get(child, child)} ({child}) within "
        f"{labels.get(parent, parent)} ({parent}) would close a cycle: "
        "refused, review the identity of the places on it"
        for child, parent, _link in refused
    ]
    properties, _ = client.vocabulary()
    artifacts = client.string_statement_index(properties["media_sha1"])
    writer = BatchWriter(client)
    counts = {WRITTEN: 0, REFERENCED: 0, PRESENT: 0}
    run_qid = None
    fingerprint = hashlib.sha256(csv_path.read_bytes()).hexdigest()[:10]

    def run_item() -> str:
        nonlocal run_qid
        if run_qid is None:
            run_qid = ensure_places_run_item(client, fingerprint, model)
        return run_qid

    for child, parent, link in kept:
        artifact = artifacts.get(link.sha1)
        if artifact is None:
            notes.append(
                f"no artifact item holds the media {link.sha1}: its link "
                f"{child} within {parent} was not written"
            )
            continue
        counts[
            write_link(writer, child, parent, link, artifact, run_item)
        ] += 1
    return {
        "written": counts[WRITTEN],
        "referenced": counts[REFERENCED],
        "skipped": counts[PRESENT],
        "cycles": len(refused),
        "unresolved": planned.unresolved,
        "notes": notes,
        "run_qid": run_qid,
    }
