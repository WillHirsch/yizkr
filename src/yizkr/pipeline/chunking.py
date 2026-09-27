"""Semantically chunked claim extraction (ADR-0006).

Dense documents overflow a single model response — the constraint is
OUTPUT size (claim JSON amplifies source text ~16x), never input size.
So: one segmentation call maps the transcription to semantic units;
units greedy-pack into chunks; each chunk call keeps the full document
in context (by forking the segmentation conversation where the engine
supports it) but emits claims for its assigned units only; a
deterministic merge namespaces local ids and leaves same-label
unification to the existing label-keyed resolve/write machinery.

The entity manifest, every chunk and the merged batch are stages in the
stage store (yizkr.pipeline.stages), each stored with the raw response of
every call beside its parsed output. So every stage is inspectable and
resumable: a failed chunk costs seconds, not the document, and a parser
fix re-parses stored raw responses without re-invoking the model.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from yizkr.claims import ClaimBatch, EntityRecord, load_vocabulary
from yizkr.pipeline.extract import (
    entity_kind_prompt_lines,
    run_metadata,
    salvage_valid_claims,
    scan_json_objects,
    vocabulary_prompt_lines,
)
from yizkr.pipeline.llm import ConversationalLlmClient, LlmClient
from yizkr.pipeline.stages import (
    DispatchStopped,
    StageFailed,
    StageKey,
    StageRecord,
    StageResult,
    StageScope,
    StageStore,
    content_hash,
    doc_scope,
    manifest_hash,
    rebind_artifact,
    stage_key,
)
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
        f"{number}: {line}"
        for number, line in enumerate(text.splitlines(), start=1)
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
{{"units": [{{"id": "u01", "label": "<short name>",
"kind": "<person entry|narrative section|...>",
"first_line": <int>, "last_line": <int>,
"context": "<what a reader needs from OUTSIDE the unit,
e.g. the ancestor path in a tree: 'child of X and Y (generation 2)'>",
"extract": <true, or false for regions asserting no facts about
people, places or events
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
- Use ONLY these property keys (property key: datatype — what it means),
  for claims and qualifiers alike — a claim naming any other key is
  discarded. The description is authoritative: it says what the VALUE
  must be, and so which way a relation points. If the document asserts
  something no listed property expresses, skip it:
{property_keys}
- Vocabulary item keys available for "vocab" references:
{item_keys}
- A person's ordinary name lives in their entity-manifest label and
  aliases; do NOT restate it as a claim. Use the typed name properties
  only when the source marks a name's TYPE: birth_name (a name/maiden
  name given at birth), married_name (taken on marriage), nickname (an
  informal/pet name or "aka"), official_name (a formal or legal change).
  The marker may be CONTEXTUAL (a née/geb. maiden cue, a stated legal or
  immigration change), not only an explicit tag. Never infer a name's kind
  (given vs family) or type from word order or position — type only what a
  source marks (ADR-0022).
- Kinship: use ONLY the primitives father, mother, spouse, sibling,
  child. Do NOT invent properties for compound relations (aunt, cousin,
  grandmother, brother-in-law) — those are derived from the primitives.
  If a source states a compound relation you cannot reduce to a
  primitive (e.g. "maternal aunt" with the connecting parent unknown),
  use `relative` with a `kinship_type` qualifier holding the source's
  exact words. Record `sex_or_gender` (male/female vocab) when a source
  gives it.
- Relational / perspectival appellations (ADR-0018): a reference like
  "our great grandmother", "meine Oma", "Tante Rosa", "my husband" is
  NOT a name and never an alias. Two cases:
  * If it is a LITERAL kinship term, record the kinship — `relative` +
    `kinship_type` (the term verbatim) with the OTHER party (the "our" /
    "my" ego) as the object, or a primitive when reducible.
  * If it is a courtesy title or family/pet name that is NOT a literal
    relation (a "Tante" who is no aunt, "Bubele"), record it as
    `referred_to_as` (the term verbatim), and add a `perspective`
    qualifier naming the ego (the person or family whose viewpoint it is)
    ONLY when that ego is an entity you can point to. Omit `perspective`
    when the ego is a bare "our"/"we" with no named person or family —
    never invent an entity to fill it.
  When unsure whether a term is literal kinship, prefer `referred_to_as`
  (it asserts only that the term was used, never a relation).
- Residence: an address stated on a form is asserted for the person that
  field names (e.g. the informant/applicant on a certificate), NOT for
  everyone the document mentions. Do not copy one person's stated address
  onto others as their own `residence` — co-residence ("lived with") is
  derived later from separately-sourced residences over overlapping time,
  never asserted here. If you do record a dependent's residence from a
  relative's address (e.g. a newborn at the parents' address), lower its
  confidence to mark the inference.
- Persecution and displacement are EVENTS, not relations and not
  locations on a person (ADR-0014). "Deported to X in 1942" is a
  `deportation`; "held at", "interned at", "taken to" an
  `imprisonment`; "released from", "freed from" a `release`. Put the
  claims on the EVENT: `participant` (the person), `location` (the
  camp, ghetto, or prison), `point_in_time` (the date) — or
  `start_time`/`end_time` for a period of detention. Never record the
  camp or prison as the person's own `location` or `residence`, and
  never emit a gap line for a relation the event model already
  carries. If no such event is available to you, a gap line is the
  honest fallback — but check first: it should be rare.
- Set confidence (0-1) per claim: how certain you are the source asserts it.

{assignment_block}

Output NDJSON: one complete claim JSON object per line — no wrapper
object, no code fences, no prose. Each claim matches the "Claim"
definition of this JSON Schema:

{schema}

If the source asserts a RELATION that no listed property expresses AND
no event kind captures (e.g. "godparent of", "buried alongside"), do
NOT force it into an adjacent-but-wrong property and do NOT silently
drop it: emit a GAP line instead —
{{"gap": {{"subject": "<local_id or name>",
"predicate": "<the relation, verbatim>",
"object": "<local_id, name, or literal>", "evidence": "<short excerpt>"}}}}
Gaps are reviewed to grow the vocabulary; they are never invented, and
never used for something the vocabulary already holds. Check the
property list and the persecution-event rule before emitting one.

After the last line, end with exactly one final line:
{{"done": true, "claims_emitted": N, "gaps_emitted": M}}
where N and M count the claim and gap lines you emitted.
{document_block}"""


LINE_ASSIGNMENT_BLOCK = """\
Refer to people, places, organizations, events, and occupations with
"local" references (stable local_id per entity within this response,
label as written — prefer the fullest name the document gives for a
person anywhere, instance_of from: {kinds}).
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
a date) — not a place or a bare relation. Unify aliases: one entity that
appears as "Rosa Genserowski", "Rosa", and "R. Genserowski" is ONE
manifest entry whose label is the fullest form the document gives and
whose aliases list every other NAME form, verbatim. Aliases are actual
NAMES the document uses for the entity. They are NOT:
- role or form-field labels — MOTHER, FATHER, CHILD, INFORMANT, BRIDE,
  WITNESS name a ROLE, not the person;
- relational or perspectival references — "our great grandmother", "my
  husband", "Tante Rosa", "Oma", "the deceased". These describe a
  relationship or viewpoint, not a name; they are captured as claims at
  extraction, not as aliases (a literal kinship term becomes `relative` +
  `kinship_type`; a courtesy/pet name becomes `referred_to_as`). Never
  record any of these as an alias (ADR-0018).

Name composition and the canonical label (ADR-0022):
- Do NOT enumerate full-name PERMUTATIONS as aliases. A "/" between family
  names ("Frida Hollfeld/Falk/Gellerowski") is ONE person with alternative
  family names held at different times: give the label ONE clean form (given
  + one surname), list the OTHER surname forms as aliases, and do NOT emit the
  composed cross-products ("Frida Falk", "Frida Gellerowski"). Never guess
  which surname is maiden vs married — that is a name TYPE, recorded only when
  the source marks it, at extraction.
- Do NOT list a bare given name ("Sigmund") or an initialism ("S. Gellerowski")
  as an alias — a reader derives those from a fuller form. A genuinely
  distinct informal form ("Siggi") IS an alias.
- Keep the canonical label CLEAN: parenthetical name metadata is NOT part of
  it. "Rieke Sadelman-Rauholz (b. Gellerowski)" has label "Rieke
  Sadelman-Rauholz"; "Lazar Gellerowski (later Leon Gell)" has label "Lazar
  Gellerowski" with "Leon Gell" as an alias. Keep hyphenated and
  space-separated compound/particle surnames ("de la Cruz", "van der Berg")
  WHOLE — never split them. Never infer given vs family from word order.

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

Entity kinds — pick the one whose meaning actually fits, not the
nearest-sounding one:
{kind_descriptions}

Transcription of artifact {artifact_qid} (language: {language}):

{numbered_text}
"""


class ChunkIncomplete(Exception):
    """Chunk response truncated: cut off before its terminal sentinel."""


def parse_chunk_response(
    response: str, warn: Callable[[str], None] | None = None
) -> list[dict]:
    """NDJSON claims + a terminal sentinel that proves completeness.

    A missing sentinel means the output was cut off mid-stream (silent
    truncation) and raises ChunkIncomplete — cheap to retry, unlike a
    whole-document retry.

    The sentinel's self-reported ``claims_emitted`` is advisory only.
    Models routinely miscount a dense chunk's claims by one or more, and
    a present sentinel already proves nothing was truncated — so a count
    mismatch keeps every parsed claim and is only surfaced via ``warn``.
    Rejecting a complete, valid response over an off-by-one self-count
    would discard the whole chunk's extraction (see ADR-0006).
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
    if sentinel != len(claims) and warn is not None:
        warn(
            f"    claim count mismatch: sentinel reported {sentinel}, "
            f"kept {len(claims)} parsed (response complete)"
        )
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
        if (
            isinstance(gap, dict)
            and gap.get("subject")
            and gap.get("predicate")
        ):
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
    reassembly nets are the backstop for that case).
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
        if (
            isinstance(ref, dict)
            and ref.get("kind") == "local"
            and isinstance(ref.get("local_id"), str)
        ):
            fix(ref)

    visit(fixed.get("subject"))
    value = fixed.get("value")
    if isinstance(value, dict):
        visit(value.get("value"))
    for qualifier in fixed.get("qualifiers") or []:
        if isinstance(qualifier, dict) and isinstance(
            qualifier.get("value"), dict
        ):
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
            number
            for number in entity.mention_lines
            if 1 <= number <= len(text_lines)
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


def normalize_to_manifest(
    claim: dict, records: dict[str, EntityRecord]
) -> dict:
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


# the stages the extractor stores, in order
EXTRACTION_STAGES = ("entity_manifest", "chunk", "batch")


class ChunkedClaimExtractor:
    """Entity manifest -> packed chunks -> per-chunk calls -> merge.

    Each phase is a stage in the stage store (yizkr.pipeline.stages): the
    entity manifest, every chunk and the merged batch are stored under
    keys built from the transcription's content, the prompt templates, the
    model manifest and the model, with the raw response of every call
    beside the parsed output. A re-run reuses whatever is unchanged, so a
    failed chunk costs one chunk, not the document.

    Pass `stages` (a StageScope for the document) to share a store with
    the rest of a run; otherwise the store lives in `state_dir`, the
    document is named by its transcription's SHA-1, and `resume` decides
    whether earlier outputs there are reused or recomputed.

    Matches LlmClaimExtractor's extract() signature so the CLI treats the
    strategies interchangeably. After extract(), `failed_chunks` lists
    chunks that produced nothing despite retries — the batch is still
    returned (partial progress is the point) but not stored, and the
    caller decides how loudly to fail.
    """

    def __init__(
        self,
        client: LlmClient,
        state_dir: Path | None = None,
        budget: int = DEFAULT_CHUNK_BUDGET,
        retries: int = 2,
        resume: bool = False,
        concurrency: int = 1,
        log: Callable[[str], None] | None = None,
        *,
        stages: StageScope | None = None,
        model: Mapping[str, Any] | None = None,
    ) -> None:
        if stages is None and state_dir is None:
            raise ValueError("pass a stage scope or a state directory")
        self.client = client
        self.state_dir = state_dir
        self.stages = stages
        self.model = model
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
        self._handle: Any = None
        self.failed_chunks: list[str] = []
        # open-schema gaps (ADR-0013): relations no property expresses,
        # collected per run for review; never written to the graph
        self.gaps: list[dict] = []
        # entity manifest unusable -> line-range segmentation was used
        self.used_fallback_manifest = False
        # line-range segmentation also unusable -> offset windows
        self.used_fallback_segmentation = False
        # how chunk calls saw the document: fork, inline or compressed
        self.context_mode: str | None = None
        # the stored batch, when every chunk succeeded
        self.batch_record: StageRecord | None = None
        self._rejects: list[dict] = []

    # -- helpers ---------------------------------------------------------

    def _scope(self, transcription: Transcription) -> StageScope:
        if self.stages is not None:
            return self.stages
        sha1 = hashlib.sha1(transcription.text.encode()).hexdigest()
        return StageScope(
            StageStore(self.state_dir),
            doc_scope(sha1),
            recompute=() if self.resume else EXTRACTION_STAGES,
        )

    def _model_component(self) -> dict:
        if self.model is not None:
            return dict(self.model)
        return {"engine": "client", "model": self.client.model_identifier}

    def _engine_meta(self, responses: list[str], handle: Any = None) -> dict:
        """Provenance for a record whose calls answered: the engine's own
        model identifier and, for a forkable session, its id."""
        meta: dict[str, Any] = {}
        if responses:
            meta["model_identifier"] = self.client.model_identifier
        if handle is not None and not isinstance(handle, list | dict):
            meta["session_id"] = str(handle)
        return meta

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
        self,
        transcription: Transcription,
        artifact_qid: str,
        responses: list[str],
    ) -> tuple[list[EntityRecord] | None, Any, str, bool]:
        """(entities, conversation handle, last error, any call answered).

        No entities means the manifest was unusable after a retry; the
        caller falls back to line-range segmentation. As with
        segmentation fallback, no handle survives a rejected manifest —
        forking would plant known-bad output in every chunk's context.
        """
        line_count = len(transcription.text.splitlines())
        kinds, kind_descriptions = entity_kind_prompt_lines()
        prompt = ENTITY_MANIFEST_PROMPT.format(
            kinds=kinds,
            kind_descriptions=kind_descriptions,
            artifact_qid=artifact_qid,
            language=transcription.language,
            numbered_text=number_lines(transcription.text),
        )
        handle = None
        answered = False
        last_error = "engine has no conversation support"
        for _attempt in range(2):
            try:
                if isinstance(self.client, ConversationalLlmClient):
                    response, handle = self.client.open_conversation(prompt)
                else:
                    response = self.client.complete(prompt)
            except Exception as exc:
                last_error, handle = f"{type(exc).__name__}: {exc}", None
                continue
            answered = True
            responses.append(response)
            entities, problems = self._parse_entity_manifest(
                response, line_count
            )
            if entities is not None and not problems:
                return entities, handle, "", answered
            last_error, handle = "; ".join(problems or ["unparseable"]), None
        return None, None, last_error, answered

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
                    problems.append(
                        f"entity {record.id}: unknown kind {record.kind!r}"
                    )
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
        self,
        transcription: Transcription,
        artifact_qid: str,
        responses: list[str],
    ) -> tuple[DocumentSegmentation, Any, str, bool]:
        """(segmentation, handle or None, fallback reason, any call answered).

        A None handle means chunk calls cannot fork (context mode 2/3).
        On segmentation fallback the handle is always None and the reason
        is set: forking a conversation whose segmentation turn was
        rejected would plant known-bad output in every chunk's context.
        """
        line_count = len(transcription.text.splitlines())
        prompt = SEGMENTATION_PROMPT.format(
            artifact_qid=artifact_qid,
            language=transcription.language,
            numbered_text=number_lines(transcription.text),
        )
        handle = None
        answered = False
        last_error = "engine has no conversation support"
        for _attempt in range(2):
            try:
                if isinstance(self.client, ConversationalLlmClient):
                    response, handle = self.client.open_conversation(prompt)
                else:
                    response = self.client.complete(prompt)
            except Exception as exc:
                last_error, handle = f"{type(exc).__name__}: {exc}", None
                continue
            answered = True
            responses.append(response)
            segmentation, problems = self._parse_segmentation(
                response, line_count
            )
            if segmentation is not None and not problems:
                return segmentation, handle, "", answered
            last_error, handle = "; ".join(problems or ["unparseable"]), None
        return (
            self._fallback_segmentation(line_count),
            None,
            last_error,
            answered,
        )

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

    def _fallback_segmentation(self, line_count: int) -> DocumentSegmentation:
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
        return DocumentSegmentation(units=units)

    def _phase_one(
        self, transcription: Transcription, artifact_qid: str
    ) -> StageResult:
        """The entity_manifest stage: a manifest, or the recorded fallback.

        A fallback is stored only when the engine answered. When every
        call of a tier raised (an engine outage), the stage fails instead,
        so the next run retries it rather than reusing a degraded result.
        """
        self._log("  scanning document for entities...")
        responses: list[str] = []
        entities, handle, manifest_error, answered = self._entity_manifest(
            transcription, artifact_qid, responses
        )
        if entities is not None:
            self._handle = handle
            return StageResult(
                {
                    "strategy": "entity",
                    "entities": [
                        record.model_dump(mode="json") for record in entities
                    ],
                },
                raw=responses,
                meta=self._engine_meta(responses, handle),
            )
        if not answered:
            raise StageFailed(
                f"entity manifest: no engine response ({manifest_error})"
            )
        segmentation, handle, segmentation_error, answered = self._segment(
            transcription, artifact_qid, responses
        )
        if not answered:
            raise StageFailed(
                f"segmentation: no engine response ({segmentation_error})"
            )
        self._handle = handle
        fallback = {"manifest": manifest_error}
        attention = [
            "entity manifest fell back to line-range segmentation: "
            f"{manifest_error}"
        ]
        if segmentation_error:
            fallback["segmentation"] = segmentation_error
            attention.append(
                f"segmentation fell back to line windows: {segmentation_error}"
            )
        return StageResult(
            {
                "strategy": "line-range",
                "segmentation": segmentation.model_dump(mode="json"),
                "fallback": fallback,
            },
            raw=responses,
            attention=attention,
            meta=self._engine_meta(responses, handle),
        )

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
            LINE_ASSIGNMENT_BLOCK.format(
                kinds=entity_kind_prompt_lines()[0],
                units_block=units_block,
            ),
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
            + (
                f" (aliases: {', '.join(entity.aliases)})"
                if entity.aliases
                else ""
            )
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
        self,
        chunk_id: str,
        prompt: str,
        handle: Any,
        fallback_prompt: str,
        responses: list[str],
    ) -> list[dict]:
        """Call (fork if possible), keep every response, parse; retries
        inside. `responses` receives each answer, parseable or not."""
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
            responses.append(response)
            try:
                return parse_chunk_response(response, warn=self._log)
            except ChunkIncomplete as exc:
                last_error = exc
        raise last_error

    def _compute_chunk(
        self,
        stages: StageScope,
        key: StageKey,
        chunk_id: str,
        prompt: str,
        fallback_prompt: str,
        handle: Any,
    ) -> StageResult:
        """The chunk stage: one assignment's claims and gaps.

        The latest logged failure's response is re-parsed first: a parser
        fix recovers a chunk that failed to parse without a second model
        call (ADR-0006)."""
        meta = {"parse_version": _parse_version(), "context_mode": None}
        for failure in reversed(stages.failures("chunk", key)):
            raw = failure.get("raw")
            if not raw:
                break
            try:
                claims = parse_chunk_response(raw, warn=self._log)
            except ChunkIncomplete:
                break
            return StageResult(
                {"claims": claims, "gaps": parse_chunk_gaps(raw)},
                raw=[raw],
                meta=meta | {"recovered": "re-parsed a logged failure"},
            )
        responses: list[str] = []
        try:
            claims = self._run_chunk(
                chunk_id, prompt, handle, fallback_prompt, responses
            )
        except Exception as exc:
            truncated = (
                responses[-1]
                if isinstance(exc, ChunkIncomplete) and responses
                else None
            )
            raise StageFailed(
                f"{type(exc).__name__}: {exc}",
                attempts=self.retries + 1,
                raw=truncated,
            ) from exc
        meta["context_mode"] = self.context_mode
        return StageResult(
            {"claims": claims, "gaps": parse_chunk_gaps(responses[-1])},
            raw=responses,
            meta=meta | self._engine_meta(responses),
        )

    def _chunk_output(
        self, record: StageRecord, artifact_qid: str
    ) -> tuple[list[dict], list[dict]] | None:
        """A stored chunk's (claims, gaps), re-parsed from its raw response
        when the parser has changed since it was stored; None when the
        current parser rejects it."""
        if record.meta.get("parse_version") == _parse_version():
            output = record.output()
            claims, gaps = output["claims"], output["gaps"]
        else:
            raw = record.raw()[-1]
            try:
                claims = parse_chunk_response(raw, warn=self._log)
            except ChunkIncomplete:
                return None
            gaps = parse_chunk_gaps(raw)
        previous = record.meta.get("artifact_qid")
        return rebind_artifact(claims, previous, artifact_qid), gaps

    # -- orchestration ---------------------------------------------------

    def extract(
        self, transcription: Transcription, artifact_qid: str
    ) -> ClaimBatch:
        stages = self._scope(transcription)
        text_lines = transcription.text.splitlines()
        text_hash = content_hash(
            {"text": transcription.text, "language": transcription.language}
        )
        model = self._model_component()
        vocabulary = manifest_hash()

        self._handle = None
        manifest_key = stage_key(
            "entity_manifest",
            inputs={"transcription": text_hash},
            prompts={
                "entity_manifest": ENTITY_MANIFEST_PROMPT,
                "segmentation": SEGMENTATION_PROMPT,
            },
            manifest=vocabulary,
            model=model,
        )
        phase_one = stages.run(
            "entity_manifest",
            manifest_key,
            lambda: self._phase_one(transcription, artifact_qid),
        )
        found = phase_one.record.output()
        entities: list[EntityRecord] | None = None
        segmentation: DocumentSegmentation | None = None
        if found["strategy"] == "entity":
            entities = [
                EntityRecord.model_validate(record)
                for record in found["entities"]
            ]
        else:
            segmentation = DocumentSegmentation.model_validate(
                found["segmentation"]
            )
        fallback = found.get("fallback") or {}
        self.used_fallback_manifest = entities is None
        self.used_fallback_segmentation = bool(fallback.get("segmentation"))
        # a reused manifest has no live session: its conversation may be
        # gone, so resumption drops to inline context rather than
        # re-running phase 1
        handle = None if phase_one.reused else self._handle

        inline_document = (
            handle is None and len(transcription.text) <= INLINE_DOCUMENT_LIMIT
        )
        self.context_mode = (
            "fork"
            if handle is not None
            else "inline"
            if inline_document
            else "compressed"
        )

        schema = json.dumps(ClaimBatch.model_json_schema(), indent=2)
        if entities is not None:
            records = {record.id: record for record in entities}
            chunk_groups: list[list] = pack_entities(
                entities, text_lines, self.budget
            )
            assignment_template = ENTITY_ASSIGNMENT_BLOCK

            def build_prompt(group: list, inline: bool) -> str:
                return self._entity_chunk_prompt(
                    group,
                    entities,
                    text_lines,
                    transcription,
                    artifact_qid,
                    inline,
                )

            def member_ids(group: list) -> list[str]:
                return [record.id for record in group]

            def transform(claim: dict, chunk_id: str) -> dict:
                return normalize_to_manifest(claim, records)

        else:
            chunk_groups = pack_units(
                segmentation.units, text_lines, self.budget
            )
            assignment_template = LINE_ASSIGNMENT_BLOCK

            def build_prompt(group: list, inline: bool) -> str:
                return self._chunk_prompt(
                    group, text_lines, transcription, artifact_qid, inline
                )

            def member_ids(group: list) -> list[str]:
                return [unit.id for unit in group]

            def transform(claim: dict, chunk_id: str) -> dict:
                return prefix_local_ids(claim, chunk_id)

        chunk_prompts = {
            "chunk": CHUNK_PROMPT,
            "assignment": assignment_template,
            "schema": schema,
        }
        self.failed_chunks = []
        chunk_ids = [
            f"c{index:03d}" for index in range(1, len(chunk_groups) + 1)
        ]
        total = len(chunk_ids)
        self._chunks_done = 0
        if entities is not None:
            self._log(f"  extract: {len(entities)} entities, {total} chunk(s)")
        else:
            self._log(f"  extract: {total} chunk(s) (line-range fallback)")

        def run_one(chunk_id: str, group: list) -> _Chunk | None:
            """One chunk end to end, or None on failure. Thread-safe:
            records are per key, shared state goes through the lock."""
            key = stage_key(
                "chunk",
                inputs={
                    "transcription": text_hash,
                    "entity_manifest": phase_one.record.output_hash,
                },
                prompts=chunk_prompts,
                manifest=vocabulary,
                model=model,
                params={"members": member_ids(group)},
            )

            def compute() -> StageResult:
                return self._compute_chunk(
                    stages,
                    key,
                    chunk_id,
                    build_prompt(group, False),
                    build_prompt(group, inline_document),
                    handle,
                )

            try:
                outcome = stages.run(
                    "chunk",
                    key,
                    compute,
                    item=chunk_id,
                    artifact_qid=artifact_qid,
                )
                parsed = self._chunk_output(outcome.record, artifact_qid)
                if parsed is None:  # the current parser rejects it: redo
                    outcome = stages.run(
                        "chunk",
                        key,
                        compute,
                        item=chunk_id,
                        artifact_qid=artifact_qid,
                        force=True,
                    )
                    parsed = self._chunk_output(outcome.record, artifact_qid)
                if parsed is None:
                    raise ChunkIncomplete("stored response does not parse")
            except DispatchStopped:
                raise
            except Exception:
                with self._state_lock:
                    self.failed_chunks.append(chunk_id)
                self._note_chunk_done(total, ok=False)
                return None
            claims, gaps = parsed
            self._note_chunk_done(
                total, ok=True, claims=len(claims), resumed=outcome.reused
            )
            return _Chunk(
                claims,
                gaps,
                content_hash(
                    {"claims": claims, "gaps": gaps},
                    artifact_qid=artifact_qid,
                ),
                outcome.record.meta.get("model_identifier"),
            )

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            outcomes = list(pool.map(run_one, chunk_ids, chunk_groups))
        self.failed_chunks.sort()

        def merge() -> tuple[ClaimBatch, dict[str, str], list[str]]:
            return self._merge(
                chunk_ids,
                outcomes,
                transform,
                entities,
                segmentation,
                text_lines,
                phase_one.record,
            )

        if self.failed_chunks:
            batch, _extras, _attention = merge()
            stages.emit(
                "failed",
                "batch",
                None,
                reason=f"{len(self.failed_chunks)} chunk(s) failed; the "
                "partial batch is not stored",
            )
            self._refuse_empty(batch, rejected=len(self._rejects))
            return batch

        batch_key = stage_key(
            "batch",
            inputs={
                "transcription": text_hash,
                "entity_manifest": phase_one.record.output_hash,
                "chunks": [chunk.digest for chunk in outcomes],
            },
            manifest=vocabulary,
        )

        def compute_batch() -> StageResult:
            batch, extras, attention = merge()
            self._refuse_empty(batch, rejected=len(self._rejects))
            return StageResult(
                batch.model_dump(mode="json"),
                extras=extras,
                attention=attention,
            )

        stored = stages.run(
            "batch", batch_key, compute_batch, artifact_qid=artifact_qid
        )
        self.batch_record = stored.record
        gaps_text = stored.record.extra("gaps.ndjson") or ""
        self.gaps = [
            json.loads(line) for line in gaps_text.splitlines() if line.strip()
        ]
        return ClaimBatch.model_validate(
            stored.record.bound_output(artifact_qid)
        )

    def _refuse_empty(self, batch: ClaimBatch, *, rejected: int) -> None:
        if not batch.claims and (self.failed_chunks or rejected):
            raise ValueError(
                f"chunked extraction produced no valid claims "
                f"({len(self.failed_chunks)} failed chunks, "
                f"{rejected} rejected claims)"
            )

    def _merge(
        self,
        chunk_ids: list[str],
        outcomes: list[_Chunk | None],
        transform: Callable[[dict, str], dict],
        entities: list[EntityRecord] | None,
        segmentation: DocumentSegmentation | None,
        text_lines: list[str],
        manifest_record: StageRecord,
    ) -> tuple[ClaimBatch, dict[str, str], list[str]]:
        """The batch stage: merge chunk claims into one validated batch,
        with rejects, gaps and the coverage report as extras."""
        raw_claims: list[dict] = []
        transform_rejects: list[dict] = []
        gaps: list[dict] = []
        for chunk_id, chunk in zip(chunk_ids, outcomes, strict=True):
            if chunk is None:
                continue
            for claim in chunk.claims:
                # one malformed claim (a double-nested local_id, say)
                # must never sink the whole document's merge — degrade
                # it to a reject, the way salvage_valid_claims does
                # downstream
                try:
                    raw_claims.append(transform(claim, chunk_id))
                except Exception as exc:  # keep the other claims
                    transform_rejects.append(
                        {
                            "claim": claim,
                            "problems": [f"transform failed: {exc}"],
                        }
                    )
            gaps.extend(chunk.gaps)
        self.gaps = gaps

        deduped: list[dict] = []
        for claim in raw_claims:
            if claim not in deduped:
                deduped.append(claim)

        if entities is not None:
            # ADR-0022 §4: hygiene the alias bag (drop derivable
            # short-forms, keep compounds whole) — the deterministic safety
            # net over the LLM manifest; typing/composition stay the LLM's
            # job.
            from yizkr.pipeline.names import normalize_entity_dict

            entities_payload = [
                normalize_entity_dict(record.model_dump())
                for record in entities
            ]
        else:
            entities_payload = []
        # the engines that answered, as their records name them: a batch
        # merged from reused chunks names the models that made them
        identifiers = sorted(
            {
                identifier
                for identifier in [
                    manifest_record.meta.get("model_identifier"),
                    *(chunk.model_identifier for chunk in outcomes if chunk),
                ]
                if identifier
            }
        )
        batch, rejects = salvage_valid_claims(
            deduped,
            run_metadata(", ".join(identifiers) or "unknown").model_dump(
                mode="json"
            ),
            entities=entities_payload,
        )
        rejects = transform_rejects + rejects
        self._rejects = rejects
        extras: dict[str, str] = {}
        if rejects:
            extras["rejects.json"] = json.dumps(rejects, indent=2)
        if gaps:
            extras["gaps.ndjson"] = "\n".join(map(json.dumps, gaps))
        if entities is not None:
            coverage = self._entity_coverage(entities, batch, text_lines)
        else:
            coverage = self._coverage(segmentation, batch, len(text_lines))
        extras["coverage.json"] = json.dumps(coverage, indent=2)
        attention = [] if batch.claims else ["no claims extracted"]
        return batch, extras, attention

    def _entity_coverage(
        self,
        entities: list[EntityRecord],
        batch: ClaimBatch,
        text_lines: list[str],
    ) -> dict:
        """Advisory completeness report, entity flavour (ADR-0007).

        Used in place of the line-range path's unit audit (`_coverage`):
        flags manifest entities with zero claims, and lines that carry
        name-shaped tokens matching no manifest label/alias (a
        possibly-missed entity)."""
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
        return {
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
        }

    def _coverage(
        self,
        segmentation: DocumentSegmentation,
        batch: ClaimBatch,
        line_count: int,
    ) -> dict:
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
            if unit.extract
            and not any(label in unit.label for label in subject_labels)
        ]
        return {
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
        }


class _Chunk(NamedTuple):
    """One chunk's parsed output, its content hash and its engine."""

    claims: list[dict]
    gaps: list[dict]
    digest: str
    model_identifier: str | None


def _parse_version() -> int:
    from yizkr.pipeline.stageversions import STAGES

    return STAGES["chunk_parse"].version
