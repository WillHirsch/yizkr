"""Semantically chunked claim extraction (ADR-0006).

Dense documents overflow a single model response — the constraint is
OUTPUT size (claim JSON amplifies source text ~16x), never input size.
So: one segmentation call maps the transcription to semantic units;
units greedy-pack into chunks; each chunk call keeps the full document
in context (by forking the segmentation conversation where the engine
supports it) but emits claims for its assigned units only; a
deterministic merge namespaces local ids and leaves same-label
unification to the existing label-keyed resolve/write machinery.

Per-chunk state under <out>.chunks/ makes every stage inspectable and
resumable: a failed chunk costs seconds, not the document, and a parser
fix re-parses persisted raw responses without re-invoking the model.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from yizkr.claims import ClaimBatch, EntityRecord, load_vocabulary
from yizkr.pipeline.extract import (
    run_metadata,
    salvage_valid_claims,
    scan_json_objects,
    vocabulary_prompt_lines,
)
from yizkr.pipeline.llm import ConversationalLlmClient, LlmClient
from yizkr.pipeline.transcribe import Transcription

# Transcriptions above this size use chunked extraction under
# --strategy auto. Conservative on purpose: density (the real driver)
# is unknowable upfront, and chunking a document that would have fit
# costs a few extra calls, never correctness.
AUTO_CHUNK_THRESHOLD = 4_000

# Combined assigned-unit text per chunk. Bounds what a chunk call must
# EMIT claims for (and so its expected output), not what it sees.
DEFAULT_CHUNK_BUDGET = 2_000

# Above this size the transcription is not carried inline per call
# (context mode 3, compressed): long-context attention degradation and
# cost outweigh the benefit.
INLINE_DOCUMENT_LIMIT = 100_000


class SegmentationUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    kind: str = "section"
    first_line: int = Field(ge=1)
    last_line: int = Field(ge=1)
    context: str = ""
    extract: bool = True


class DocumentSegmentation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    units: list[SegmentationUnit]

    def problems(self, line_count: int) -> list[str]:
        """Structural violations; empty means usable."""
        errors: list[str] = []
        if not self.units:
            errors.append("no units")
        seen_ids: set[str] = set()
        for unit in self.units:
            if unit.id in seen_ids:
                errors.append(f"duplicate unit id {unit.id!r}")
            seen_ids.add(unit.id)
            if unit.last_line < unit.first_line:
                errors.append(f"unit {unit.id}: last_line < first_line")
            if unit.last_line > line_count:
                errors.append(
                    f"unit {unit.id}: last_line {unit.last_line} beyond "
                    f"document ({line_count} lines)"
                )
        ordered = sorted(self.units, key=lambda u: u.first_line)
        for before, after in zip(ordered, ordered[1:], strict=False):
            if after.first_line <= before.last_line:
                errors.append(f"units {before.id} and {after.id} overlap")
        return errors

    def unsegmented_lines(self, line_count: int) -> list[tuple[int, int]]:
        """Line ranges covered by no unit (gaps are allowed but audited)."""
        gaps: list[tuple[int, int]] = []
        cursor = 1
        for unit in sorted(self.units, key=lambda u: u.first_line):
            if unit.first_line > cursor:
                gaps.append((cursor, unit.first_line - 1))
            cursor = max(cursor, unit.last_line + 1)
        if cursor <= line_count:
            gaps.append((cursor, line_count))
        return gaps


def number_lines(text: str) -> str:
    return "\n".join(
        f"{number}: {line}" for number, line in enumerate(text.splitlines(), start=1)
    )


def unit_text(text_lines: list[str], unit: SegmentationUnit) -> str:
    return "\n".join(text_lines[unit.first_line - 1 : unit.last_line])


SEGMENTATION_PROMPT = """\
You segment a document transcription into semantic units for
genealogical claim extraction. A unit is a self-contained region a
person could extract claims from in isolation: one person's entry in a
family tree, one narrative section, one table. Prefer natural units
over fixed sizes.

Output ONE JSON object, no prose, no code fences:
{{"units": [{{"id": "u01", "label": "<short name>", "kind": "<person entry|narrative section|...>",
"first_line": <int>, "last_line": <int>, "context": "<what a reader needs from OUTSIDE the unit,
e.g. the ancestor path in a tree: 'child of X and Y (generation 2)'>",
"extract": <true, or false for regions asserting no facts about people, places or events
(logistics, boilerplate, tables of contents)>}}, ...]}}

Rules:
- Units must not overlap. Cover every line that asserts extractable
  facts; lines you skip are audited as gaps.
- Mark non-assertive regions extract=false rather than skipping them.
- Line numbers refer to the numbering below (1-based, inclusive).

Transcription of artifact {artifact_qid} (language: {language}):

{numbered_text}
"""


CHUNK_PROMPT = """\
You extract source-attributed genealogical claims from transcriptions of
historical documents.

Rules — these mirror the archive's non-negotiable conventions:
- Record exactly what the source asserts. Never round dates, never guess,
  never narrow a vague statement. Use precision (year/month/day/decade/
  century), earliest_date/latest_date qualifiers for ranges, and the
  sourcing_circumstances qualifier with the "circa" vocabulary item for
  approximate values. Never silently disambiguate an ambiguous date
  format — downgrade to the precision actually supported.
- Every claim MUST include the source reference: artifact QID {artifact_qid},
  plus page number and a short verbatim transcription excerpt supporting it.
- Use ONLY these property keys (property key: datatype), for claims and
  qualifiers alike — a claim naming any other key is discarded. If the
  document asserts something no listed property expresses, skip it:
{property_keys}
- Vocabulary item keys available for "vocab" references:
{item_keys}
- A person's ordinary name lives in their entity-manifest label and
  aliases; do NOT restate it as a claim. Use the typed name properties
  only when the source marks a name's TYPE: birth_name (a name/maiden
  name given at birth), married_name (taken on marriage), nickname (an
  informal/pet name or "aka"), official_name (a formal or legal change).
- Kinship: use ONLY the primitives father, mother, spouse, sibling,
  child. Do NOT invent properties for compound relations (aunt, cousin,
  grandmother, brother-in-law) — those are derived from the primitives.
  If a source states a compound relation you cannot reduce to a
  primitive (e.g. "maternal aunt" with the connecting parent unknown),
  use `relative` with a `kinship_type` qualifier holding the source's
  exact words. Record `sex_or_gender` (male/female vocab) when a source
  gives it.
- Residence: an address stated on a form is asserted for the person that
  field names (e.g. the informant/applicant on a certificate), NOT for
  everyone the document mentions. Do not copy one person's stated address
  onto others as their own `residence` — co-residence ("lived with") is
  derived later from separately-sourced residences over overlapping time,
  never asserted here. If you do record a dependent's residence from a
  relative's address (e.g. a newborn at the parents' address), lower its
  confidence to mark the inference.
- Set confidence (0-1) per claim: how certain you are the source asserts it.

{assignment_block}

Output NDJSON: one complete claim JSON object per line — no wrapper
object, no code fences, no prose. Each claim matches the "Claim"
definition of this JSON Schema:

{schema}

If the source asserts a RELATION that NO listed property expresses (e.g.
"deported to", "imprisoned at", "employed by" when no such property is
listed), do NOT force it into a wrong property and do NOT silently drop
it: emit a GAP line instead —
{{"gap": {{"subject": "<local_id or name>", "predicate": "<the relation, verbatim>",
"object": "<local_id, name, or literal>", "evidence": "<short excerpt>"}}}}
Gaps are reviewed to grow the vocabulary; they are never invented and
never used when a real property fits.

After the last line, end with exactly one final line:
{{"done": true, "claims_emitted": N, "gaps_emitted": M}}
where N and M count the claim and gap lines you emitted.
{document_block}"""


LINE_ASSIGNMENT_BLOCK = """\
Refer to people, places, organizations, events, and occupations with
"local" references (stable local_id per entity within this response,
label as written — prefer the fullest name the document gives for a
person anywhere, instance_of from: human, place, organization, event,
birth, death, marriage, migration, military_service, occupation_class).
An organization (company, committee, agency) is an agent, never a place.
An occupation value keeps the source's exact wording as its label.

Extract claims ONLY for the assigned units below — nothing from other
parts of the document, even though you can see them. Use the rest of
the document only as context (identity, relationships, spellings).
Ownership rule: every claim whose SUBJECT is a person/place/event of an
assigned unit belongs to you, including relations to entities outside
the unit — a unit person's father/mother from the enclosing tree
structure or the unit's context line, their spouse, their instance_of.
Do not skip a claim because its object or evidence lies partly outside
your units.

Assigned units:
{units_block}"""


ENTITY_ASSIGNMENT_BLOCK = """\
Refer to EVERY entity — subjects and objects alike — with a "local"
reference whose local_id is the entity's manifest id and whose label is
the manifest's canonical label. Ids outside the manifest are rejected.
The full manifest (id = canonical label):
{roster_block}

Emit every claim whose SUBJECT is one of your ASSIGNED entities below —
and only those. Relations to entities outside your assignment (their
parents, spouses, locations) still belong to you when your entity is
the subject; refer to the other entity by its manifest id. Do not emit
claims whose subject is not assigned to you, and do not emit
instance_of claims (the manifest's kind already records it).

Assigned entities:
{entities_block}"""


ENTITY_MANIFEST_PROMPT = """\
You are preparing a genealogical claim extraction. First pass: read the
whole transcription and produce a manifest of EVERY distinct entity it
mentions — people, places, organizations (a company, bank, committee,
government body, school, or congregation — anything the source treats as
an AGENT that employs, helps, hires, or is registered/taxed; kind
organization, NOT place — an organization has a location, it is not
one), events (births, deaths, marriages, migrations, military service),
and occupations or professions it names for people (kind
occupation_class, label in the source's exact wording). A hospital,
clinic, school, or congregation is an `organization` even when the
source names it only as WHERE an event happened (a place of birth or
death): an organization HAS a location, it is not one. Also: a named
family or household ("the Wulffs") is a `family`; a ship is a `vessel`;
a cited law, decree, gazette, or register is a `legal_instrument`; a
regiment or brigade is a `military_unit`. A forced removal is a
`deportation` event, a detention an `imprisonment` event, a freeing a
`release` event (each an event with a person participant, a place, and
a date) — not a place or a bare relation. Unify aliases:
one entity that appears
as "Rosa Genserowski", "Rosa", and "our great grandmother" is ONE
manifest entry whose label is the fullest form the document gives and
whose aliases list every other form, verbatim. Aliases are ways the
document REFERS TO the entity — in form-like documents (certificates,
registers), field and section labels such as MOTHER, FATHER, CHILD,
INFORMANT, BRIDE name a ROLE, not the person: never record them as
aliases.

Output ONE JSON object, no prose, no code fences:
{{"entities": [{{"id": "e01", "label": "<canonical, fullest form>",
"aliases": ["<other forms, verbatim>", ...], "kind": "<one of: {kinds}>",
"summary": "<one line: who/what this is, with key dates/relations>",
"mention_lines": [<line numbers where it is mentioned>],
"type_hint": "<only when kind is unclassified_entity — see below>",
"extract": <true, or false — see below>}}, ...]}}

Rules:
- One entry per real-world entity, however many aliases it has.
- ids must be unique. Do NOT include a "qid" field.
- NEVER force an entity into a wrong-but-adjacent kind. A company,
  bank, committee, or agency is an `organization`, NOT a place and NOT a
  person. If an entity clearly exists but NO listed kind fits it (a
  ship, a regiment, a building that is not a settlement), use kind
  `unclassified_entity` and set `type_hint` to what it actually is
  ("ship", "military regiment"). This is a LAST resort — use a real
  kind whenever one fits; `unclassified_entity` without a type_hint is
  invalid.
- Prefer completeness: an entity mentioned once still gets an entry.
- extract=false for entities that appear ONLY in non-assertive context:
  travel/event logistics, room or seating assignments, contemporary
  trip participants, boilerplate. They are recorded for audit but no
  claims are extracted about them. Entities the document asserts
  historical/genealogical facts about get extract=true.
- On official certificates and registers the administrative apparatus is
  boilerplate: the registrar, officiant, or witness who certifies the
  record; the issuing authority (e.g. a General Register Office); and the
  register or certificate the document cites as its own source are all
  extract=false. They attest the record — they are not its genealogical
  subject. (A witness or officiant who is ALSO a relative of the subject
  is the exception: extract=true for the relationship.)
- summary is evidence for later identity matching: pack it with the
  document's key facts (born/died/married when-where, parents, spouse).

Transcription of artifact {artifact_qid} (language: {language}):

{numbered_text}
"""


class ChunkIncomplete(Exception):
    """Chunk response unusable: missing/mismatched sentinel, no claims."""


def parse_chunk_response(response: str) -> list[dict]:
    """NDJSON claims + required sentinel; raises ChunkIncomplete.

    The sentinel converts silent truncation into a detected, retryable
    failure: chunk retries are cheap, unlike whole-document retries.
    """
    claims: list[dict] = []
    sentinel: int | None = None
    for obj in scan_json_objects(response):
        if obj.get("done") is True:
            sentinel = obj.get("claims_emitted")
        elif "subject" in obj and "property" in obj:
            claims.append(obj)
    if sentinel is None:
        raise ChunkIncomplete("no terminal sentinel — response truncated?")
    if sentinel != len(claims):
        raise ChunkIncomplete(f"sentinel says {sentinel} claims, parsed {len(claims)}")
    return claims


def parse_chunk_gaps(response: str) -> list[dict]:
    """Gap lines (ADR-0013): relations no property expresses, surfaced
    for review, never written to the graph.

    A gap line is ``{"gap": {"subject", "predicate", "object",
    "evidence"?}}``. Malformed gaps are dropped silently — a gap is an
    advisory signal, never a gate, so it must never fail a chunk (that is
    the sentinel's job, and gaps are counted separately in it).
    """
    gaps: list[dict] = []
    for obj in scan_json_objects(response):
        gap = obj.get("gap")
        if isinstance(gap, dict) and gap.get("subject") and gap.get("predicate"):
            gaps.append(
                {
                    "subject": str(gap.get("subject", "")),
                    "predicate": str(gap.get("predicate", "")),
                    "object": str(gap.get("object", "")),
                    "evidence": str(gap.get("evidence", "")),
                }
            )
    return gaps


def pack_units(
    units: list[SegmentationUnit],
    text_lines: list[str],
    budget: int = DEFAULT_CHUNK_BUDGET,
) -> list[list[SegmentationUnit]]:
    """Greedy-pack extractable units, in document order, under budget.

    A single unit over budget becomes its own chunk (the multi-turn
    reassembly nets remain as backstop for that case).
    """
    chunks: list[list[SegmentationUnit]] = []
    current: list[SegmentationUnit] = []
    current_size = 0
    for unit in sorted(units, key=lambda u: u.first_line):
        if not unit.extract:
            continue
        size = len(unit_text(text_lines, unit))
        if current and current_size + size > budget:
            chunks.append(current)
            current, current_size = [], 0
        current.append(unit)
        current_size += size
    if current:
        chunks.append(current)
    return chunks


def _rewrite_local_refs(claim: dict, fix) -> dict:
    """Deep-copy a claim dict and apply `fix` to every local ref in it."""
    fixed = json.loads(json.dumps(claim))  # deep copy, JSON-native

    def visit(ref: Any) -> None:
        if isinstance(ref, dict) and ref.get("kind") == "local" and "local_id" in ref:
            fix(ref)

    visit(fixed.get("subject"))
    value = fixed.get("value")
    if isinstance(value, dict):
        visit(value.get("value"))
    for qualifier in fixed.get("qualifiers") or []:
        if isinstance(qualifier, dict) and isinstance(qualifier.get("value"), dict):
            visit(qualifier["value"].get("value"))
    source = fixed.get("source")
    if isinstance(source, dict):
        visit(source.get("artifact"))
        visit(source.get("fragment"))
    return fixed


def pack_entities(
    entities: list[EntityRecord],
    text_lines: list[str],
    budget: int = DEFAULT_CHUNK_BUDGET,
) -> list[list[EntityRecord]]:
    """Greedy-pack entities, in manifest order, under the output budget.

    An entity's size is its evidence volume (total text of its mention
    lines) when mention_lines is usable, else a flat estimate — the
    budget bounds what a chunk must emit claims about, not what it
    sees.
    """
    flat_estimate = max(1, budget // 10)
    chunks: list[list[EntityRecord]] = []
    current: list[EntityRecord] = []
    current_size = 0
    for entity in entities:
        if not entity.extract:
            continue
        lines = [
            number for number in entity.mention_lines if 1 <= number <= len(text_lines)
        ]
        size = (
            sum(len(text_lines[number - 1]) for number in lines)
            if lines
            else flat_estimate
        )
        if current and current_size + size > budget:
            chunks.append(current)
            current, current_size = [], 0
        current.append(entity)
        current_size += size
    if current:
        chunks.append(current)
    return chunks


def normalize_to_manifest(claim: dict, records: dict[str, EntityRecord]) -> dict:
    """Normalize local refs to the manifest: canonical label, kind.

    Labels identify entities, so they take the manifest's canonical
    form; transcription excerpts stay verbatim — they quote the source.
    Unknown ids are left untouched for validation to reject.
    """

    def fix(ref: dict) -> None:
        record = records.get(ref.get("local_id"))
        if record is not None:
            ref["label"] = record.label
            ref["instance_of"] = record.kind

    return _rewrite_local_refs(claim, fix)


def prefix_local_ids(claim: dict, prefix: str) -> dict:
    """Chunk-namespace every local id so ids never collide across
    chunks (line-range fallback path only — entity-first ids are
    document-scoped manifest ids). Same-label unification stays with
    the label-keyed resolve/write machinery — identity is not decided
    here."""
    return _rewrite_local_refs(
        claim, lambda ref: ref.update(local_id=f"{prefix}:{ref['local_id']}")
    )


class ChunkedClaimExtractor:
    """Segmentation -> packed chunks -> per-chunk calls -> merge.

    Matches LlmClaimExtractor's extract() signature so the CLI treats
    the strategies interchangeably. After extract(), `failed_chunks`
    lists chunks that produced nothing despite retries — the batch is
    still returned (partial progress is the point), and the caller
    decides how loudly to fail.
    """

    def __init__(
        self,
        client: LlmClient,
        state_dir: Path,
        budget: int = DEFAULT_CHUNK_BUDGET,
        retries: int = 2,
        resume: bool = False,
        concurrency: int = 1,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.state_dir = state_dir
        self.budget = budget
        self.retries = retries
        self.resume = resume
        # chunks are independent by design (ADR-0006); >1 runs their
        # engine calls in parallel threads. Forks of one session are
        # safe to run concurrently: each fork branches into its own
        # new session without mutating the original.
        self.concurrency = max(1, concurrency)
        # progress feedback (chunked extraction is otherwise silent for
        # minutes on a large document); a no-op when no logger is given.
        self._log = log if log is not None else (lambda _message: None)
        self._chunks_done = 0
        self._state_lock = threading.Lock()
        self.failed_chunks: list[str] = []
        # open-schema gaps (ADR-0013): relations no property expresses,
        # collected per run for review; never written to the graph
        self.gaps: list[dict] = []
        # entity manifest unusable -> line-range segmentation was used
        self.used_fallback_manifest = False
        # line-range segmentation also unusable -> offset windows
        self.used_fallback_segmentation = False

    # -- state helpers ---------------------------------------------------

    def _write(self, name: str, content: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / name).write_text(content)

    def _read(self, name: str) -> str | None:
        path = self.state_dir / name
        return path.read_text() if path.exists() else None

    def _note_chunk_done(
        self, total: int, *, ok: bool, claims: int = 0, resumed: bool = False
    ) -> None:
        """Emit a running `chunk k/total` progress line as each chunk
        finishes. Called from worker threads; the counter is locked."""
        with self._state_lock:
            self._chunks_done += 1
            done = self._chunks_done
        if not ok:
            tag = "failed"
        else:
            plural = "" if claims == 1 else "s"
            prefix = "resumed, " if resumed else ""
            tag = f"{prefix}{claims} claim{plural}"
        self._log(f"  chunk {done}/{total} ({tag})")

    # -- phase 1a: entity manifest (ADR-0007, the default) ----------------

    def _entity_manifest(
        self, transcription: Transcription, artifact_qid: str
    ) -> tuple[list[EntityRecord] | None, Any]:
        """Returns (entities, conversation handle) or (None, None).

        (None, None) means the manifest was unusable after a retry; the
        caller falls back to line-range segmentation. As with
        segmentation fallback, no handle survives a rejected manifest —
        forking would plant known-bad output in every chunk's context.
        """
        line_count = len(transcription.text.splitlines())
        prompt = ENTITY_MANIFEST_PROMPT.format(
            kinds="human, place, organization, family, vessel, "
            "legal_instrument, military_unit, event, birth, death, "
            "marriage, migration, military_service, deportation, "
            "imprisonment, release, occupation_class, unclassified_entity",
            artifact_qid=artifact_qid,
            language=transcription.language,
            numbered_text=number_lines(transcription.text),
        )
        handle = None
        last_error = "engine has no conversation support"
        for _attempt in range(2):
            try:
                if isinstance(self.client, ConversationalLlmClient):
                    response, handle = self.client.open_conversation(prompt)
                else:
                    response = self.client.complete(prompt)
            except Exception as exc:
                last_error, handle = str(exc), None
                continue
            entities, problems = self._parse_entity_manifest(response, line_count)
            if entities is not None and not problems:
                return entities, handle
            last_error, handle = "; ".join(problems or ["unparseable"]), None
        self.used_fallback_manifest = True
        self._write(
            "manifest-fallback.txt",
            f"entity manifest fell back to line-range segmentation: {last_error}\n",
        )
        return None, None

    @staticmethod
    def _parse_entity_manifest(
        response: str, line_count: int
    ) -> tuple[list[EntityRecord] | None, list[str]]:
        _, item_keys = load_vocabulary()
        for obj in scan_json_objects(response):
            if "entities" not in obj:
                continue
            try:
                records = []
                for raw in obj["entities"] or []:
                    data = dict(raw)
                    data.pop("qid", None)  # resolution state, never model-set
                    records.append(EntityRecord.model_validate(data))
            except Exception as exc:
                return None, [str(exc)[:300]]
            problems: list[str] = []
            if not records:
                problems.append("empty entity list")
            seen: set[str] = set()
            for record in records:
                if record.id in seen:
                    problems.append(f"duplicate entity id {record.id!r}")
                seen.add(record.id)
                if record.kind not in item_keys:
                    problems.append(f"entity {record.id}: unknown kind {record.kind!r}")
                # mention lines are best-effort: clamp, don't reject
                record.mention_lines = [
                    number
                    for number in record.mention_lines
                    if 1 <= number <= line_count
                ]
            return records, problems
        return None, ["no JSON object with an 'entities' array"]

    # -- phase 1b: line-range segmentation (fallback tier) -----------------

    def _segment(
        self, transcription: Transcription, artifact_qid: str
    ) -> tuple[DocumentSegmentation, Any]:
        """Returns (segmentation, conversation handle or None).

        A None handle means chunk calls cannot fork (context mode 2/3).
        On segmentation fallback the handle is always None: forking a
        conversation whose segmentation turn was rejected would plant
        known-bad output in every chunk's context.
        """
        line_count = len(transcription.text.splitlines())
        prompt = SEGMENTATION_PROMPT.format(
            artifact_qid=artifact_qid,
            language=transcription.language,
            numbered_text=number_lines(transcription.text),
        )
        handle = None
        last_error = "engine has no conversation support"
        for _attempt in range(2):
            try:
                if isinstance(self.client, ConversationalLlmClient):
                    response, handle = self.client.open_conversation(prompt)
                else:
                    response = self.client.complete(prompt)
            except Exception as exc:
                last_error, handle = str(exc), None
                continue
            segmentation, problems = self._parse_segmentation(response, line_count)
            if segmentation is not None and not problems:
                return segmentation, handle
            last_error, handle = "; ".join(problems or ["unparseable"]), None
        self.used_fallback_segmentation = True
        return self._fallback_segmentation(line_count, last_error), None

    @staticmethod
    def _parse_segmentation(
        response: str, line_count: int
    ) -> tuple[DocumentSegmentation | None, list[str]]:
        for obj in scan_json_objects(response):
            if "units" not in obj:
                continue
            try:
                segmentation = DocumentSegmentation.model_validate(obj)
            except Exception as exc:
                return None, [str(exc)[:300]]
            return segmentation, segmentation.problems(line_count)
        return None, ["no JSON object with a 'units' array"]

    def _fallback_segmentation(
        self, line_count: int, reason: str
    ) -> DocumentSegmentation:
        """Degraded offset chunking: fixed line windows, empty context."""
        window = max(1, self.budget // 40)  # ~40 chars/line heuristic
        units = [
            SegmentationUnit(
                id=f"w{start:04d}",
                label=f"lines {start}-{min(start + window - 1, line_count)}",
                kind="window (segmentation fallback)",
                first_line=start,
                last_line=min(start + window - 1, line_count),
                context="",
            )
            for start in range(1, line_count + 1, window)
        ]
        self._write(
            "segmentation-fallback.txt",
            f"segmentation fell back to line windows: {reason}\n",
        )
        return DocumentSegmentation(units=units)

    # -- chunk calls -----------------------------------------------------

    def _document_block(
        self, transcription: Transcription, artifact_qid: str, inline: bool
    ) -> str:
        if not inline:
            return ""
        return (
            f"\nFull transcription of artifact {artifact_qid} for "
            f"context (extract ONLY for your assignment):\n\n"
            f"{number_lines(transcription.text)}\n"
        )

    def _render_chunk_prompt(
        self,
        assignment_block: str,
        transcription: Transcription,
        artifact_qid: str,
        inline_document: bool,
    ) -> str:
        property_keys, item_keys = vocabulary_prompt_lines()
        return CHUNK_PROMPT.format(
            artifact_qid=artifact_qid,
            property_keys=property_keys,
            item_keys=item_keys,
            assignment_block=assignment_block,
            schema=json.dumps(ClaimBatch.model_json_schema(), indent=2),
            document_block=self._document_block(
                transcription, artifact_qid, inline_document
            ),
        )

    def _chunk_prompt(
        self,
        chunk_units: list[SegmentationUnit],
        text_lines: list[str],
        transcription: Transcription,
        artifact_qid: str,
        inline_document: bool,
    ) -> str:
        units_block = "\n\n".join(
            f"- unit {unit.id} ({unit.label}; lines "
            f"{unit.first_line}-{unit.last_line})"
            + (f"\n  context: {unit.context}" if unit.context else "")
            + f"\n  text:\n{unit_text(text_lines, unit)}"
            for unit in chunk_units
        )
        return self._render_chunk_prompt(
            LINE_ASSIGNMENT_BLOCK.format(units_block=units_block),
            transcription,
            artifact_qid,
            inline_document,
        )

    def _entity_chunk_prompt(
        self,
        chunk_entities: list[EntityRecord],
        all_entities: list[EntityRecord],
        text_lines: list[str],
        transcription: Transcription,
        artifact_qid: str,
        inline_document: bool,
    ) -> str:
        roster_block = "\n".join(
            f"  {entity.id} = {entity.label}" for entity in all_entities
        )

        def mention_text(entity: EntityRecord) -> str:
            lines = [
                f"    {number}: {text_lines[number - 1]}"
                for number in entity.mention_lines
            ]
            return "\n  mentions:\n" + "\n".join(lines) if lines else ""

        entities_block = "\n\n".join(
            f"- {entity.id}: {entity.label}"
            + (f" (aliases: {', '.join(entity.aliases)})" if entity.aliases else "")
            + f"; kind: {entity.kind}"
            + (f"\n  {entity.summary}" if entity.summary else "")
            + mention_text(entity)
            for entity in chunk_entities
        )
        return self._render_chunk_prompt(
            ENTITY_ASSIGNMENT_BLOCK.format(
                roster_block=roster_block, entities_block=entities_block
            ),
            transcription,
            artifact_qid,
            inline_document,
        )

    def _run_chunk(
        self, chunk_id: str, prompt: str, handle: Any, fallback_prompt: str
    ) -> list[dict]:
        """Call (fork if possible), persist raw, parse; retries inside."""
        last_error: Exception = ChunkIncomplete("not attempted")
        for _attempt in range(1 + self.retries):
            try:
                if handle is not None:
                    try:
                        response = self.client.fork(handle, prompt)
                    except Exception:
                        # fork machinery failed (session gone, old CLI):
                        # this chunk drops to inline context
                        handle = None
                        response = self.client.complete(fallback_prompt)
                else:
                    response = self.client.complete(fallback_prompt)
            except Exception as exc:
                last_error = exc
                continue
            self._write(f"{chunk_id}.raw.txt", response)
            try:
                return parse_chunk_response(response)
            except ChunkIncomplete as exc:
                last_error = exc
        raise last_error

    # -- orchestration ---------------------------------------------------

    def extract(self, transcription: Transcription, artifact_qid: str) -> ClaimBatch:
        text_lines = transcription.text.splitlines()
        fingerprint = hashlib.sha256(transcription.text.encode()).hexdigest()

        manifest: dict = {"transcription_sha256": fingerprint, "chunks": {}}
        entities: list[EntityRecord] | None = None
        segmentation: DocumentSegmentation | None = None
        handle: Any = None
        if self.resume:
            previous_raw = self._read("manifest.json")
            previous = json.loads(previous_raw) if previous_raw else None
            if previous and previous.get("transcription_sha256") == fingerprint:
                entity_raw = self._read("entity-manifest.json")
                segmentation_raw = self._read("segmentation.json")
                if entity_raw:
                    entities = [
                        EntityRecord.model_validate(record)
                        for record in json.loads(entity_raw)
                    ]
                    manifest = previous
                elif segmentation_raw:
                    segmentation = DocumentSegmentation.model_validate_json(
                        segmentation_raw
                    )
                    manifest = previous
                # the recorded session may be gone; resumption drops to
                # inline context rather than re-running phase 1
                handle = None
        if entities is None and segmentation is None:
            self._log("  scanning document for entities...")
            entities, handle = self._entity_manifest(transcription, artifact_qid)
            if entities is not None:
                self._write(
                    "entity-manifest.json",
                    json.dumps([record.model_dump() for record in entities], indent=2),
                )
            else:
                segmentation, handle = self._segment(transcription, artifact_qid)
                self._write("segmentation.json", segmentation.model_dump_json(indent=2))

        inline_document = (
            handle is None and len(transcription.text) <= INLINE_DOCUMENT_LIMIT
        )
        manifest["strategy"] = "entity" if entities is not None else "line-range"
        manifest["context_mode"] = (
            "fork"
            if handle is not None
            else "inline"
            if inline_document
            else "compressed"
        )
        if handle is not None and not isinstance(handle, list | dict):
            manifest["session_id"] = str(handle)

        if entities is not None:
            records = {record.id: record for record in entities}
            chunk_groups: list[list] = pack_entities(entities, text_lines, self.budget)

            def build_prompt(group: list, inline: bool) -> str:
                return self._entity_chunk_prompt(
                    group, entities, text_lines, transcription, artifact_qid, inline
                )

            def member_ids(group: list) -> list[str]:
                return [record.id for record in group]

            def transform(claim: dict, chunk_id: str) -> dict:
                return normalize_to_manifest(claim, records)

            entities_payload = [record.model_dump() for record in entities]
        else:
            chunk_groups = pack_units(segmentation.units, text_lines, self.budget)

            def build_prompt(group: list, inline: bool) -> str:
                return self._chunk_prompt(
                    group, text_lines, transcription, artifact_qid, inline
                )

            def member_ids(group: list) -> list[str]:
                return [unit.id for unit in group]

            def transform(claim: dict, chunk_id: str) -> dict:
                return prefix_local_ids(claim, chunk_id)

            entities_payload = []

        self.failed_chunks = []
        chunk_ids = [f"c{index:03d}" for index in range(1, len(chunk_groups) + 1)]
        for chunk_id, group in zip(chunk_ids, chunk_groups, strict=True):
            manifest["chunks"].setdefault(chunk_id, {"members": member_ids(group)})

        total = len(chunk_ids)
        self._chunks_done = 0
        if entities is not None:
            self._log(f"  extract: {len(entities)} entities, {total} chunk(s)")
        else:
            self._log(f"  extract: {total} chunk(s) (line-range fallback)")

        def run_one(chunk_id: str, group: list) -> list[dict] | None:
            """One chunk end to end; returns parsed claims or None on
            failure. Thread-safe: per-chunk files are exclusive to this
            call, shared manifest state goes through the lock."""
            parsed = self._reusable_claims(chunk_id)
            resumed = parsed is not None
            if parsed is None:
                prompt = build_prompt(group, False)
                fallback_prompt = build_prompt(group, inline_document)
                try:
                    parsed = self._run_chunk(chunk_id, prompt, handle, fallback_prompt)
                except Exception as exc:
                    with self._state_lock:
                        manifest["chunks"][chunk_id]["status"] = (
                            f"failed: {str(exc)[:200]}"
                        )
                        self.failed_chunks.append(chunk_id)
                        self._write("manifest.json", json.dumps(manifest, indent=2))
                    self._note_chunk_done(total, ok=False)
                    return None
                self._write(f"{chunk_id}.ndjson", "\n".join(map(json.dumps, parsed)))
            with self._state_lock:
                manifest["chunks"][chunk_id]["status"] = "ok"
                manifest["chunks"][chunk_id]["claims"] = len(parsed)
                self._write("manifest.json", json.dumps(manifest, indent=2))
            self._note_chunk_done(total, ok=True, claims=len(parsed), resumed=resumed)
            return parsed

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            outcomes = list(pool.map(run_one, chunk_ids, chunk_groups))
        self.failed_chunks.sort()

        raw_claims: list[dict] = []
        gaps: list[dict] = []
        for chunk_id, parsed in zip(chunk_ids, outcomes, strict=True):
            if parsed is not None:
                raw_claims.extend(transform(claim, chunk_id) for claim in parsed)
                # gaps come from the persisted raw response, so they cost
                # no extra model call and survive resume (ADR-0013)
                raw = self._read(f"{chunk_id}.raw.txt")
                if raw:
                    gaps.extend(parse_chunk_gaps(raw))
        self.gaps = gaps
        if gaps:
            self._write("gaps.ndjson", "\n".join(map(json.dumps, gaps)))

        deduped: list[dict] = []
        for claim in raw_claims:
            if claim not in deduped:
                deduped.append(claim)

        batch, rejects = salvage_valid_claims(
            deduped,
            run_metadata(self.client.model_identifier).model_dump(mode="json"),
            entities=entities_payload,
        )
        if rejects:
            self._write("rejects.json", json.dumps(rejects, indent=2))
        if entities is not None:
            self._write_entity_coverage(entities, batch, text_lines)
        else:
            self._write_coverage(segmentation, batch, len(text_lines))
        if not batch.claims and (self.failed_chunks or rejects):
            raise ValueError(
                f"chunked extraction produced no valid claims "
                f"({len(self.failed_chunks)} failed chunks, "
                f"{len(rejects)} rejected claims; see {self.state_dir})"
            )
        return batch

    def _reusable_claims(self, chunk_id: str) -> list[dict] | None:
        """On resume: parsed output verbatim, else re-parse persisted raw
        (a parser fix must never cost a second model run)."""
        if not self.resume:
            return None
        parsed = self._read(f"{chunk_id}.ndjson")
        if parsed is not None:
            return [json.loads(line) for line in parsed.splitlines() if line.strip()]
        raw = self._read(f"{chunk_id}.raw.txt")
        if raw is not None:
            try:
                claims = parse_chunk_response(raw)
            except ChunkIncomplete:
                return None
            self._write(f"{chunk_id}.ndjson", "\n".join(map(json.dumps, claims)))
            return claims
        return None

    def _write_entity_coverage(
        self,
        entities: list[EntityRecord],
        batch: ClaimBatch,
        text_lines: list[str],
    ) -> None:
        """Advisory completeness report, entity flavour (ADR-0007).

        Replaces the line-audit: flags manifest entities with zero
        claims, and lines that carry name-shaped tokens matching no
        manifest label/alias (a possibly-missed entity)."""
        subject_ids = {
            claim.subject.local_id
            for claim in batch.claims
            if hasattr(claim.subject, "local_id")
        }
        name_tokens = {
            token.lower()
            for entity in entities
            for name in [entity.label, *entity.aliases]
            for token in name.split()
            if len(token) >= 3
        }
        unmatched_lines = []
        for number, line in enumerate(text_lines, start=1):
            capitalized = [
                token.strip(".,;:()")
                for token in line.split()
                if token[:1].isupper() and len(token.strip(".,;:()")) >= 3
            ]
            if capitalized and not any(
                token.lower() in name_tokens for token in capitalized
            ):
                unmatched_lines.append(number)
        self._write(
            "coverage.json",
            json.dumps(
                {
                    "strategy": "entity",
                    "entities_total": len(entities),
                    "entities_not_extracted": [
                        entity.id for entity in entities if not entity.extract
                    ],
                    "entities_without_claims": [
                        entity.id
                        for entity in entities
                        if entity.extract and entity.id not in subject_ids
                    ],
                    "lines_matching_no_entity": unmatched_lines,
                    "distinct_subject_ids": sorted(subject_ids),
                    "failed_chunks": self.failed_chunks,
                },
                indent=2,
            ),
        )

    def _write_coverage(
        self, segmentation: DocumentSegmentation, batch: ClaimBatch, line_count: int
    ) -> None:
        """Advisory completeness report for the curator (no auto-retry)."""
        subject_labels = {
            claim.subject.label
            for claim in batch.claims
            if hasattr(claim.subject, "label")
        }
        # heuristic: a unit whose label names no claim subject probably
        # yielded nothing — advisory, so false positives are acceptable
        silent_units = [
            unit.id
            for unit in segmentation.units
            if unit.extract and not any(label in unit.label for label in subject_labels)
        ]
        self._write(
            "coverage.json",
            json.dumps(
                {
                    "units_total": len(segmentation.units),
                    "units_extractable": sum(
                        1 for unit in segmentation.units if unit.extract
                    ),
                    "units_without_matching_subject": silent_units,
                    "unsegmented_line_ranges": segmentation.unsegmented_lines(
                        line_count
                    ),
                    "distinct_subject_labels": sorted(subject_labels),
                    "failed_chunks": self.failed_chunks,
                },
                indent=2,
            ),
        )
