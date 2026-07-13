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
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from yizkr.claims import ClaimBatch
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
- Refer to people, places, and events with "local" references (stable
  local_id per entity within this response, label as written —
  prefer the fullest name the document gives for a person anywhere,
  instance_of from: human, place, event, birth, death, marriage,
  migration, military_service).
- Use ONLY these property keys (property key: datatype), for claims and
  qualifiers alike — a claim naming any other key is discarded. If the
  document asserts something no listed property expresses, skip it:
{property_keys}
- Vocabulary item keys available for "vocab" references:
{item_keys}
- Set confidence (0-1) per claim: how certain you are the source asserts it.

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
{units_block}

Output NDJSON: one complete claim JSON object per line — no wrapper
object, no code fences, no prose. Each claim matches the "Claim"
definition of this JSON Schema:

{schema}

After the last claim, end with exactly one final line:
{{"done": true, "claims_emitted": N}}
where N is the number of claim lines you emitted.
{document_block}"""


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


def prefix_local_ids(claim: dict, prefix: str) -> dict:
    """Chunk-namespace every local id so ids never collide across
    chunks. Same-label unification stays with the label-keyed resolve/
    write machinery — identity is not decided here."""

    def fix(ref: Any) -> None:
        if isinstance(ref, dict) and ref.get("kind") == "local" and "local_id" in ref:
            ref["local_id"] = f"{prefix}:{ref['local_id']}"

    fixed = json.loads(json.dumps(claim))  # deep copy, JSON-native
    fix(fixed.get("subject"))
    value = fixed.get("value")
    if isinstance(value, dict):
        fix(value.get("value"))
    for qualifier in fixed.get("qualifiers") or []:
        if isinstance(qualifier, dict) and isinstance(qualifier.get("value"), dict):
            fix(qualifier["value"].get("value"))
    source = fixed.get("source")
    if isinstance(source, dict):
        fix(source.get("artifact"))
        fix(source.get("fragment"))
    return fixed


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
    ) -> None:
        self.client = client
        self.state_dir = state_dir
        self.budget = budget
        self.retries = retries
        self.resume = resume
        self.failed_chunks: list[str] = []
        self.used_fallback_segmentation = False

    # -- state helpers ---------------------------------------------------

    def _write(self, name: str, content: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / name).write_text(content)

    def _read(self, name: str) -> str | None:
        path = self.state_dir / name
        return path.read_text() if path.exists() else None

    # -- segmentation ----------------------------------------------------

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

    def _chunk_prompt(
        self,
        chunk_units: list[SegmentationUnit],
        text_lines: list[str],
        transcription: Transcription,
        artifact_qid: str,
        inline_document: bool,
    ) -> str:
        property_keys, item_keys = vocabulary_prompt_lines()
        schema = json.dumps(ClaimBatch.model_json_schema(), indent=2)
        units_block = "\n\n".join(
            f"- unit {unit.id} ({unit.label}; lines "
            f"{unit.first_line}-{unit.last_line})"
            + (f"\n  context: {unit.context}" if unit.context else "")
            + f"\n  text:\n{unit_text(text_lines, unit)}"
            for unit in chunk_units
        )
        if inline_document:
            document_block = (
                f"\nFull transcription of artifact {artifact_qid} for "
                f"context (extract ONLY from the assigned units):\n\n"
                f"{number_lines(transcription.text)}\n"
            )
        else:
            document_block = ""
        return CHUNK_PROMPT.format(
            artifact_qid=artifact_qid,
            property_keys=property_keys,
            item_keys=item_keys,
            units_block=units_block,
            schema=schema,
            document_block=document_block,
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
        segmentation: DocumentSegmentation | None = None
        handle: Any = None
        if self.resume:
            previous_raw = self._read("manifest.json")
            previous = json.loads(previous_raw) if previous_raw else None
            segmentation_raw = self._read("segmentation.json")
            if (
                previous
                and previous.get("transcription_sha256") == fingerprint
                and segmentation_raw
            ):
                segmentation = DocumentSegmentation.model_validate_json(
                    segmentation_raw
                )
                manifest = previous
                # the recorded session may be gone; resumption drops to
                # inline context rather than re-running segmentation
                handle = None
        if segmentation is None:
            segmentation, handle = self._segment(transcription, artifact_qid)
            self._write("segmentation.json", segmentation.model_dump_json(indent=2))

        inline_document = (
            handle is None and len(transcription.text) <= INLINE_DOCUMENT_LIMIT
        )
        manifest["context_mode"] = (
            "fork"
            if handle is not None
            else "inline"
            if inline_document
            else "compressed"
        )
        if handle is not None and not isinstance(handle, list | dict):
            manifest["session_id"] = str(handle)

        chunks = pack_units(segmentation.units, text_lines, self.budget)
        self.failed_chunks = []
        raw_claims: list[dict] = []
        for index, chunk_units in enumerate(chunks, start=1):
            chunk_id = f"c{index:03d}"
            chunk_state = manifest["chunks"].setdefault(
                chunk_id, {"units": [unit.id for unit in chunk_units]}
            )
            parsed = self._reusable_claims(chunk_id)
            if parsed is None:
                prompt = self._chunk_prompt(
                    chunk_units,
                    text_lines,
                    transcription,
                    artifact_qid,
                    inline_document=False,
                )
                fallback_prompt = self._chunk_prompt(
                    chunk_units,
                    text_lines,
                    transcription,
                    artifact_qid,
                    inline_document=inline_document,
                )
                try:
                    parsed = self._run_chunk(chunk_id, prompt, handle, fallback_prompt)
                except Exception as exc:
                    chunk_state["status"] = f"failed: {str(exc)[:200]}"
                    self.failed_chunks.append(chunk_id)
                    self._write("manifest.json", json.dumps(manifest, indent=2))
                    continue
                self._write(f"{chunk_id}.ndjson", "\n".join(map(json.dumps, parsed)))
            chunk_state["status"] = "ok"
            chunk_state["claims"] = len(parsed)
            self._write("manifest.json", json.dumps(manifest, indent=2))
            raw_claims.extend(prefix_local_ids(claim, chunk_id) for claim in parsed)

        deduped: list[dict] = []
        for claim in raw_claims:
            if claim not in deduped:
                deduped.append(claim)

        batch, rejects = salvage_valid_claims(
            deduped,
            run_metadata(self.client.model_identifier).model_dump(mode="json"),
        )
        if rejects:
            self._write("rejects.json", json.dumps(rejects, indent=2))
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
