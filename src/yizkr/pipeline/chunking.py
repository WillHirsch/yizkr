"""Semantically chunked claim extraction (ADR-0006).

Dense documents overflow a single model response — the constraint is
OUTPUT size (claim JSON amplifies source text ~16x), never input size.
So: one segmentation call maps the transcription to semantic units;
units greedy-pack into chunks; each chunk call keeps the full document
in context (by forking the segmentation conversation where the engine
supports it) but emits claims for its assigned units only; a
deterministic merge namespaces local ids and leaves same-label
unification to the existing label-keyed resolve/write machinery.

Every call goes to the client the extractor is given. Behind the LLM
cache (ADR-0032) a re-run replays the calls that completed and asks the
engine only for the rest, so a failed chunk costs one chunk, not the
document, while parsing, the merge and validation run every time, so a
fix to any of them costs no model call.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
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
from yizkr.pipeline.llm import (
    ConversationalLlmClient,
    ConversationChanged,
    LlmClient,
    for_attempt,
    reject_response,
)
from yizkr.pipeline.stages import atomic_write_text
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

# The longest a single reason runs inside another message (a chunk's, or
# an attempt's at a fallback tier), so that a failure message, which leads
# with the outcome, stays well within what a stage's failed event keeps.
REASON_LIMIT = 40


def _brief(reason: str) -> str:
    """`reason` on one line, cut to REASON_LIMIT characters."""
    flat = " ".join(reason.split())
    if len(flat) <= REASON_LIMIT:
        return flat
    return flat[: REASON_LIMIT - 1] + "…"


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
  "our great grandmother", "meine Oma", "Aunt Pünktchen", "my husband" is
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
  locations on a person (ADR-0014). "Deported to Klurnau in 1942" is a
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
family or household ("the Löbigs") is a `family`; a ship is a `vessel`;
a cited law, decree, gazette, or register is a `legal_instrument`; a
regiment or brigade is a `military_unit`. A forced removal is a
`deportation` event, a detention an `imprisonment` event, a freeing a
`release` event (each an event with a person participant, a place, and
a date) — not a place or a bare relation. Unify aliases: one entity that
appears as "Cäcilie Löbig", "Cäcilie", and "C. Löbig" is ONE
manifest entry whose label is the fullest form the document gives and
whose aliases list every other NAME form, verbatim. Aliases are actual
NAMES the document uses for the entity. They are NOT:
- role or form-field labels — MOTHER, FATHER, CHILD, INFORMANT, BRIDE,
  WITNESS name a ROLE, not the person;
- relational or perspectival references — "our great grandmother", "my
  husband", "Aunt Pünktchen", "Oma", "the deceased". These describe a
  relationship or viewpoint, not a name; they are captured as claims at
  extraction, not as aliases (a literal kinship term becomes `relative` +
  `kinship_type`; a courtesy/pet name becomes `referred_to_as`). Never
  record any of these as an alias (ADR-0018).

Name composition and the canonical label (ADR-0022):
- Do NOT enumerate full-name PERMUTATIONS as aliases. A "/" between family
  names ("Ida Sörne/Löbig") is ONE person with alternative family names held
  at different times: give the label ONE clean form (given + one surname),
  list the OTHER surname forms as aliases, and do NOT emit the composed
  cross-products ("Ida Löbig"). Never guess which surname is maiden vs
  married — that is a name TYPE, recorded only when the source marks it, at
  extraction.
- Do NOT list a bare given name ("Sebastian") or an initialism ("S. Löbig")
  as an alias — a reader derives those from a fuller form. A genuinely
  distinct informal form ("Wastl") IS an alias.
- Keep the canonical label CLEAN: parenthetical name metadata is NOT part of
  it. "Hildegard Löbig (b. Laudler)" has label "Hildegard Löbig"; "Gustav
  Löbig (later Gustave Lobig)" has label "Gustav Löbig" with "Gustave Lobig"
  as an alias. Keep hyphenated and space-separated compound/particle
  surnames ("A-B", "de la Cruz", "van der Berg") WHOLE — never split them.
  Never infer given vs family from word order.

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
  travel or event logistics (schedules, bookings, contact lists), people
  taking part in present-day travel or events, boilerplate.
  They are recorded for audit but no claims are extracted about them.
  Entities the document asserts historical/genealogical facts about get
  extract=true.
- On official certificates and registers the administrative apparatus is
  boilerplate: the registrar, officiant, or witness who certifies the
  record; the issuing authority (e.g. a civil registry office such as a
  Standesamt); and the register or certificate the document cites as its
  own source are all extract=false. They attest the record — they are not
  its genealogical subject. (A witness or officiant who is ALSO a relative
  of the subject is the exception: extract=true for the relationship.)
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


class ExtractionFailed(RuntimeError):
    """The extraction ended with no claims for want of a usable answer: a
    chunk failed, the chunks' answers held objects the parser could not
    read as claims, or validation rejected every claim. The message leads
    with that outcome and its causes, then names any fallback tier taken,
    with the reason each attempt at the refused turn gave; it is the
    reason the extraction stage records.

    An extraction that ends with no claims because the answers it accepted
    say the document holds none, on whichever tier, is not a failure: its
    batch is returned, and the extractor's `attention` says why it is
    empty."""


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


def _gap_line(gap: Any) -> bool:
    """Whether the value under a `gap` key is one parse_chunk_gaps reads
    as a gap: an object with a subject and a predicate."""
    return (
        isinstance(gap, dict)
        and bool(gap.get("subject"))
        and bool(gap.get("predicate"))
    )


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
        if _gap_line(gap):
            gaps.append(
                {
                    "subject": str(gap.get("subject", "")),
                    "predicate": str(gap.get("predicate", "")),
                    "object": str(gap.get("object", "")),
                    "evidence": str(gap.get("evidence", "")),
                }
            )
    return gaps


def _holds(value: Any) -> bool:
    """Whether a JSON value holds something at any depth: an object when
    one of its values does, a list when one of its items does, anything
    else when it is not empty (null, false, 0 or "").

    It walks the value with a stack of its own rather than by recursion,
    so a nest as deep as json decodes never raises RecursionError."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif item:
            return True
    return False


_GAP_FIELDS = frozenset({"subject", "predicate", "object", "evidence"})


def _merged_gap_line(gap: Any) -> bool:
    """Whether the value under the terminal sentinel's `gap` key is a gap
    line merged into it: a gap line (_gap_line) that carries nothing else
    that holds something, no list or object that holds something under a
    key other than the four that parse_chunk_gaps reads."""
    return _gap_line(gap) and not any(
        isinstance(value, (list, dict)) and _holds(value)
        for key, value in gap.items()
        if key not in _GAP_FIELDS
    )


def parse_chunk_unread(response: str) -> list[dict]:
    """The JSON objects of a chunk answer that hold something the claim
    parser cannot read: claims wrapped in one object, say, or written with
    other keys than `subject` and `property`.

    Whether an object holds something (_holds) decides, not its shape. A
    claim and a gap line (well formed or not) are read. The terminal
    sentinel is read too, unless one of its keys holds a list or object
    that holds something, such as claims carried inside it. A note in a
    string on it is harmless, and so is a gap line merged into it
    (_merged_gap_line), which parse_chunk_gaps reads: a `gap` that has a
    subject and a predicate and carries nothing else that holds something
    in a list or object. Anything else under `gap`, such as claims, a gap
    line that carries them or a list of gap lines, is unread like a list
    of notes or of line numbers under any other key, since a failure that
    shows, which `--refresh extract` asks the model again for, is
    preferred to claims lost in silence. Any other object is unread unless
    it holds nothing at any depth, as in `{}`, `{"claims": []}` or
    `{"result": {"claims": [{}]}}`, which say there is nothing.

    They are counted, never guessed at. A chunk whose answer held such
    objects and no claim reads as unreadable rather than as answered with
    none, so an extraction left without claims by them fails
    (_refuse_empty) instead of passing for a claim-free document's.
    """
    unread = []
    for obj in scan_json_objects(response):
        if obj.get("done") is True:
            carried = [
                value
                for key, value in obj.items()
                if not (key == "gap" and _merged_gap_line(value))
                and isinstance(value, (list, dict))
                and _holds(value)
            ]
            if carried:
                unread.append(obj)
        elif "gap" in obj or ("subject" in obj and "property" in obj):
            continue
        elif _holds(obj):
            unread.append(obj)
    return unread


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


# what a complete extraction writes to its state directory, when it has one
SIDE_OUTPUTS = ("coverage.json", "rejects.json", "gaps.ndjson")


class ChunkedClaimExtractor:
    """Entity manifest -> packed chunks -> per-chunk calls -> merge.

    Every call goes to `client`. A chunk response cut off before its
    terminal sentinel is rejected through it (llm.reject_response): behind
    the LLM cache (ADR-0032) that makes the retry, in this run or the
    next, ask the engine again rather than be answered with the same text,
    and a chunk that failed every retry is asked again in the next run. A
    rejected answer the cache kept from an earlier call spends no retry:
    the engine has not answered the chunk yet, and has as many tries as
    it would without the cache.
    The entity manifest and the segmentation are asked in two attempts
    each, keyed apart behind the cache (llm.for_attempt): a refused
    answer is kept, so a later run replays the same attempts and takes the
    same fallback. When a conversation behind the cache has to be opened
    again and answers differently (llm.ConversationChanged), the
    extraction starts over from the new answer.

    `state_dir`, when given, receives the advisory side outputs of a
    complete extraction: `coverage.json`, and `rejects.json` and
    `gaps.ndjson` when there are any (a file the extraction no longer
    has anything for is removed).

    Matches LlmClaimExtractor's extract() signature so the CLI treats the
    strategies interchangeably. After extract(), `failed_chunks` lists
    chunks that produced nothing despite retries, with their reasons in
    `failure_reasons` — the batch is still returned (partial progress is
    the point) and the caller decides how loudly to fail — and
    `attention` says what a reader of the batch should know: a fallback
    tier, or why it holds no claims. An extraction that would end with no
    claims for want of a usable answer raises ExtractionFailed instead
    (`_refuse_empty`), so a batch without claims is returned only when the
    answers the extractor accepted say the document holds none.
    """

    def __init__(
        self,
        client: LlmClient,
        state_dir: Path | None = None,
        budget: int = DEFAULT_CHUNK_BUDGET,
        retries: int = 2,
        concurrency: int = 1,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.state_dir = state_dir
        self.budget = budget
        self.retries = retries
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
        self.failure_reasons: dict[str, str] = {}
        # open-schema gaps (ADR-0013): relations no property expresses,
        # collected per run for review; never written to the graph
        self.gaps: list[dict] = []
        # entity manifest unusable -> line-range segmentation was used
        self.used_fallback_manifest = False
        # line-range segmentation also unusable -> offset windows
        self.used_fallback_segmentation = False
        # why each fallback tier was taken: "manifest", "segmentation"
        self.fallback: dict[str, str] = {}
        # per fallback tier taken, why each of its attempts failed
        self._refusals: dict[str, list[str]] = {}
        # per chunk, the objects in its answer that held something the
        # parser could not read (parse_chunk_unread)
        self._unread: dict[str, list[dict]] = {}
        # how chunk calls saw the document: fork, inline or compressed
        self.context_mode: str | None = None
        self.attention: list[str] = []
        self._rejects: list[dict] = []

    # -- helpers ---------------------------------------------------------

    def _note_chunk_done(
        self, total: int, *, ok: bool, claims: int = 0
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
            tag = f"{claims} claim{plural}"
        self._log(f"  chunk {done}/{total} ({tag})")

    # -- phase 1a: entity manifest (ADR-0007, the default) ----------------

    def _first_turn(
        self, prompt: str, parse: Callable[[str], tuple[Any, list[str]]]
    ) -> tuple[Any, Any, list[str], bool]:
        """(parsed answer or None, conversation handle, why each attempt
        that gave no usable answer failed, any call answered) for a turn
        that opens the extraction: the entity manifest, or the
        segmentation behind it.

        Two attempts, each asked through `for_attempt`, so that behind the
        LLM cache a refused answer is kept under its own attempt and a
        later run replays the same attempts to the same outcome. An
        attempt fails when the engine raises or its answer is refused, and
        each gives its own reason. No handle survives a refused answer:
        forking would plant known-bad output in every chunk's context."""
        handle = None
        answered = False
        reasons: list[str] = []
        for attempt in range(2):
            client = for_attempt(self.client, attempt)
            try:
                if isinstance(client, ConversationalLlmClient):
                    response, handle = client.open_conversation(prompt)
                else:
                    response = client.complete(prompt)
            except Exception as exc:
                reasons.append(f"{type(exc).__name__}: {exc}")
                handle = None
                continue
            answered = True
            parsed, problems = parse(response)
            if parsed is not None and not problems:
                return parsed, handle, reasons, answered
            reasons.append("; ".join(problems or ["unparseable"]))
            self._close_conversation(handle)
            handle = None
        return None, None, reasons, answered

    def _entity_manifest(
        self, transcription: Transcription, artifact_qid: str
    ) -> tuple[list[EntityRecord] | None, Any, list[str], bool]:
        """(entities, conversation handle, why each attempt failed, any
        call answered).

        No entities means the manifest was unusable in both attempts; the
        caller falls back to line-range segmentation."""
        line_count = len(transcription.text.splitlines())
        kinds, kind_descriptions = entity_kind_prompt_lines()
        prompt = ENTITY_MANIFEST_PROMPT.format(
            kinds=kinds,
            kind_descriptions=kind_descriptions,
            artifact_qid=artifact_qid,
            language=transcription.language,
            numbered_text=number_lines(transcription.text),
        )
        return self._first_turn(
            prompt,
            lambda response: self._parse_entity_manifest(response, line_count),
        )

    @staticmethod
    def _parse_entity_manifest(
        response: str, line_count: int
    ) -> tuple[list[EntityRecord] | None, list[str]]:
        """(entities, problems); entities is None when the answer holds no
        entity list. Only a list, or null, is one, and any other value is
        unusable. An empty one, the engine's word that the document names
        nothing, comes with the problem "empty entity list", so that it is
        refused and the line-range tier looks again."""
        _, item_keys = load_vocabulary()
        for obj in scan_json_objects(response):
            if "entities" not in obj:
                continue
            listed = obj["entities"]
            if listed is not None and not isinstance(listed, list):
                return None, [
                    f"'entities' is not a list ({type(listed).__name__})"
                ]
            try:
                records = []
                for raw in listed or []:
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
        self, transcription: Transcription, artifact_qid: str
    ) -> tuple[DocumentSegmentation, Any, list[str], bool]:
        """(segmentation, handle or None, why each attempt failed when it
        fell back, any call answered).

        A None handle means chunk calls cannot fork (context mode 2/3).
        On segmentation fallback the handle is always None and the reasons
        are set: forking a conversation whose segmentation turn was
        refused would plant known-bad output in every chunk's context.
        """
        line_count = len(transcription.text.splitlines())
        prompt = SEGMENTATION_PROMPT.format(
            artifact_qid=artifact_qid,
            language=transcription.language,
            numbered_text=number_lines(transcription.text),
        )
        segmentation, handle, reasons, answered = self._first_turn(
            prompt,
            lambda response: self._parse_segmentation(response, line_count),
        )
        if segmentation is not None:
            return segmentation, handle, [], answered
        return (
            self._fallback_segmentation(line_count),
            None,
            reasons,
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
    ) -> tuple[list[EntityRecord] | None, DocumentSegmentation | None, Any]:
        """(entities, segmentation, conversation handle): the entity
        manifest, or the line-range fallback when the manifest was
        unusable.

        A fallback tier is taken only when the engine answered. When every
        call of a tier raised (an engine outage), extraction fails
        instead, so the document is retried rather than extracted by a
        degraded path the engine never chose.
        """
        self._log("  scanning document for entities...")
        entities, handle, reasons, answered = self._entity_manifest(
            transcription, artifact_qid
        )
        if entities is not None:
            return entities, None, handle
        if not answered:
            raise RuntimeError(
                f"entity manifest: no engine response ({reasons[-1]})"
            )
        self.used_fallback_manifest = True
        self.fallback["manifest"] = reasons[-1]
        self._refusals["manifest"] = reasons
        segmentation, handle, reasons, answered = self._segment(
            transcription, artifact_qid
        )
        if not answered:
            raise RuntimeError(
                f"segmentation: no engine response ({reasons[-1]})"
            )
        if reasons:
            self.used_fallback_segmentation = True
            self.fallback["segmentation"] = reasons[-1]
            self._refusals["segmentation"] = reasons
        return None, segmentation, handle

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
        self, prompt: str, handle: Any, fallback_prompt: str
    ) -> tuple[list[dict], str]:
        """(claims, response) for one chunk: call (fork if possible) and
        parse, with retries. A response cut off before its sentinel is
        rejected, so the retry asks again; one the client kept from an
        earlier call is asked for once more without spending a retry
        (llm.reject_response). A conversation that changed
        (ConversationChanged) is no failure of the chunk: it ends the
        extraction's pass, which starts over."""
        last_error: Exception = ChunkIncomplete("not attempted")
        tries = 1 + self.retries
        kept_rejected = False
        while tries:
            tries -= 1
            try:
                if handle is not None:
                    try:
                        response = self.client.fork(handle, prompt)
                    except ConversationChanged:
                        raise
                    except Exception:
                        # fork machinery failed (session gone, old CLI):
                        # this chunk drops to inline context
                        handle = None
                        response = self.client.complete(fallback_prompt)
                else:
                    response = self.client.complete(fallback_prompt)
            except ConversationChanged:
                raise
            except Exception as exc:
                last_error = exc
                continue
            try:
                return parse_chunk_response(response, warn=self._log), response
            except ChunkIncomplete as exc:
                last_error = exc
                kept = reject_response(self.client, response, f"chunk: {exc}")
                if kept and not kept_rejected:
                    kept_rejected = True
                    tries += 1
        raise last_error

    # -- orchestration ---------------------------------------------------

    def extract(
        self, transcription: Transcription, artifact_qid: str
    ) -> ClaimBatch:
        """The document's batch. When its conversation has to be opened
        again and answers differently (ConversationChanged), the pass is
        void and the extraction starts over from the new answer, at most
        twice."""
        for _pass in range(2):
            try:
                return self._extract(transcription, artifact_qid)
            except ConversationChanged as exc:
                self._log(f"  {exc}: extracting again from the new answer")
        return self._extract(transcription, artifact_qid)

    def _extract(
        self, transcription: Transcription, artifact_qid: str
    ) -> ClaimBatch:
        text_lines = transcription.text.splitlines()
        self.failed_chunks = []
        self.failure_reasons = {}
        self.gaps = []
        self.fallback = {}
        self.attention = []
        self._rejects = []
        self.used_fallback_manifest = False
        self.used_fallback_segmentation = False
        self._refusals = {}
        self._unread = {}

        entities, segmentation, handle = self._phase_one(
            transcription, artifact_qid
        )
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

        if entities is not None:
            records = {record.id: record for record in entities}
            chunk_groups: list[list] = pack_entities(
                entities, text_lines, self.budget
            )

            def build_prompt(group: list, inline: bool) -> str:
                return self._entity_chunk_prompt(
                    group,
                    entities,
                    text_lines,
                    transcription,
                    artifact_qid,
                    inline,
                )

            def transform(claim: dict, chunk_id: str) -> dict:
                return normalize_to_manifest(claim, records)

        else:
            chunk_groups = pack_units(
                segmentation.units, text_lines, self.budget
            )

            def build_prompt(group: list, inline: bool) -> str:
                return self._chunk_prompt(
                    group, text_lines, transcription, artifact_qid, inline
                )

            def transform(claim: dict, chunk_id: str) -> dict:
                return prefix_local_ids(claim, chunk_id)

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
            shared state goes through the lock."""
            try:
                claims, response = self._run_chunk(
                    build_prompt(group, False),
                    handle,
                    build_prompt(group, inline_document),
                )
            except ConversationChanged:
                raise
            except Exception as exc:
                with self._state_lock:
                    self.failed_chunks.append(chunk_id)
                    self.failure_reasons[chunk_id] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                self._note_chunk_done(total, ok=False)
                return None
            self._note_chunk_done(total, ok=True, claims=len(claims))
            return _Chunk(
                claims,
                parse_chunk_gaps(response),
                parse_chunk_unread(response),
            )

        try:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                outcomes = list(pool.map(run_one, chunk_ids, chunk_groups))
        finally:
            self._close_conversation(handle)
        self.failed_chunks.sort()

        batch, extras = self._merge(
            chunk_ids, outcomes, transform, entities, segmentation, text_lines
        )
        self._refuse_empty(batch, chunks=total)
        self.attention = self._attention(batch, chunks=total)
        if not self.failed_chunks:
            self._write_side_outputs(extras)
        return batch

    def _close_conversation(self, handle: Any) -> None:
        """Let the client release a conversation no chunk forks any more
        (the claude-code engine keeps its session transcript until then)."""
        close = getattr(self.client, "close_conversation", None)
        if handle is not None and callable(close):
            close(handle)

    def _attention(self, batch: ClaimBatch, *, chunks: int) -> list[str]:
        """What a reader of the batch should know: each fallback tier
        taken, with its reason, and why a batch without claims is empty.
        Only a claim-free batch gets this far without claims
        (_refuse_empty)."""
        reasons = []
        if self.used_fallback_manifest:
            reasons.append(
                "entity manifest fell back to line-range segmentation: "
                f"{self.fallback['manifest']}"
            )
        if self.used_fallback_segmentation:
            reasons.append(
                "segmentation fell back to line windows: "
                f"{self.fallback['segmentation']}"
            )
        if not batch.claims:
            reasons.append(f"no claims extracted: {self._why_empty(chunks)}")
        return reasons

    def _why_empty(self, chunks: int) -> str:
        """Why an extraction holds no claims when no chunk failed, none
        gave an object the parser could not read and validation rejected
        nothing: what the answers it accepted say, naming the refused
        manifest when the line-range tier stood in for it."""
        if chunks:
            plural = "" if chunks == 1 else "s"
            found = f"{chunks} chunk{plural} answered with none"
        elif self.used_fallback_manifest:
            found = "every unit in the segmentation is extract=false"
        else:
            return "every entity in the manifest is extract=false"
        if not self.used_fallback_manifest:
            return found
        return (
            "the entity manifest was refused "
            f"({_brief(self.fallback['manifest'])}), and on the line-range "
            f"tier {found}"
        )

    def _write_side_outputs(self, extras: dict[str, str]) -> None:
        if self.state_dir is None:
            return
        for name in SIDE_OUTPUTS:
            path = self.state_dir / name
            if name in extras:
                atomic_write_text(path, extras[name])
            else:
                path.unlink(missing_ok=True)

    def _refuse_empty(self, batch: ClaimBatch, *, chunks: int) -> None:
        """Raise ExtractionFailed when the batch holds no claims because
        the model's answers could not be used, rather than hand on an empty
        batch that reads as a claim-free document's:

        - chunks failed after their retries;
        - the chunks' answers held objects the parser could not read as
          claims (parse_chunk_unread), such as claims wrapped in one
          object or written with other keys;
        - validation rejected every claim the chunks gave.

        The message leads with the outcome and every cause, then names any
        fallback tier taken, with why each attempt at the turn it stood in
        for failed, each reason cut short (REASON_LIMIT). A failure keeps
        no view, so each run makes the extraction again, from the LLM
        cache at no engine cost, and a parser fix reaches it.

        Otherwise a batch without claims is claim-free, on the word of the
        answers the extractor accepted, the line-range tier's included
        when it stood in for a refused manifest (ADR-0032 §5), and
        `attention` says why."""
        if batch.claims:
            return
        causes = []
        if self.failed_chunks:
            first = self.failed_chunks[0]
            plural = "" if chunks == 1 else "s"
            causes.append(
                f"{len(self.failed_chunks)} of {chunks} chunk{plural} failed "
                f"({first}: {_brief(self.failure_reasons.get(first, ''))})"
            )
        if self._unread:
            count = sum(len(objects) for objects in self._unread.values())
            first, objects = next(iter(self._unread.items()))
            keys = _brief(", ".join(map(str, objects[0])))
            plural = "" if count == 1 else "s"
            causes.append(
                f"{count} unreadable object{plural} in the chunk answers "
                f"({first}: keys {keys})"
            )
        if self._rejects:
            causes.append(
                f"validation rejected every claim ({len(self._rejects)})"
            )
        if not causes:
            return
        message = "no claims extracted: " + "; ".join(causes)
        if self.used_fallback_manifest:
            message += (
                "; the entity manifest was refused "
                f"({self._attempts('manifest')})"
            )
        if self.used_fallback_segmentation:
            message += (
                f", and the segmentation ({self._attempts('segmentation')})"
            )
        raise ExtractionFailed(message)

    def _attempts(self, tier: str) -> str:
        """Why each attempt at a refused turn failed, cut short: once for
        them all when every attempt failed the same way, else numbered."""
        reasons = self._refusals[tier]
        if len(set(reasons)) == 1:
            return f"every attempt: {_brief(reasons[0])}"
        return "; ".join(
            f"{number}: {_brief(reason)}"
            for number, reason in enumerate(reasons, start=1)
        )

    def _merge(
        self,
        chunk_ids: list[str],
        outcomes: list[_Chunk | None],
        transform: Callable[[dict, str], dict],
        entities: list[EntityRecord] | None,
        segmentation: DocumentSegmentation | None,
        text_lines: list[str],
    ) -> tuple[ClaimBatch, dict[str, str]]:
        """Merge chunk claims into one validated batch, with rejects, gaps
        and the coverage report as side outputs."""
        raw_claims: list[dict] = []
        transform_rejects: list[dict] = []
        gaps: list[dict] = []
        unread: dict[str, list[dict]] = {}
        for chunk_id, chunk in zip(chunk_ids, outcomes, strict=True):
            if chunk is None:
                continue
            if chunk.unread:
                unread[chunk_id] = chunk.unread
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
        self._unread = unread

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
        batch, rejects = salvage_valid_claims(
            deduped,
            run_metadata(self.client.model_identifier).model_dump(mode="json"),
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
        # the extraction's health, on either tier, for the checks that read
        # a stored batch: how many chunks it asked, what they came to, and
        # each fallback tier it took, with the reason
        coverage |= {
            "chunks": len(chunk_ids),
            "claims": len(batch.claims),
            "rejected_claims": len(rejects),
            "unread_objects": sum(len(found) for found in unread.values()),
            "fallback": dict(self.fallback),
        }
        extras["coverage.json"] = json.dumps(coverage, indent=2)
        return batch, extras

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
    """One chunk's parsed claims and gaps, and the objects in its answer
    that held something the parser could not read (parse_chunk_unread):
    their count is what tells an unreadable answer from one with no
    claims."""

    claims: list[dict]
    gaps: list[dict]
    unread: list[dict]
