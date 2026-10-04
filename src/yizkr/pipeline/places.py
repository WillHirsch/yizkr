"""Place hierarchy pass (ADR-0009).

Demand-driven containment: the worklist is derived from the
inconsistency report's own unresolved place pairs plus stated-parent
candidates: the containers a place's mention records state (ADR-0035
§2.2: "Fläckfeld, Kreis Mockburg" is Fläckfeld within Kreis Mockburg),
each against every existing place of that name. A label's punctuation
never says what contains what. Questions are ordered spine-first by the
graph's own containment (a place the graph puts more places inside is
of larger scale) and each `--apply` round re-derives the worklist
through the graph's `(location|part_of)+` closure — transitive
elimination between rounds.

Verdicts extend ADR-0007: same | a_within_b | b_within_a | distinct |
uncertain. Only human-approved containment rows are ever written
(`part_of`, referenced to the pass's run item — world knowledge
carries process provenance, ADR-0009); `same` rows route to the
existing curator-executed identity flow and are never applied here.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from yizkr.claims import ClaimBatch, PlaceStructure
from yizkr.core.config import Config
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.extract import scan_json_objects
from yizkr.pipeline.llm import CUT_OFF_AT_LIMIT, LlmClient, at_output_limit
from yizkr.queries.publish import prefix_block

VERDICTS = ("same", "a_within_b", "b_within_a", "distinct", "uncertain")
BATCH_PAIRS = 40

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
]


@dataclass
class PlaceRecord:
    qid: str
    label: str
    aliases: list[str] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)  # existing containment
    # the containers its mention records state, as written, in the order
    # the records give them (ADR-0035 §2.2); never written to the graph
    within: list[str] = field(default_factory=list)


@dataclass
class Question:
    kind: str  # "competing" (report pair) | "parent" (stated container)
    qid_a: str
    qid_b: str
    context: str = ""  # human-readable why-this-is-asked


PLACES_QUERY = """\
SELECT ?place ?placeLabel
       (GROUP_CONCAT(DISTINCT ?alias; separator="|") AS ?aliases)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?parent), "/entity/");
                      separator="|") AS ?parents)
WHERE {{
  ?place wdt:{instance_of} wd:{place} .
  ?place rdfs:label ?placeLabel . FILTER(LANG(?placeLabel) = "en")
  OPTIONAL {{ ?place skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }}
  OPTIONAL {{ ?place (wdt:{part_of}|wdt:{location}) ?parent . }}
}}
GROUP BY ?place ?placeLabel
"""

# place-valued value pairs of one entity+property, still unresolved:
# not identical, not connected by containment — the report's own rows
COMPETING_QUERY = """\
SELECT DISTINCT ?valueA ?valueB ?entityLabel ?propertyLabel
WHERE {{
  VALUES ?property {{ wd:{place_of_birth} wd:{place_of_death} }}
  ?property wikibase:claim ?p ;
            wikibase:statementProperty ?ps .
  ?entity ?p ?stA , ?stB .
  ?stA ?ps ?rawA .
  ?stB ?ps ?rawB .
  OPTIONAL {{ ?rawA <http://www.w3.org/2002/07/owl#sameAs> ?canonA . }}
  OPTIONAL {{ ?rawB <http://www.w3.org/2002/07/owl#sameAs> ?canonB . }}
  BIND(COALESCE(?canonA, ?rawA) AS ?valueA)
  BIND(COALESCE(?canonB, ?rawB) AS ?valueB)
  FILTER(?valueA != ?valueB)
  FILTER(STR(?valueA) < STR(?valueB))
  FILTER NOT EXISTS {{ ?valueA (wdt:{location}|wdt:{part_of})+ ?valueB . }}
  FILTER NOT EXISTS {{ ?valueB (wdt:{location}|wdt:{part_of})+ ?valueA . }}
  OPTIONAL {{
    ?entity rdfs:label ?entityLabel .
    FILTER(LANG(?entityLabel) = "en")
  }}
  OPTIONAL {{ ?property rdfs:label ?propertyLabel .
              FILTER(LANG(?propertyLabel) = "en") }}
}}
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
            )
        )
    return records


def fetch_competing_pairs(
    client: WikiClient, config: Config
) -> list[Question]:
    properties, _ = client.vocabulary()
    query = prefix_block(config.concept_uri) + COMPETING_QUERY.format(
        place_of_birth=properties["place_of_birth"],
        place_of_death=properties["place_of_death"],
        part_of=properties["part_of"],
        location=properties["location"],
    )
    questions = []
    for row in _run_query(config, query):
        value_a = row["valueA"]["value"]
        value_b = row["valueB"]["value"]
        if "/entity/Q" not in value_a or "/entity/Q" not in value_b:
            continue  # dates etc. are not place questions
        questions.append(
            Question(
                kind="competing",
                qid_a=value_a.rsplit("/", 1)[-1],
                qid_b=value_b.rsplit("/", 1)[-1],
                context=(
                    f"both asserted as "
                    f"{row.get('propertyLabel', {}).get('value', '?')}"
                    f" of {row.get('entityLabel', {}).get('value', '?')}"
                ),
            )
        )
    return questions


# a run directory's document views, newest first: the batch the writer
# wrote, the one reconcile resolved, and the extraction's own
_BATCH_VIEWS = ("batch.approved.json", "batch.resolved.json", "batch.json")


def _document_dirs(run_dir: Path) -> list[Path]:
    """Each document's directory in a run directory, an excerpt's too;
    the corpus stages' directory holds no document."""
    from yizkr.pipeline.orchestrate import EXCERPTS_DIR
    from yizkr.pipeline.scratch import CORPUS_DIR

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
        and entry.name not in (CORPUS_DIR, EXCERPTS_DIR)
    ]


def record_containers(run_dir: Path) -> dict[str, list[str]]:
    """QID -> the containers the mention records of a written place
    state, from the run directory that wrote the graph.

    A document's place reaches its QID through the batch it was written
    from: the QID reconcile resolved it to, else the item the writer
    minted for its local id (`write.json`'s `created_items`). A place no
    write reached, or whose record states no container, is left out; a
    directory set aside (a dot name) is passed over."""
    found: dict[str, list[str]] = {}
    for directory in _document_dirs(run_dir):
        try:
            written = json.loads((directory / "write.json").read_text())
        except (OSError, ValueError):
            continue
        created = written.get("created_items") or {}
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
        for entity in batch.entities:
            record = entity.structure
            if not isinstance(record, PlaceStructure) or not record.within:
                continue
            qid = entity.qid or created.get(entity.id)
            if not qid:
                continue
            stated = found.setdefault(qid, [])
            stated.extend(c for c in record.within if c not in stated)
    return found


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


def stated_parent_candidates(places: list[PlaceRecord]) -> list[Question]:
    """Parent candidates from the containers a place's records state.

    Each stated container proposes every other existing place whose label
    or an alias is that container's name, folded for case and accents and
    with umlauts written out (ADR-0035 §7); namesakes are each asked,
    never one guessed. A
    candidate already among the place's containers, or one the graph puts
    inside the place, is not asked. Structure never rides the generative
    path: these arrive pre-evidenced."""
    from yizkr.pipeline.clustering import part_keys

    by_key: dict[str, list[str]] = {}
    for place in places:
        for name in (place.label, *place.aliases):
            for key in part_keys(name):
                by_key.setdefault(key, []).append(place.qid)
    ancestors = _ancestors(places)
    questions = []
    asked: set[tuple[str, str]] = set()
    for place in places:
        for container in place.within:
            for key in sorted(part_keys(container)):
                for parent in by_key.get(key, ()):
                    if (
                        parent == place.qid
                        or (place.qid, parent) in asked
                        or parent in ancestors.get(place.qid, ())
                        or place.qid in ancestors.get(parent, ())
                    ):
                        continue
                    asked.add((place.qid, parent))
                    questions.append(
                        Question(
                            kind="parent",
                            qid_a=place.qid,
                            qid_b=parent,
                            context=(
                                f"a record states it within {container!r}, "
                                f"which names an existing place"
                            ),
                        )
                    )
    return questions


def build_worklist(
    places: list[PlaceRecord],
    competing: list[Question],
    stated: list[Question],
) -> list[Question]:
    """Deduplicated, spine-first: a question whose places the graph puts
    more places inside comes first (larger scale), then one whose places
    it puts in fewer."""
    ancestors = _ancestors(places)
    inside: dict[str, int] = dict.fromkeys(ancestors, 0)
    for found in ancestors.values():
        for qid in found:
            inside[qid] = inside.get(qid, 0) + 1
    seen: set[frozenset] = set()
    worklist: list[Question] = []
    for question in [*stated, *competing]:
        key = frozenset((question.qid_a, question.qid_b))
        if key in seen:
            continue
        seen.add(key)
        worklist.append(question)
    worklist.sort(
        key=lambda q: (
            -max(inside.get(q.qid_a, 0), inside.get(q.qid_b, 0)),
            min(
                len(ancestors.get(q.qid_a, ())),
                len(ancestors.get(q.qid_b, ())),
            ),
        )
    )
    return worklist


ADJUDICATION_PROMPT = """\
You adjudicate PLACE relationships for a family archive. For each
question below, decide how the two places relate. Weigh the full
hierarchical labels; places recur under different naming conventions
and at different resolutions (a building, a street, a town, a county).

Verdicts:
  "same"        the two names denote the SAME place
  "a_within_b"  A is contained in B (a building in a town, a town in a
                county/country)
  "b_within_a"  B is contained in A
  "distinct"    different places, neither contains the other
  "uncertain"   cannot tell from the names and evidence

For every question output one NDJSON line:
{{"qid_a": "<Q..>", "qid_b": "<Q..>", "verdict": "...",
"confidence": <0-1>, "evidence": "<one sentence>"}}
No wrapper object, no code fences, no prose. End with exactly:
{{"done": true}}

Questions:
{questions_block}
"""


@dataclass
class PlaceProposal:
    qid_a: str
    qid_b: str
    verdict: str
    confidence: float | None = None
    evidence: str = ""


def adjudicate_places(
    client: LlmClient,
    questions: list[Question],
    places: list[PlaceRecord],
    batch_pairs: int = BATCH_PAIRS,
) -> list[PlaceProposal]:
    labels = {place.qid: place.label for place in places}
    proposals: list[PlaceProposal] = []
    batches = [
        questions[start : start + batch_pairs]
        for start in range(0, len(questions), batch_pairs)
    ]
    print(
        f"adjudicating {len(questions)} place questions in "
        f"{len(batches)} batch(es) of <= {batch_pairs}",
        file=sys.stderr,
    )
    for index, chunk in enumerate(batches, start=1):
        block = "\n".join(
            f"- A: {question.qid_a} {labels.get(question.qid_a, '?')!r}"
            f"  vs  B: {question.qid_b} {labels.get(question.qid_b, '?')!r}"
            f"  ({question.context})"
            for question in chunk
        )
        response = client.complete(
            ADJUDICATION_PROMPT.format(questions_block=block)
        )
        valid = {
            frozenset((question.qid_a, question.qid_b)) for question in chunk
        }
        parsed = _parse_place_proposals(
            response, valid, cut_off=at_output_limit(client, response)
        )
        print(
            f"  batch {index}/{len(batches)}: {len(chunk)} questions -> "
            f"{len(parsed)} proposals (sentinel ok)",
            file=sys.stderr,
        )
        proposals.extend(parsed)
    return proposals


def _parse_place_proposals(
    response: str, valid: set[frozenset], *, cut_off: bool = False
) -> list[PlaceProposal]:
    """The proposals of a place adjudication answer on the questions it
    was asked. Raises ValueError for an answer cut off: one the engine
    stopped at its output limit (`cut_off`, llm.at_output_limit), or one
    without its terminal sentinel, a line whose `done` is true."""
    proposals = []
    finished = False
    for obj in scan_json_objects(response):
        if obj.get("done") is True:
            finished = True
            continue
        if "qid_a" not in obj or "qid_b" not in obj:
            continue
        verdict = obj.get("verdict", "")
        if verdict not in VERDICTS:
            continue
        if frozenset((obj["qid_a"], obj["qid_b"])) not in valid:
            continue  # the adjudicator may only rule on asked questions
        proposals.append(
            PlaceProposal(
                qid_a=obj["qid_a"],
                qid_b=obj["qid_b"],
                verdict=verdict,
                confidence=obj.get("confidence"),
                evidence=obj.get("evidence", ""),
            )
        )
    if cut_off:
        raise ValueError(f"place adjudication response {CUT_OFF_AT_LIMIT}")
    if not finished:
        raise ValueError(
            "place adjudication response carried no terminal sentinel"
        )
    return proposals


def export_places_csv(
    proposals: list[PlaceProposal],
    questions: list[Question],
    places: list[PlaceRecord],
    path: Path,
) -> int:
    labels = {place.qid: place.label for place in places}
    contexts = {
        frozenset((question.qid_a, question.qid_b)): question.context
        for question in questions
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for proposal in proposals:
            writer.writerow(
                {
                    "qid_a": proposal.qid_a,
                    "label_a": labels.get(proposal.qid_a, ""),
                    "qid_b": proposal.qid_b,
                    "label_b": labels.get(proposal.qid_b, ""),
                    "question": contexts.get(
                        frozenset((proposal.qid_a, proposal.qid_b)), ""
                    ),
                    "verdict": proposal.verdict,
                    "confidence": proposal.confidence,
                    "evidence": proposal.evidence,
                    "decision": "",
                }
            )
    return len(proposals)


def plan_containment(
    rows: list[dict], creates_cycle
) -> tuple[list[tuple[str, str]], list[str]]:
    """Approved rows -> (child, parent) edges, with the cycle guard.

    `creates_cycle(child, parent)` answers "is the parent already
    (transitively) contained in the child" against the live closure;
    batch-internal edges are checked here. `same` and other
    non-containment verdicts are skipped with a note — identity stays
    curator-executed (ADR-0007).
    """
    edges: list[tuple[str, str]] = []
    notes: list[str] = []
    planned_parents: dict[str, set[str]] = {}

    def reaches(node: str, target: str, seen=None) -> bool:
        """Walking UP the planned edges from `node`, do we hit `target`?"""
        seen = seen or set()
        if node in seen:
            return False
        seen.add(node)
        for parent in planned_parents.get(node, set()):
            if parent == target or reaches(parent, target, seen):
                return True
        return False

    for row in rows:
        if row.get("decision", "").strip().lower() != "approve":
            continue
        verdict = row.get("verdict", "")
        if verdict == "same":
            notes.append(
                f"{row['qid_a']} = {row['qid_b']}: identity is "
                f"curator-executed (yizkr merge-items, or a referenced "
                f"'said to be the same as') — not applied here"
            )
            continue
        if verdict not in ("a_within_b", "b_within_a"):
            notes.append(
                f"{row['qid_a']} vs {row['qid_b']}: verdict {verdict!r} "
                "is not containment — nothing to apply"
            )
            continue
        child, parent = (
            (row["qid_a"], row["qid_b"])
            if verdict == "a_within_b"
            else (row["qid_b"], row["qid_a"])
        )
        if child == parent:
            notes.append(f"{child}: cannot contain itself — skipped")
            continue
        # cyclic if the parent already sits inside the child (live
        # closure), or would after this batch's earlier edges
        if creates_cycle(child, parent) or reaches(parent, child):
            notes.append(
                f"applying {child} ⊂ {parent} would create a cycle — "
                f"skipped, review the conflicting edge"
            )
            continue
        edges.append((child, parent))
        planned_parents.setdefault(child, set()).add(parent)
    return edges, notes


def ensure_places_run_item(
    client: WikiClient, fingerprint: str, model: str
) -> str:
    """The pass's run item, found by its label or made. Its ``instance of
    -> extraction run`` cites the item itself, so it is written in a
    second write, and a run item that a pass cut short between the two
    writes left without it gets it when the next pass finds it
    (registrar.ensure_self_typed). Raises ValueError, naming the
    characters, before anything is read when the label holds a character
    Python's Unicode tables do not know (writer.unknown_characters): the
    instance may keep it in another form, and the next pass would not
    find the item by it and would make another."""
    from yizkr.pipeline.registrar import ensure_self_typed
    from yizkr.pipeline.writer import _unknown_fault, unknown_characters

    label = f"place-hierarchy run {model} {fingerprint}"
    unknown = unknown_characters(label)
    if unknown:
        raise ValueError(
            f"the place-hierarchy run's label {_unknown_fault(unknown)}: "
            f"give the model identifier without it. Nothing was written"
        )
    qid = client.find_item_by_label(label)
    if qid is None:
        item = client.wbi.item.new()
        item.labels.set("en", label)
        item.descriptions.set(
            "en",
            "reconcile-places pass (ADR-0009): human-approved containment "
            "between place items",
        )
        qid = item.write(summary="reconcile-places: run item").id
    ensure_self_typed(
        client,
        qid,
        "extraction_run",
        summary="reconcile-places: run item statements",
    )
    return qid


def apply_containment(
    client: WikiClient, config: Config, csv_path: Path, model: str
) -> dict:
    """Write approved containment edges as referenced part_of claims."""
    from yizkr.pipeline.registrar import (
        _append,
        _existing_claim_targets,
        _item_claim,
    )

    properties, _ = client.vocabulary()

    def creates_cycle(child: str, parent: str) -> bool:
        # cyclic iff the parent is already (transitively) inside the child
        query = prefix_block(config.concept_uri) + (
            f"ASK {{ wd:{parent} (wdt:{properties['location']}|"
            f"wdt:{properties['part_of']})+ wd:{child} }}"
        )
        url = (
            config.sparql_url
            + "?"
            + urllib.parse.urlencode({"query": query, "format": "json"})
        )
        with urllib.request.urlopen(url, timeout=60) as response:
            return bool(json.load(response).get("boolean"))

    with open(csv_path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    edges, notes = plan_containment(rows, creates_cycle)
    written = skipped = 0
    run_qid = None
    fingerprint = hashlib.sha256(csv_path.read_bytes()).hexdigest()[:10]
    for child, parent in edges:
        item = client.wbi.item.get(child)
        if parent in _existing_claim_targets(item, properties["part_of"]):
            skipped += 1
            continue
        if run_qid is None:
            run_qid = ensure_places_run_item(client, fingerprint, model)
        claim = _item_claim(
            properties["part_of"],
            parent,
            properties["inferred_from_extraction_run"],
            run_qid,
        )
        _append(item, claim)
        item.write(summary="reconcile-places: approved containment")
        written += 1
    return {
        "written": written,
        "skipped": skipped,
        "notes": notes,
        "run_qid": run_qid,
    }
