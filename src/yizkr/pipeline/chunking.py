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
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yizkr.claims import (
    ARTIFACT_KIND,
    MANIFEST_REASON,
    REJECT_REASONS,
    SCHEMA_REASON,
    STRUCTURED_KINDS,
    ArtifactRef,
    Claim,
    ClaimBatch,
    EntityRecord,
    JudgementNote,
    PersonStructure,
    SourceReference,
    drop_containers,
    load_vocabulary,
    set_aside_structure,
    structure_problems,
)
from yizkr.core.verbatim import Transcript, words
from yizkr.model.schema import load_acceptance
from yizkr.pipeline import preparation
from yizkr.pipeline.extract import (
    ExtractionFailed,
    _normalize_claim_shape,
    cite_artifact,
    claim_schema_prompt_text,
    document_block,
    entity_kind_prompt_lines,
    language_on_source,
    placed_by,
    run_metadata,
    salvage_valid_claims,
    scan_json_answer,
    scan_json_objects,
    vocabulary_prompt_lines,
)
from yizkr.pipeline.llm import (
    CUT_OFF_AT_LIMIT,
    ClassifierRefused,
    ConversationalLlmClient,
    ConversationChanged,
    LlmClient,
    OutputLimitReached,
    at_output_limit,
    for_attempt,
    reject_response,
)
from yizkr.pipeline.prompts import load_prompt
from yizkr.pipeline.review import claim_id
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


SEGMENTATION_PROMPT = load_prompt("segmentation.md")


CHUNK_PROMPT = load_prompt("chunk-extraction.md").rstrip("\n")


LINE_ASSIGNMENT_BLOCK = load_prompt("line-assignment.md").rstrip("\n")


ENTITY_ASSIGNMENT_BLOCK = load_prompt("entity-assignment.md").rstrip("\n")


ENTITY_MANIFEST_PROMPT = load_prompt("entity-manifest.md")


# Appended to a chunk's prompt when every claim of its answer lacks the
# same required field (missing_field): the chunk is asked again once, as
# attempt 1 of the call (llm.for_attempt), naming the field.
MISSING_FIELD_NOTE = "\n\n" + load_prompt("missing-field.md")


class ChunkIncomplete(Exception):
    """A chunk answer cut off: stopped at the engine's output limit, or
    without its terminal sentinel."""


def parse_chunk_response(
    response: str, *, cut_off: bool = False
) -> list[dict]:
    """The claims of a chunk answer: NDJSON claim lines, then the terminal
    sentinel, a line whose `done` is true (`{"done": true}`), which shows
    the answer ran to its end. What else the sentinel carries is not read
    here (parse_chunk_unread reads it).

    An answer is cut off when its engine stopped it at its output limit
    (`cut_off`, llm.at_output_limit), whatever it holds, the sentinel
    included, or when it has no sentinel (silent truncation). Either
    raises ChunkIncomplete, which is cheap to retry, unlike a
    whole-document retry.

    Like every reader of a chunk answer, it reads a single trailing comma
    before a closing bracket or brace as JSON syntax (scan_json_answer).
    """
    claims: list[dict] = []
    finished = False
    for obj in scan_json_answer(response).objects:
        if obj.get("done") is True:
            finished = True
        elif "subject" in obj and "property" in obj:
            claims.append(obj)
    if cut_off:
        raise ChunkIncomplete(CUT_OFF_AT_LIMIT)
    if not finished:
        raise ChunkIncomplete("no terminal sentinel — response truncated?")
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
    the sentinel's job).
    """
    gaps: list[dict] = []
    for obj in scan_json_answer(response).objects:
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
    for obj in scan_json_answer(response).objects:
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
    """Deep-copy a claim dict and apply `fix` to every local ref in it:
    its subject's, its item value's and its qualifiers' item values'. A
    local ref the model put in the source is left as it is: the extractor
    replaces the source artifact afterwards (cite_artifact)."""
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
SIDE_OUTPUTS = (
    "coverage.json",
    "rejects.json",
    "gaps.ndjson",
    # each place container checked, with its evidence and the line it was
    # compared with (claims.drop_containers): local, never in a report
    "containers.json",
    # each claim whose excerpt its transcription does not hold, with the
    # line nearest to it (excerpt_audit): local, never in a report
    "quotes.json",
    # the statement about the document's preparation that the reading
    # quoted (yizkr.pipeline.preparation): local, never in a report
    "preparation.json",
)


# the reading an extractor that reads no statement about the document's
# preparation hands the merge (ChunkedClaimExtractor._merge)
_UNREAD = object()
# the `preparation` value of a manifest answer that gives none, and of an
# extraction that took no manifest
_ABSENT = object()


class ChunkedClaimExtractor:
    """Entity manifest -> packed chunks -> per-chunk calls -> merge.

    Every call goes to `client`. A chunk response cut off, at the engine's
    output limit (llm.at_output_limit) or before its terminal sentinel, is
    rejected through it (llm.reject_response): behind the LLM cache
    (ADR-0032) that makes the retry, in this run or the next, ask the
    engine again rather than be answered with the same text, and a chunk
    that failed every retry is asked again in the next run. A
    rejected answer the cache kept from an earlier call spends no retry:
    the engine has not answered the chunk yet, and has as many tries as
    it would without the cache.
    A chunk whose answer gives no claim with a field the claim schema
    requires, the same field for every claim (missing_field), is asked
    again once, naming the field, as a second attempt keyed apart
    (`_ask_again`), and `retried_chunks` counts such chunks. A call the
    safety classifier refused (llm.ClassifierRefused), a chunk's, its
    fork's, the manifest's or the segmentation's, is never asked again,
    and a refused fork does not drop to inline context: the chunk, or
    the extraction, fails with the refusal's class in its reason.
    The entity manifest and the segmentation are asked in two attempts
    each, keyed apart behind the cache (llm.for_attempt): a refused
    answer is kept, so a later run replays the same attempts and takes the
    same fallback. An attempt the engine gave up on at its output limit is
    kept alike, as a give-up, so a later run gives up on it again without
    asking the engine, and takes the same fallback. When a conversation behind
    the cache has to be opened again and answers differently
    (llm.ConversationChanged), the extraction starts over from the new
    answer.

    `state_dir`, when given, receives the advisory side outputs of a
    complete extraction: `coverage.json`, and `rejects.json`,
    `gaps.ndjson`, `containers.json`, `quotes.json` and
    `preparation.json` when there are any (a file the extraction no
    longer has anything for is removed).

    With `read_preparation`, the entity manifest also reads the
    document's statement about how its own text was prepared, the one a
    curator pointed to (`pointer`) when one did
    (yizkr.pipeline.preparation), and the merge puts the machine-assisted
    marker on every claim read from the text it covers, after dropping
    any marker a chunk answer gave (ADR-0008).

    Matches LlmClaimExtractor's extract() signature so the CLI treats the
    strategies interchangeably. After extract(), `failed_chunks` lists
    chunks that produced nothing (after their retries, or at once when
    refused), with their reasons in `failure_reasons` — the batch is still
    returned (partial progress is the point) and the caller decides how
    loudly to fail — and `attention` says what a reader of the batch should
    know: a fallback tier, or why it holds no claims. An extraction that
    would end with no claims for want of a usable answer raises
    ExtractionFailed instead (`_refuse_empty`), so a batch without claims
    is returned only when the answers the extractor accepted say the
    document holds none.
    """

    def __init__(
        self,
        client: LlmClient,
        state_dir: Path | None = None,
        budget: int = DEFAULT_CHUNK_BUDGET,
        retries: int = 2,
        concurrency: int = 1,
        log: Callable[[str], None] | None = None,
        read_preparation: bool = False,
        pointer: str | None = None,
    ) -> None:
        self.client = client
        self.read_preparation = read_preparation
        self.pointer = pointer
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
        # chunks asked again because every claim of their answer lacked
        # the same required field (_ask_again)
        self.retried_chunks = 0
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
        # the entity manifest's mention records and judgement notes set
        # aside, counted by the category of each reason, and the number of
        # entities whose mention record was set aside
        self._set_aside: dict[str, int] = {}
        self._set_aside_records = 0
        # each place container the manifest's records gave, checked and
        # kept or dropped alone (claims.drop_containers)
        self._containers: list[dict] = []
        # the trailing commas read in the manifest answer the extraction
        # used (scan_json_answer)
        self._trailing_commas = 0
        # the entities the manifest dropped for a kind no extraction makes
        # (`birth`, `death`), by id, with that kind (ADR-0040 §7)
        self._dropped_kinds: dict[str, str] = {}
        # each claim whose excerpt the transcription does not hold
        # (excerpt_audit)
        self._quotes: list[dict] = []
        # what settling the manifest's names counted (_settle_names)
        self._appellations = {"labels": 0, "label_names": 0, "parts": 0}
        # how chunk calls saw the document: fork, inline or compressed
        self.context_mode: str | None = None
        # the claims the merge marked machine-assisted, and those it could
        # not place within or outside the passages the statement covers
        self._marked = {"marked": 0, "straddling": 0, "unlocated": 0}
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
        each gives its own reason. An answer the engine gave up on at its
        output limit (OutputLimitReached) is an answer refused, not a call
        unanswered: the engine answered, past its limit. Behind the cache
        it is kept under its attempt, as a refused answer is, so a later
        run raises it again without asking the engine. No handle
        survives a refused answer: forking would plant known-bad output in
        every chunk's context.

        A call the safety classifier refused (llm.ClassifierRefused) is not
        asked again: the refusal is the platform's decision about the
        call's content, which a second attempt sends unchanged. It ends
        the turn as a call unanswered, whatever an earlier attempt gave,
        so no fallback tier stands in for it."""
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
            except OutputLimitReached:
                answered = True
                reasons.append(CUT_OFF_AT_LIMIT)
                handle = None
                continue
            except ClassifierRefused as exc:
                reasons.append(f"{type(exc).__name__}: {exc}")
                return None, None, reasons, False
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
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> tuple[_Manifest | None, Any, list[str], bool]:
        """(manifest, conversation handle, why each attempt failed, any
        call answered).

        No manifest means it was unusable in both attempts; the caller
        falls back to line-range segmentation. A manifest of no entities,
        the engine's word that the document names nothing, is usable, and
        the first attempt that gives one ends the turn."""
        line_count = len(transcription.text.splitlines())
        kinds, kind_descriptions = entity_kind_prompt_lines()
        prompt = ENTITY_MANIFEST_PROMPT.format(
            kinds=kinds,
            kind_descriptions=kind_descriptions,
            artifact_sha1=artifact.sha1,
            language=transcription.language,
            numbered_text=number_lines(transcription.prompt_text()),
        )
        if self.read_preparation:
            prompt += preparation.manifest_block(self.pointer)
        return self._first_turn(
            prompt,
            lambda response: self._parse_entity_manifest(
                response, line_count, transcription.text
            ),
        )

    @staticmethod
    def _parse_entity_manifest(
        response: str, line_count: int, text: str | None = None
    ) -> tuple[_Manifest | None, list[str]]:
        """(manifest, problems); the manifest is None when the answer holds
        no entity list. Only a list, or null, is one, and any other value
        is unusable. An empty one, or null, is the engine's word that the
        document names nothing: a manifest of no entities, accepted as it
        stands, so no chunk is asked and no fallback tier is taken. A
        record of a kind that is no manifest item, or of an artifact kind,
        which no extraction names (ADR-0039 §10.1(b)), is a problem that
        makes the answer unusable. A record of a kind only a curator makes
        (`birth`, `death`, ADR-0040 §7) is dropped alone, never failing the
        turn, and kept by id with its kind, so its claims are rejected
        under a reason of their own (salvage_valid_claims).

        An entity's mention record or judgement note that fails validation
        is set aside, never repaired and never the reason the manifest is
        refused (ADR-0035 §5): the entity keeps its label, aliases and
        kind, and the manifest counts each reason by its category, and the
        records set aside (one record can give several reasons, and one
        whose only reasons are name parts dropped for an empty string or
        entries read as their text stands). A place's container that
        cannot stand, or that has the wrong shape (`malformed`), is
        dropped alone, its record standing (claims.drop_containers): its
        evidence is looked for in `text`,
        the transcription, when given, and each container checked is kept
        for the local audit. A record that stood only by a name read as
        its label and a container (claims.label_container) is checked
        again once its containers are, and set aside, its containers left
        out of the audit, when that container was dropped: the container
        must stand on its evidence. A trailing comma before a closing
        bracket or brace is read as JSON syntax, and the manifest counts
        each one (scan_json_answer)."""
        _, item_keys = load_vocabulary()
        acceptance = load_acceptance()
        artifact_kinds = acceptance.artifact_kinds
        scan = scan_json_answer(response)
        for obj in scan.objects:
            if "entities" not in obj:
                continue
            listed = obj["entities"]
            if listed is not None and not isinstance(listed, list):
                return None, [
                    f"'entities' is not a list ({type(listed).__name__})"
                ]
            set_aside: dict[str, int] = {}
            set_aside_records = 0
            transcript = None if text is None else Transcript(text)
            containers: list[dict] = []
            try:
                records = []
                for raw in listed or []:
                    data = dict(raw)
                    data.pop("qid", None)  # resolution state, never model-set
                    data, reasons = _set_aside_note(data)
                    malformed: list[dict] = []
                    data, more = set_aside_structure(data, malformed)
                    set_aside_records += bool(more) and "structure" not in data
                    data, checked = drop_containers(
                        data, transcript, malformed
                    )
                    if "structure" in data and (
                        late := structure_problems(
                            EntityRecord.model_validate(data)
                        )
                    ):
                        # a name read as its label and a container that
                        # was then dropped
                        data = {
                            k: v for k, v in data.items() if k != "structure"
                        }
                        more += late
                        set_aside_records += 1
                        checked = []
                    containers += checked
                    for reason in reasons + more:
                        category = reason.split(":", 1)[0]
                        set_aside[category] = set_aside.get(category, 0) + 1
                    records.append(EntityRecord.model_validate(data))
            except Exception as exc:
                return None, [str(exc)[:300]]
            problems: list[str] = []
            # an id given twice is a problem whatever the kinds, so it is
            # found before an entity of a kind only a curator makes is
            # dropped (which would take both with it)
            seen: set[str] = set()
            for record in records:
                if record.id in seen:
                    problems.append(f"duplicate entity id {record.id!r}")
                seen.add(record.id)
            dropped = {
                record.id: record.kind
                for record in records
                if record.kind in acceptance.unextracted_kinds
            }
            records = [
                record for record in records if record.id not in dropped
            ]
            for record in records:
                if record.kind not in item_keys:
                    problems.append(
                        f"entity {record.id}: unknown kind {record.kind!r}"
                    )
                elif record.kind in artifact_kinds:
                    # a document the text mentions is an unclassified
                    # entity with a type hint (ADR-0039 §10.1(b))
                    problems.append(
                        f"entity {record.id}: artifact kind "
                        f"{record.kind!r}, which no extraction names "
                        f"[{ARTIFACT_KIND}]"
                    )
                # mention lines are best-effort: clamp, don't reject
                record.mention_lines = [
                    number
                    for number in record.mention_lines
                    if 1 <= number <= line_count
                ]
            return (
                _Manifest(
                    records,
                    set_aside,
                    set_aside_records,
                    containers,
                    scan.trailing_commas,
                    dropped,
                    obj.get(preparation.KEY, _ABSENT),
                ),
                problems,
            )
        return None, ["no JSON object with an 'entities' array"]

    # -- phase 1b: line-range segmentation (fallback tier) -----------------

    def _segment(
        self, transcription: Transcription, artifact: ArtifactRef
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
            artifact_sha1=artifact.sha1,
            language=transcription.language,
            numbered_text=number_lines(transcription.prompt_text()),
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
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> tuple[list[EntityRecord] | None, DocumentSegmentation | None, Any]:
        """(entities, segmentation, conversation handle): the entity
        manifest, or the line-range fallback when the manifest was
        unusable.

        A fallback tier is taken only when the engine answered, an answer
        it gave up on at its output limit included (_first_turn). When
        every call of a tier raised otherwise (an engine outage),
        extraction fails instead, so the document is retried rather than
        extracted by a degraded path the engine never chose.
        """
        self._log("  scanning document for entities...")
        manifest, handle, reasons, answered = self._entity_manifest(
            transcription, artifact
        )
        # the manifest answers refused for naming an artifact kind, whether
        # a later attempt mended the turn or not (ADR-0039 §10.1(b))
        self._manifest_artifact_kind = sum(
            f"[{ARTIFACT_KIND}]" in reason for reason in reasons
        )
        if manifest is not None:
            self._set_aside = manifest.set_aside
            self._set_aside_records = manifest.set_aside_records
            self._containers = manifest.containers
            self._trailing_commas = manifest.trailing_commas
            self._dropped_kinds = manifest.dropped_kinds
            self._preparation_answer = manifest.preparation
            return manifest.entities, None, handle
        if not answered:
            raise RuntimeError(
                f"entity manifest: no engine response ({reasons[-1]})"
            )
        self.used_fallback_manifest = True
        self.fallback["manifest"] = reasons[-1]
        self._refusals["manifest"] = reasons
        segmentation, handle, reasons, answered = self._segment(
            transcription, artifact
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
        self, transcription: Transcription, artifact: ArtifactRef, inline: bool
    ) -> str:
        if not inline:
            return ""
        return (
            f"\nFull transcription of artifact sha1:{artifact.sha1} for "
            f"context (extract ONLY for your assignment):\n\n"
            f"{number_lines(transcription.prompt_text())}\n"
        )

    def _render_chunk_prompt(
        self,
        assignment_block: str,
        transcription: Transcription,
        artifact: ArtifactRef,
        inline_document: bool,
    ) -> str:
        property_keys, item_keys = vocabulary_prompt_lines()
        return CHUNK_PROMPT.format(
            artifact_sha1=artifact.sha1,
            language=transcription.language,
            property_keys=property_keys,
            item_keys=item_keys,
            assignment_block=assignment_block,
            document_paragraphs=document_block(transcription, artifact),
            schema=claim_schema_prompt_text(),
            document_block=self._document_block(
                transcription, artifact, inline_document
            ),
        )

    def _chunk_prompt(
        self,
        chunk_units: list[SegmentationUnit],
        text_lines: list[str],
        transcription: Transcription,
        artifact: ArtifactRef,
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
            artifact,
            inline_document,
        )

    def _entity_chunk_prompt(
        self,
        chunk_entities: list[EntityRecord],
        all_entities: list[EntityRecord],
        text_lines: list[str],
        transcription: Transcription,
        artifact: ArtifactRef,
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
                roster_block=roster_block,
                entities_block=entities_block,
                first_entity=first_assigned(all_entities),
            ),
            transcription,
            artifact,
            inline_document,
        )

    @staticmethod
    def _fork(client: Any, handle: Any, prompt: str) -> str | None:
        """A chunk's answer from a fork of the conversation `handle` names,
        made through `client`, or None when the fork machinery failed
        (session gone, old CLI): the chunk then drops to inline context. A
        changed conversation, an answer the engine gave up on at its
        output limit and a refusal of the call (ClassifierRefused) are no
        failure of the fork machinery: they propagate, so a refused fork
        is never asked again inline."""
        try:
            return client.fork(handle, prompt)
        except (ConversationChanged, OutputLimitReached, ClassifierRefused):
            raise
        except Exception:
            return None

    def _run_chunk(
        self,
        prompt: str,
        handle: Any,
        fallback_prompt: str,
        missing: Callable[[list[dict]], str | None] | None = None,
    ) -> tuple[list[dict], str]:
        """(claims, response) for one chunk: call (fork if possible) and
        parse, with retries. A response cut off, at the engine's output
        limit or before its sentinel (parse_chunk_response), is rejected,
        so the retry asks again; one the client kept from an earlier call
        is asked for once more without spending a retry
        (llm.reject_response). A call the engine gave up on at its output
        limit (OutputLimitReached) gave no response to reject: it spends
        its try as a cut-off one does, and its reason is the same. A
        conversation that changed (ConversationChanged) is no failure of
        the chunk: it ends the extraction's pass, which starts over. A
        call the safety classifier refused (ClassifierRefused) fails the
        chunk at once, with no retry and no inline fallback: the refusal
        is the platform's decision about the call's content, which asking
        again sends unchanged.

        `missing`, when given, names the required field every claim of
        an answer lacks (missing_field), or None; an answer it names one
        for is asked again once (_ask_again)."""
        last_error: Exception = ChunkIncomplete("not attempted")
        tries = 1 + self.retries
        kept_rejected = False
        while tries:
            tries -= 1
            try:
                response = None
                if handle is not None:
                    response = self._fork(self.client, handle, prompt)
                    if response is None:
                        handle = None
                if response is None:
                    response = self.client.complete(fallback_prompt)
            except (ConversationChanged, ClassifierRefused):
                raise
            except OutputLimitReached:
                last_error = ChunkIncomplete(CUT_OFF_AT_LIMIT)
                continue
            except Exception as exc:
                last_error = exc
                continue
            cut = at_output_limit(self.client, response)
            try:
                claims = parse_chunk_response(response, cut_off=cut)
            except ChunkIncomplete as exc:
                last_error = exc
                kept = reject_response(self.client, response, f"chunk: {exc}")
                if kept and not kept_rejected:
                    kept_rejected = True
                    tries += 1
                continue
            field = None if missing is None else missing(claims)
            if field is None:
                return claims, response
            return self._ask_again(
                prompt, handle, fallback_prompt, field, claims, response
            )
        raise last_error

    def _ask_again(
        self,
        prompt: str,
        handle: Any,
        fallback_prompt: str,
        field: str,
        claims: list[dict],
        response: str,
    ) -> tuple[list[dict], str]:
        """(claims, response) for a chunk whose answer, `claims` as parsed
        from `response`, gives no claim with the required `field`: the
        chunk asked again once, its prompt naming the field
        (MISSING_FIELD_NOTE), as attempt 1 of the call (llm.for_attempt),
        in the same conversation the answer came from, or inline when it
        came inline. Behind the LLM cache the first answer is kept and the
        second is keyed apart, so a later run replays both.

        The second answer replaces the first, and is validated as usual
        in the merge. When the second call gives none (it fails, the
        engine gives up on it at its output limit, it is cut off, or the
        safety classifier refuses it, which is never asked again), the
        first answer stands. A cut-off second answer is
        rejected, and one the client kept from an earlier call is asked
        for once more (llm.reject_response), so a later run asks the
        engine again; a give-up at the output limit is kept under the
        attempt's key behind the LLM cache, so a later run gives up on it
        again without a call, and the first answer stands again."""
        with self._state_lock:
            self.retried_chunks += 1
        note = MISSING_FIELD_NOTE.format(count=len(claims), field=field)
        client = for_attempt(self.client, 1)
        for _ask in range(2):
            try:
                again = None
                if handle is not None:
                    again = self._fork(client, handle, prompt + note)
                    if again is None:
                        handle = None
                if again is None:
                    again = client.complete(fallback_prompt + note)
            except ConversationChanged:
                raise
            except ClassifierRefused:
                break
            except Exception:
                break
            cut = at_output_limit(client, again)
            try:
                return parse_chunk_response(again, cut_off=cut), again
            except ChunkIncomplete as exc:
                if not reject_response(client, again, f"chunk: {exc}"):
                    break
        return claims, response

    # -- orchestration ---------------------------------------------------

    def extract(
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> ClaimBatch:
        """The document's batch, every claim citing `artifact`, the
        document's content key, by which the prompts name the document too,
        and the page number printed on the page it cites, where the
        transcription's record gives one (cite_artifact). The prompts
        carry the transcription's text with each printed page number
        beside its page's marker, every line where it is in the text
        (Transcription.prompt_text). When its conversation has to be
        opened again and answers differently (ConversationChanged), the
        pass is void and the extraction starts over from the new answer,
        at most twice."""
        for _pass in range(2):
            try:
                return self._extract(transcription, artifact)
            except ConversationChanged as exc:
                self._log(f"  {exc}: extracting again from the new answer")
        return self._extract(transcription, artifact)

    def _extract(
        self, transcription: Transcription, artifact: ArtifactRef
    ) -> ClaimBatch:
        text_lines = transcription.prompt_text().splitlines()
        self.failed_chunks = []
        self.failure_reasons = {}
        self.retried_chunks = 0
        self.gaps = []
        self.fallback = {}
        self.attention = []
        self._rejects = []
        self.used_fallback_manifest = False
        self.used_fallback_segmentation = False
        self._refusals = {}
        self._manifest_artifact_kind = 0
        self._unread = {}
        self._set_aside = {}
        self._set_aside_records = 0
        self._containers = []
        self._trailing_commas = 0
        self._dropped_kinds = {}
        self._quotes = []
        self._appellations = {"labels": 0, "label_names": 0, "parts": 0}

        self._preparation_answer = _ABSENT
        if self.read_preparation:
            # a pointer names the document's words, or the extraction
            # fails before anything is asked
            preparation.pointed_lines(
                self.pointer, Transcript(transcription.text)
            )

        entities, segmentation, handle = self._phase_one(
            transcription, artifact
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
                    artifact,
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
                    group, text_lines, transcription, artifact, inline
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

        def missing(claims: list[dict]) -> str | None:
            """The required field every claim of a chunk answer lacks, the
            artifact cited as the merge cites it (missing_field), or
            None."""
            return missing_field(cite_artifact(claims, artifact))

        def run_one(chunk_id: str, group: list) -> _Chunk | None:
            """One chunk end to end, or None on failure. Thread-safe:
            shared state goes through the lock."""
            try:
                claims, response = self._run_chunk(
                    build_prompt(group, False),
                    handle,
                    build_prompt(group, inline_document),
                    missing,
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
                scan_json_answer(response).trailing_commas,
            )

        try:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                outcomes = list(pool.map(run_one, chunk_ids, chunk_groups))
        finally:
            self._close_conversation(handle)
        self.failed_chunks.sort()

        reading, read_reasons = self._reading(transcription)
        batch, extras = self._merge(
            chunk_ids,
            outcomes,
            transform,
            entities,
            segmentation,
            text_lines,
            artifact,
            transcription.printed_pages(),
            placed_by(transcription, artifact),
            transcription.text,
            reading,
            preparation.statement_source(
                None if reading is _UNREAD else reading,
                transcription,
                artifact,
            ),
        )
        self._refuse_empty(batch, chunks=total)
        self.attention = self._attention(batch, chunks=total)
        if self.read_preparation:
            self.attention += preparation.attention(
                reading, read_reasons, self._marked
            )
        if not self.failed_chunks:
            self._write_side_outputs(extras)
        return batch

    def _reading(self, transcription: Transcription) -> tuple[Any, list[str]]:
        """(reading, why it is unread) of the document's statement about
        its preparation, from the `preparation` value of the manifest
        answer the extraction used (preparation.parse): `_UNREAD` for an
        extractor that reads none; None, unread, for an answer without
        the value, or an extraction that fell back from the manifest,
        with a reason only where a curator pointed to the statement."""
        if not self.read_preparation:
            return _UNREAD, []
        answer = self._preparation_answer
        if answer is _ABSENT:
            if self.pointer is None:
                return None, []
            return None, [
                "the entity manifest gave no reading of the statement"
            ]
        return preparation.parse(
            answer,
            Transcript(transcription.text),
            len(transcription.text.splitlines()),
            self.pointer,
        )

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
            reasons.append(
                "no claims extracted: "
                f"{self._why_empty(chunks, named=len(batch.entities))}"
            )
        return reasons

    def _why_empty(self, chunks: int, *, named: int) -> str:
        """Why an extraction holds no claims when no chunk failed, none
        gave an object the parser could not read and validation rejected
        nothing: what the answers it accepted say, naming the refused
        manifest when the line-range tier stood in for it. `named` is the
        number of entities the batch holds, none when the manifest named
        nothing."""
        if chunks:
            plural = "" if chunks == 1 else "s"
            found = f"{chunks} chunk{plural} answered with none"
        elif self.used_fallback_manifest:
            found = "every unit in the segmentation is extract=false"
        elif not named:
            return "the entity manifest names no entity"
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

        - chunks failed;
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
        artifact: ArtifactRef,
        printed_pages: dict[int, str],
        placed: Mapping[str, Collection[str]] | None = None,
        text: str | None = None,
        reading: Any = _UNREAD,
        stated: SourceReference | None = None,
    ) -> tuple[ClaimBatch, dict[str, str]]:
        """Merge chunk claims into one validated batch, each claim citing
        `artifact`, the page number printed on its page where
        `printed_pages` gives one, and each once (cite_artifact), validated
        with the artifacts the document places (`placed`,
        salvage_valid_claims), with rejects, gaps and the coverage report
        as side outputs: the coverage counts the rejects by reason
        (rejected_by_reason), and, when `text`, the transcription, is
        given, the claims whose excerpt it does not hold (excerpt_audit),
        each kept in the side output `quotes.json`. Such a claim stays
        in the batch: the miss is counted, never a reject.

        `reading`, the document's statement about its preparation as the
        manifest read it (preparation.parse; None when unread), marks
        the claims read from the text it covers, located in `text`, each
        with `stated`, where the document makes the statement
        (preparation.statement_source), as its circumstance source, once
        every marker the chunk answers gave is dropped, and is counted in
        the coverage (`machine_assisted`) and, when it
        quoted a statement, kept in the side output `preparation.json`.
        `_UNREAD`, for an extractor that reads none, leaves the claims as
        the chunks gave them."""
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
                            "reasons": [SCHEMA_REASON],
                        }
                    )
            gaps.extend(chunk.gaps)
        self.gaps = gaps
        self._unread = unread

        dropped = 0
        if reading is not _UNREAD:
            raw_claims, dropped = preparation.drop_markers(raw_claims)
        deduped = cite_artifact(raw_claims, artifact, printed_pages)
        run = run_metadata(self.client.model_identifier).model_dump(
            mode="json"
        )

        settled_rejects: list[dict] = []
        self._appellations = {"labels": 0, "label_names": 0, "parts": 0}
        if entities is not None:
            settled = _settle_names(deduped, entities, run)
            deduped, entities_payload = settled.claims, settled.entities
            settled_rejects = settled.rejects
            self._appellations = {
                "labels": settled.labels,
                "label_names": settled.label_names,
                "parts": settled.parts,
            }
        else:
            entities_payload = []
        # the claims whose excerpt's language validation finds moved in
        # from `source` (extract._normalize_claim_shape)
        moved = sum(language_on_source(claim) for claim in deduped)
        batch, rejects = salvage_valid_claims(
            deduped,
            run,
            entities=entities_payload,
            placed=placed,
            dropped=self._dropped_kinds,
        )
        rejects = transform_rejects + settled_rejects + rejects
        self._rejects = rejects
        self._marked = {"marked": 0, "straddling": 0, "unlocated": 0}
        if reading is not _UNREAD:
            claims, self._marked = preparation.mark(
                batch.claims, reading, Transcript(text or ""), stated
            )
            batch = batch.model_copy(update={"claims": claims})
        quotes = (
            None
            if text is None
            else excerpt_audit(batch.claims, Transcript(text))
        )
        self._quotes = [] if quotes is None else quotes["missing"]
        extras: dict[str, str] = {}
        if rejects:
            extras["rejects.json"] = json.dumps(rejects, indent=2)
        if gaps:
            extras["gaps.ndjson"] = "\n".join(map(json.dumps, gaps))
        if self._containers:
            extras["containers.json"] = json.dumps(
                self._containers, indent=2, ensure_ascii=False
            )
        if self._quotes:
            extras["quotes.json"] = json.dumps(
                self._quotes, indent=2, ensure_ascii=False
            )
        if entities is not None:
            coverage = self._manifest_coverage(entities, batch)
        else:
            coverage = self._coverage(segmentation, batch, len(text_lines))
        # the extraction's health, on either tier, for the checks that read
        # a stored batch: how many chunks it asked, what they came to, the
        # trailing commas its manifest and chunk answers were read past
        # (scan_json_answer), the claims whose excerpt's language was
        # moved in from `source`, and each fallback tier it took, with the
        # reason
        coverage |= {
            "chunks": len(chunk_ids),
            "claims": len(batch.claims),
            "rejected_claims": len(rejects),
            "rejected_by_reason": rejected_by_reason(rejects),
            "unread_objects": sum(len(found) for found in unread.values()),
            "trailing_commas": self._trailing_commas
            + sum(c.trailing_commas for c in outcomes if c is not None),
            "fallback": dict(self.fallback),
            "manifest_artifact_kind": self._manifest_artifact_kind,
            # the manifest's entities dropped for a kind only a curator
            # makes, by kind (ADR-0040 §7)
            "manifest_vital_kind": dict(
                sorted(Counter(self._dropped_kinds.values()).items())
            ),
            "retried_chunks": self.retried_chunks,
            "excerpt_language_moved": moved,
        }
        if quotes is not None:
            coverage["quotes"] = quotes["counts"]
        if reading is not _UNREAD:
            coverage["machine_assisted"] = preparation.coverage(
                reading, self._marked, dropped
            )
            stated = preparation.side_output(reading)
            if stated is not None:
                extras["preparation.json"] = json.dumps(
                    stated, indent=2, ensure_ascii=False
                )
        extras["coverage.json"] = json.dumps(coverage, indent=2)
        return batch, extras

    def _manifest_coverage(
        self, entities: list[EntityRecord], batch: ClaimBatch
    ) -> dict:
        """Advisory completeness report, entity flavour (ADR-0007), in
        place of the line-range path's unit audit (`_coverage`): the
        manifest entities with zero claims, and counts of the mention
        records and judgement notes the extraction gave (ADR-0035 §5):
        the records given, by status, and the unsplit name forms they
        hold; the mentions recordless; the records set aside, and the
        reasons they and the notes were set aside by, by category; the
        place containers checked, and those dropped alone from records
        that stand, by reason, `malformed` among them
        (claims.drop_containers); the labels the appellation label rule
        replaced with a name (names.appellation_label), the `name`
        claims dropped for restating one, and the record name parts that
        are an appellation; and the notes by category. A mention
        of a kind that takes a record but has none is recordless: its
        record was set aside, or never given. Nothing in it quotes a label."""
        subject_ids = {
            claim.subject.local_id
            for claim in batch.claims
            if hasattr(claim.subject, "local_id")
        }
        structured = [e for e in entities if e.kind in STRUCTURED_KINDS]
        statuses: dict[str, int] = {}
        for entity in structured:
            status = getattr(entity.structure, "status", None)
            if entity.structure is not None and status is not None:
                statuses[status] = statuses.get(status, 0) + 1
        notes: dict[str, int] = {}
        noted = [*entities, *batch.claims]
        for note in (fact.note for fact in noted):
            if note is not None:
                notes[note.category] = notes.get(note.category, 0) + 1
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
            "distinct_subject_ids": sorted(subject_ids),
            "failed_chunks": self.failed_chunks,
            "records": {
                "given": sum(e.structure is not None for e in structured),
                "recordless": sum(e.structure is None for e in structured),
                "by_status": dict(sorted(statuses.items())),
                "unsplit_forms": sum(
                    len(entity.structure.unsplit)
                    for entity in structured
                    if isinstance(entity.structure, PersonStructure)
                ),
                "set_aside_records": self._set_aside_records,
                "set_aside": dict(sorted(self._set_aside.items())),
                "containers_checked": len(self._containers),
                "containers_dropped": dict(
                    sorted(
                        Counter(
                            found["reason"]
                            for found in self._containers
                            if found["reason"] is not None
                        ).items()
                    )
                ),
                "appellation_labels": self._appellations["labels"],
                "appellation_label_names": self._appellations["label_names"],
                "appellation_parts": self._appellations["parts"],
            },
            "notes": dict(sorted(notes.items())),
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


class _Settled(NamedTuple):
    """What settling a manifest's names gives (_settle_names): the claims
    to validate, the entities as the batch carries them, the claims
    rejected already, and the counts coverage.json gives: the labels that
    became a name, the `name` claims dropped for restating one, and the
    record name parts that are an appellation."""

    claims: list[dict]
    entities: list[dict]
    rejects: list[dict]
    labels: int
    label_names: int
    parts: int


def first_assigned(entities: list[EntityRecord]) -> str:
    """The id of the first manifest entity a chunk is assigned
    (pack_entities assigns those marked for extraction alone), whose
    chunk owns the claims about the document itself and its captions;
    "none" when no entity is assigned."""
    return next((entity.id for entity in entities if entity.extract), "none")


def excerpt_audit(claims: Iterable[Claim], transcript: Transcript) -> dict:
    """Each claim's `transcription_excerpt` looked for in the
    transcription it quotes, read as core/verbatim.py reads both (folded,
    whitespace and line breaks collapsed, a hyphen at a line's end
    joined, kept, or kept with a space after it, for an excerpt that
    crosses it, a soft hyphen dropped, whole words only; ADR-0035 §5).

    `counts`, for coverage.json: the claims checked, those with no
    excerpt (or one that holds no word, as a container's evidence that
    holds none is none), and those whose excerpt the transcription
    does not hold, by property. `missing`, for the local audit: each of
    those last, by its claim id (review.claim_id), with its property, its
    excerpt, and the number and text of the transcription's line nearest
    to it (Transcript.nearest; none when no line shares a word with it).
    A miss is a flag, never a reject: the claim stays in its batch."""
    checked = without = 0
    by_property: Counter = Counter()
    missing: list[dict] = []
    for claim in claims:
        excerpt = claim.source.transcription_excerpt
        if excerpt is None or not words(excerpt.text):
            without += 1
            continue
        checked += 1
        if transcript.span(excerpt.text) is not None:
            continue
        by_property[claim.property] += 1
        line = transcript.nearest(excerpt.text)
        missing.append(
            {
                "claim_id": claim_id(claim),
                "property": claim.property,
                "excerpt": excerpt.text,
                "nearest_line": line,
                "nearest_text": None
                if line is None
                else transcript.line(line),
            }
        )
    return {
        "counts": {
            "checked": checked,
            "without_excerpt": without,
            "not_in_source": len(missing),
            "not_in_source_by_property": dict(sorted(by_property.items())),
        },
        "missing": missing,
    }


def missing_field(claims: Iterable[Any]) -> str | None:
    """The required field every one of `claims` lacks, by its path in the
    claim's JSON (`source.transcription_excerpt.language`, `value.year`;
    _field_path), or None: for no claims, or when any claim lacks no
    required field, or when they lack none in common. When they lack
    several in common, the first path in sorted order. Each claim's shape
    is repaired as validation repairs it (salvage_valid_claims) before it
    is checked; `claims` are to be cited as the merge cites them
    (cite_artifact), so that the source's artifact is not missing. The
    local refs the merge settles fill no required field. A claim that
    fails for any other reason, a value of the wrong type or a manifest
    problem, lacks none."""
    common: set[str] | None = None
    for claim in claims:
        lacking: set[str] = set()
        if isinstance(claim, dict):
            shaped = _normalize_claim_shape(claim)
            try:
                Claim.model_validate(shaped)
            except ValidationError as exc:
                lacking = {
                    _field_path(shaped, error["loc"])
                    for error in exc.errors()
                    if error["type"] == "missing"
                }
        common = lacking if common is None else common & lacking
    return min(common) if common else None


def _field_path(claim: dict, loc: Iterable[Any]) -> str:
    """The path of the field a validation error's `loc` names, as the
    claim's JSON spells it: walked along `claim`, a list's index and a
    tagged union's tag (`time` in `value.time.year`), which are no keys
    of the claim, are left out, and the last part, the field missing, is
    kept."""
    parts = list(loc)
    path: list[str] = []
    node: Any = claim
    for number, part in enumerate(parts):
        if isinstance(node, list) and isinstance(part, int):
            node = node[part] if 0 <= part < len(node) else None
        elif isinstance(node, dict) and part in node:
            path.append(str(part))
            node = node[part]
        elif number == len(parts) - 1:
            path.append(str(part))
    return ".".join(path)


def rejected_by_reason(rejects: Iterable[dict]) -> dict[str, int]:
    """How many rejects give each reason code (claims.REJECT_REASONS), in
    that order, each reject counted once under each of its reasons: a
    reject that records none counts as `manifest`."""
    counts = Counter(
        reason
        for reject in rejects
        for reason in (reject.get("reasons") or [MANIFEST_REASON])
    )
    return {
        reason: counts[reason] for reason in REJECT_REASONS if counts[reason]
    }


def _name_text(claim: dict) -> str | None:
    """The text a `name` or `referred_to_as` claim dict gives, if any."""
    value = claim.get("value")
    inner = value.get("value") if isinstance(value, dict) else None
    text = inner.get("text") if isinstance(inner, dict) else None
    return text if isinstance(text, str) else None


def _subject_id(claim: dict) -> str | None:
    subject = claim.get("subject")
    if isinstance(subject, dict) and subject.get("kind") == "local":
        local_id = subject.get("local_id")
        return local_id if isinstance(local_id, str) else None
    return None


def _settle_names(
    claims: list[dict], entities: list[EntityRecord], run: dict
) -> _Settled:
    """Each entity's label set and its alias bag cleaned (ADR-0022 §4,
    ADR-0035 §8; names.normalize_entity_dict), once the chunks' claims
    exist: the deterministic safety net over the model's manifest, by
    the forms the document gives as its appellations (ADR-0018). Those
    are the values of the `referred_to_as` claims about it that validate
    against the manifest as the model gave it (names.appellations_of);
    only those claims and the `name` claims are checked here, each on its
    own. A label that changes changes in every local ref to the entity
    too. A `name` claim on such a person that the first check rejected
    (one restating the model's own label) keeps that reject, and one that
    restates the new label is dropped and counted, never a reject of the
    model's."""
    from yizkr.claims import name_form_key
    from yizkr.pipeline.names import (
        appellation_parts,
        appellations_of,
        normalize_entity_dict,
    )

    given = [record.model_dump() for record in entities]
    valid: list[dict] = []
    first_rejects: dict[int, dict] = {}
    for index, claim in enumerate(claims):
        if claim.get("property") not in ("referred_to_as", "name"):
            continue
        checked, rejected = salvage_valid_claims([claim], run, entities=given)
        if checked.claims:
            valid.append(claim)
        else:
            first_rejects[index] = rejected[0]
    forms = appellations_of(
        claim for claim in valid if claim["property"] == "referred_to_as"
    )
    settled = [
        normalize_entity_dict(entity, forms.get(entity["id"], ()))
        for entity in given
    ]
    parts = sum(
        appellation_parts(entity, forms.get(entity["id"], ()))
        for entity in given
    )
    labels = {
        entity["id"]: entity["label"]
        for entity, before in zip(settled, given, strict=True)
        if entity["label"] != before["label"]
    }
    kept: list[dict] = []
    rejects: list[dict] = []
    label_names = 0
    for index, claim in enumerate(claims):
        subject = _subject_id(claim)
        if claim.get("property") == "name" and subject in labels:
            if index in first_rejects:
                rejects.append(first_rejects[index])
                continue
            text = _name_text(claim)
            if text is not None and name_form_key(text) == name_form_key(
                labels[subject]
            ):
                label_names += 1
                continue
        kept.append(claim)
    if labels:

        def fix(ref: dict) -> None:
            if ref["local_id"] in labels:
                ref["label"] = labels[ref["local_id"]]

        kept = [_rewrite_local_refs(claim, fix) for claim in kept]
    return _Settled(kept, settled, rejects, len(labels), label_names, parts)


class _Manifest(NamedTuple):
    """An entity manifest as parsed: its entities; the mention records
    and judgement notes set aside, counted by the category of each
    reason; the number of entities whose mention record was set
    aside; the audit of each place container checked; the trailing
    commas read as JSON syntax in its answer (scan_json_answer); and the
    entities dropped for a kind only a curator makes."""

    entities: list[EntityRecord]
    set_aside: dict[str, int]
    set_aside_records: int
    # each place container checked, kept or dropped
    # (claims.drop_containers)
    containers: list[dict]
    # the trailing commas read as JSON syntax (scan_json_answer)
    trailing_commas: int
    # the entities dropped for a kind only a curator makes, by id, with
    # that kind (ADR-0040 §7)
    dropped_kinds: dict[str, str]
    # the answer's reading of the document's statement about its
    # preparation, as it gives it (preparation.parse); _ABSENT for none
    preparation: Any = _ABSENT


def _set_aside_note(entity: dict) -> tuple[dict, list[str]]:
    """An entity of an entity manifest, as the model wrote it, with its
    judgement note set aside when the note does not validate, and the
    reason (ADR-0035 §5): `note` and the first error. The entity keeps
    everything else."""
    note = entity.get("note")
    if note is None:
        return entity, []
    try:
        JudgementNote.model_validate(note)
    except Exception as exc:
        rest = {key: value for key, value in entity.items() if key != "note"}
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return rest, [f"note: {first}"]
    return entity, []


class _Chunk(NamedTuple):
    """One chunk's parsed claims and gaps, and the objects in its answer
    that held something the parser could not read (parse_chunk_unread):
    their count is what tells an unreadable answer from one with no
    claims; and the trailing commas read as JSON syntax in its answer
    (scan_json_answer)."""

    claims: list[dict]
    gaps: list[dict]
    unread: list[dict]
    # the trailing commas read as JSON syntax (scan_json_answer)
    trailing_commas: int
